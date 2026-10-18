"""
Warn when no IBKR sync has *succeeded* for a while.

Individually, a failed sync is unremarkable — `Code=1001` is routine and the schedule is
built to shrug it off. What is not routine is the *absence of a success*, and until
2026-07-31 nothing watched for it: failures were recorded and visible in
`/api/scheduler/history`, but a portfolio quietly serving last week's positions looks
exactly like one serving today's.

**This is a precondition for shortening the Flex Query period.** A Year-to-Date query
re-delivers everything, so a gap costs freshness and nothing more. A bounded window does
not: trades falling out of it before a sync succeeds are gone from every future statement,
recoverable only by a manual period change and an offline XML ingest.
"""
from datetime import datetime, timedelta

import pytest

import app.models  # noqa: F401
from app.models.sync_run import SyncRun
from app.services.scheduler_service import IBKR_SYNC_STALE_DAYS, SchedulerService

NOW = datetime(2026, 7, 31, 20, 0, 0)


async def _run(db, sync_type, status, days_ago):
    db.add(SyncRun(
        sync_type=sync_type, status=status,
        finished_at=NOW - timedelta(days=days_ago),
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_a_recent_success_is_quiet(db):
    await _run(db, "full_sync", "success", days_ago=1)

    assert await SchedulerService().find_stale_ibkr_sync(db, as_of=NOW) == []


@pytest.mark.asyncio
async def test_an_old_success_is_reported_with_its_age(db):
    await _run(db, "full_sync", "success", days_ago=IBKR_SYNC_STALE_DAYS + 4)

    warnings = await SchedulerService().find_stale_ibkr_sync(db, as_of=NOW)

    assert len(warnings) == 1
    assert f"{IBKR_SYNC_STALE_DAYS + 4} days ago" in warnings[0]
    # Names the recovery that actually works, since waiting is what loses the data.
    assert "ingest_flex_xml" in warnings[0]


@pytest.mark.asyncio
async def test_failures_do_not_reset_the_clock(db):
    """
    The whole point: `1001` every few hours must not read as "IBKR is being contacted,
    therefore fine". Only a success counts.
    """
    await _run(db, "full_sync", "success", days_ago=IBKR_SYNC_STALE_DAYS + 2)
    for d in range(IBKR_SYNC_STALE_DAYS + 1):
        await _run(db, "ibkr_sync", "error", days_ago=d)

    warnings = await SchedulerService().find_stale_ibkr_sync(db, as_of=NOW)

    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_a_market_data_success_does_not_count(db):
    """A green Yahoo sync says nothing about whether Flex is answering."""
    await _run(db, "full_sync", "success", days_ago=IBKR_SYNC_STALE_DAYS + 1)
    await _run(db, "market_data_only", "success", days_ago=0)

    assert len(await SchedulerService().find_stale_ibkr_sync(db, as_of=NOW)) == 1


@pytest.mark.asyncio
async def test_the_offline_xml_path_resets_the_clock(db):
    """
    Ingesting a browser download genuinely refreshes the data, so it must count exactly
    like an API sync — otherwise the documented escape hatch from a token lockout would
    leave the alarm blaring and train the reader to ignore it.
    """
    await _run(db, "full_sync", "success", days_ago=IBKR_SYNC_STALE_DAYS + 5)
    await _run(db, "ibkr_manual_xml", "success", days_ago=1)

    assert await SchedulerService().find_stale_ibkr_sync(db, as_of=NOW) == []


@pytest.mark.asyncio
async def test_a_fresh_install_with_no_history_is_quiet(db):
    """An empty table is a new deployment, not an outage. The first sync fixes it."""
    assert await SchedulerService().find_stale_ibkr_sync(db, as_of=NOW) == []


@pytest.mark.asyncio
async def test_failures_only_and_never_a_success_is_reported(db):
    """Distinct from a fresh install: attempts are being made and all of them fail."""
    await _run(db, "ibkr_sync", "error", days_ago=0)

    warnings = await SchedulerService().find_stale_ibkr_sync(db, as_of=NOW)

    assert len(warnings) == 1
    assert "no sync has ever succeeded" in warnings[0]


@pytest.mark.asyncio
async def test_the_boundary_does_not_fire_a_day_early(db):
    svc = SchedulerService()
    await _run(db, "full_sync", "success", days_ago=IBKR_SYNC_STALE_DAYS - 1)
    assert await svc.find_stale_ibkr_sync(db, as_of=NOW) == []


@pytest.mark.asyncio
async def test_the_boundary_fires_exactly_on_the_threshold(db):
    svc = SchedulerService()
    await _run(db, "full_sync", "success", days_ago=IBKR_SYNC_STALE_DAYS)
    assert len(await svc.find_stale_ibkr_sync(db, as_of=NOW)) == 1
