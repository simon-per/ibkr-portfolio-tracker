"""
The single-flight gate: everything that can reach IBKR or Yahoo shares one
pipeline lock, because two concurrent pipelines racing the Flex budget is the
Code=1025 lockout mechanism, and /api/ is public and unauthenticated.
"""
import pytest

from app.single_flight import (
    SYNC_PIPELINE,
    SyncBusy,
    cooldown_remaining,
    single_flight,
)


def test_the_gate_rejects_a_concurrent_entry_then_releases():
    with single_flight("t-concurrent"):
        with pytest.raises(SyncBusy):
            with single_flight("t-concurrent"):
                pass
    with single_flight("t-concurrent"):
        pass  # released after the block


def test_the_cooldown_rejects_an_immediate_restart():
    with single_flight("t-cooldown", cooldown_seconds=60):
        pass
    with pytest.raises(SyncBusy) as exc:
        with single_flight("t-cooldown", cooldown_seconds=60):
            pass
    assert 0 < exc.value.retry_after_seconds <= 61


def test_no_cooldown_allows_back_to_back_runs():
    for _ in range(3):
        with single_flight("t-sequential"):
            pass


def test_names_are_independent():
    with single_flight("t-a"):
        with single_flight("t-b"):
            pass


def test_the_gate_is_released_when_the_body_raises():
    with pytest.raises(ValueError):
        with single_flight("t-raise"):
            raise ValueError("boom")
    with single_flight("t-raise"):
        pass


@pytest.mark.asyncio
async def test_a_scheduled_job_skips_and_records_when_the_pipeline_is_busy(monkeypatch):
    from app.services.scheduler_service import SchedulerService

    svc = SchedulerService()
    recorded = []

    async def _fake_record(result, started_at):
        recorded.append(result)

    monkeypatch.setattr(svc, "_record_run", _fake_record)

    with single_flight(SYNC_PIPELINE):  # a manual run is already in flight
        result = await svc.full_sync_job()

    assert result["status"] == "skipped"
    assert recorded and recorded[0]["type"] == "full_sync"


@pytest.mark.asyncio
async def test_a_manual_trigger_surfaces_busy_instead_of_pretending_success(monkeypatch):
    from app.services.scheduler_service import SchedulerService

    svc = SchedulerService()

    async def _fake_record(result, started_at):
        pass

    monkeypatch.setattr(svc, "_record_run", _fake_record)

    with single_flight(SYNC_PIPELINE):
        with pytest.raises(SyncBusy):
            await svc.trigger_sync_now()


@pytest.mark.asyncio
async def test_the_dividend_card_does_not_start_a_second_yahoo_pass(monkeypatch):
    """
    The 08:00 full_sync runs sync_dividends() too. A dashboard load that finds
    the card stale must not push a second yfinance pass through it.
    """
    import app.routers.dividends as div

    called = []

    class _Boom:
        async def __aenter__(self):
            called.append(1)
            raise AssertionError("dividend sync ran while the pipeline was held")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(div, "AsyncSessionLocal", lambda: _Boom())

    with single_flight(SYNC_PIPELINE):          # a scheduled job owns the pipeline
        await div._run_dividend_sync_background()

    assert not called
    assert div._sync_in_progress is False


async def _watchlist_session():
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.pool import StaticPool
    from app.database import Base
    import app.models  # noqa: F401
    from app.models.watchlist_item import WatchlistItem

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[WatchlistItem.__table__])
        )
    return engine, AsyncSession(engine, expire_on_commit=False)


def _reset_gate(monkeypatch):
    """SYNC_PIPELINE state is module-global; earlier tests stamp its cooldown clock."""
    import app.single_flight as sf

    monkeypatch.setattr(sf, "_running", set())
    monkeypatch.setattr(sf, "_last_start", {})


@pytest.mark.asyncio
async def test_adding_a_watchlist_ticker_never_fetches_while_the_pipeline_is_held(monkeypatch):
    """
    POST /api/watchlist fires a per-add yfinance fetch. It must go through the
    shared gate — but the add itself must still succeed, or a running 08:00 job
    would turn a bookkeeping action into a 429.
    """
    from app.routers import watchlist as wl
    from app.schemas.portfolio import AddWatchlistItemRequest

    _reset_gate(monkeypatch)
    engine, session = await _watchlist_session()
    fetched = []

    async def _record(self, ticker, force=False):
        fetched.append(ticker)

    monkeypatch.setattr(wl.WatchlistService, "sync_item", _record)

    try:
        with single_flight(SYNC_PIPELINE):      # a scheduled job owns the pipeline
            response = await wl.add_to_watchlist(
                AddWatchlistItemRequest(yahoo_ticker="msft"), db=session
            )

        assert fetched == []                    # no Yahoo touch while held
        assert response.yahoo_ticker == "MSFT"  # but the row was created
        assert response.last_synced is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_rapid_fire_watchlist_adds_get_one_fetch_not_n(monkeypatch):
    from app.routers import watchlist as wl
    from app.schemas.portfolio import AddWatchlistItemRequest

    _reset_gate(monkeypatch)
    engine, session = await _watchlist_session()
    fetched = []

    async def _record(self, ticker, force=False):
        fetched.append(ticker)

    monkeypatch.setattr(wl.WatchlistService, "sync_item", _record)

    try:
        for ticker in ("aaa", "bbb", "ccc"):
            await wl.add_to_watchlist(
                AddWatchlistItemRequest(yahoo_ticker=ticker), db=session
            )

        assert fetched == ["AAA"]               # the cooldown absorbs the burst
        items = await wl.get_watchlist(db=session)
        assert {i.yahoo_ticker for i in items} == {"AAA", "BBB", "CCC"}
    finally:
        await session.close()
        await engine.dispose()


# ── POST /api/fundamentals/sync ────────────────────────────────────────
#
# is_running() fences only *overlapping* runs, so a poller that waits for each
# pass to end before starting the next ran them back to back forever — ~5 Yahoo
# calls per security per pass across the whole portfolio. Its sibling
# /sync-stale always carried the 300s; this one never did.


def test_cooldown_remaining_reports_zero_until_the_gate_is_entered():
    import app.single_flight as sf

    sf._last_start.pop("t-remaining", None)
    assert cooldown_remaining("t-remaining", 300) == 0
    with single_flight("t-remaining"):
        pass
    remaining = cooldown_remaining("t-remaining", 300)
    assert 0 < remaining <= 301
    assert cooldown_remaining("t-remaining", 0) == 0  # no cooldown, no wait


@pytest.mark.asyncio
async def test_rapid_fire_fundamentals_syncs_get_one_pass_not_n(monkeypatch):
    from fastapi import BackgroundTasks, HTTPException
    from app.routers import fundamentals as fnd

    _reset_gate(monkeypatch)

    first = await fnd.sync_fundamentals(BackgroundTasks(), force_refresh=True)
    assert first["status"] == "started"

    # Entering the gate is what stamps the cooldown clock, so run the queued work.
    passes = []

    async def _record(self, force_refresh=False):
        passes.append(force_refresh)

    monkeypatch.setattr(fnd.FundamentalsService, "sync_fundamentals_data", _record)
    await fnd._run_sync_background(True)
    assert passes == [True]

    with pytest.raises(HTTPException) as exc:
        await fnd.sync_fundamentals(BackgroundTasks(), force_refresh=True)
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) > 0

    # And the background half refuses too, so the gate — not the handler — is
    # what actually enforces it.
    await fnd._run_sync_background(True)
    assert passes == [True], "a second pass ran inside the cooldown"


# ── POST /api/dividends/sync ───────────────────────────────────────────────
#
# The one bulk Yahoo route with no cooldown: `_sync_in_progress` fences only
# *overlapping* runs, so a poller that waited for each pass to end and POSTed
# again ran full yfinance passes back to back indefinitely.


@pytest.mark.asyncio
async def test_rapid_fire_dividend_syncs_get_one_pass_not_n(monkeypatch):
    from fastapi import BackgroundTasks, HTTPException
    from app.routers import dividends as div

    _reset_gate(monkeypatch)
    monkeypatch.setattr(div, "_sync_in_progress", False)

    first = await div.sync_dividends(BackgroundTasks())
    assert first["status"] == "started"

    # Entering the gate is what stamps the cooldown clock, so run the queued work.
    passes = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    async def _sync(self):
        passes.append("sync")
        return {}

    async def _compute(self):
        passes.append("compute")
        return {}

    monkeypatch.setattr(div, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(div.DividendService, "sync_dividend_data", _sync)
    monkeypatch.setattr(div.DividendService, "compute_dividend_income", _compute)
    await div._run_dividend_sync_background()
    assert passes == ["sync", "compute"]

    with pytest.raises(HTTPException) as exc:
        await div.sync_dividends(BackgroundTasks())
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) > 0

    # And the background half refuses too, so the gate — not the handler — is
    # what actually enforces it.
    await div._run_dividend_sync_background()
    assert passes == ["sync", "compute"], "a second pass ran inside the cooldown"
    assert div._sync_in_progress is False
