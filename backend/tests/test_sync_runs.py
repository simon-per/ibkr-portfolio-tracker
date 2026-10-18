"""
Tests for the persisted sync history.

`SchedulerService.last_sync_result` lives only in memory, so it is wiped by every
container restart — and auto-deploy restarts on each push. That left the daily
`ibkr-sync-validator` routine unable to distinguish "no sync ran" from "the process
restarted since". These tests pin the durable record and, importantly, that writing
it can never break a sync.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.sync_run import SyncRun
from app.repositories.sync_run_repository import SyncRunRepository, utc_iso
from app.services.scheduler_service import SchedulerService
from tests.memory_db import memory_engine, new_memory_engine


async def _make_session():
    engine = await memory_engine(tables=[SyncRun.__table__])
    return engine, AsyncSession(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_record_and_read_back_a_run():
    engine, session = await _make_session()
    try:
        repo = SyncRunRepository(session)
        started = datetime.now() - timedelta(seconds=42)

        await repo.record(
            sync_type="ibkr", status="success", message="Successfully synced data from IBKR",
            details={"securities_synced": 38, "taxlots_synced": 971},
            warnings=["dropped IBKR attribute(s): Trade.subCategory x12"],
            started_at=started,
        )

        run = await repo.get_latest()
        assert run is not None
        as_dict = SyncRunRepository.to_dict(run)
        assert as_dict["type"] == "ibkr"
        assert as_dict["status"] == "success"
        assert as_dict["details"]["securities_synced"] == 38
        assert "Trade.subCategory" in as_dict["warnings"][0]
        assert as_dict["timestamp"]  # ISO string for the API/UI
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_failures_are_recorded_with_their_message():
    """The lockout message is the single most useful thing to keep."""
    engine, session = await _make_session()
    try:
        repo = SyncRunRepository(session)
        await repo.record(
            sync_type="ibkr", status="error",
            message="Code=1025: Too many failed attempts. Please review your configuration.",
        )

        run = await repo.get_latest()
        assert run.status == "error"
        assert "1025" in run.message
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_is_newest_first_and_limited():
    engine, session = await _make_session()
    try:
        repo = SyncRunRepository(session)
        for i in range(5):
            await repo.record(sync_type="ibkr_sync", status="success", message=f"run {i}")

        recent = await repo.get_recent(limit=3)
        assert len(recent) == 3
        assert recent[0].message == "run 4"  # newest first
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_recording_never_breaks_a_sync():
    """
    Bookkeeping must not be able to fail a sync that otherwise worked, nor mask the
    real error on the failure path. The table is absent here, so the insert fails.
    """
    engine = new_memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        result = await SyncRunRepository(session).record(
            sync_type="ibkr", status="success", message="all good",
        )
        assert result is None  # swallowed, not raised
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_record_run_survives_a_broken_database(monkeypatch):
    """_record_run opens its own session; a failure there must not propagate."""
    svc = SchedulerService()

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr('app.services.scheduler_service.AsyncSessionLocal', lambda: _Boom())

    # Must return normally, logging rather than raising — the jobs call this last, after
    # last_sync_result is already set, so a throw here would turn a good sync into a crash.
    await svc._record_run({"type": "ibkr_sync", "status": "success"}, datetime.now())


def test_timestamps_are_utc_tagged_so_browsers_convert_them():
    """
    The dashboard renders `new Date(timestamp).toLocaleString()`. A bare naive
    isoformat() is parsed as *local* time, so the 08:00 Europe/Berlin sync displayed as
    06:02 next to a correctly-offset "Next: 13:00" — making one failure look like two.
    """
    run = SyncRun(
        sync_type="full_sync", status="error", message="boom",
        finished_at=datetime(2026, 7, 26, 6, 2, 46),  # naive UTC, as stored
    )

    stamped = SyncRunRepository.to_dict(run)["timestamp"]

    assert stamped == "2026-07-26T06:02:46+00:00"
    # An offset must be present, otherwise the browser guesses.
    assert stamped.endswith("+00:00")
    assert utc_iso(None) is None


def test_already_aware_timestamps_are_not_shifted():
    aware = datetime(2026, 7, 26, 6, 2, 46, tzinfo=timezone.utc)
    assert utc_iso(aware) == "2026-07-26T06:02:46+00:00"
