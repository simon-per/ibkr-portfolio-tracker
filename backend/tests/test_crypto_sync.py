"""
The crypto sync end to end: CoinStats and CoinGecko (through `httpx.MockTransport`) into an
in-memory database, with the FX warm-up recorded rather than fetched.

What is pinned is what would be expensive or misleading to get wrong: nothing happens
without configuration; a snapshot is stored whole or not at all; the previous snapshot
survives every refusal; CoinGecko's prices — missing key, 429, unmatched coin — never cost
the snapshot; the re-asks are bounded; and the `sync_runs` row — which lives in a table a
public endpoint reads — carries no counts, credits or amounts.
"""
from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select

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
from app.models.sync_run import SyncRun
from app.services import crypto_service
from app.services.crypto_service import (
    COINGECKO_NOT_CONFIGURED,
    SNAPSHOT_RUN_COST,
    SYNC_TYPE,
    CryptoSyncService,
    parse_coins,
    resolve_coingecko_id,
)
from tests.crypto_fakes import (
    FakeCoinGecko,
    FakeCoinStats,
    RecordingCurrencyService,
    coin,
    configure,
    configure_gecko,
    credits_body,
    memory_session_factory,
    value_body,
)

NOW = datetime(2026, 10, 2, 10, 0)
TODAY = NOW.date()


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(crypto_service, "utcnow", lambda: NOW)


@pytest_asyncio.fixture
async def db():
    engine, factory = await memory_session_factory()
    RecordingCurrencyService.calls = []
    try:
        yield factory
    finally:
        await engine.dispose()


def _service(factory, fake, gecko=None):
    return CryptoSyncService(
        session_factory=factory,
        client_factory=fake.client_factory(),
        currency_service_factory=RecordingCurrencyService,
        gecko_factory=(gecko or FakeCoinGecko()).client_factory(),
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
async def test_a_full_first_sync_stores_the_snapshot_holdings_and_prices(db, monkeypatch):
    configure(monkeypatch)
    configure_gecko(monkeypatch)
    fake, gecko = FakeCoinStats(), FakeCoinGecko()
    result = await _service(db, fake, gecko).sync()

    assert result["status"] == "success" and result["type"] == SYNC_TYPE
    # CoinStats' history is no longer asked: the book computes its own.
    assert fake.paths() == ["/usage/credits", "/portfolio/value", "/portfolio/coins"]
    assert gecko.calls[:2] == ["/coins/list", "/simple/price"]
    assert sorted(gecko.calls[2:]) == [
        "/coins/bitcoin/market_chart/range", "/coins/solana/market_chart/range",
    ]
    # The key travels in its header.
    assert all(h.get("x-cg-demo-api-key") for h in gecko.headers)

    snapshot, holdings = await _latest_holdings(db)
    assert snapshot.total_value_usd == 1310.0
    assert snapshot.valued_count == 3 and snapshot.spam_count == 1 and snapshot.unpriced_count == 1
    assert snapshot.credits_spent == SNAPSHOT_RUN_COST
    assert holdings["bitcoin"].status == VALUED
    assert holdings["scam-token"].status == SPAM and holdings["scam-token"].value_usd is None
    assert holdings["mystery-token"].status == UNPRICED
    assert holdings["FiatCoinEUR"].is_fiat

    # Today's set: valued, non-fiat coins only — no spam, no unpriced, no exchange cash.
    async with db() as s:
        daily = (await s.execute(select(CryptoDailyHolding))).scalars().all()
        mapping = {r.coinstats_id: r.coingecko_id
                   for r in (await s.execute(select(CryptoCoinId))).scalars()}
        spots = (await s.execute(select(CryptoCoinPrice).where(
            CryptoCoinPrice.date == TODAY))).scalars().all()
        closes = (await s.execute(select(func.count()).select_from(CryptoCoinPrice).where(
            CryptoCoinPrice.source == "daily"))).scalar_one()
    assert {(r.date, r.coin_id, r.source) for r in daily} == {
        (TODAY, "bitcoin", "snapshot"), (TODAY, "solana", "snapshot"),
    }
    assert mapping == {"bitcoin": "bitcoin", "solana": "solana"}
    assert {(r.coingecko_id, r.source) for r in spots} == {("bitcoin", "spot"), ("solana", "spot")}
    # Backfilled to 1 Jan: one close per finished day per coin.
    assert closes == 2 * (TODAY - date(2026, 1, 1)).days

    # The FX warm-up covered the crypto book's own currency and every non-EUR base.
    assert {(c, to) for c, to, _ in RecordingCurrencyService.calls} == {
        (("USD",), "EUR"), (("EUR",), "CHF"), (("EUR",), "USD"),
    }


@pytest.mark.asyncio
async def test_without_a_coingecko_key_the_snapshot_and_holdings_are_still_stored(
    db, monkeypatch
):
    configure(monkeypatch)
    gecko = FakeCoinGecko()
    result = await _service(db, FakeCoinStats(), gecko).sync()
    assert result["status"] == "success"
    assert COINGECKO_NOT_CONFIGURED in result["warnings"]
    assert gecko.calls == []
    assert await _count(db, CryptoSnapshot) == 1
    assert await _count(db, CryptoDailyHolding) == 2
    assert await _count(db, CryptoCoinPrice) == 0


@pytest.mark.asyncio
async def test_the_second_slot_asks_only_for_todays_price(db, monkeypatch):
    configure(monkeypatch)
    configure_gecko(monkeypatch)
    await _service(db, FakeCoinStats()).sync()
    gecko = FakeCoinGecko()
    await _service(db, FakeCoinStats(), gecko).sync()
    assert gecko.calls == ["/simple/price"]
    # Today's set replaced, not duplicated; FX warmed once per Berlin day.
    assert await _count(db, CryptoDailyHolding) == 2
    assert len(RecordingCurrencyService.calls) == 3


@pytest.mark.asyncio
async def test_a_coingecko_429_abandons_the_pass_and_never_costs_the_snapshot(
    db, monkeypatch
):
    configure(monkeypatch)
    configure_gecko(monkeypatch)
    gecko = FakeCoinGecko()
    gecko.responses["/simple/price"] = (429, {"status": {"error_message": "rate limited"}})
    result = await _service(db, FakeCoinStats(), gecko).sync()
    assert result["status"] == "success"
    assert any("(quota)" in w for w in result["warnings"])
    assert gecko.calls == ["/coins/list", "/simple/price"]
    assert await _count(db, CryptoSnapshot) == 1
    # The mapping fetched before the 429 is kept; no price was.
    assert await _count(db, CryptoCoinId) == 2
    assert await _count(db, CryptoCoinPrice) == 0


@pytest.mark.asyncio
async def test_an_unmatched_coin_is_named_and_re_asked_only_after_a_week(db, monkeypatch):
    configure(monkeypatch)
    configure_gecko(monkeypatch)
    book = FakeCoinStats(coins=[coin("bitcoin", "BTC", 0.01, 60_000.0, rank=1),
                                coin("obscure-token", "OBSC", 10.0, 2.0, rank=800)],
                         value=value_body(total=620.0))
    result = await _service(db, book).sync()
    assert any("No CoinGecko coin found for OBSC" in w for w in result["warnings"])
    gecko = FakeCoinGecko()
    await _service(db, book, gecko).sync()
    assert "/coins/list" not in gecko.calls


def test_a_coingecko_id_is_an_exact_id_else_a_unique_symbol():
    listing = [{"id": "bitcoin", "symbol": "btc", "name": ""},
               {"id": "pepe", "symbol": "pepe", "name": ""},
               {"id": "pepe-on-sol", "symbol": "pepe", "name": ""},
               {"id": "jupiter-exchange-solana", "symbol": "jup", "name": ""}]
    assert resolve_coingecko_id("bitcoin", "BTC", listing) == ("bitcoin", "id")
    assert resolve_coingecko_id("sol:JUPxyz", "JUP", listing) == (
        "jupiter-exchange-solana", "symbol"
    )
    # A symbol two CoinGecko coins share is never guessed.
    assert resolve_coingecko_id("sol:PEPExyz", "PEPE", listing) == (None, None)


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
