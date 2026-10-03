"""
The crypto book as `/api/crypto` serves it: the computed series, base-currency projection,
and what the read path must never do.

**Database only.** Every network path the FX machinery owns is replaced by a raiser —
Frankfurter's range fetch, the fallback provider, `get_exchange_rate` itself — and so are
the CoinStats and CoinGecko clients, so a GET that reached for any of them fails here
rather than quietly spending a rate limit or taking SQLite's write lock in production. The
seeded rates are **weekday-only** and the snapshot is taken on a **Sunday**, the case that
sent a naive read path to the network: the ECB publishes nothing at weekends.

The book (every figure invented, see `tests/crypto_fakes.py`):

    day     holdings (source)                     prices (CoinGecko)
    01-01   — (before the first set: 01-02's)     BTC 50k, SOL 100
    01-02   BTC 0.01, SOL 5          (snapshot)   BTC 55k, SOL 100
    01-03   + ETH 1 arrives          (snapshot)   BTC 60k, SOL 100, ETH 3000
    01-04   + USDC 100 arrives       (snapshot)   BTC 60k, SOL 100, ETH 3100, USDC: none

    value   1000, 1050, 4100, 4300 (USDC at its 1.00 peg)
    pnl     None, 50, 50, 100 — ETH and USDC arriving move the value, never the P&L
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.app_settings import AppSetting
from app.models.crypto import (
    SPAM,
    UNPRICED,
    VALUED,
    CryptoCoinId,
    CryptoCoinPrice,
    CryptoDailyHolding,
    CryptoHolding,
    CryptoSnapshot,
)
from app.models.exchange_rate import ExchangeRate
from app.services import coingecko_client, coinstats_client, crypto_service
from app.services.crypto_service import CryptoService
from app.services.currency_service import CurrencyService
from tests.crypto_fakes import memory_session_factory

SUNDAY_NOON = datetime(2026, 1, 4, 12, 0)
USD_EUR_DEC31 = Decimal("0.90")
USD_EUR = Decimal("0.85")
EUR_CHF = Decimal("0.94")

PRICES = {
    "bitcoin": {1: 50_000.0, 2: 55_000.0, 3: 60_000.0, 4: 60_000.0},
    "solana": {1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0},
    "ethereum": {3: 3000.0, 4: 3100.0},
}
SETS = {
    2: {"bitcoin": ("BTC", 0.01), "solana": ("SOL", 5.0)},
    3: {"bitcoin": ("BTC", 0.01), "solana": ("SOL", 5.0), "ethereum": ("ETH", 1.0)},
    4: {"bitcoin": ("BTC", 0.01), "solana": ("SOL", 5.0), "ethereum": ("ETH", 1.0),
        "usd-coin": ("USDC", 100.0)},
}


def _network(*_args, **_kwargs):
    raise AssertionError("the crypto read path reached for the network")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    for name in ("get_exchange_rate", "_batch_fetch_rates", "_fetch_fallback_table",
                 "_fetch_fallback_rate", "_fetch_from_api", "warm_rates"):
        monkeypatch.setattr(CurrencyService, name, _network)
    monkeypatch.setattr(coinstats_client.CoinStatsClient, "__init__", _network)
    monkeypatch.setattr(coingecko_client.CoinGeckoClient, "__init__", _network)
    monkeypatch.setattr(crypto_service, "utcnow", lambda: SUNDAY_NOON)


def _holding(snapshot_id, coin_id, symbol, status, count, price, *, rank=None, fiat=False):
    return CryptoHolding(
        snapshot_id=snapshot_id, coin_id=coin_id, symbol=symbol, name=symbol.title(),
        rank=rank, is_fiat=fiat, status=status, count=count,
        price_usd=price if status == VALUED else None,
        value_usd=count * price if status == VALUED else None,
    )


@pytest_asyncio.fixture
async def session():
    engine, factory = await memory_session_factory()
    async with factory() as s:
        # An older snapshot whose holdings must never be paired with the newer one.
        old = CryptoSnapshot(taken_at=datetime(2026, 1, 3, 8), total_value_usd=5.0,
                             valued_count=1, spam_count=0, unpriced_count=0)
        s.add(old)
        await s.flush()
        s.add(_holding(old.id, "dogecoin", "DOGE", VALUED, 50.0, 0.1, rank=9))

        snap = CryptoSnapshot(
            taken_at=SUNDAY_NOON, total_value_usd=4410.0, defi_value_usd=0.0,
            valued_count=5, spam_count=1, unpriced_count=1, credits_remaining=19_900,
            credits_total=20_000, credits_plan="free", credits_spent=18,
            warnings=["a warning from the sync"],
        )
        s.add(snap)
        await s.flush()
        s.add_all([
            _holding(snap.id, "solana", "SOL", VALUED, 5.0, 101.0, rank=5),
            _holding(snap.id, "bitcoin", "BTC", VALUED, 0.01, 60_100.0, rank=1),
            _holding(snap.id, "ethereum", "ETH", VALUED, 1.0, 3101.0, rank=2),
            _holding(snap.id, "usd-coin", "USDC", VALUED, 100.0, 1.0, rank=7),
            _holding(snap.id, "FiatCoinEUR", "EUR", VALUED, 100.0, 1.1, fiat=True),
            _holding(snap.id, "mystery-token", "MYST", UNPRICED, 42.0, None),
            _holding(snap.id, "scam-token", "VISIT-SCAM.EXAMPLE", SPAM, 1e6, None),
        ])
        for day, coins in SETS.items():
            s.add_all(
                CryptoDailyHolding(date=date(2026, 1, day), coin_id=c, symbol=sym, count=n,
                                   source="snapshot")
                for c, (sym, n) in coins.items()
            )
        for coin, sym in (("bitcoin", "BTC"), ("solana", "SOL"), ("ethereum", "ETH"),
                          ("usd-coin", "USDC")):
            s.add(CryptoCoinId(coinstats_id=coin, coingecko_id=coin, symbol=sym, method="id",
                               checked_at=datetime(2026, 1, 4)))
        for coin, days in PRICES.items():
            s.add_all(
                CryptoCoinPrice(coingecko_id=coin, date=date(2026, 1, d), price_usd=p,
                                source="spot" if d == 4 else "daily",
                                fetched_at=datetime(2026, 1, 4, 11))
                for d, p in days.items()
            )
        # Weekday-only rates, as the ECB publishes them.
        s.add_all([
            ExchangeRate(date=date(2025, 12, 31), from_currency="USD", to_currency="EUR",
                         rate=USD_EUR_DEC31, source="test"),
            ExchangeRate(date=date(2026, 1, 2), from_currency="USD", to_currency="EUR",
                         rate=USD_EUR, source="test"),
            ExchangeRate(date=date(2026, 1, 2), from_currency="EUR", to_currency="CHF",
                         rate=EUR_CHF, source="test"),
        ])
        await s.commit()
    async with factory() as s:
        yield s
    await engine.dispose()


async def _base(session, currency):
    session.add(AppSetting(key="base_currency", value=currency))
    await session.commit()


async def _drop_price(session, coin, day):
    await session.execute(delete(CryptoCoinPrice).where(
        CryptoCoinPrice.coingecko_id == coin, CryptoCoinPrice.date == date(2026, 1, day)
    ))
    await session.commit()


@pytest.mark.asyncio
async def test_the_history_is_quantities_times_prices_from_the_first_of_january(session):
    await _base(session, "USD")
    out = await CryptoService(session).history()
    points = {p["date"]: p for p in out["points"]}
    assert list(points) == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]
    assert [p["value"] for p in out["points"]] == [1000.0, 1050.0, 4100.0, 4300.0]
    # ETH arriving on 01-03 and USDC on 01-04 move the value and never the P&L.
    assert [p["pnl"] for p in out["points"]] == [None, 50.0, 50.0, 100.0]
    # Before the first synced day the earliest basket is used, and says so.
    assert [p["reconstructed"] for p in out["points"]] == [True, False, False, False]
    assert out["start_date"] == "2026-01-01"
    assert out["basket_date"] == out["first_snapshot_date"] == "2026-01-02"


@pytest.mark.asyncio
async def test_usdc_without_a_price_is_pegged_at_one_dollar_and_says_so(session):
    await _base(session, "USD")
    out = await CryptoService(session).portfolio()
    usdc = next(h for h in out["holdings"] if h["coin_id"] == "usd-coin")
    assert usdc["price"] == 1.0 and usdc["price_source"] == "peg"
    assert usdc["value"] == 100.0
    btc = next(h for h in out["holdings"] if h["coin_id"] == "bitcoin")
    assert btc["price_source"] == "coingecko"
    assert out["peg_note"] and "USDC" in out["peg_note"]


@pytest.mark.asyncio
async def test_a_coin_without_a_price_is_left_out_and_named_never_blanking_the_rest(session):
    """One unpriced coin used to blank the whole book (BNB, 2026-10-03). Now it is left out
    of the total — never valued at 0 — and named, and the rest still adds up."""
    await _base(session, "USD")
    await _drop_price(session, "ethereum", 4)
    out = await CryptoService(session).portfolio()
    assert out["total_value"] == 1200.0          # BTC 600 + SOL 500 + USDC 100, ETH out
    eth = next(h for h in out["holdings"] if h["coin_id"] == "ethereum")
    assert eth["status"] == "no_price" and eth["price"] is None and eth["value"] is None
    assert eth["weight_pct"] is None
    btc = next(h for h in out["holdings"] if h["coin_id"] == "bitcoin")
    assert btc["weight_pct"] == 50.0              # a share of the PRICED total
    assert out["no_price_symbols"] == ["ETH"]
    assert any("ETH" in w and "left out" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_a_day_without_a_coins_price_leaves_it_out_and_never_books_its_return(session):
    await _base(session, "USD")
    await _drop_price(session, "ethereum", 3)
    history = await CryptoService(session).history()
    values = {p["date"]: (p["value"], p["pnl"]) for p in history["points"]}
    assert values["2026-01-03"] == (1100.0, 50.0)  # ETH left out of that day's value
    # ETH's move into 01-04 starts from no price, so it is not P&L: BTC and SOL were flat.
    assert values["2026-01-04"] == (4300.0, 0.0)
    excluded = {p["date"]: p["excluded"] for p in history["points"]}
    assert excluded["2026-01-03"] == excluded["2026-01-04"] == ["ETH"]
    assert excluded["2026-01-02"] == []
    assert any("ETH" in w and "left out" in w for w in history["warnings"])
    portfolio = await CryptoService(session).portfolio()
    assert portfolio["pnl_since_start"] == 100.0


@pytest.mark.asyncio
async def test_the_tiles_come_from_the_same_series_as_the_chart(session):
    await _base(session, "USD")
    out = await CryptoService(session).portfolio()
    assert out["total_value"] == 4300.0
    assert out["pnl_since_start"] == 200.0
    assert out["change_today"] == 100.0
    assert out["change_today_pct"] == pytest.approx(100 / 4100 * 100, abs=0.01)
    eth = next(h for h in out["holdings"] if h["coin_id"] == "ethereum")
    assert eth["change_today_pct"] == pytest.approx(3.33, abs=0.01)


@pytest.mark.asyncio
async def test_a_sunday_converts_at_friday_rates_without_the_network(session):
    await _base(session, "CHF")
    out = await CryptoService(session).portfolio()
    factor = float(USD_EUR * EUR_CHF)
    assert out["total_value"] == pytest.approx(4300 * factor, abs=0.01)
    btc = next(h for h in out["holdings"] if h["coin_id"] == "bitcoin")
    assert btc["price"] == pytest.approx(60_000 * factor, rel=1e-7)


@pytest.mark.asyncio
async def test_each_history_point_converts_at_its_own_date(session):
    await _base(session, "EUR")
    out = await CryptoService(session).history()
    points = {p["date"]: p for p in out["points"]}
    assert points["2026-01-01"]["value"] == pytest.approx(1000 * 0.90, abs=0.01)
    assert points["2026-01-04"]["value"] == pytest.approx(4300 * 0.85, abs=0.01)


@pytest.mark.asyncio
async def test_holdings_weights_cash_spam_and_unpriced(session):
    await _base(session, "USD")
    out = await CryptoService(session).portfolio()
    ids = [h["coin_id"] for h in out["holdings"]]
    # Valued by value, largest first, then the unpriced; spam never itemised, the older
    # snapshot's coins never paired with this one, exchange cash beside the total.
    assert ids == ["ethereum", "bitcoin", "solana", "usd-coin", "mystery-token"]
    assert out["cash_value"] == 110.0
    weights = [h["weight_pct"] for h in out["holdings"] if h["status"] == VALUED]
    assert sum(weights) == pytest.approx(100, abs=0.05)
    assert out["spam_count"] == 1 and "VISIT-SCAM.EXAMPLE" not in str(out)
    assert out["unpriced_symbols"] == ["MYST"]
    assert out["color_order"] == ["bitcoin", "ethereum", "solana", "usd-coin"]


@pytest.mark.asyncio
async def test_no_eur_to_base_rate_at_all_is_unconvertible_not_euros_in_disguise(session):
    await _base(session, "GBP")
    out = await CryptoService(session).portfolio()
    assert out["total_value"] is None
    assert out["fx_unavailable"] > 0


@pytest.mark.asyncio
async def test_without_a_coingecko_key_the_page_still_answers_with_unknowns(session):
    await _base(session, "USD")
    await session.execute(delete(CryptoCoinPrice))
    await session.commit()
    out = await CryptoService(session).portfolio()
    assert out["prices_configured"] is False  # conftest blanks the key
    # USDC's peg is the only priced coin: the total is it alone, every other coin named.
    assert out["total_value"] == 100.0
    assert out["no_price_symbols"] == ["BTC", "ETH", "SOL"]
    assert out["pnl_since_start"] is None
    assert any("COINGECKO_API_KEY" in w for w in out["warnings"])
    history = await CryptoService(session).history()
    # Before USDC was held no coin is priced, and a day with nothing priced is unknown.
    assert [p["value"] for p in history["points"]] == [None, None, None, 100.0]


@pytest.mark.asyncio
async def test_an_empty_book_answers_with_empty_shapes():
    engine, factory = await memory_session_factory()
    try:
        async with factory() as s:
            portfolio = await CryptoService(s).portfolio()
            history = await CryptoService(s).history()
        assert portfolio["holdings"] == [] and portfolio["total_value"] is None
        assert portfolio["configured"] is False
        assert history["points"] == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_status_reports_the_last_snapshot_and_credits_without_asking_anyone(session):
    out = await CryptoService(session).status(
        next_run=None, sync_in_progress=False, retry_after_seconds=0
    )
    assert out["last_run"] is None
    assert out["credits_remaining"] == 19_900 and out["credits_plan"] == "free"
    assert out["last_snapshot_at"].startswith("2026-01-04T12:00:00")
