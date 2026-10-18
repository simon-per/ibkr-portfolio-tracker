"""
A scheduled job waits, bounded, for a SYNC_PIPELINE gate a public GET is holding (issue #16).

`GET /api/dividends/summary` enqueues `_run_dividend_sync_background` (a Yahoo loop with
1-2 s sleeps per due security) whenever a check is due, at most every 6 h, and
`GET /api/portfolio/benchmark` fetches under the gate on a cache miss. `_gated_job` used
to try the gate once, so a dashboard load just before 18:00 Berlin cost the day its
730-day pass and look-through upkeep. The APScheduler entry points now poll the gate for
up to SCHEDULED_GATE_WAIT_SECONDS; manual triggers still fail fast with 429.

Also pins that the dividend route's 300 s *cooldown* does not block the scheduled job
once the pass has finished: `single_flight` applies the entering caller's cooldown and
`_gated_job` enters with none.

Offline: DividendService is stubbed and the wait's sleep is replaced; nothing reaches
Yahoo or IBKR and nothing sleeps for real.
"""
import asyncio
import re
from pathlib import Path

import pytest

from app import single_flight as sf
from app.routers import dividends as div_router
from app.services import scheduler_service as sched
from app.services.scheduler_service import (
    ALL_SYNC_HOURS,
    SCHEDULED_GATE_POLL_SECONDS,
    SCHEDULED_GATE_WAIT_SECONDS,
    SchedulerService,
)
from app.single_flight import SYNC_PIPELINE, SyncBusy, single_flight

AUTO_DEPLOY = Path(__file__).resolve().parents[2] / "ops" / "auto-deploy.sh"

# What `start()` registers against SYNC_PIPELINE, entry point -> the `_locked` body it
# reaches. The crypto entry is absent: it has its own gate.
SCHEDULED_ENTRIES = [
    (sched.full_sync_job_entry, "_full_sync_job_locked"),
    (sched.ibkr_only_sync_job_entry, "_ibkr_only_sync_locked"),
    (sched.market_data_only_sync_job_entry, "_market_data_only_locked"),
    (sched.watchlist_refresh_job_entry, "_watchlist_refresh_locked"),
    (sched.fundamentals_refresh_job_entry, "_fundamentals_refresh_locked"),
]


@pytest.fixture(autouse=True)
def _clean_gate():
    sf._running.clear()
    sf._last_start.clear()
    yield
    sf._running.clear()
    sf._last_start.clear()


@pytest.fixture
def svc(monkeypatch):
    """The scheduler singleton the entry points resolve, with run recording stubbed."""
    service = SchedulerService()
    service.recorded = []

    async def _record(result, _started):
        service.recorded.append(result)

    monkeypatch.setattr(service, "_record_run", _record)
    monkeypatch.setattr(sched, "_scheduler_service", service)
    return service


def _fake_sleep(monkeypatch, on_sleep=None):
    """Replace the wait's sleep; returns the list of seconds it was asked to sleep."""
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)
        if on_sleep is not None:
            await on_sleep()

    monkeypatch.setattr(sched, "_gate_sleep", _sleep)
    return slept


def _stub_dividend_pass(monkeypatch, release: asyncio.Event, started: asyncio.Event):
    class SlowDividendService:
        def __init__(self, db):
            pass

        async def sync_dividend_data(self):
            started.set()
            await release.wait()        # a Yahoo loop still running at 18:00

        async def compute_dividend_income(self):
            return None

    class _NullSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(div_router, "DividendService", SlowDividendService)
    monkeypatch.setattr(div_router, "AsyncSessionLocal", lambda: _NullSession())


@pytest.mark.asyncio
async def test_a_dashboard_triggered_dividend_pass_delays_the_scheduled_full_sync(monkeypatch, svc):
    release, started = asyncio.Event(), asyncio.Event()
    _stub_dividend_pass(monkeypatch, release, started)

    ran = []

    async def _full_sync_body():
        ran.append("full_sync")
        return {"type": "full_sync", "status": "success"}

    monkeypatch.setattr(svc, "_full_sync_job_locked", _full_sync_body)

    background = asyncio.create_task(div_router._run_dividend_sync_background())
    await started.wait()

    async def _the_pass_finishes():
        release.set()
        await background

    slept = _fake_sleep(monkeypatch, _the_pass_finishes)

    result = await sched.full_sync_job_entry()  # 18:00 fires while the GET's pass runs

    assert ran == ["full_sync"], (
        f"the scheduled full sync did not run: {result.get('status')}/{result.get('reason')}"
    )
    assert slept == [SCHEDULED_GATE_POLL_SECONDS]
    assert svc.recorded == [], "a run that went ahead recorded a skip"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry, body", SCHEDULED_ENTRIES, ids=lambda v: getattr(v, "__name__", v))
async def test_a_gate_held_past_the_wait_still_records_pipeline_busy(monkeypatch, svc, entry, body):
    """Every scheduled entry waits, and gives up after a bounded number of polls."""
    async def _must_not_run():
        raise AssertionError("the job ran beside a held gate")

    monkeypatch.setattr(svc, body, _must_not_run)
    slept = _fake_sleep(monkeypatch)

    with single_flight(SYNC_PIPELINE):          # held for longer than any wait
        result = await entry()

    assert slept == [SCHEDULED_GATE_POLL_SECONDS] * (
        SCHEDULED_GATE_WAIT_SECONDS // SCHEDULED_GATE_POLL_SECONDS
    )
    assert result["status"] == "skipped" and result["reason"] == "pipeline_busy"
    assert svc.recorded == [result]


@pytest.mark.asyncio
async def test_a_manual_trigger_fails_fast_without_waiting(monkeypatch, svc):
    slept = _fake_sleep(monkeypatch)

    with single_flight(SYNC_PIPELINE):
        with pytest.raises(SyncBusy):
            await svc.trigger_sync_now()

    assert slept == [], "POST /api/scheduler/trigger waited instead of answering 429"


@pytest.mark.asyncio
async def test_the_dividend_cooldown_does_not_block_the_job_after_the_pass(monkeypatch, svc):
    """Not worse than stated: once the GET's pass is done, the job enters immediately."""
    release, started = asyncio.Event(), asyncio.Event()
    _stub_dividend_pass(monkeypatch, release, started)
    release.set()
    await div_router._run_dividend_sync_background()   # finished; cooldown window open

    ran = []

    async def _body():
        ran.append(1)
        return {"type": "full_sync", "status": "success"}

    monkeypatch.setattr(svc, "_full_sync_job_locked", _body)
    slept = _fake_sleep(monkeypatch)

    await sched.full_sync_job_entry()
    assert ran == [1]
    assert slept == []


@pytest.mark.skipif(not AUTO_DEPLOY.exists(), reason="ops/auto-deploy.sh not present")
def test_the_gate_wait_ends_inside_the_deploy_guard_window():
    """
    `ops/auto-deploy.sh` defers deploys only within SLOT_MARGIN_MIN of a slot, so a job
    still waiting after that could be killed by a deploy. And the wait must never reach
    the next slot.
    """
    match = re.search(r"^SLOT_MARGIN_MIN=(\d+)", AUTO_DEPLOY.read_text(encoding="utf-8"), re.M)
    assert match, "SLOT_MARGIN_MIN not found in ops/auto-deploy.sh"
    assert 0 < SCHEDULED_GATE_POLL_SECONDS <= SCHEDULED_GATE_WAIT_SECONDS
    assert SCHEDULED_GATE_WAIT_SECONDS < int(match.group(1)) * 60

    hours = sorted(ALL_SYNC_HOURS)
    gaps = [b - a for a, b in zip(hours, hours[1:])] + [hours[0] + 24 - hours[-1]]
    assert SCHEDULED_GATE_WAIT_SECONDS < min(gaps) * 3600
