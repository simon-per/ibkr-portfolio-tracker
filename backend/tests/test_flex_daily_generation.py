"""
IBKR issues about one Flex statement generation per US-Eastern calendar day.

Every attempt after the day's first success is refused with `Code=1001` at the
SendRequest step, and a refusal there is a failed *generation* — precisely what the
`Code=1025` token lockout counts. So asking twice is not a wasted round trip, it is
spending the budget that protects every future sync. Before this guard existed, two of
three scheduled slots plus every manual Sync press did exactly that, daily.

Everything here is offline: no Flex client is ever constructed on the skip paths, which
is asserted rather than assumed.
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.sync_run import SyncRun
from app.services.flex_generation import (
    FLEX_API_SYNC_TYPES,
    FLEX_WINDOW_DAYS_FALLBACK,
    IBKR_SYNC_TYPES,
    et_date,
    et_day_bounds_utc,
    flex_window_days,
    generation_already_spent,
    last_generation_today,
    next_generation_opens_at,
)
from app.services.scheduler_service import (
    FLEX_GENERATION_GAP_WARN_DAYS,
    FLEX_GENERATION_GAP_WARN_DAYS_FLOOR,
    IBKR_SYNC_STALE_DAYS,
    SchedulerService,
)

# 2026-08-08 12:00 UTC is 08:00 ET — mid-morning in New York, comfortably inside one ET
# day, so a test that meant to write "today" cannot land on a boundary by accident.
NOW = datetime(2026, 8, 8, 12, 0, 0)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def _run(db, sync_type, status, at, details=None):
    db.add(SyncRun(sync_type=sync_type, status=status, finished_at=at, details=details))
    await db.flush()


def _span(days, end="2026-08-21", nested=True):
    """A recorded run's `details` carrying a statement span `days` calendar days wide."""
    last = datetime.fromisoformat(end).date()
    inner = {
        "data_from": (last - timedelta(days=days - 1)).isoformat(),
        "data_to": last.isoformat(),
    }
    return {"ibkr_result": inner} if nested else inner


class _ExplodingFlex:
    """Any construction or call is a test failure: these paths must not reach IBKR."""

    def __init__(self, *a, **kw):
        raise AssertionError("the Flex client was constructed on a path that must not ask IBKR")


# ── the rule itself ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_success_earlier_in_the_same_et_day_spends_the_generation(db):
    await _run(db, "ibkr_sync", "success", NOW - timedelta(hours=4))

    assert await last_generation_today(db, as_of=NOW) is not None


@pytest.mark.asyncio
async def test_a_success_in_the_previous_et_day_does_not(db):
    """
    The boundary, from the far side. A statement generated yesterday says nothing about
    today's availability — midnight ET is exactly when the window rolls forward and a
    new generation becomes both possible and *worth* making.
    """
    yesterday_start, _ = et_day_bounds_utc(NOW - timedelta(days=1))
    await _run(db, "ibkr_sync", "success", yesterday_start + timedelta(hours=23, minutes=30))

    assert await last_generation_today(db, as_of=NOW) is None


@pytest.mark.asyncio
async def test_the_boundary_is_midnight_new_york_not_midnight_utc(db):
    """
    The one that catches a UTC-vs-ET slip, which would otherwise be invisible for most
    of the day and wrong for six hours of it.

    03:00 UTC on 08-08 is 23:00 ET on 08-07 — the same *UTC* day as a 12:00 UTC "now",
    but the ET day before it. Reading the column as UTC would call that success today's
    and suppress a generation that is genuinely available.
    """
    late_yesterday_et = datetime(2026, 8, 8, 3, 0, 0)
    assert et_date(late_yesterday_et) != et_date(NOW), "fixture no longer spans an ET boundary"

    await _run(db, "ibkr_sync", "success", late_yesterday_et)

    assert await last_generation_today(db, as_of=NOW) is None


@pytest.mark.asyncio
async def test_a_failure_today_does_not_spend_the_generation(db):
    """
    A day full of failures has consumed nothing, and the next slot must still try. This
    is the case the 2026-08-02 and 08-03 recoveries depended on: the 00:00 ET attempt
    failed both days and the 02:00 ET one succeeded.
    """
    await _run(db, "ibkr_sync", "error", NOW - timedelta(hours=4))

    assert await last_generation_today(db, as_of=NOW) is None


@pytest.mark.asyncio
async def test_an_offline_xml_ingest_does_not_suppress_the_days_attempt(db):
    """
    `ibkr_manual_xml` is excluded from FLEX_API_SYNC_TYPES, and that is empirical rather
    than a guess: the browser download and the Web Service are independent channels, so
    an offline ingest costs no generation. This account's history shows it twice — an
    offline ingest at 00:16 ET on 07-28 followed by a *successful* API generation at
    02:05 ET the same day, and the same shape on 07-31.

    Counting it would suppress the day's real attempt on exactly the days someone had
    just recovered by hand, which is the worst possible day to skip one.
    """
    assert 'ibkr_manual_xml' not in FLEX_API_SYNC_TYPES
    await _run(db, "ibkr_manual_xml", "success", NOW - timedelta(hours=4))

    assert await last_generation_today(db, as_of=NOW) is None


@pytest.mark.asyncio
async def test_a_market_data_success_is_not_a_flex_generation(db):
    """Yahoo answering says nothing about whether Flex will."""
    await _run(db, "market_data_only", "success", NOW - timedelta(hours=1))

    assert await last_generation_today(db, as_of=NOW) is None


@pytest.mark.asyncio
async def test_an_unreadable_history_asks_ibkr_rather_than_skipping(db):
    """
    The guard fails **open**, and the asymmetry is the reason.

    Failing closed would skip the sync, and a skipped sync is invisible — no error, no
    warning, just a portfolio that quietly stops updating. That is the same silent-outage
    shape that hid the stalled 730-day pass for four days. Failing open costs at most one
    refused request.
    """
    class _BrokenSession:
        async def execute(self, *a, **kw):
            raise RuntimeError("database is locked")

    assert await generation_already_spent(_BrokenSession()) is None

    # And the honest query still raises, so the policy lives in one place rather than
    # being swallowed everywhere the question is asked.
    with pytest.raises(RuntimeError):
        await last_generation_today(_BrokenSession())


def test_the_next_window_opens_at_the_next_midnight_new_york():
    opens = next_generation_opens_at(NOW)

    assert opens > NOW
    assert et_date(opens) == et_date(NOW) + timedelta(days=1)
    # Exactly midnight ET, i.e. the first instant of the next ET day — not merely
    # somewhere inside it.
    assert et_day_bounds_utc(opens)[0] == opens


# ── the scheduler step ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_scheduled_sync_skips_without_touching_ibkr(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.services.scheduler_service.AsyncSessionLocal", maker)
    monkeypatch.setattr("app.services.scheduler_service.IBKRService", _ExplodingFlex)

    try:
        async with maker() as seed:
            seed.add(SyncRun(sync_type="full_sync", status="success", finished_at=utcnow_today()))
            await seed.commit()

        result = await SchedulerService().sync_ibkr_data()

        assert result["status"] == "skipped"
        assert result["reason"] == "already_generated_today"
        assert result["last_success_at"]
        assert result["next_attempt_after"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_force_bypasses_the_guard(monkeypatch):
    """
    The escape hatch, and it has a real use: editing the query definition in the IBKR
    portal resets the daily generation (07-31 is the only two-success ET day on record).
    Nothing scheduled passes it, so this cannot become the normal path by accident.

    Reaching the exploding client *is* the assertion — it proves the guard was skipped.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.services.scheduler_service.AsyncSessionLocal", maker)
    monkeypatch.setattr("app.services.scheduler_service.IBKRService", _ExplodingFlex)

    try:
        async with maker() as seed:
            seed.add(SyncRun(sync_type="full_sync", status="success", finished_at=utcnow_today()))
            await seed.commit()

        # sync_ibkr_data swallows every exception into an error dict, so the exploding
        # client surfaces as a failed sync rather than a raise. Either way it was called.
        result = await SchedulerService().sync_ibkr_data(force=True)

        assert result["status"] == "error"
        assert "must not ask IBKR" in result["message"]
    finally:
        await engine.dispose()


# ── the two alarms, which ask different questions ─────────────────────────────

@pytest.mark.asyncio
async def test_a_skipped_run_is_not_a_success_for_the_staleness_alarm(db):
    """
    The guard turns most IBKR runs into `skipped` rows, so the alarm that counts
    *successes* had better keep ignoring them — otherwise a permanently misfiring guard
    would look, to the only thing watching, exactly like a healthy schedule.
    """
    await _run(db, "full_sync", "success", NOW - timedelta(days=IBKR_SYNC_STALE_DAYS + 1))
    for day in range(IBKR_SYNC_STALE_DAYS + 1):
        await _run(db, "ibkr_sync", "skipped", NOW - timedelta(days=day))

    warnings = await SchedulerService().find_stale_ibkr_sync(db, as_of=NOW)

    assert warnings and "days ago" in warnings[0]


@pytest.mark.asyncio
async def test_the_gap_alarm_is_quiet_while_generations_land(db):
    await _run(db, "full_sync", "success", NOW - timedelta(days=1))

    assert await SchedulerService().find_flex_generation_gap(db, as_of=NOW) == []


@pytest.mark.asyncio
async def test_the_gap_alarm_fires_before_the_flex_window_loses_the_trades(db):
    """
    Two ET days is the whole margin under a `Last 3 Calendar Days` query, which is why
    this exists beside the 7-day detector rather than instead of it: warning at 7 about
    a 3-day window is warning four days after the trades have gone.
    """
    await _run(db, "full_sync", "success", NOW - timedelta(days=FLEX_GENERATION_GAP_WARN_DAYS))

    warnings = await SchedulerService().find_flex_generation_gap(db, as_of=NOW)

    assert len(warnings) == 1
    assert "US-Eastern days" in warnings[0]
    assert "ingest_flex_xml" in warnings[0]


@pytest.mark.asyncio
async def test_an_offline_ingest_closes_the_gap_even_though_it_spends_no_generation(db):
    """
    The mirror image of `test_an_offline_xml_ingest_does_not_suppress_the_days_attempt`,
    and the pair is the point: one table, two questions, two different type sets.

    The alarm asks *"has the data been refreshed?"* — an offline ingest genuinely has
    refreshed it, so the alarm must quieten or it blares straight through the documented
    recovery and trains the reader to ignore it. The guard asks *"has today's generation
    been spent?"* — it has not, so the next slot must still try. Getting either one
    backwards is a real bug in the opposite direction.
    """
    assert 'ibkr_manual_xml' in IBKR_SYNC_TYPES
    await _run(db, "full_sync", "success", NOW - timedelta(days=5))
    await _run(db, "ibkr_manual_xml", "success", NOW - timedelta(hours=2))

    assert await SchedulerService().find_flex_generation_gap(db, as_of=NOW) == []


@pytest.mark.asyncio
async def test_the_gap_alarm_is_silent_on_a_fresh_install(db):
    """
    No successful sync ever is `find_stale_ibkr_sync`'s case. Two warnings on one
    condition is how a warnings list stops being read.
    """
    await _run(db, "full_sync", "error", NOW - timedelta(days=9))

    assert await SchedulerService().find_flex_generation_gap(db, as_of=NOW) == []


def utcnow_today() -> datetime:
    """
    A naive-UTC instant guaranteed to fall inside the *current* ET day.

    The scheduler step reads the real clock, so these fixtures cannot pin `NOW`. Taking
    the ET day's own start and adding an hour keeps the row inside the day whatever time
    the suite runs at — including the six hours when the UTC and ET dates disagree,
    which a naive `utcnow() - 1h` would get wrong just after midnight UTC.
    """
    from app.clock import utcnow

    start, _ = et_day_bounds_utc(utcnow())
    return start + timedelta(hours=1)


# ── the manual endpoint, which is where a human meets this ────────────────────

def test_the_route_answers_200_skipped_and_records_nothing(monkeypatch):
    """
    **200, not an error.** Pressing Sync after the day's statement has landed is a no-op,
    not a failure: nothing is missing and nothing needs retrying. It rendered as a red
    "✗ Sync failed" before, because the only way to learn the answer was to ask IBKR and
    be refused — and every refusal spent `Code=1025` lockout budget to produce that
    banner.

    And **no `sync_runs` row**: nothing was attempted. One row per button press would
    bury the real history behind the cheapest possible action.
    """
    import asyncio

    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    # Without this, a regression that drops the guard would make this test issue a real
    # Flex request with the real token from `.env` — the one thing the whole suite is
    # built never to do. It fails offline and loudly instead.
    monkeypatch.setattr("app.routers.sync.IBKRService", _ExplodingFlex)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    session = AsyncSession(engine, expire_on_commit=False)
    loop = asyncio.new_event_loop()

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session.add(SyncRun(
            sync_type="ibkr_sync", status="success", finished_at=utcnow_today(),
        ))
        await session.commit()

    loop.run_until_complete(_setup())

    async def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        # No context manager: that would run the lifespan and start the scheduler.
        response = TestClient(app).post("/api/sync/ibkr")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "skipped"
        assert body["reason"] == "already_generated_today"
        assert body["next_attempt_after"]

        rows = loop.run_until_complete(session.execute(select(func.count(SyncRun.id))))
        assert rows.scalar_one() == 1, "the skip wrote a sync_runs row"
    finally:
        app.dependency_overrides.clear()
        loop.run_until_complete(session.close())
        loop.run_until_complete(engine.dispose())
        loop.close()


@pytest.mark.asyncio
async def test_the_type_sets_differ_by_exactly_the_offline_path(db):
    """
    Pinned as a relationship, not as two literals. They were two literals in two modules
    once, which is the shape every duplicated-logic bug in this codebase has taken.
    """
    assert set(IBKR_SYNC_TYPES) - set(FLEX_API_SYNC_TYPES) == {'ibkr_manual_xml'}
    assert set(FLEX_API_SYNC_TYPES) < set(IBKR_SYNC_TYPES)

    # And the guard's set really is what the query filters on.
    await _run(db, "ibkr", "success", NOW - timedelta(hours=1))
    assert await last_generation_today(db, as_of=NOW) is not None

    total = await db.execute(select(func.count(SyncRun.id)))
    assert total.scalar_one() == 1


# ── the window is measured, not declared ──────────────────────────────────────
#
# The bug these cover, found on production 2026-08-24: the live Flex Query was
# `Last 30 Calendar Days` while the alarm threshold was still the 2 that `N=3` implies,
# so an ordinary two-day gap produced a warning asserting trades were "about to become
# unreachable" with four weeks of margin in hand. The period is a portal setting; a
# constant in the repo tracks it only until somebody edits the portal.


@pytest.mark.asyncio
async def test_the_window_is_read_off_the_statement_ibkr_served(db):
    await _run(db, "full_sync", "success", NOW, details=_span(30))

    assert await flex_window_days(db) == 30


@pytest.mark.asyncio
async def test_the_span_is_found_at_the_top_level_too(db):
    """
    The scheduled jobs nest it under `ibkr_result`; the manual endpoint and the offline
    CLI write the same keys flat. Reading one shape blinds the measurement to half the
    history.
    """
    await _run(db, "ibkr", "success", NOW, details=_span(30, nested=False))

    assert await flex_window_days(db) == 30


@pytest.mark.asyncio
async def test_a_hand_widened_offline_download_does_not_define_the_window(db):
    """
    `ibkr_manual_xml` is the documented *recovery* for a gap and its period is whatever
    the operator typed — routinely wider than the query. Letting it set the window would
    take one wide download and relax the alarm for every day after it.
    """
    await _run(db, "full_sync", "success", NOW - timedelta(days=1), details=_span(3))
    await _run(db, "ibkr_manual_xml", "success", NOW, details=_span(365))

    assert await flex_window_days(db) == 3


@pytest.mark.asyncio
async def test_a_success_without_a_recorded_span_falls_through_to_an_older_one(db):
    """
    A success is not guaranteed to carry the keys. Giving up on the newest row would send
    the measurement to the fallback while a good statement sat one row below.
    """
    await _run(db, "full_sync", "success", NOW - timedelta(days=1), details=_span(30))
    await _run(db, "full_sync", "success", NOW, details={"status": "success"})

    assert await flex_window_days(db) == 30


@pytest.mark.asyncio
async def test_no_measurement_available_is_none_rather_than_a_guess(db):
    await _run(db, "full_sync", "success", NOW)

    assert await flex_window_days(db) is None


@pytest.mark.asyncio
async def test_a_wide_window_does_not_warn_at_the_narrow_threshold(db):
    """
    The regression itself. Two ET days into a 30-day window is not a data-loss event, and
    a false alarm that names one is worse than silence — the next true one reads the same.
    """
    await _run(db, "full_sync", "success", NOW - timedelta(days=2), details=_span(30))

    assert await SchedulerService().find_flex_generation_gap(db, as_of=NOW) == []


@pytest.mark.asyncio
async def test_a_wide_window_still_warns_once_its_own_margin_runs_out(db):
    await _run(db, "full_sync", "success", NOW - timedelta(days=29), details=_span(30))

    warnings = await SchedulerService().find_flex_generation_gap(db, as_of=NOW)

    assert len(warnings) == 1
    assert "30 calendar days" in warnings[0]
    assert "ingest_flex_xml" in warnings[0]


@pytest.mark.asyncio
async def test_a_narrow_window_keeps_the_threshold_it_had(db):
    """N=3 must still fire at 2, or fixing the wide case would break the narrow one."""
    await _run(db, "full_sync", "success", NOW - timedelta(days=2), details=_span(3))

    warnings = await SchedulerService().find_flex_generation_gap(db, as_of=NOW)

    assert len(warnings) == 1
    assert "3 calendar days" in warnings[0]


@pytest.mark.asyncio
async def test_the_floor_stops_a_one_day_window_warning_every_weekend(db):
    """
    IBKR issues nothing Saturday or Sunday, so a Friday success is two ET days old by
    Sunday through no fault of anything. `max(floor, N-1)` is what keeps a pathologically
    narrow period off a hair trigger.
    """
    await _run(db, "full_sync", "success", NOW - timedelta(days=1), details=_span(1))

    assert await SchedulerService().find_flex_generation_gap(db, as_of=NOW) == []
    assert FLEX_GENERATION_GAP_WARN_DAYS_FLOOR == 2


@pytest.mark.asyncio
async def test_an_unmeasurable_window_says_so_rather_than_stating_a_length(db):
    """
    Erring narrow is deliberate — it warns early, and early costs a line while late costs
    trades — but the message must not dress the assumption up as a measurement.
    """
    await _run(db, "full_sync", "success", NOW - timedelta(days=FLEX_WINDOW_DAYS_FALLBACK))

    warnings = await SchedulerService().find_flex_generation_gap(db, as_of=NOW)

    assert len(warnings) == 1
    assert "window length is unknown" in warnings[0]


@pytest.mark.asyncio
async def test_a_gap_past_the_whole_window_says_the_margin_is_gone(db):
    """
    "Act now" and "it is already too late" are different instructions to whoever reads
    this, and the recovery differs too: past the window a wider portal download is the
    only route back.
    """
    await _run(db, "full_sync", "success", NOW - timedelta(days=40), details=_span(30))

    warnings = await SchedulerService().find_flex_generation_gap(db, as_of=NOW)

    assert len(warnings) == 1
    assert "already gone" in warnings[0]
