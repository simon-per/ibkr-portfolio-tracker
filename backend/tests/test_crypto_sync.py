"""
The crypto sync end to end: CoinStats (through `httpx.MockTransport`) into an in-memory
database, with the FX warm-up recorded rather than fetched.

What is pinned is what would be expensive or misleading to get wrong: nothing happens
without configuration; a snapshot is stored whole or not at all; the previous snapshot
survives every refusal; the daily history pull is bounded and can never cost the
snapshot; and the `sync_runs` row — which lives in a table a public endpoint reads —
carries no counts, credits or amounts.
"""
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.clock import utcnow
from app.models.crypto import SPAM, UNPRICED, VALUED, CryptoDailyPoint, CryptoHolding, CryptoSnapshot
from app.models.sync_run import SyncRun
from app.services import crypto_service
from app.services.crypto_service import (
    HISTORY_ATTEMPTS_PER_DAY,
    HISTORY_CREDIT_FLOOR,
    HISTORY_PULL_COST,
    SNAPSHOT_RUN_COST,
    SYNC_TYPE,
    CryptoSyncService,
    parse_coins,
)
from tests.crypto_fakes import (
    FakeCoinStats,
    RecordingCurrencyService,
    coin,
    configure,
    credits_body,
    memory_session_factory,
    value_body,
)


@pytest_asyncio.fixture
async def db():
    engine, factory = await memory_session_factory()
    RecordingCurrencyService.calls = []
    try:
        yield factory
    finally:
        await engine.dispose()


def _service(factory, fake):
    return CryptoSyncService(
        session_factory=factory,
        client_factory=fake.client_factory(),
        currency_service_factory=RecordingCurrencyService,
    )


async def _count(factory, model):
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _runs(factory):
    async with factory() as s:
        return (await s.execute(select(SyncRun).order_by(SyncRun.id))).scalars().all()


async def _latest_holdings(factory):
    async with factory() as s:
        latest = (await s.execute(
            select(CryptoSnapshot).order_by(CryptoSnapshot.id.desc()).limit(1)
        )).scalar_one()
        rows = (await s.execute(
            select(CryptoHolding).where(CryptoHolding.snapshot_id == latest.id)
        )).scalars().all()
        return latest, {r.coin_id: r for r in rows}


@pytest.mark.asyncio
async def test_not_configured_asks_nothing_and_records_nothing(db):
    fake = FakeCoinStats()
    assert await _service(db, fake).sync() is None
    assert fake.calls == []
    assert await _runs(db) == []


@pytest.mark.asyncio
async def test_a_full_first_sync_stores_the_snapshot_and_the_history(db, monkeypatch):
    configure(monkeypatch)
    fake = FakeCoinStats()
    result = await _service(db, fake).sync()

    assert result["status"] == "success" and result["type"] == SYNC_TYPE
    assert fake.paths() == [
        "/usage/credits", "/portfolio/value", "/portfolio/coins",
        "/portfolio/chart", "/portfolio/pl/history",
    ]

    snapshot, holdings = await _latest_holdings(db)
    assert snapshot.total_value_usd == 1310.0
    assert snapshot.valued_count == 3 and snapshot.spam_count == 1 and snapshot.unpriced_count == 1
    assert snapshot.pl_24h_usd == pytest.approx(7.0)
    assert snapshot.credits_remaining == 19_900 and snapshot.credits_plan == "free"
    assert snapshot.credits_spent == SNAPSHOT_RUN_COST + HISTORY_PULL_COST
    assert snapshot.history_status == "refreshed"

    assert holdings["bitcoin"].status == VALUED
    assert holdings["bitcoin"].value_usd == pytest.approx(600.0)
    assert holdings["scam-token"].status == SPAM and holdings["scam-token"].value_usd is None
    assert holdings["mystery-token"].status == UNPRICED
    assert holdings["mystery-token"].price_usd is None
    assert holdings["FiatCoinEUR"].is_fiat

    assert await _count(db, CryptoDailyPoint) == 30
    # The FX warm-up covered the crypto book's own currency and every non-EUR base.
    assert {(c, to) for c, to, _ in RecordingCurrencyService.calls} == {
        (("USD",), "EUR"), (("EUR",), "CHF"), (("EUR",), "USD"),
    }


@pytest.mark.asyncio
async def test_the_public_run_row_carries_no_counts_credits_or_amounts(db, monkeypatch):
    configure(monkeypatch)
    await _service(db, FakeCoinStats()).sync()
    (run,) = await _runs(db)
    assert run.sync_type == SYNC_TYPE and run.status == "success"
    assert run.details is None and run.warnings is None
    for leaked in ("1310", "19900", "BTC", "bitcoin", "600"):
        assert leaked not in (run.message or "")


@pytest.mark.asyncio
async def test_the_history_is_pulled_once_per_day_after_a_success(db, monkeypatch):
    configure(monkeypatch)
    fake = FakeCoinStats()
    service = _service(db, fake)
    await service.sync()
    fake.calls.clear()
    await service.sync()
    assert "/v1/portfolio/chart" not in fake.calls
    assert "/v1/portfolio/pl/history" not in fake.calls
    assert await _count(db, CryptoSnapshot) == 2


@pytest.mark.asyncio
async def test_a_failed_history_pull_keeps_the_snapshot_and_is_retried_a_bounded_number_of_times(
    db, monkeypatch
):
    configure(monkeypatch)
    fake = FakeCoinStats()
    fake.responses["/v1/portfolio/pl/history"] = (503, {"message": "down"})
    service = _service(db, fake)

    pulls_per_run = []
    for _ in range(HISTORY_ATTEMPTS_PER_DAY + 2):
        fake.calls.clear()
        result = await service.sync()
        # The snapshot is stored every time; the failure is a warning on a success.
        assert result["status"] == "success"
        pulled = fake.calls.count("/v1/portfolio/chart")
        pulls_per_run.append(pulled)
        assert any("history not refreshed" in w for w in result["warnings"]) == bool(pulled)

    # Attempted on the first runs of the day, then no longer asked until tomorrow.
    assert pulls_per_run == [1] * HISTORY_ATTEMPTS_PER_DAY + [0, 0]
    assert await _count(db, CryptoSnapshot) == HISTORY_ATTEMPTS_PER_DAY + 2
    assert await _count(db, CryptoDailyPoint) == 0


@pytest.mark.asyncio
async def test_the_wipe_guard_refuses_an_empty_list_beside_a_positive_total(db, monkeypatch):
    configure(monkeypatch)
    await _service(db, FakeCoinStats()).sync()

    result = await _service(db, FakeCoinStats(coins=[])).sync()
    assert result["status"] == "error" and result["reason"] == "empty_holdings"
    snapshot, holdings = await _latest_holdings(db)
    assert await _count(db, CryptoSnapshot) == 1
    assert "bitcoin" in holdings


@pytest.mark.asyncio
async def test_a_failed_coin_page_refuses_the_whole_snapshot(db, monkeypatch):
    configure(monkeypatch)
    await _service(db, FakeCoinStats()).sync()

    many = [coin(f"c{i}", f"C{i}", 1.0, 1.0, rank=i) for i in range(150)]
    broken = FakeCoinStats(coins=many)
    broken.coin_page_status[2] = 503
    result = await _service(db, broken).sync()
    assert result["status"] == "error" and result["reason"] == "transient"
    assert await _count(db, CryptoSnapshot) == 1
    _, holdings = await _latest_holdings(db)
    assert set(holdings) == {"bitcoin", "solana", "scam-token", "mystery-token", "FiatCoinEUR"}


@pytest.mark.asyncio
async def test_coinstats_still_syncing_is_a_skip_not_an_error(db, monkeypatch):
    configure(monkeypatch)
    fake = FakeCoinStats()
    fake.responses["/v1/portfolio/value"] = (409, {"message": "Transactions not synced"})
    result = await _service(db, fake).sync()
    assert result["status"] == "skipped" and result["reason"] == "not_synced"
    assert await _count(db, CryptoSnapshot) == 0


@pytest.mark.asyncio
async def test_a_429_abandons_the_pass_immediately(db, monkeypatch):
    configure(monkeypatch)
    fake = FakeCoinStats()
    fake.responses["/v1/portfolio/value"] = (429, {"message": "Insufficient credits"})
    result = await _service(db, fake).sync()
    assert result["status"] == "error" and result["reason"] == "quota"
    assert fake.paths() == ["/usage/credits", "/portfolio/value"]


@pytest.mark.asyncio
async def test_too_few_credits_for_a_run_asks_nothing_that_costs(db, monkeypatch):
    configure(monkeypatch)
    fake = FakeCoinStats(credits=credits_body(remaining=SNAPSHOT_RUN_COST - 1))
    result = await _service(db, fake).sync()
    assert result["status"] == "skipped" and result["reason"] == "credits_exhausted"
    assert fake.paths() == ["/usage/credits"]


@pytest.mark.asyncio
async def test_the_history_is_skipped_below_the_credit_floor_but_the_snapshot_is_not(
    db, monkeypatch
):
    configure(monkeypatch)
    fake = FakeCoinStats(credits=credits_body(remaining=HISTORY_CREDIT_FLOOR))
    result = await _service(db, fake).sync()
    assert result["status"] == "success"
    assert "/v1/portfolio/chart" not in fake.calls
    snapshot, _ = await _latest_holdings(db)
    assert snapshot.history_status == "skipped_credits"
    # 500 of 20,000 is under a fifth: the low-credit warning rides along.
    assert any("credits are low" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_a_sharply_shorter_history_is_refused(db, monkeypatch):
    configure(monkeypatch)
    await _service(db, FakeCoinStats(history_days=100)).sync()
    assert await _count(db, CryptoDailyPoint) == 100

    # Next Berlin day: the pull is due again, and comes back far shorter.
    async with db() as s:
        for snap in (await s.execute(select(CryptoSnapshot))).scalars():
            snap.taken_at = utcnow() - timedelta(days=2)
        await s.commit()
    result = await _service(db, FakeCoinStats(history_days=10)).sync()
    assert result["status"] == "success"
    assert any("refused rather than shrinking" in w for w in result["warnings"])
    assert await _count(db, CryptoDailyPoint) == 100


@pytest.mark.asyncio
async def test_only_the_newest_snapshot_keeps_its_holdings(db, monkeypatch):
    configure(monkeypatch)
    await _service(db, FakeCoinStats()).sync()
    await _service(db, FakeCoinStats(coins=[coin("ethereum", "ETH", 1.0, 3000.0, rank=2)],
                                     value=value_body(total=3000.0))).sync()
    async with db() as s:
        snapshot_ids = {r for r in (await s.execute(select(CryptoHolding.snapshot_id))).scalars()}
    assert len(snapshot_ids) == 1
    _, holdings = await _latest_holdings(db)
    assert set(holdings) == {"ethereum"}
    assert await _count(db, CryptoSnapshot) == 2  # the totals survive as history


@pytest.mark.asyncio
async def test_a_sub_micro_dollar_price_survives_the_round_trip(db, monkeypatch):
    """`Float`, not `Numeric(18, 6)`: six decimals would read 1.2e-8 back as 0 and turn
    a valued coin into one that looks unpriced."""
    configure(monkeypatch)
    tiny = coin("tiny-token", "TINY", 50_000_000.0, 1.2e-8, rank=900)
    await _service(db, FakeCoinStats(coins=[tiny], value=value_body(total=0.6))).sync()
    _, holdings = await _latest_holdings(db)
    assert holdings["tiny-token"].status == VALUED
    assert holdings["tiny-token"].price_usd == pytest.approx(1.2e-8)


def test_one_coin_listed_twice_is_merged_and_unknowns_stay_unknown():
    a = coin("bitcoin", "BTC", 0.01, 60_000.0, rank=1, cost=400.0, unrealized=200.0, hour24=1.0)
    b = coin("bitcoin", "BTC", 0.02, 60_000.0, rank=1, cost=800.0, unrealized=400.0, hour24=None)
    (merged,), malformed, closed = parse_coins([a, b, {"coin": {}}, {"count": 1}])
    assert malformed == 2 and closed == 0
    assert merged.count == pytest.approx(0.03)
    assert merged.value_usd == pytest.approx(1800.0)
    assert merged.total_cost_usd == pytest.approx(1200.0)
    assert merged.avg_buy_usd == pytest.approx(40_000.0)
    assert merged.unrealized_pl_pct == pytest.approx(50.0)
    # One unknown part makes the sum unknown — never a quietly smaller known number.
    assert merged.pl_24h_usd is None


@pytest.mark.asyncio
async def test_the_scheduled_job_and_the_button_never_use_the_stock_gate(db, monkeypatch):
    """Entering SYNC_PIPELINE would put the stock Sync buttons on cooldown."""
    from app.single_flight import SYNC_PIPELINE, is_running

    configure(monkeypatch)
    seen = []

    class Watching(CryptoSyncService):
        async def sync(self):
            seen.append(is_running(SYNC_PIPELINE))
            return await super().sync()

    monkeypatch.setattr(crypto_service, "CryptoSyncService", Watching)
    from app.services.scheduler_service import SchedulerService

    fake = FakeCoinStats()
    monkeypatch.setattr(
        crypto_service.CryptoSyncService, "__init__",
        lambda self: CryptoSyncService.__init__(
            self, session_factory=db, client_factory=fake.client_factory(),
            currency_service_factory=RecordingCurrencyService,
        ),
    )
    result = await SchedulerService().crypto_sync_job()
    assert result["status"] == "success"
    assert seen == [False]


@pytest.mark.asyncio
async def test_a_transient_credit_check_failure_does_not_cost_the_snapshot(db, monkeypatch):
    configure(monkeypatch)
    fake = FakeCoinStats()
    fake.responses["/v1/usage/credits"] = (503, {"message": "down"})
    result = await _service(db, fake).sync()
    assert result["status"] == "success"
    assert any("credit balance could not be read" in w for w in result["warnings"])
    snapshot, _ = await _latest_holdings(db)
    assert snapshot.credits_remaining is None


@pytest.mark.asyncio
async def test_a_rejected_key_stops_at_the_free_credit_check(db, monkeypatch):
    configure(monkeypatch)
    fake = FakeCoinStats()
    fake.responses["/v1/usage/credits"] = (401, {"message": "Invalid API key"})
    result = await _service(db, fake).sync()
    assert result["status"] == "error" and result["reason"] == "auth"
    assert fake.paths() == ["/usage/credits"]


def test_a_closed_position_is_skipped_without_a_warning():
    """CoinStats lists every coin ever held; a sold one comes back at quantity 0 (19 of 31
    on the first real run). Not a holding, and not an error either."""
    sold = coin("dogecoin", "DOGE", 0.0, 0.12, rank=9, realized=40.0)
    held = coin("bitcoin", "BTC", 0.01, 60_000.0, rank=1)
    negative = coin("weird", "WRD", -1.0, 1.0)
    holdings, malformed, closed = parse_coins([sold, held, negative])
    assert [h.coin_id for h in holdings] == ["bitcoin"]
    assert closed == 1
    assert malformed == 1  # a negative quantity is not a position we can draw


@pytest.mark.asyncio
async def test_closed_positions_put_nothing_in_warnings(db, monkeypatch):
    configure(monkeypatch)
    book = FakeCoinStats(coins=[coin("bitcoin", "BTC", 0.01, 60_000.0, rank=1),
                                coin("dogecoin", "DOGE", 0.0, 0.12, rank=9)],
                         value=value_body(total=600.0))
    result = await _service(db, book).sync()
    assert result["status"] == "success"
    assert not any("skipped" in w for w in result["warnings"])
