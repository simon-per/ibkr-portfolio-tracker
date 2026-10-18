"""
Every read endpoint, through the real HTTP stack, against a realistic fixture.

Every other test calls services directly, so nothing exercised FastAPI's
serialization layer — and on 2026-07-29 a `Decimal + None` shipped to production
and 500'd `/api/dividends/breakdown` with a full green suite behind it. The
fixture therefore carries the shapes that actually break things: a dual-listed
ticker (identity is isin + exchange), a closed lot, a dividend row predating the
withholding-fields migration (gross set, net NULL), a pre-ownership zero row, a
non-EUR security, and CHF as the base currency, since production runs CHF while
the tests otherwise run EUR.

`yfinance` is replaced by a raiser for the whole module: these endpoints must
serve from cache, and an accidental network reach has to fail loudly rather than
quietly spend the rate-limit budget (CLAUDE.md rule 1).

POST routes are deliberately not exercised — they start real syncs.
"""
import hashlib
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
import app.models  # noqa: F401
from app.models.app_settings import AppSetting
from app.models.crypto import (
    CryptoCoinId,
    CryptoCoinPrice,
    CryptoDailyHolding,
    CryptoHolding,
    CryptoSnapshot,
)
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW
from app.models.dividend_forecast_snapshot import (
    DividendForecastSnapshot,
    DividendForecastSnapshotLine,
)
from app.models.dividend_payment import DividendPayment
from app.models.analyst_rating import AnalystRating
from app.models.earnings_event import EarningsEvent
from app.models.exchange_rate import ExchangeRate
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.security_logo import SecurityLogo
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.models.watchlist_item import WatchlistItem
from app.clock import utc_today

TODAY = utc_today()
START = TODAY - timedelta(days=200)
SOLD_ON = TODAY - timedelta(days=40)

# GETs only. /api/portfolio/benchmark is excluded on purpose: on a cache miss it
# lazy-fetches Yahoo, which is exactly what must never happen in a test.
READ_ENDPOINTS = [
    "/health",
    "/api/settings",
    "/api/sync/status",
    "/api/scheduler/status",
    "/api/scheduler/history?limit=5",
    "/api/portfolio/summary",
    f"/api/portfolio/value-over-time?start_date={START}&end_date={TODAY}",
    "/api/portfolio/positions",
    "/api/portfolio/contributions",
    "/api/portfolio/activity",
    f"/api/portfolio/activity?start_date={START}&end_date={TODAY}",
    "/api/portfolio/activity?kind=trade,cash_flow",
    "/api/portfolio/activity?symbol=ASML&limit=5",
    "/api/portfolio/activity.csv",
    f"/api/portfolio/annualized-return?start_date={START}&end_date={TODAY}",
    f"/api/portfolio/attribution?start_date={START}&end_date={TODAY}",
    "/api/portfolio/benchmarks",
    "/api/market-data/status",
    "/api/analyst-ratings/status",
    "/api/allocation/status",
    "/api/fundamentals/status",
    "/api/fundamentals/portfolio",
    "/api/fundamentals/earnings/upcoming",
    "/api/fundamentals/earnings/history",
    "/api/watchlist",
    "/api/dividends/summary",
    "/api/dividends/breakdown",
    f"/api/dividends/breakdown?year={TODAY.year}",
    f"/api/dividends/breakdown?year={TODAY.year + 1}",   # a future year
    "/api/dividends/breakdown?forecast=false",
    "/api/dividends/breakdown?period=24m",
    "/api/dividends/forecast-history",
    f"/api/tax/report?year={TODAY.year}",
    f"/api/tax/report.csv?year={TODAY.year}",
    "/api/portfolio/lookthrough",
    "/api/portfolio/lookthrough?limit=5",
    f"/api/performance/decomposition?start_date={START}&end_date={TODAY}",
    f"/api/performance/segments?start_date={START}&end_date={TODAY}",
    "/api/performance/closed-positions",
    "/api/logos",
    "/api/logos/2",
    "/api/logos/isin/NL0010273215",
]

# The crypto book's reads need the admin key (docs/crypto.md), so they run through
# their own test below rather than the open-read loop.
CRYPTO_READ_ENDPOINTS = [
    "/api/crypto/portfolio",
    "/api/crypto/history",
    "/api/crypto/status",
]
SMOKE_ADMIN_TOKEN = "smoke-admin-token-long-enough"
SMOKE_LOGO = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


async def _seed(session: AsyncSession) -> None:
    session.add(AppSetting(key="base_currency", value="CHF"))

    securities = [
        # Dual listing: same ISIN, two venues, two securities.
        Security(id=1, isin="NL0010273215", symbol="ASML", description="ASML Holding",
                 currency="EUR", conid=101, asset_category="STK", exchange="NASDAQ"),
        Security(id=2, isin="NL0010273215", symbol="ASML", description="ASML Holding",
                 currency="EUR", conid=102, asset_category="STK", exchange="AEB"),
        # Non-EUR, so price FX conversion is exercised.
        Security(id=3, isin="US0378331005", symbol="AAPL", description="Apple Inc",
                 currency="USD", conid=103, asset_category="STK", exchange="NASDAQ"),
        # Bought weeks ago: per-share history but nothing received yet.
        Security(id=4, isin="TW0002330008", symbol="TSMC", description="TSMC",
                 currency="EUR", conid=104, asset_category="STK", exchange="TWSE"),
    ]
    for s in securities:
        session.add(s)
    # A logo for ASML's ISIN, so both of its listings serve it (docs/logos.md).
    session.add(SecurityLogo(isin="NL0010273215", image=SMOKE_LOGO, content_type="image/png",
                             sha256=hashlib.sha256(SMOKE_LOGO).hexdigest(), source="logo.dev",
                             fetched_at=datetime.combine(TODAY, time(5)),
                             checked_at=datetime.combine(TODAY, time(5))))

    lots = [
        (1, START, "10", "1000", None),
        (2, START + timedelta(days=10), "4", "400", None),
        (3, START + timedelta(days=5), "20", "2000", SOLD_ON),   # closed mid-window
        (4, TODAY - timedelta(days=30), "50", "500", None),      # bought weeks ago
    ]
    for sid, opened, qty, cost, closed in lots:
        session.add(TaxLot(
            security_id=sid, open_date=opened, close_date=closed,
            is_open=closed is None, quantity=Decimal(qty),
            cost_basis=Decimal(cost), cost_basis_eur=Decimal(cost),
            price_per_unit=Decimal(cost) / Decimal(qty), currency="EUR",
        ))

    session.add(Trade(
        ib_key="T1", conid="103", security_id=3, symbol="AAPL", trade_date=SOLD_ON,
        buy_sell="SELL", quantity=Decimal("-20"), price=Decimal("110"),
        proceeds=Decimal("2200"), commission=Decimal("-1"), currency="USD",
        realized_pnl=Decimal("200"),
    ))

    for d in _weekdays(START - timedelta(days=20), TODAY):
        for sid, px, cur in ((1, "105", "EUR"), (2, "104", "EUR"), (3, "110", "USD")):
            session.add(MarketPrice(security_id=sid, date=d, close_price=Decimal(px),
                                    currency=cur, source="test"))
        session.add(ExchangeRate(date=d, from_currency="USD", to_currency="EUR",
                                 rate=Decimal("0.9"), source="test"))
        session.add(ExchangeRate(date=d, from_currency="EUR", to_currency="CHF",
                                 rate=Decimal("0.94"), source="test"))

    dividends = [
        # Real IBKR income.
        dict(security_id=1, ex_date=TODAY - timedelta(days=60),
             pay_date=TODAY - timedelta(days=60), shares_held=Decimal("0"),
             gross_amount_eur=Decimal("12"), withholding_tax_eur=Decimal("2"),
             net_amount_eur=Decimal("10"), source="ibkr"),
        # A payer bought recently: quarterly per-share history, nothing received
        # yet. This is the shape that had TSMC, Samsung and the SOXQ ETF
        # projecting nothing at all.
        *[
            dict(security_id=4, ex_date=TODAY - timedelta(days=d),
                 amount_per_share=Decimal("0.25"), shares_held=Decimal("0"),
                 gross_amount_eur=Decimal("0"), withholding_tax_eur=Decimal("0"),
                 net_amount_eur=Decimal("0"), source="yfinance_estimate")
            for d in (275, 184, 92)
        ],
        # Legacy shape: gross set, net NULL — the row that 500'd production.
        dict(security_id=1, ex_date=TODAY - timedelta(days=150),
             pay_date=TODAY - timedelta(days=150), shares_held=Decimal("10"),
             gross_amount_eur=Decimal("8"), withholding_tax_eur=None,
             net_amount_eur=None, source="yfinance_estimate"),
        # Pre-ownership zero row from yfinance's full history.
        dict(security_id=1, ex_date=date(1999, 4, 4), pay_date=date(1999, 4, 4),
             shares_held=Decimal("0"), gross_amount_eur=Decimal("0"),
             withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("0"),
             source="yfinance_estimate"),
        # Second listing, so the dual-listing path has income on both.
        dict(security_id=2, ex_date=TODAY - timedelta(days=60),
             pay_date=TODAY - timedelta(days=60), shares_held=Decimal("0"),
             gross_amount_eur=Decimal("3"), withholding_tax_eur=Decimal("0"),
             net_amount_eur=Decimal("3"), source="ibkr"),
    ]
    for d in dividends:
        session.add(DividendPayment(currency="EUR", **d))

    # Analyst coverage: a real consensus, and the stored "no analysts" answer
    # (analyst_rating_service.no_rating), which every reader must show as absent.
    now = datetime.combine(TODAY, time(4))
    session.add(AnalystRating(security_id=4, strong_buy=3, buy=1, hold=1, sell=0,
                              strong_sell=0, last_updated=now))
    session.add(AnalystRating(security_id=1, strong_buy=0, buy=0, hold=0, sell=0,
                              strong_sell=0, last_updated=now))
    # Fundamentals in two currencies, so the base-currency market cap is exercised.
    session.add(FundamentalMetrics(security_id=3, quote_type="EQUITY", trailing_pe=-12.0,
                                   market_cap=3_000_000_000_000, data_currency="USD",
                                   last_updated=now))
    session.add(FundamentalMetrics(security_id=1, quote_type="EQUITY", trailing_pe=35.0,
                                   market_cap=250_000_000_000, data_currency="EUR",
                                   last_updated=now))
    session.add(FundamentalMetrics(security_id=4, quote_type="EQUITY",
                                   market_cap=10_000_000_000, data_currency="TWD",
                                   last_updated=now))
    # Earnings both sides of now, so the two earnings lists serve rows, not `[]`: ASML
    # reports in ten days, TSMC beat a month ago, and AAPL (sold) is filtered out of both.
    session.add(EarningsEvent(security_id=1, earnings_date=datetime.combine(
        TODAY + timedelta(days=10), time(12)), eps_estimate=5.1, is_upcoming=True))
    session.add(EarningsEvent(security_id=4, earnings_date=datetime.combine(
        TODAY - timedelta(days=30), time(6)), eps_estimate=1.0, reported_eps=1.1,
        surprise_percent=10.0, is_upcoming=False))
    session.add(EarningsEvent(security_id=3, earnings_date=datetime.combine(
        TODAY - timedelta(days=30), time(20)), eps_estimate=1.5, reported_eps=1.6,
        surprise_percent=6.7, is_upcoming=False))
    # A watchlist name quoted in USD: its cap is served in the base currency too.
    session.add(WatchlistItem(yahoo_ticker="NVDA", symbol="NVDA", company_name="NVIDIA",
                              market_cap=4_000_000_000_000, data_currency="USD",
                              currency="USD"))
    # A watchlist name that is one of our securities (AAPL, sold): served with its id.
    session.add(WatchlistItem(yahoo_ticker="AAPL", symbol="AAPL", company_name="Apple"))

    session.add(CashFlow(
        ib_key="C1", flow_date=START + timedelta(days=1), flow_type=DEPOSIT_WITHDRAW,
        amount=Decimal("3000"), amount_eur=Decimal("3000"), currency="EUR",
        description="deposit",
    ))

    # The crypto book: a valued coin, an unpriced one, a flagged spam token and a fiat
    # balance, plus three days of CoinStats history, all USD like CoinStats itself —
    # served in the CHF base through the weekday-only rates above (a weekend TODAY
    # forward-fills from Friday). Invented figures, like everything in this fixture.
    snapshot = CryptoSnapshot(
        taken_at=datetime.combine(TODAY, time(9)), total_value_usd=700.0,
        defi_value_usd=None, total_cost_usd=500.0, unrealized_pl_usd=200.0,
        unrealized_pl_pct=40.0, realized_pl_usd=None, realized_pl_pct=None,
        all_time_pl_usd=200.0, all_time_pl_pct=40.0, pl_24h_usd=None,
        valued_count=2, spam_count=1, unpriced_count=1, credits_remaining=19_000,
        credits_total=20_000, credits_plan="free", credits_spent=18, warnings=None,
    )
    session.add(snapshot)
    await session.flush()
    for coin_id, symbol, status, count, price in (
        ("bitcoin", "BTC", "valued", 0.01, 60_000.0),
        ("FiatCoinEUR", "EUR", "valued", 50.0, 1.1),
        ("mystery-token", "MYST", "unpriced", 3.0, None),
        ("scam-token", "SCAM.EXAMPLE", "spam", 1e6, None),
    ):
        session.add(CryptoHolding(
            snapshot_id=snapshot.id, coin_id=coin_id, symbol=symbol, name=symbol,
            rank=1 if coin_id == "bitcoin" else None, is_fiat=coin_id == "FiatCoinEUR",
            status=status, count=count, price_usd=price,
            value_usd=count * price if price else None,
        ))
    # Four days of holdings and CoinGecko prices: the book computes its own history.
    session.add(CryptoCoinId(coinstats_id="bitcoin", coingecko_id="bitcoin", symbol="BTC",
                             method="id", checked_at=datetime.combine(TODAY, time(6))))
    for back in (0, 1, 2, 3):
        session.add(CryptoDailyHolding(date=TODAY - timedelta(days=back), coin_id="bitcoin",
                                       symbol="BTC", count=0.01, source="snapshot"))
        session.add(CryptoCoinPrice(coingecko_id="bitcoin", date=TODAY - timedelta(days=back),
                                    price_usd=60_000.0 - 100 * back, source="daily",
                                    fetched_at=datetime.combine(TODAY, time(6))))
    # Two days of the dividend forecast with their per-security lines: two more ASML
    # shares bought in between, at an unchanged payout.
    for back, shares, total in ((1, "8", "32.00"), (0, "10", "40.00")):
        day = TODAY - timedelta(days=back)
        session.add(DividendForecastSnapshot(
            snapshot_date=day, next_12m_eur=Decimal(total), ttm_net_eur=Decimal("20.00"),
            recorded_at=datetime.combine(day, time(18)),
        ))
        session.add(DividendForecastSnapshotLine(
            snapshot_date=day, security_id=1, shares=Decimal(shares),
            next_12m_eur=Decimal(total), currency="EUR", fx_to_eur=Decimal("1"),
        ))
    await session.flush()
    await session.commit()


async def _seed_zero_cost(session: AsyncSession) -> None:
    """
    One holding received for nothing (a stock distribution), held across the whole
    window at a flat price: zero cost basis, so every gain percentage is None, and zero
    P&L, so the attribution's contribution share is None too (`percentages.share_pct`).
    """
    session.add(Security(id=1, isin="US0000000ZC1", symbol="ZC", description="Zero Cost",
                         currency="EUR", conid=201, asset_category="STK", exchange="NYSE"))
    session.add(TaxLot(
        security_id=1, open_date=START - timedelta(days=10), close_date=None, is_open=True,
        quantity=Decimal("5"), cost_basis=Decimal("0"), cost_basis_eur=Decimal("0"),
        price_per_unit=Decimal("0"), currency="EUR",
    ))
    for d in _weekdays(START - timedelta(days=20), TODAY):
        session.add(MarketPrice(security_id=1, date=d, close_price=Decimal("20"),
                                currency="EUR", source="test"))
    await session.commit()


def _serve(monkeypatch, seed):
    # Any accidental Yahoo reach must fail loudly, not spend rate-limit budget.
    def _forbidden(*_a, **_kw):
        raise AssertionError("a test tried to call Yahoo Finance")

    for mod in ("app.services.market_data_service", "app.services.dividend_service",
                "app.services.benchmark_service", "app.services.watchlist_service",
                "app.services.allocation_service", "app.services.analyst_rating_service",
                "app.services.fundamentals_service"):
        try:
            m = __import__(mod, fromlist=["yf"])
        except ImportError:
            continue
        if hasattr(m, "yf"):
            monkeypatch.setattr(m.yf, "Ticker", _forbidden, raising=False)

    # /api/dividends/summary enqueues a background sync when data looks stale.
    import app.routers.dividends as div_router

    async def _no_sync():
        return None

    monkeypatch.setattr(div_router, "_run_dividend_sync_background", _no_sync)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    session = AsyncSession(engine, expire_on_commit=False)

    import asyncio
    loop = asyncio.new_event_loop()

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await seed(session)

    loop.run_until_complete(_setup())

    from app.main import app

    async def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    # No context manager: that would run the lifespan and start the scheduler.
    yield TestClient(app)
    app.dependency_overrides.clear()
    loop.run_until_complete(session.close())
    loop.run_until_complete(engine.dispose())
    loop.close()


@pytest.fixture()
def client(monkeypatch):
    yield from _serve(monkeypatch, _seed)


@pytest.fixture()
def zero_cost_client(monkeypatch):
    yield from _serve(monkeypatch, _seed_zero_cost)


def test_a_share_of_nothing_serializes_as_null(zero_cost_client):
    """
    The six percentage fields are `Optional` on their models: a None that a `float`
    field refused would 500 the endpoint rather than reach the client as null.
    """
    c = zero_cost_client
    summary = c.get("/api/portfolio/summary")
    assert summary.status_code == 200, summary.text
    assert summary.json()["total_cost_basis_eur"] == 0
    assert summary.json()["total_gain_loss_percent"] is None

    [position] = c.get("/api/portfolio/positions").json()
    assert position["market_value_eur"] > 0 and position["gain_loss_percent"] is None

    vot = c.get(f"/api/portfolio/value-over-time?start_date={START}&end_date={TODAY}")
    assert vot.status_code == 200, vot.text
    assert vot.json() and all(p["gain_loss_percent"] is None for p in vot.json())

    attr = c.get(f"/api/portfolio/attribution?start_date={START}&end_date={TODAY}")
    assert attr.status_code == 200, attr.text
    [row] = attr.json()["attributions"]
    assert attr.json()["total_pnl_eur"] == 0
    assert row["contribution_percent"] is None and row["weight_percent"] == 100.0


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoint_responds(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:400]}"


def test_logos_serve_both_listings_of_an_isin_through_the_real_stack(client):
    version = hashlib.sha256(SMOKE_LOGO).hexdigest()[:12]
    assert client.get("/api/logos").json() == {
        "logos": {"1": version, "2": version}, "isins": {"NL0010273215": version},
    }
    r = client.get(f"/api/logos/1?v={version}")
    assert r.content == SMOKE_LOGO and r.headers["content-type"] == "image/png"
    assert "immutable" in r.headers["cache-control"]
    assert client.get("/api/logos/3").status_code == 404
    r = client.get(f"/api/logos/isin/nl0010273215?v={version}")
    assert r.content == SMOKE_LOGO and "immutable" in r.headers["cache-control"]
    assert client.get("/api/logos/isin/US0378331005").status_code == 404


@pytest.mark.parametrize("path", CRYPTO_READ_ENDPOINTS)
def test_crypto_read_endpoint_responds_to_the_admin_key(client, monkeypatch, path):
    monkeypatch.setattr(settings, "api_admin_token", SMOKE_ADMIN_TOKEN, raising=False)
    assert client.get(path).status_code == 401
    r = client.get(path, headers={"X-API-Key": SMOKE_ADMIN_TOKEN})
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:400]}"


def test_the_crypto_book_serializes_in_the_base_currency(client, monkeypatch):
    monkeypatch.setattr(settings, "api_admin_token", SMOKE_ADMIN_TOKEN, raising=False)
    headers = {"X-API-Key": SMOKE_ADMIN_TOKEN}

    book = client.get("/api/crypto/portfolio", headers=headers).json()
    assert book["base_currency"] == "CHF"
    # Σ count × CoinGecko price; the exchange's EUR cash beside it, not in it.
    assert book["total_value"] == pytest.approx(600 * 0.9 * 0.94, abs=0.01)
    assert book["cash_value"] == pytest.approx(55 * 0.9 * 0.94, abs=0.01)
    assert book["change_today"] == pytest.approx(1 * 0.9 * 0.94, abs=0.01)
    assert book["pnl_since_start"] is None  # 1 Jan is far before the fixture's prices
    assert [h["status"] for h in book["holdings"]] == ["valued", "unpriced"]
    assert book["spam_count"] == 1 and "SCAM.EXAMPLE" not in str(book)

    history = client.get("/api/crypto/history", headers=headers).json()
    assert history["points"][-1]["date"] == TODAY.isoformat()
    assert history["points"][-1]["value"] == pytest.approx(book["total_value"], abs=0.01)


def test_the_shapes_that_broke_production_serialize(client):
    """The regressions this file exists to catch, asserted on the real payloads."""
    bd = client.get(f"/api/dividends/breakdown?year={TODAY.year}").json()

    # The legacy NULL-net row is income, via the gross fallback (this was the 500).
    assert bd["total_net_eur"] > 0
    # The dual listing stays two rows, distinguishable by venue...
    asml = [r for r in bd["securities"] if r["symbol"] == "ASML"]
    assert len(asml) == 2 and {r["exchange"] for r in asml} == {"NASDAQ", "AEB"}
    # ...while the chart merges them into one stack key.
    month = (TODAY - timedelta(days=60)).strftime("%Y-%m")
    bar = next(m for m in bd["months"] if m["month"] == month)
    assert bar["actual"]["ASML"] == pytest.approx(13 * 0.94, rel=1e-3)
    # The 1999 pre-ownership zero row never surfaces.
    assert all(m["month"] >= "2000-01" for m in bd["months"])
    assert bd["base_currency"] == "CHF"

    rolling = client.get("/api/dividends/breakdown?period=24m").json()
    assert rolling["period"] == "24m"
    assert len(rolling["months"]) == 24
    assert rolling["months"][-1]["month"] == TODAY.strftime("%Y-%m")
    assert client.get("/api/dividends/breakdown?year=2026&period=24m").status_code == 422
    assert client.get("/api/dividends/breakdown?period=12m").status_code == 422

    # This fixture's income starts ~150 days ago, so no twelve-month window is
    # covered yet and the rolling series is EMPTY rather than a row of nulls or a
    # short sum wearing a year's label. That is the interesting assertion here.
    assert rolling["ttm_series"] == []
    assert client.get(
        "/api/dividends/breakdown?forecast=false"
    ).json()["ttm_series"] == []

    summary = client.get("/api/dividends/summary").json()
    assert all(abs(m["amount_eur"]) > 0 for m in summary["monthly"])
    # The card's footnote reads off these. /summary now has a response_model, which
    # cuts both ways: it documents the contract, but it also *filters* — so a key the
    # service returns and the model omits vanishes silently. test_dividend_summary_
    # contract.py pins the two sets equal; these assertions pin the values.
    assert summary["source"] in {"ibkr", "mixed", "yfinance_estimate"}
    assert summary["total_eur"] == summary["total_net_eur"]  # back-compat key is NET
    assert summary["total_gross_eur"] >= summary["total_net_eur"]
    assert "total_withholding_eur" in summary and "ibkr_from" in summary

    # A payer bought recently, with per-share history but nothing received yet,
    # must still be forecast — and must say the amount is a gross estimate.
    tsmc = next(r for r in bd["securities"] if r["symbol"] == "TSMC")
    assert tsmc["payouts"] == 0 and tsmc["forecast_payouts"] > 0
    assert tsmc["forecast_basis"] == "gross_estimate"

    # Next year is offered and projects a full twelve months.
    nxt = client.get(f"/api/dividends/breakdown?year={TODAY.year + 1}").json()
    assert TODAY.year + 1 in nxt["years"]
    assert len(nxt["months"]) == 12
    assert nxt["total_forecast_net_eur"] > 0

    # The growth block survives response_model serialization — a Pydantic model
    # silently drops keys it does not declare, so a field added to the service
    # and not to the schema would vanish here rather than raise anywhere.
    growth = bd["growth"]
    for key in ("ttm", "ytd", "avg_month"):
        assert set(growth[key]) == {"net_eur", "prev_net_eur", "pct"}
    assert isinstance(growth["ttm_crosses_era"], bool)
    assert isinstance(growth["annual"], list)
    assert "next_12m_eur" in growth
    # Percentages are nullable by design (a zero base is undefined, not large),
    # so the schema must not coerce them to 0.0.
    assert all(
        row["yoy_pct"] is None or isinstance(row["yoy_pct"], float)
        for row in growth["annual"]
    )
    # Growth is unwindowed: the same numbers whichever year is selected, which is
    # the only reason a year-filtered response can show year-over-year at all.
    unwindowed = client.get("/api/dividends/breakdown").json()
    assert unwindowed["growth"] == growth

    # The rolling series deliberately out-reaches the monthly chart: it runs to the
    # projection horizon while `months` stops inside this year, which is the whole
    # reason it is a separate list rather than four more fields on a month bar.
    # On this fixture every covered window is still ahead of us, so they are all
    # projection — and each one says so rather than reporting a received figure it
    # has no basis for.
    assert unwindowed["ttm_series"]
    assert unwindowed["ttm_series"][-1]["month"] > unwindowed["months"][-1]["month"]
    for point in unwindowed["ttm_series"]:
        assert point["partial"] is True
        assert point["net_eur"] + point["forecast_net_eur"] == pytest.approx(
            point["total_eur"], abs=0.01
        )
        # The split a stacked bar is drawn from must add up to the figure quoted
        # beside it, or the segments and the total are two computations.
        #
        # A few cents of tolerance, and only here: each per-symbol value is rounded
        # on its own while the scalar is rounded once from the unrounded sum, so a
        # window holding twenty-odd payers drifts by a cent or two (measured live:
        # worst case 0.03 on 309.96). The service-level test asserts EXACT equality
        # on a clean two-security fixture, which is where a real bucketing bug
        # would show — widening that one would hide it.
        assert sum(point["actual"].values()) == pytest.approx(point["net_eur"], abs=0.05)
        assert sum(point["forecast"].values()) == pytest.approx(
            point["forecast_net_eur"], abs=0.05
        )
    # The earliest covered windows straddle today — real receipts behind, projection
    # ahead. The last one is wholly in the future, so it has received nothing and
    # must not claim a provenance for money that never arrived.
    furthest = unwindowed["ttm_series"][-1]
    assert furthest["net_eur"] == 0 and furthest["actual"] == {}
    assert furthest["source"] is None
    assert furthest["forecast_net_eur"] == furthest["total_eur"] > 0

    # The earliest window over the WHOLE history, not this range's first point —
    # it is what tells the client whether a growth rate measured from the first
    # bar on screen has a coverage-limited base.
    assert unwindowed["ttm_coverage_start"] == unwindowed["ttm_series"][0]["month"]
    assert rolling["ttm_coverage_start"] == unwindowed["ttm_coverage_start"]


    # Completeness travels with the headline. `total_market_value_eur` is a sum over the
    # holdings the backend could price, so a partial one has to declare itself — the SBI
    # shape, where 446.93 CHF left this figure with only a sync warning to catch it. The
    # timeline's per-point field and this one come from the same helper, so they must agree
    # on the fixture: nothing here is unpriced.
    summary_now = client.get("/api/portfolio/summary").json()
    assert "unpriced_holdings" in summary_now, "response_model dropped it"
    assert isinstance(summary_now["unpriced_holdings"], int)
    vot = client.get(
        f"/api/portfolio/value-over-time?start_date={START}&end_date={TODAY}"
    ).json()
    assert all("unpriced_holdings" in p for p in vot)
    assert summary_now["unpriced_holdings"] == vot[-1]["unpriced_holdings"]

    # Cash survives its response_model on both endpoints. A response_model is a FILTER,
    # so an undeclared key is dropped silently — which for these four would put the
    # holdings-only total back on the hero card with nothing saying so, and leave the
    # chart quietly falling back to its pre-cash lines.
    for key in ("total_cash_eur", "total_value_eur", "cash_source"):
        assert key in summary_now, f"response_model dropped {key}"
    # The realized headline's completeness, same reasoning (GitHub issue #26).
    assert isinstance(summary_now.get("unpriced_disposals"), int), (
        "response_model dropped unpriced_disposals"
    )
    assert summary_now["total_value_eur"] == pytest.approx(
        summary_now["total_market_value_eur"] + summary_now["total_cash_eur"]
    )
    for key in ("cash_eur", "total_value_eur", "money_in_eur", "cash_source"):
        assert all(key in p for p in vot), f"response_model dropped {key}"
    assert vot[-1]["total_value_eur"] == pytest.approx(
        vot[-1]["market_value_eur"] + vot[-1]["cash_eur"]
    )
    # The fixture carries no trades and no cash flows, so there is nothing to derive a
    # balance from — and that must read as `unknown` rather than as a confident zero,
    # which is what would let the chart relabel a holdings-only line "Total Value".
    assert summary_now["cash_source"] in ("ibkr", "derived", "unknown")
    assert all(p["cash_source"] == summary_now["cash_source"] for p in vot)

    # Contributions: the monthly series must carry money in over the wire. It is the
    # series the deployment card draws first, and a response_model is a FILTER — an
    # undeclared key is dropped silently and the chart draws a row of zeros, which
    # reads as "you contributed nothing" rather than as a missing field.
    contrib = client.get("/api/portfolio/contributions").json()
    for row in contrib["monthly"]:
        for key in ("money_in_eur", "deployed_eur", "net_eur"):
            assert key in row, f"response_model dropped {key}"
    # And the identity that keeps the series and the windows honest about each other.
    all_window = next(w for w in contrib["windows"] if w["label"] == "all")
    assert sum(m["money_in_eur"] for m in contrib["monthly"]) == pytest.approx(
        all_window["money_in_eur"]
    )

    # The forward yield is a SIBLING of growth, not a member — it moves with a market
    # price, so it is neither growth nor history. Same year-invariance, for its own
    # reason: a rate describing the next twelve months cannot depend on the window
    # being viewed.
    fy = bd["forward_yield"]
    assert unwindowed["forward_yield"] == fy
    if fy is not None:
        # Absent as a whole object or complete — never a zeroed one. `paying_holdings: 0`
        # beside a null percentage would read as "nothing here pays a dividend".
        assert isinstance(fy["pct"], float)
        assert fy["on_cost_pct"] is None or isinstance(fy["on_cost_pct"], float)
        assert fy["paying_holdings"] <= fy["priced_holdings"]
        assert fy["basis"] in ("net", "mixed", "gross_estimate")
        assert 0 <= fy["gross_estimate_eur"] <= fy["annual_eur"]
        # An appreciated book yields more on cost than on value, and the two must be
        # computed over the same securities for that gap to mean appreciation at all.
        if fy["on_cost_pct"] is not None:
            assert fy["on_cost_pct"] > 0
    # With no projection there is nothing to divide, and the object must be absent
    # rather than a 0.00% that asserts this portfolio pays nothing.
    assert client.get(
        "/api/dividends/breakdown?forecast=false"
    ).json()["forward_yield"] is None

    # The calendar is dated and ordered, and its rows name a real security.
    upcoming = bd["upcoming"]
    assert [u["date"] for u in upcoming] == sorted(u["date"] for u in upcoming)
    known = {r["security_id"] for r in bd["securities"]}
    assert all(u["security_id"] in known for u in upcoming)

    # A sale mid-window must not read as a total loss.
    attr = client.get(
        f"/api/portfolio/attribution?start_date={START}&end_date={TODAY}"
    ).json()
    aapl = next(r for r in attr["attributions"] if r["symbol"] == "AAPL")
    assert aapl["disposal_proceeds_eur"] > 0
    assert aapl["pnl_contribution_eur"] > -100

    # Attribution's completeness signal must survive its response_model. The model is a
    # FILTER, so an undeclared key is dropped silently -- and this fixture has an
    # unpriced holding (TSMC), which is exactly the case the field exists to report. A
    # zero here would mean the whole book was attributed when it was not.
    assert "unpriced_holdings" in attr, (
        "PerformanceAttributionResponse dropped unpriced_holdings from the wire"
    )
    assert isinstance(attr["unpriced_holdings"], int)
    # TSMC is the fixture's unpriced holding, so this must be the non-zero case rather
    # than an assertion that would pass on any book. Asserting only `>= 0` here is the
    # same vacuous shape as the Sharpe clamp test that asserted `abs(0) <= 10`.
    assert attr["unpriced_holdings"] >= 1
    # Excluded and reported are alternatives, never both: a security left out for being
    # unpriced must not also appear as a contributor with a fabricated -start_value.
    assert "TSMC" not in {r["symbol"] for r in attr["attributions"]}

    # XIRR is the last member of that family, and it reports rather than excludes: the
    # tax-lot purchases are unconditional flows, so dropping the security would leave a
    # cost with no matching value. Same fixture, same unpriced TSMC, so this is the
    # non-zero case and not an assertion true of any book.
    ann = client.get(
        f"/api/portfolio/annualized-return?start_date={START}&end_date={TODAY}"
    ).json()
    assert "unpriced_holdings" in ann, (
        "AnnualizedReturnResponse dropped unpriced_holdings from the wire"
    )
    assert ann["unpriced_holdings"] >= 1

    # The tax report's honesty fields survive the HTTP layer. holdings_snapshot_total
    # is nullable now — a failed snapshot is a missing wealth-tax base, not a zero
    # one — so the route must not coerce None to 0.0 or drop the flag.
    # The two qualifying fields on a security row must survive DividendSecurityRow —
    # a Pydantic model drops what it does not declare, and both exist precisely to
    # stop a figure being read as more solid than it is.
    for row in bd["securities"]:
        assert "trailing_yield_partial" in row and isinstance(
            row["trailing_yield_partial"], bool
        )
        assert "forecast_samples" in row and "forecast_cadence_days" in row
        # The forward yield's per-security audit. Nullable by design — a row with no
        # projection or no price has no rate, which is not a rate of zero.
        assert row["forward_yield_pct"] is None or isinstance(
            row["forward_yield_pct"], float
        )
        if row["forecast_payouts"] > 0:
            assert row["forecast_samples"] and row["forecast_samples"] >= 2
            # How the amount was sized and what withholding it deducts, and from
            # which source — the reason the forecast can be trusted (or not) per row.
            assert row["forecast_method"] in {
                "announced", "latest_payment", "same_payment_last_year", "estimate",
            }
            assert row["forecast_withholding_source"] in {
                "accrual", "ibkr_measured", "ibkr_country", "assumed",
            }
            assert isinstance(row["forecast_withholding_pct"], float)
    for entry in bd["upcoming"]:
        assert entry["amount_source"] in {
            "announced", "latest_payment", "same_payment_last_year", "estimate",
        }, entry

    tax = client.get(f"/api/tax/report?year={TODAY.year}").json()
    assert tax["holdings_snapshot_error"] is False
    assert tax["holdings_snapshot_total"] is not None
    # This asserted `warnings == []` until 2026-08-05, which was pinning the bug: the
    # fixture holds an unpriced TSMC, so the snapshot really does omit it and the
    # wealth-tax base really does understate. A silent partial snapshot was the whole
    # defect, so the clean-report expectation belongs on a fixture that IS clean —
    # `test_tax_snapshot_partial.py` covers that direction.
    assert any("TSMC" in w and "understates" in w for w in tax["warnings"]), (
        "the Steuerwert omits an unpriceable holding without saying so"
    )
    assert tax["realized_source"] in {"trades", "closed_lot_estimate"}

    # The activity ledger unions four tables that each had no read surface at all.
    act = client.get(f"/api/portfolio/activity?start_date={START}&end_date={TODAY}").json()
    assert act["base_currency"] == "CHF"
    kinds = {row["kind"] for row in act["items"]}
    assert {"trade", "dividend", "cash_flow"} <= kinds
    # Newest first, and the deposit is flagged as money in while nothing else is.
    dates = [row["date"] for row in act["items"]]
    assert dates == sorted(dates, reverse=True)
    deposit = next(r for r in act["items"] if r["ib_key"] == "C1")
    assert deposit["counts_as_money_in"] is True
    assert all(r["counts_as_money_in"] is None for r in act["items"] if r["kind"] != "cash_flow")
    # The fixture's SELL carries IBKR's own realized P&L through to the row.
    sell = next(r for r in act["items"] if r["ib_key"] == "T1")
    assert sell["realized_pnl_base"] is not None
    # The holding's id rides on every security row, for the client's logo; a cash flow
    # has none.
    assert sell["security_id"] == 3
    assert deposit["security_id"] is None
    assert all(r["security_id"] is not None for r in act["items"] if r["kind"] == "dividend")

    # The ledger is era-spliced like every reader that computes a figure. Without this
    # it listed the same dividend twice — the yfinance estimate under its ex-date and the
    # IBKR actual under its pay date — and overstated dividend income by 72%. Asserted
    # over the HTTP layer because the splice is the sort of thing a response_model or a
    # windowing change can quietly undo.
    boundary = bd["ibkr_from"]
    if boundary:
        stale = [
            r for r in act["items"]
            if r["kind"] == "dividend"
            and r["source"] == "yfinance_estimate"
            and r["date"] >= boundary
        ]
        assert not stale, f"estimates superseded by IBKR rows are still on the ledger: {stale}"
        # ...and the mirror-image bug: pre-boundary estimates are the only source for
        # that era and must survive, still badged.
        early = [
            r for r in act["items"]
            if r["kind"] == "dividend" and r["source"] == "yfinance_estimate"
        ]
        assert all(r["date"] < boundary for r in early)

    # An unknown kind is refused rather than silently returning everything.
    assert client.get("/api/portfolio/activity?kind=nonsense").status_code == 400
    # And the window is bounded like value-over-time's.
    assert client.get(
        f"/api/portfolio/activity?start_date=1990-01-01&end_date={TODAY}"
    ).status_code == 400

    csv_text = client.get("/api/portfolio/activity.csv").text
    assert csv_text.splitlines()[0].startswith("date,kind,subtype")
    assert "amount_chf" in csv_text.splitlines()[0]


def test_the_forecast_history_serves_its_breakdown(client):
    """
    The since-start growth and the breakdown reach the wire, in the base currency —
    a nested model the response_model would drop whole if it were not declared.
    """
    history = client.get("/api/dividends/forecast-history").json()
    first, last = history["points"]
    assert first["breakdown"] is None and first["next_12m_since_start_pct"] is None
    assert last["next_12m_since_start_pct"] == 25.0
    assert last["breakdown"] == {
        "since": first["date"],
        "shares": 7.52,             # 2 shares x 4.00 EUR x 0.94 CHF
        "raises": 0.0, "timing": 0.0, "newly_forecast": 0.0, "fx": 0.0,
        "unattributed": 0.0,
    }

    # And the figures the recorder stores are on the breakdown, one per holding held.
    lines = client.get("/api/dividends/breakdown").json()["forecast_by_security"]
    assert {ln["security_id"] for ln in lines} == {1, 2, 4}     # AAPL was sold
    assert all(
        set(ln) == {"security_id", "shares", "next_12m_eur", "currency", "fx_to_eur",
                    "level_eur"}
        for ln in lines
    )


def test_the_lookthrough_folds_a_dual_listing_through_the_real_stack(client):
    """
    The look-through endpoint over the real HTTP stack, `response_model` included.

    This fixture is deliberately all-direct — no fund carries a stored basket — which is the
    state a fresh deployment is in, and the one where the view must still be useful rather
    than empty. What it does exercise is the fold that needs no provider at all: ASML is two
    `securities` rows sharing ISIN NL0010273215, and folding them is the whole reason this
    feature ships before any identity is resolved.

    A fund plus a basket is deliberately NOT added to the shared `_seed`: thirty other
    endpoints read it, and the basket paths are covered against a dedicated fixture in
    `tests/test_lookthrough_partition.py`.
    """
    lt = client.get("/api/portfolio/lookthrough").json()

    # The two ASML listings are one company row carrying both venues.
    asml = next(r for r in lt["companies"] if "NL0010273215" in r["isins"])
    assert sorted(asml["listings"]) == ["ASML@AEB", "ASML@NASDAQ"]
    assert asml["direct_value_eur"] == asml["value_eur"], "nothing here comes via a fund"
    assert asml["via_funds"] == []
    # Folded on the ISIN floor, with no `isin_identities` row in this fixture at all — so
    # the row must also declare that a sibling ISIN could still be missing.
    assert asml["key_type"] == "isin"
    assert asml["partially_resolved"] is True

    # The partition closes over whatever the endpoint served.
    buckets = (
        "direct_equity_eur", "looked_through_equity_eur", "fund_residual_eur",
        "nested_fund_eur", "uncovered_fund_eur",
    )
    assert sum(Decimal(str(lt[b])) for b in buckets) == Decimal(
        str(lt["total_market_value_eur"])
    )

    # And it agrees with the headline, because both come from the same valuation helper.
    # Two figures under one meaning on two screens is this codebase's dominant failure mode.
    summary = client.get("/api/portfolio/summary").json()
    assert lt["total_market_value_eur"] == summary["total_market_value_eur"]
    assert lt["base_currency"] == summary["base_currency"] == "CHF"

    # Cash survives the response_model (GitHub #28): the whole-book denominator and the
    # Cash row's share. A dropped `total_value_eur` would leave the client dividing by
    # holdings again with nothing saying so.
    for key in ("cash_eur", "cash_source", "total_value_eur", "cash_pct_of_portfolio"):
        assert key in lt, f"response_model dropped {key}"
    assert lt["cash_source"] == summary["cash_source"]
    # One read with Allocation's, so the two tabs agree about the book they divide.
    allocation = client.get("/api/allocation/portfolio").json()
    assert lt["total_value_eur"] == pytest.approx(allocation["total_value_eur"])
    assert lt["total_value_eur"] == pytest.approx(
        lt["total_market_value_eur"] + max(lt["cash_eur"] or 0.0, 0.0)
    )

    # Truncation reports the tail as a value rather than only a count.
    small = client.get("/api/portfolio/lookthrough?limit=1").json()
    assert small["companies_shown"] == 1
    assert small["company_count_total"] == lt["company_count_total"]
    if small["other_companies_count"]:
        assert small["other_companies_eur"] > 0
    # The clamp is enforced by FastAPI rather than trusted.
    assert client.get("/api/portfolio/lookthrough?limit=0").status_code == 422
    assert client.get("/api/portfolio/lookthrough?limit=9999").status_code == 422


def test_no_analyst_coverage_reads_as_absent_on_the_positions_table(client):
    """
    A zero-count rating row is the stored answer "no analysts cover this"
    (`analyst_rating_service.no_rating`); the positions table renders a badge for any
    rating it is handed, so that row must arrive as `null` — the "-" it always showed.
    """
    positions = {p["security_id"]: p for p in client.get("/api/portfolio/positions").json()}
    assert positions[1]["analyst_rating"] is None
    assert positions[4]["analyst_rating"]["consensus"] == "Buy"


def test_fundamentals_serve_a_base_currency_market_cap_and_utc_timestamps(client):
    """
    The tab sorts market caps on `market_cap_base` — the raw figures are each in their
    listing's own currency — and prints `last_updated` as its as-of line, so both have
    to arrive in a form the browser cannot misread.
    """
    rows = {r["security_id"]: r for r in client.get("/api/fundamentals/portfolio").json()}
    # AAPL (3) was sold mid-window: not a holding, so not on the tab, however much
    # data it has. Its USD -> EUR -> CHF path is test_fundamentals_market_cap.py's and
    # the watchlist case below.
    assert set(rows) == {1, 2, 4}
    # EUR -> CHF (0.94), from cached rates only.
    assert rows[1]["market_cap_base"] == int(250_000_000_000 * 0.94)
    # No cached TWD rate: absent, never the unconverted figure.
    assert rows[4]["market_cap_base"] is None
    assert rows[2]["market_cap_base"] is None and rows[2]["last_updated"] is None
    assert rows[1]["last_updated"].endswith("+00:00")

    status = client.get("/api/fundamentals/status").json()
    assert status["newest_update"].endswith("+00:00")

    # The watchlist serves the same conversion (`app/services/market_cap.py`), so a cap
    # in another currency no longer reaches the page as a bare native number.
    watch = {row["yahoo_ticker"]: row for row in client.get("/api/watchlist").json()}
    nvda = watch["NVDA"]
    assert nvda["market_cap"] == 4_000_000_000_000
    assert nvda["market_cap_base"] == int(4_000_000_000_000 * 0.9 * 0.94)
    # A ticker that is one of our securities carries its id, for its logo; one that is
    # not carries none.
    assert watch["AAPL"]["security_id"] == 3
    assert nvda["security_id"] is None
    ratings = client.get("/api/analyst-ratings/status").json()
    assert ratings["newest_update"].endswith("+00:00")
    assert client.get("/api/analyst-ratings/4").json()["last_updated"].endswith("+00:00")


@pytest.mark.parametrize("path, method, model_name", [
    ("/api/fundamentals/portfolio", "get_fundamentals_for_portfolio",
     "FundamentalMetricsResponse"),
    ("/api/fundamentals/earnings/upcoming", "get_earnings_calendar", "EarningsCalendarItem"),
    ("/api/fundamentals/earnings/history", "get_earnings_history", "EarningsHistoryItem"),
])
def test_the_fundamentals_lists_build_exactly_their_response_model(
    client, monkeypatch, path, method, model_name
):
    """
    These three routes returned bare dicts until GitHub issue #44 attached their models.
    A `response_model` is a filter — a key the service adds and the model lacks is
    dropped, and a key the model declares with a default is filled in — so the wire
    alone cannot show drift. Compare what the service builds, in both directions.
    """
    import app.schemas.portfolio as schemas
    from app.services.fundamentals_service import FundamentalsService

    built = []
    original = getattr(FundamentalsService, method)

    async def _spy(self, *args, **kwargs):
        rows = await original(self, *args, **kwargs)
        built.extend(rows)
        return rows

    monkeypatch.setattr(FundamentalsService, method, _spy)
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert built, "the fixture must give this list rows, or the comparison is vacuous"
    declared = set(getattr(schemas, model_name).model_fields)
    for row in built:
        assert set(row) == declared, (set(row) ^ declared)
    assert [r["security_id"] for r in response.json()] == [r["security_id"] for r in built]
    # Held only: AAPL (3) was sold, so its earnings rows stay off both lists.
    assert 3 not in {r["security_id"] for r in built}


def test_fundamentals_status_says_whether_a_sync_is_running(client, monkeypatch):
    """
    The tab ends its "Updating" banner on a row newer than the click *or* on a poll
    that says no sync is running, since a pass that writes nothing never moves
    `newest_update` (GitHub issue #18). The flag must survive `response_model`.
    """
    import app.single_flight as sf
    from app.schemas.portfolio import FundamentalsStatus

    status = client.get("/api/fundamentals/status").json()
    assert set(status) == set(FundamentalsStatus.model_fields)
    assert status["sync_running"] is False
    # Held and Yahoo-eligible: ASML on both venues and TSMC; AAPL was sold.
    assert (status["total_securities"], status["securities_with_data"]) == (3, 2)

    monkeypatch.setattr(sf, "_running", {sf.SYNC_PIPELINE})
    assert client.get("/api/fundamentals/status").json()["sync_running"] is True
