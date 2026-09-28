"""
The crypto book as `/api/crypto` serves it: base-currency projection, and what the read
path must never do.

**Database only.** Every network path the FX machinery owns is replaced by a raiser —
Frankfurter's range fetch, the fallback provider, `get_exchange_rate` itself — and so is
the CoinStats client, so a GET that reached for any of them fails here rather than
quietly spending a rate limit or taking SQLite's write lock in production. The seeded
rates are **weekday-only** and the snapshot is taken on a **Sunday**, which is the case
that sent a naive read path to the network: the ECB publishes nothing at weekends.

Every figure is invented (see `tests/crypto_fakes.py`).
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from app.models.app_settings import AppSetting
from app.models.crypto import SPAM, UNPRICED, VALUED, CryptoDailyPoint, CryptoHolding, CryptoSnapshot
from app.models.exchange_rate import ExchangeRate
from app.services import coinstats_client
from app.services.crypto_service import CryptoService
from app.services.currency_service import CurrencyService
from tests.crypto_fakes import memory_session_factory

FRIDAY = date(2026, 9, 18)
SUNDAY_NOON = datetime(2026, 9, 20, 12, 0)
USD_EUR = Decimal("0.85")
EUR_CHF = Decimal("0.94")


def _network(*_args, **_kwargs):
    raise AssertionError("the crypto read path reached for the network")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    for name in ("get_exchange_rate", "_batch_fetch_rates", "_fetch_fallback_table",
                 "_fetch_fallback_rate", "_fetch_from_api", "warm_rates"):
        monkeypatch.setattr(CurrencyService, name, _network)
    monkeypatch.setattr(coinstats_client.CoinStatsClient, "__init__", _network)


def _holding(snapshot_id, coin_id, symbol, status, count, price, *, rank=None, fiat=False,
             cost=None, avg=None, unrealized=None, pct=None, realized=None, change=None):
    return CryptoHolding(
        snapshot_id=snapshot_id, coin_id=coin_id, symbol=symbol, name=symbol.title(),
        rank=rank, is_fiat=fiat, status=status, count=count,
        price_usd=price if status == VALUED else None,
        value_usd=count * price if status == VALUED else None,
        total_cost_usd=cost, avg_buy_usd=avg, unrealized_pl_usd=unrealized,
        unrealized_pl_pct=pct, realized_pl_usd=realized, pl_24h_usd=None,
        change_24h_pct=change,
    )


@pytest_asyncio.fixture
async def session():
    engine, factory = await memory_session_factory()
    async with factory() as s:
        # An older snapshot whose holdings must never be paired with the newer total.
        old = CryptoSnapshot(taken_at=datetime(2026, 9, 19, 8), total_value_usd=5.0,
                             valued_count=1, spam_count=0, unpriced_count=0)
        s.add(old)
        await s.flush()
        s.add(_holding(old.id, "dogecoin", "DOGE", VALUED, 50.0, 0.1, rank=9))

        snap = CryptoSnapshot(
            taken_at=SUNDAY_NOON, total_value_usd=1310.0, defi_value_usd=0.0,
            total_cost_usd=910.0, unrealized_pl_usd=300.0, unrealized_pl_pct=32.97,
            realized_pl_usd=10.0, realized_pl_pct=1.1, all_time_pl_usd=310.0,
            all_time_pl_pct=34.07, pl_24h_usd=7.0, valued_count=3, spam_count=1,
            unpriced_count=1, credits_remaining=19_900, credits_total=20_000,
            credits_plan="free", credits_spent=53, warnings=["a warning from the sync"],
        )
        s.add(snap)
        await s.flush()
        s.add_all([
            _holding(snap.id, "solana", "SOL", VALUED, 5.0, 100.0, rank=5, cost=400.0,
                     avg=80.0, unrealized=100.0, pct=25.0, realized=0.0, change=-1.0),
            _holding(snap.id, "bitcoin", "BTC", VALUED, 0.01, 60_000.0, rank=1, cost=400.0,
                     avg=40_000.0, unrealized=200.0, pct=50.0, realized=10.0, change=2.0),
            _holding(snap.id, "FiatCoinEUR", "EUR", VALUED, 100.0, 1.1, fiat=True),
            _holding(snap.id, "mystery-token", "MYST", UNPRICED, 42.0, None),
            _holding(snap.id, "scam-token", "VISIT-SCAM.EXAMPLE", SPAM, 1e6, None),
        ])
        s.add_all([
            CryptoDailyPoint(date=date(2026, 9, 17), value_usd=1000.0, pnl_usd=100.0,
                             fetched_at=datetime(2026, 9, 20, 6)),
            CryptoDailyPoint(date=date(2026, 9, 19), value_usd=1200.0, pnl_usd=250.0,
                             fetched_at=datetime(2026, 9, 20, 6)),
        ])
        # Weekday-only rates, as the ECB publishes them.
        s.add_all([
            ExchangeRate(date=date(2026, 9, 17), from_currency="USD", to_currency="EUR",
                         rate=Decimal("0.84"), source="test"),
            ExchangeRate(date=FRIDAY, from_currency="USD", to_currency="EUR",
                         rate=USD_EUR, source="test"),
            ExchangeRate(date=FRIDAY, from_currency="EUR", to_currency="CHF",
                         rate=EUR_CHF, source="test"),
        ])
        await s.commit()
    async with factory() as s:
        yield s
    await engine.dispose()


async def _base(session, currency):
    session.add(AppSetting(key="base_currency", value=currency))
    await session.commit()


@pytest.mark.asyncio
async def test_usd_base_serves_coinstats_figures_exactly_and_needs_no_rate(session):
    await _base(session, "USD")
    out = await CryptoService(session).portfolio()
    assert out["base_currency"] == "USD"
    assert out["total_value"] == 1310.0
    assert out["itemised_value"] == 1210.0
    assert out["unitemised_value"] == 100.0
    assert out["fx_caveat"] is None
    assert out["fx_unavailable"] == 0


@pytest.mark.asyncio
async def test_a_sunday_snapshot_converts_at_friday_rates_without_the_network(session):
    await _base(session, "CHF")
    out = await CryptoService(session).portfolio()
    factor = float(USD_EUR * EUR_CHF)
    assert out["total_value"] == pytest.approx(1310.0 * factor, abs=0.01)
    btc = next(h for h in out["holdings"] if h["coin_id"] == "bitcoin")
    assert btc["price"] == pytest.approx(60_000.0 * factor, rel=1e-7)
    assert btc["value"] == pytest.approx(600.0 * factor, abs=0.01)
    # Cost and P&L are USD figures at one day's rate — and the response says so.
    assert out["unrealized_pl"] == pytest.approx(300.0 * factor, abs=0.01)
    assert out["fx_caveat"] and "2026-09-20" in out["fx_caveat"]
    # Percentages are ratios: the same in every currency.
    assert btc["unrealized_pl_pct"] == 50.0


@pytest.mark.asyncio
async def test_totals_and_table_come_from_the_newest_snapshot_only(session):
    await _base(session, "USD")
    out = await CryptoService(session).portfolio()
    ids = [h["coin_id"] for h in out["holdings"]]
    assert "dogecoin" not in ids
    # Valued by value, largest first, then the unpriced; spam never itemised.
    assert ids == ["bitcoin", "solana", "FiatCoinEUR", "mystery-token"]


@pytest.mark.asyncio
async def test_weights_are_shares_of_the_total_and_never_renormalised(session):
    await _base(session, "USD")
    out = await CryptoService(session).portfolio()
    weights = [h["weight_pct"] for h in out["holdings"] if h["status"] == VALUED]
    assert weights == [pytest.approx(45.8, abs=0.01), pytest.approx(38.17, abs=0.01),
                       pytest.approx(8.4, abs=0.01)]
    # The shortfall is the unitemised share, reported rather than spread over the rows.
    assert sum(weights) == pytest.approx(100 * 1210 / 1310, abs=0.02)


@pytest.mark.asyncio
async def test_spam_is_counted_but_never_named_and_unpriced_is_both(session):
    await _base(session, "USD")
    out = await CryptoService(session).portfolio()
    assert out["spam_count"] == 1
    assert out["unpriced_count"] == 1 and out["unpriced_symbols"] == ["MYST"]
    assert "VISIT-SCAM.EXAMPLE" not in str(out)
    myst = next(h for h in out["holdings"] if h["coin_id"] == "mystery-token")
    assert myst["price"] is None and myst["value"] is None and myst["weight_pct"] is None


@pytest.mark.asyncio
async def test_colour_order_is_market_cap_rank_not_position(session):
    await _base(session, "USD")
    out = await CryptoService(session).portfolio()
    # Unranked (the fiat balance) last; BTC first although SOL could outgrow it.
    assert out["color_order"] == ["bitcoin", "solana", "FiatCoinEUR"]


@pytest.mark.asyncio
async def test_24h_change_is_measured_against_the_value_a_day_ago(session):
    await _base(session, "USD")
    out = await CryptoService(session).portfolio()
    assert out["change_24h"] == 7.0
    assert out["change_24h_pct"] == pytest.approx(7.0 / (1210.0 - 7.0) * 100, abs=0.01)


@pytest.mark.asyncio
async def test_a_missing_usd_rate_is_absent_and_counted_never_zero(session):
    """Far enough from any cached USD->EUR rate that the forward-fill refuses."""
    await _base(session, "EUR")
    session.add(CryptoSnapshot(
        taken_at=datetime(2026, 12, 1, 9), total_value_usd=100.0,
        valued_count=0, spam_count=0, unpriced_count=0,
    ))
    await session.commit()
    out = await CryptoService(session).portfolio()
    assert out["total_value"] is None
    assert out["fx_unavailable"] >= 1
    assert any("could not be converted" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_no_eur_to_base_rate_at_all_is_unconvertible_not_euros_in_disguise(session):
    """`BaseFx` hands back the EUR amount when it has no rate at all; here that would
    print euros under a GBP label. The crypto view reports it missing instead — and never
    backfills from inside the GET."""
    await _base(session, "GBP")
    out = await CryptoService(session).portfolio()
    assert out["total_value"] is None
    assert out["fx_unavailable"] > 0


@pytest.mark.asyncio
async def test_the_history_converts_each_point_at_its_date_and_ends_at_the_snapshot(session):
    await _base(session, "EUR")
    out = await CryptoService(session).history()
    points = {p["date"]: p for p in out["points"]}
    assert list(points) == ["2026-09-17", "2026-09-19", "2026-09-20"]
    assert points["2026-09-17"]["value"] == pytest.approx(1000 * 0.84, abs=0.01)
    # Saturday: Friday's rate, from the preload — not a network fetch.
    assert points["2026-09-19"]["value"] == pytest.approx(1200 * 0.85, abs=0.01)
    # The newest snapshot is the last point, so the chart is current between pulls.
    assert points["2026-09-20"]["value"] == pytest.approx(1310 * 0.85, abs=0.01)
    assert points["2026-09-20"]["pnl"] is None
    assert out["fx_caveat"] is not None


@pytest.mark.asyncio
async def test_an_empty_book_answers_with_empty_shapes(monkeypatch):
    engine, factory = await memory_session_factory()
    try:
        async with factory() as s:
            portfolio = await CryptoService(s).portfolio()
            history = await CryptoService(s).history()
        assert portfolio["holdings"] == [] and portfolio["total_value"] is None
        assert portfolio["configured"] is False  # conftest blanks the credentials
        assert history["points"] == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_status_reports_the_last_snapshot_and_credits_without_asking_coinstats(session):
    out = await CryptoService(session).status(
        next_run=None, sync_in_progress=False, retry_after_seconds=0
    )
    assert out["last_run"] is None
    assert out["credits_remaining"] == 19_900 and out["credits_plan"] == "free"
    assert out["last_snapshot_at"].startswith("2026-09-20T12:00:00")
    assert out["last_snapshot_at"].endswith("+00:00")


@pytest.mark.asyncio
async def test_a_rounding_sized_gap_is_not_served_as_not_itemised(session):
    """CoinStats' total and its per-coin figures disagree in the last digits; inside the
    tolerance that is rounding, and a "Not itemised" line of cents would be noise."""
    from sqlalchemy import select as sa_select

    await _base(session, "USD")
    snap = (await session.execute(
        sa_select(CryptoSnapshot).where(CryptoSnapshot.taken_at == SUNDAY_NOON)
    )).scalar_one()
    snap.total_value_usd = 1210.0 * (1 - 0.000002)  # a few ten-thousandths of a percent low
    await session.commit()
    out = await CryptoService(session).portfolio()
    assert out["unitemised_value"] == 0.0
    assert not any("add up to more" in w for w in out["warnings"])
