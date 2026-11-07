"""
A 429 on any endpoint a pass reaches stops the pass — driven through REAL yfinance.

Review item Y2 (2026-11-07). Every other Yahoo test stubs `yf.Ticker`, and every stub
*raises* — which real yfinance 1.1 did not: under its default
`config.debug.hide_exceptions = True`, `quarterly_financials` caught the 429 itself and
answered an empty frame. The hold was never stamped, the pass asked the next company, and
`.info`'s growth was stored as if measured. A stub cannot see that by construction, so
here the library runs for real and only its request layer (`YfData._make_request`) is
replaced: each endpoint answers a small valid document, or a 429, or "no data".

Two families:
- **Each endpoint, each pass:** for every endpoint the fundamentals and watchlist passes
  reach, a 429 there abandons the pass after the first security, stamps the hold and
  writes nothing for that security.
- **No data stays no data:** with yfinance raising, a ticker with no history
  (`YFTzMissingError`/`YFPricesMissingError`) reads as the empty answer it was before
  (`yahoo_errors.history_or_empty`), never as an error count.

Offline: nothing leaves the process; yfinance's on-disk timezone cache is replaced by its
own dummy so no file is touched.
"""

import json
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import unquote

import pytest
import yfinance as yf
from sqlalchemy import select
from yfinance import cache as yf_cache_module
from yfinance.data import YfData
from yfinance.exceptions import YFRateLimitError

import app.models  # noqa: F401  register all mappers
from app.clock import utcnow
from app.models.company_financials import CompanyFinancial, SecCompany
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.watchlist_item import WatchlistItem
from app.services.yahoo_rate_limit import check_yahoo_hold, yahoo_hold_until

SYMBOLS = ("AAA", "BBB", "CCC")


class _Response:
    def __init__(self, payload, url="", status=200):
        self.url = url
        self.status_code = status
        self.text = json.dumps(payload)
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _symbol_of(url: str) -> str:
    path = unquote(url.split("?")[0])
    return path.rstrip("/").rsplit("/", 1)[-1]


def _endpoint(url: str, params) -> str:
    """Which Yahoo endpoint a request is, in the names the parametrisation uses."""
    query = f"{url} {params or ''}"
    if "/v8/finance/chart/" in url:
        return "chart"
    if "/quoteSummary/" in url:
        if "earningsTrend" in query:
            return "trend"
        if "recommendationTrend" in query:
            return "recommendations"
        return "info"
    if "/v7/finance/quote" in url:
        return "quote"
    if "fundamentals-timeseries" in url:
        return "peg" if "trailingPegRatio" in url else "statements"
    if "/v1/finance/visualization" in url:
        return "earnings_dates"
    return "other"


def _valid(endpoint: str, symbol: str):
    """A small document yfinance parses without complaint."""
    if endpoint == "info":
        return {
            "quoteSummary": {
                "error": None,
                "result": [
                    {
                        "quoteType": {
                            "quoteType": "EQUITY",
                            "symbol": symbol,
                            "longName": symbol,
                        },
                        "financialData": {
                            "revenueGrowth": {"raw": 0.11, "fmt": "11%"},
                            "earningsGrowth": {"raw": 0.42, "fmt": "42%"},
                            "financialCurrency": "USD",
                        },
                        "defaultKeyStatistics": {
                            "trailingEps": {"raw": 2.0, "fmt": "2.00"}
                        },
                        "assetProfile": {
                            "sector": "Technology",
                            "country": "United States",
                        },
                        "summaryDetail": {
                            "trailingPE": {"raw": 20.0, "fmt": "20"},
                            "currency": "USD",
                        },
                    }
                ],
            }
        }
    if endpoint == "quote":
        return {
            "quoteResponse": {
                "error": None,
                "result": [
                    {
                        "symbol": symbol,
                        "regularMarketPrice": 40.0,
                        "currency": "USD",
                        "mostRecentQuarter": 1759190400,
                    }
                ],
            }
        }
    if endpoint == "peg":
        return {
            "timeseries": {
                "error": None,
                "result": [
                    {
                        "meta": {"symbol": [symbol], "type": ["trailingPegRatio"]},
                        "timestamp": [1759190400],
                        "trailingPegRatio": [
                            {"asOfDate": "2025-09-30", "reportedValue": {"raw": 1.5}}
                        ],
                    }
                ],
            }
        }
    if endpoint == "statements":
        return {"timeseries": {"error": None, "result": []}}
    if endpoint == "trend":
        return {
            "quoteSummary": {
                "error": None,
                "result": [
                    {
                        "earningsTrend": {
                            "trend": [
                                {
                                    "period": "+1y",
                                    "growth": {"raw": 0.2},
                                    "revenueEstimate": {
                                        "avg": {"raw": 1},
                                        "low": {"raw": 1},
                                        "high": {"raw": 1},
                                        "numberOfAnalysts": {"raw": 3},
                                        "yearAgoRevenue": {"raw": 1},
                                        "growth": {"raw": 0.1},
                                    },
                                    "earningsEstimate": {},
                                    "epsTrend": {},
                                    "epsRevisions": {},
                                }
                            ]
                        }
                    }
                ],
            }
        }
    if endpoint == "recommendations":
        return {
            "quoteSummary": {
                "error": None,
                "result": [
                    {
                        "recommendationTrend": {
                            "trend": [
                                {
                                    "period": "0m",
                                    "strongBuy": 1,
                                    "buy": 2,
                                    "hold": 3,
                                    "sell": 0,
                                    "strongSell": 0,
                                },
                            ]
                        }
                    }
                ],
            }
        }
    # A chart, earnings dates, anything else: "no data" — what a delisted ticker answers.
    return {
        "chart": {
            "result": None,
            "error": {
                "code": "Not Found",
                "description": "No data found, symbol may be delisted",
            },
        }
    }


@pytest.fixture
def yahoo(monkeypatch):
    """
    Real yfinance over a fake wire. `yahoo.refuse` names the endpoints that answer 429;
    `yahoo.asked` records `(endpoint, symbol)` per request.
    """

    class Wire:
        refuse: set = set()
        asked: list = []

    wire = Wire()
    wire.refuse = set()
    wire.asked = []

    def _make_request(
        self, url, request_method=None, body=None, params=None, timeout=30, data=None
    ):
        endpoint = _endpoint(url, params)
        wire.asked.append((endpoint, _symbol_of(url)))
        if endpoint in wire.refuse:
            raise YFRateLimitError()
        return _Response(_valid(endpoint, _symbol_of(url)), url)

    monkeypatch.setattr(YfData, "_make_request", _make_request)
    monkeypatch.setattr(
        yf_cache_module, "get_tz_cache", lambda: yf_cache_module._TzCacheDummy()
    )
    YfData.cache_get.cache_clear()
    yield wire
    YfData.cache_get.cache_clear()


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """The per-security courtesy sleeps are not what is under test."""
    for module in (
        "app.services.fundamentals_service",
        "app.services.watchlist_service",
        "app.services.company_price_sync",
        "app.services.dividend_service",
        "app.services.analyst_rating_service",
        "app.services.allocation_service",
        "app.services.market_data_service",
    ):
        monkeypatch.setattr(f"{module}.random.uniform", lambda *_a, **_kw: 0)


async def _seed_held(db, symbols=SYMBOLS):
    for i, symbol in enumerate(symbols):
        security = Security(
            isin=f"US00000Y200{i}",
            symbol=symbol,
            description=symbol,
            currency="USD",
            conid=9100 + i,
            asset_category="STK",
            exchange="NASDAQ",
        )
        db.add(security)
        await db.flush()
        db.add(
            TaxLot(
                security_id=security.id,
                open_date=date(2025, 1, 2),
                is_open=True,
                quantity=Decimal("10"),
                cost_basis=Decimal("1000"),
                price_per_unit=Decimal("100"),
                currency="USD",
                cost_basis_eur=Decimal("900"),
            )
        )
    await db.commit()


def _symbols_asked(wire) -> set:
    return {symbol for _endpoint, symbol in wire.asked if symbol in SYMBOLS}


@pytest.mark.asyncio
async def test_the_hold_check_makes_yfinance_raise():
    """The one call every Yahoo path awaits leaves yfinance raising what it meets."""
    yf.config.debug.hide_exceptions = True
    await check_yahoo_hold()
    assert yf.config.debug.hide_exceptions is False


def test_hidden_exceptions_were_the_bug(yahoo):
    """
    The library half, pinned so a yfinance upgrade that changes it is noticed: with the
    flag on, a 429 on the statements is an empty frame; with it off, it is raised.
    """
    yahoo.refuse = {"statements"}
    yf.config.debug.hide_exceptions = True
    try:
        assert yf.Ticker("AAA").quarterly_financials.empty
    finally:
        yf.config.debug.hide_exceptions = False
    YfData.cache_get.cache_clear()
    with pytest.raises(YFRateLimitError):
        yf.Ticker("AAA").quarterly_financials


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["info", "quote", "peg", "statements", "trend"])
async def test_fundamentals_stop_on_a_429_at_every_endpoint(
    db, monkeypatch, yahoo, endpoint
):
    from app.services.fundamentals_service import FundamentalsService

    await _seed_held(db)
    service = FundamentalsService(db)

    async def _ticker(security, *_a, **_kw):
        return security.symbol

    monkeypatch.setattr(service, "_get_yahoo_ticker", _ticker)
    yahoo.refuse = {endpoint}

    result = await service.sync_fundamentals_data(force_refresh=True)

    assert result["rate_limited"] is True, result
    assert result["warnings"]
    assert len(_symbols_asked(yahoo)) == 1, f"kept asking after a 429: {yahoo.asked}"
    assert yahoo_hold_until() is not None, "the 429 was not recorded"
    stored = (await db.execute(select(FundamentalMetrics))).scalars().all()
    assert stored == [], "a 429 mid-fetch stored the .info fallback as if measured"


@pytest.mark.asyncio
async def test_fundamentals_store_a_full_answer_when_nothing_refuses(
    db, monkeypatch, yahoo
):
    """The control: the same wire with no refusal stores every security."""
    from app.services.fundamentals_service import FundamentalsService

    await _seed_held(db)
    service = FundamentalsService(db)

    async def _ticker(security, *_a, **_kw):
        return security.symbol

    monkeypatch.setattr(service, "_get_yahoo_ticker", _ticker)
    result = await service.sync_fundamentals_data(force_refresh=True)

    assert result["rate_limited"] is False, result
    assert _symbols_asked(yahoo) == set(SYMBOLS)
    stored = (await db.execute(select(FundamentalMetrics))).scalars().all()
    assert len(stored) == len(SYMBOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint", ["info", "quote", "peg", "statements", "trend", "chart"]
)
async def test_the_watchlist_stops_on_a_429_at_every_endpoint(db, yahoo, endpoint):
    from app.services.watchlist_service import WatchlistService

    for symbol in SYMBOLS:
        db.add(WatchlistItem(yahoo_ticker=symbol))
    await db.commit()
    yahoo.refuse = {endpoint}

    result = await WatchlistService(db).sync_all(force=True)

    assert result["rate_limited"] is True, result
    assert len(_symbols_asked(yahoo)) == 1, f"kept asking after a 429: {yahoo.asked}"
    assert yahoo_hold_until() is not None
    items = (await db.execute(select(WatchlistItem))).scalars().all()
    assert all(item.estimates_synced_at is None for item in items)


@pytest.mark.asyncio
async def test_ratings_stop_on_a_429(db, yahoo):
    from app.services.analyst_rating_service import AnalystRatingService
    import app.services.analyst_rating_service as ratings_module

    async def _no_sleep(_seconds):
        return None

    await _seed_held(db)
    ratings_module.asyncio.sleep, original = _no_sleep, ratings_module.asyncio.sleep
    try:
        yahoo.refuse = {"recommendations"}
        result = await AnalystRatingService(db).sync_ratings_for_securities()
    finally:
        ratings_module.asyncio.sleep = original
    assert result["rate_limited"] is True
    assert len(_symbols_asked(yahoo)) == 1


@pytest.mark.asyncio
async def test_company_prices_stop_on_a_429(db, yahoo):
    from app.services.company_price_sync import CompanyPriceSync

    await _seed_companies(db)
    yahoo.refuse = {"chart"}

    async def _no_sleep(_seconds):
        return None

    result = await CompanyPriceSync(db, sleep=_no_sleep).refresh(force=True)

    assert result["rate_limited"] is True
    assert len(_symbols_asked(yahoo)) == 1
    assert yahoo_hold_until() is not None


async def _seed_companies(db):
    seen = utcnow()
    first = date(2024, 3, 31)
    for i, ticker in enumerate(SYMBOLS):
        cik = 7000 + i
        db.add(SecCompany(cik=cik, ticker=ticker, universe_seen_at=seen))
        db.add(
            CompanyFinancial(
                cik=cik,
                period_end=first,
                fiscal_year=2024,
                fiscal_period="Q1",
                metric="revenue",
                value=1.0,
                unit="USD",
                source_concept="X",
                filed=first + timedelta(days=30),
                fetched_at=utcnow(),
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_a_ticker_with_no_history_is_no_data_not_an_error(db, yahoo):
    """
    With yfinance raising, "no data" arrives as `YFTzMissingError`; the price pass still
    records a miss for it and goes on to the next company, as it did when the library
    answered an empty frame.
    """
    from app.services.company_price_sync import CompanyPriceSync

    await _seed_companies(db)

    async def _no_sleep(_seconds):
        return None

    result = await CompanyPriceSync(db, sleep=_no_sleep).refresh(force=True)

    assert result["rate_limited"] is False
    assert result["errors"] == 0, result
    assert result["no_data"] == len(SYMBOLS)
    assert _symbols_asked(yahoo) == set(SYMBOLS)
    assert yahoo_hold_until() is None


@pytest.mark.asyncio
async def test_a_ticker_with_no_dividends_is_no_data_not_an_error(
    db, monkeypatch, yahoo
):
    from app.services.dividend_service import DividendService

    await _seed_held(db)
    service = DividendService(db)

    async def _ticker(security, *_a, **_kw):
        return security.symbol

    monkeypatch.setattr(service, "_get_yahoo_ticker", _ticker)
    result = await service.sync_dividend_data()

    assert result["rate_limited"] is False
    assert result["errors"] == 0, result
    assert result["securities_processed"] == len(SYMBOLS)
    stamps = (await db.execute(select(Security.dividends_checked_at))).scalars().all()
    assert all(stamp is not None for stamp in stamps), "an answered ticker is stamped"
