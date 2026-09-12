from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services import market_data_service as mds
from app.services.market_data_service import MarketDataService


class FakeTickerMappingRepository:
    def __init__(self, mappings=None):
        self.mappings = mappings or {}
        self.saved = []

    async def get_mapping(self, ibkr_symbol: str, ibkr_exchange: str):
        return self.mappings.get((ibkr_symbol, ibkr_exchange))

    async def upsert_mapping(self, **kwargs):
        self.saved.append(kwargs)


def make_service(mappings=None) -> MarketDataService:
    service = MarketDataService.__new__(MarketDataService)
    service.ticker_mapping_repo = FakeTickerMappingRepository(mappings)
    return service


def make_security(symbol="SBI", exchange="TSE", currency="CAD"):
    return SimpleNamespace(symbol=symbol, exchange=exchange, currency=currency)


@pytest.mark.asyncio
async def test_tse_cad_uses_toronto_yahoo_suffix():
    service = make_service()
    security = make_security(currency="CAD")

    assert await service._get_yahoo_ticker(security) == "SBI.TO"

    variations = service._get_yahoo_ticker_variations(security)
    assert variations[0] == "SBI.TO"
    assert "SBI.T" not in variations


@pytest.mark.asyncio
async def test_tse_jpy_still_uses_tokyo_yahoo_suffix():
    service = make_service()
    security = make_security(currency="JPY")

    assert await service._get_yahoo_ticker(security) == "SBI.T"

    variations = service._get_yahoo_ticker_variations(security)
    assert variations[0] == "SBI.T"
    assert "SBI.TO" not in variations


@pytest.mark.asyncio
async def test_manual_ticker_mapping_overrides_tse_currency_logic():
    service = make_service({
        ("SBI", "TSE"): SimpleNamespace(yahoo_ticker="SBI.CA"),
    })

    assert await service._get_yahoo_ticker(make_security()) == "SBI.CA"


def test_to_suffix_infers_cad_price_currency():
    service = make_service()

    assert service._get_currency_from_ticker("SBI.TO", make_security()) == "CAD"


def test_reported_currency_beats_the_suffix_guess():
    service = make_service()

    # .TO would infer CAD; Yahoo saying USD wins.
    assert service._get_currency_from_ticker(
        "SBI.TO", make_security(), "USD"
    ) == "USD"


def test_a_hand_checked_override_still_beats_the_reported_currency():
    """SMH.L is a USD ETF listed in London. The override exists because the automatic
    answer was wrong once already, so it has to outrank Yahoo's own field too."""
    service = make_service()
    security = make_security(symbol="SMH", exchange="LSEETF", currency="USD")

    assert service._get_currency_from_ticker("SMH.L", security, "GBp") == "USD"


@pytest.mark.asyncio
async def test_twse_resolves_to_taiwan_yahoo_suffix():
    """TSMC arrived on a exchange nobody had held before; without TWSE in the table
    the suffix resolves to '' and we fall through to the bare-symbol trap."""
    service = make_service()
    security = make_security(symbol="2330", exchange="TWSE", currency="TWD")

    assert await service._get_yahoo_ticker(security) == "2330.TW"
    assert service._get_currency_from_ticker("2330.TW", security) == "TWD"


# --- the currency guard on auto-discovery ------------------------------------------


async def _no_sleep(*_args, **_kwargs):
    """The service sleeps 3-6s between securities to be polite to Yahoo."""
    return None


class _FakeDb:
    async def commit(self):
        return None


class _FakePriceRepo:
    """Just the two methods `fetch_and_cache_prices` reaches for."""

    def __init__(self, missing):
        self._missing = missing
        self.written = []

    async def get_missing_dates(self, _security_id, _start, _end):
        return list(self._missing)

    async def bulk_create(self, rows):
        self.written.extend(rows)
        return len(rows)


def _install_fake_yahoo(monkeypatch, responses):
    """Route yf.Ticker(...).history() through `responses`: {ticker: (close, currency)}."""
    monkeypatch.setattr(mds.random, "uniform", lambda *_: 0)

    class FakePriceHistory:
        def __init__(self):
            self._history_metadata = None

    class FakeTicker:
        def __init__(self, ticker):
            self._ticker = ticker
            self._price_history = FakePriceHistory()

        @property
        def history_metadata(self):
            # yfinance's public property re-requests at an intraday interval when
            # 'tradingPeriods' is absent, which a daily history() never sets. Touching
            # it would double our Yahoo traffic, so failing loudly here keeps that
            # regression from slipping back in.
            raise AssertionError("history_metadata issues a second Yahoo request")

        def history(self, **kwargs):
            entry = responses.get(self._ticker)
            if entry is None:
                return pd.DataFrame()
            close, currency = entry
            self._price_history._history_metadata = {"currency": currency}
            return pd.DataFrame(
                {"Close": [close]},
                index=pd.to_datetime(["2026-07-24"]),
            )

    monkeypatch.setattr(mds.yf, "Ticker", FakeTicker)


@pytest.mark.asyncio
async def test_price_currency_comes_from_yahoo_not_the_ticker_suffix(monkeypatch):
    """Bare `SBI` is a USD instrument. Inference said 'no suffix -> use the security's
    currency' and stamped it CAD, which is what hid the error."""
    _install_fake_yahoo(monkeypatch, {"SBI": (7.70, "USD")})
    service = make_service()

    prices, rate_limited = await service._try_fetch_yahoo(
        "SBI", make_security(), date(2026, 7, 24), date(2026, 7, 24)
    )

    assert rate_limited is False
    assert prices[0]["currency"] == "USD"
    assert prices[0]["close_price"] == Decimal("7.7")


@pytest.mark.asyncio
async def test_variation_quoted_in_another_currency_is_rejected_and_not_saved(monkeypatch):
    """The SBI@TSE regression end to end: the correct .TO ticker returns nothing, the
    bare symbol returns a USD fund. Adopting it overstated the position by 61% and, worse,
    persisted a mapping that shadowed the (correct) suffix logic from then on."""
    _install_fake_yahoo(monkeypatch, {"SBI": (7.70, "USD")})  # SBI.TO deliberately absent
    service = make_service()

    prices = await service.fetch_prices_from_yahoo(
        make_security(), date(2026, 7, 24), date(2026, 7, 24)
    )

    assert prices == []
    assert service.ticker_mapping_repo.saved == []


@pytest.mark.asyncio
async def test_variation_in_the_right_currency_is_still_adopted(monkeypatch):
    """The guard must not break legitimate auto-discovery."""
    _install_fake_yahoo(monkeypatch, {"SBI": (4.78, "CAD")})
    service = make_service()

    prices = await service.fetch_prices_from_yahoo(
        make_security(), date(2026, 7, 24), date(2026, 7, 24)
    )

    assert prices[0]["currency"] == "CAD"
    assert [s["yahoo_ticker"] for s in service.ticker_mapping_repo.saved] == ["SBI"]


# --- provenance: which provider actually supplied a row -------------------------------
#
# `fetch_and_cache_prices` hardcoded `'source': 'yahoo_finance'` for BOTH providers,
# with a comment conceding it was wrong when the Alpha Vantage fallback fired. So a
# fallback price claimed a provenance it did not have, in the one column every pricing
# diagnosis reads first — `market_prices.source` is how the SBI repair was traced, how
# an imported window is told from a fetched one, and what `manage_mappings` reports.


@pytest.mark.asyncio
async def test_a_yahoo_row_says_it_came_from_yahoo(monkeypatch):
    _install_fake_yahoo(monkeypatch, {"SBI.TO": (4.79, "CAD")})
    service = make_service()

    prices, _ = await service._try_fetch_yahoo(
        "SBI.TO", make_security(), date(2026, 7, 24), date(2026, 7, 24)
    )

    assert prices[0]["source"] == "yahoo_finance"


@pytest.mark.asyncio
async def test_alpha_vantage_refuses_a_security_it_cannot_quote_honestly(monkeypatch):
    """
    The endpoint serves US listings in USD and its response carries no currency to
    read back — unlike Yahoo, where the reported currency is what the row is tagged
    with. Stamping a non-USD security's own currency onto a USD quote is exactly how
    SBI was carried 61% high, so the fallback refuses instead.
    """
    monkeypatch.setattr(mds.settings, "alpha_vantage_api_key", "test-key")
    service = make_service()

    # Would otherwise be a network call; reaching it at all is the failure.
    def _explode(*_args, **_kwargs):
        raise AssertionError("Alpha Vantage must not be called for a non-USD security")

    monkeypatch.setattr(mds.httpx, "AsyncClient", _explode)

    assert await service.fetch_prices_from_alpha_vantage(make_security(currency="CAD")) == []


@pytest.mark.asyncio
async def test_alpha_vantage_is_skipped_without_a_key(monkeypatch):
    monkeypatch.setattr(mds.settings, "alpha_vantage_api_key", "")
    service = make_service()
    security = make_security(symbol="AMZN", exchange="NASDAQ", currency="USD")
    assert await service.fetch_prices_from_alpha_vantage(security) == []


def test_a_price_row_never_defaults_to_a_provider_it_did_not_come_from():
    """
    The column defaulted to `alpha_vantage` — the *fallback* provider — so a row
    inserted without an explicit source claimed the one place it almost certainly
    did not come from.
    """
    from app.models.market_price import MarketPrice

    default = MarketPrice.__table__.c.source.default
    assert default is not None
    assert default.arg not in {"alpha_vantage", "yahoo_finance"}


@pytest.mark.asyncio
async def test_a_fallback_price_is_cached_as_alpha_vantage_not_yahoo(monkeypatch):
    """
    The caller-side half, and the one that actually catches the bug: the three tests
    above pass unchanged against the original code, because they only exercise the
    fetchers. `fetch_and_cache_prices` is where the literal `'yahoo_finance'` was
    written for every row regardless of who supplied it.
    """
    # Yahoo returns nothing for every ticker, so the fallback is reached.
    _install_fake_yahoo(monkeypatch, {})
    monkeypatch.setattr(mds.settings, "alpha_vantage_api_key", "test-key")
    monkeypatch.setattr(mds.asyncio, "sleep", _no_sleep)

    day = date(2026, 7, 24)
    security = SimpleNamespace(id=1, symbol="AMZN", exchange="NASDAQ", currency="USD")

    async def _fake_alpha_vantage(_self, sec, outputsize="compact"):
        return [{
            'date': day,
            'close_price': Decimal("231.00"),
            'currency': sec.currency,
            'source': 'alpha_vantage',
        }]

    monkeypatch.setattr(
        mds.MarketDataService, "fetch_prices_from_alpha_vantage", _fake_alpha_vantage
    )

    service = make_service()
    service.db = _FakeDb()
    service.market_price_repo = _FakePriceRepo([day])

    cached = await service.fetch_and_cache_prices(security, day, day)

    assert cached == 1
    row = service.market_price_repo.written[0]
    assert row['source'] == 'alpha_vantage', (
        "a fallback price claimed Yahoo's provenance — the column every pricing "
        "diagnosis reads first"
    )
    assert row['currency'] == 'USD'


@pytest.mark.asyncio
async def test_a_yahoo_price_is_still_cached_as_yahoo(monkeypatch):
    """The other side of the same branch, so the fix cannot mislabel the primary."""
    _install_fake_yahoo(monkeypatch, {"AMZN": (231.00, "USD")})
    monkeypatch.setattr(mds.asyncio, "sleep", _no_sleep)

    day = date(2026, 7, 24)
    security = SimpleNamespace(id=1, symbol="AMZN", exchange="NASDAQ", currency="USD")

    service = make_service()
    service.db = _FakeDb()
    service.market_price_repo = _FakePriceRepo([day])

    assert await service.fetch_and_cache_prices(security, day, day) == 1
    assert service.market_price_repo.written[0]['source'] == 'yahoo_finance'


# ---------------------------------------------------------------------------
# Minor-unit quotes. Yahoo reports London equities in `GBp` (pence), Johannesburg in
# `ZAc` and Tel Aviv in `ILA` — and the code used to normalise `GBp` to `GBP` without
# touching the amount, storing a 5-pound close as 500.
#
# The nasty part is not the factor, it is that normalising the label DEFEATS the currency
# guard instead of tripping it: 'GBp'.upper() equals the security's own 'GBP', so the
# "reject a variation whose currency disagrees" check passes and nothing downstream can
# see a position carried 100x high. That is the SBI failure with its safeguard removed.
# ---------------------------------------------------------------------------


def test_a_minor_unit_report_names_its_major_currency():
    service = make_service()
    gbp = make_security(symbol="LLOY", exchange="LSE", currency="GBP")

    assert service._get_currency_from_ticker("LLOY.L", gbp, "GBp") == "GBP"
    assert service._get_currency_from_ticker("XYZ.JO", gbp, "ZAc") == "ZAR"
    assert service._get_currency_from_ticker("XYZ.TA", gbp, "ILA") == "ILS"
    # An ordinary code is untouched.
    assert service._get_currency_from_ticker("LLOY.L", gbp, "GBP") == "GBP"


@pytest.mark.asyncio
async def test_a_pence_quote_is_divided_before_it_is_stored(monkeypatch):
    """
    The whole point: label AND amount move together. 512.4 pence is 5.124 pounds, and it
    must not reach `market_prices` as 512.4 GBP.
    """
    _install_fake_yahoo(monkeypatch, {"LLOY.L": (512.4, "GBp")})
    service = make_service()
    security = make_security(symbol="LLOY", exchange="LSE", currency="GBP")

    prices, rate_limited = await service._try_fetch_yahoo(
        "LLOY.L", security, date(2026, 7, 24), date(2026, 7, 24)
    )

    assert rate_limited is False
    assert prices[0]["currency"] == "GBP"
    assert prices[0]["close_price"] == Decimal("5.124")


@pytest.mark.asyncio
async def test_the_currency_guard_still_adopts_a_pence_quoted_variation(monkeypatch):
    """
    Scaling must not turn a legitimate London listing into a rejected one: the guard
    compares the *normalised* code against the security's GBP, so the row is adopted —
    now with an amount that agrees with its label.
    """
    _install_fake_yahoo(monkeypatch, {"LLOY.L": (512.4, "GBp")})
    service = make_service()
    security = make_security(symbol="LLOY", exchange="LSE", currency="GBP")

    prices = await service.fetch_prices_from_yahoo(
        security, date(2026, 7, 24), date(2026, 7, 24)
    )

    assert [p["close_price"] for p in prices] == [Decimal("5.124")]


@pytest.mark.asyncio
async def test_a_hand_checked_override_is_not_scaled(monkeypatch):
    """
    `SMH.L` is the real case in this account: a USD-denominated ETF listed in London, so
    an override pins USD. An override means someone looked at the listing, so the
    reported code is not to be trusted — and dividing on the strength of it would move a
    correct price by 100 in the other direction.
    """
    _install_fake_yahoo(monkeypatch, {"SMH.L": (126.12, "GBp")})
    service = make_service()
    security = make_security(symbol="SMH", exchange="LSEETF", currency="USD")

    prices, _ = await service._try_fetch_yahoo(
        "SMH.L", security, date(2026, 7, 24), date(2026, 7, 24)
    )

    assert prices[0]["currency"] == "USD"
    assert prices[0]["close_price"] == Decimal("126.12")


@pytest.mark.asyncio
async def test_a_rate_limited_yahoo_does_not_fall_through_to_alpha_vantage(monkeypatch):
    """
    A 429 comes back from the Yahoo fetcher as the same empty list as "Yahoo has never
    heard of this ticker", so the fallback fired on a refusal: it spent one of the free
    tier's few daily calls and rewrote this security's `source` for a failure that had
    nothing to do with the security.
    """
    monkeypatch.setattr(mds.random, "uniform", lambda *_: 0)
    monkeypatch.setattr(mds.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(mds.settings, "alpha_vantage_api_key", "test-key")

    class RefusingTicker:
        def __init__(self, ticker):
            self._price_history = SimpleNamespace(_history_metadata=None)

        def history(self, **kwargs):
            raise Exception("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(mds.yf, "Ticker", RefusingTicker)

    fallback_calls = []

    async def _fake_alpha_vantage(_self, sec, outputsize="compact"):
        fallback_calls.append(sec.symbol)
        return []

    monkeypatch.setattr(
        mds.MarketDataService, "fetch_prices_from_alpha_vantage", _fake_alpha_vantage
    )

    day = date(2026, 7, 24)
    security = SimpleNamespace(id=1, symbol="AMZN", exchange="NASDAQ", currency="USD")
    service = make_service()
    service.db = _FakeDb()
    service.market_price_repo = _FakePriceRepo([day])

    cached = await service.fetch_and_cache_prices(security, day, day)

    assert cached == 0
    assert service.rate_limited is True
    assert fallback_calls == [], "Alpha Vantage was asked after Yahoo refused us"
