"""
The scheduled IBKR generation is skipped on ET Sunday and ET Monday.

A statement generated on either reaches the same Friday Saturday's did (the window
ends at the last completed trading day), so it can bring nothing new — and those two
ET days held ~90% of every failed scheduled generation from 2026-08-08 to 10-14, each
one counted by the `Code=1025` lockout. The rule is `generation_brings_new_trading_day`
in `app/services/flex_generation.py`; these tests pin it in ET across both DST
changeovers (where 00:00 Berlin moves between 18:00 and 19:00 ET of the *previous* ET
day), that a skipped day constructs no Flex client at all, that the market-data half
still runs, and that the gap alarm counts from the oldest trading day not yet fetched.

Everything here is offline: the clock is pinned and the Flex client explodes on
construction.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.sync_run import SyncRun
from app.services.flex_generation import (
    ET,
    et_date,
    generation_brings_new_trading_day,
    next_new_trading_day_generation_opens_at,
    oldest_uncovered_trading_day,
)
from app.services.scheduler_service import (
    FULL_SYNC_HOUR,
    IBKR_ONLY_HOURS,
    SchedulerService,
)

BERLIN = ZoneInfo("Europe/Berlin")


def _utc(moment: datetime) -> datetime:
    """An aware instant as the naive UTC the app stores and compares."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _berlin(y, m, d, hour) -> datetime:
    return _utc(datetime(y, m, d, hour, tzinfo=BERLIN))


def _et(y, m, d, hour, minute=0, second=0) -> datetime:
    return _utc(datetime(y, m, d, hour, minute, second, tzinfo=ET))


class _ExplodingFlex:
    """Any construction is a test failure: a skipped day must not reach IBKR."""

    def __init__(self, *a, **kw):
        raise AssertionError("the Flex client was constructed on a day that must not ask IBKR")


# ── the rule, in ET, at the slots the scheduler actually fires ────────────────

(RECOVERY_HOUR,) = IBKR_ONLY_HOURS

# (Berlin date of the slot, Berlin hour, the ET weekday it really is, does it ask IBKR)
SLOTS = [
    # Autumn: Europe left DST on 2026-10-25, the US only on 11-01. For that week
    # Berlin is UTC+1 against New York's UTC-4, so 00:00 Berlin is 19:00 ET.
    ((2026, 10, 25), FULL_SYNC_HOUR, "Sunday", False),
    ((2026, 10, 26), RECOVERY_HOUR, "Sunday", False),
    ((2026, 10, 26), FULL_SYNC_HOUR, "Monday", False),
    ((2026, 10, 27), RECOVERY_HOUR, "Monday", False),   # a Tuesday in Berlin
    ((2026, 10, 27), FULL_SYNC_HOUR, "Tuesday", True),
    ((2026, 10, 28), RECOVERY_HOUR, "Tuesday", True),
    ((2026, 10, 31), FULL_SYNC_HOUR, "Saturday", True),
    ((2026, 11, 1), RECOVERY_HOUR, "Saturday", True),   # a Sunday in Berlin, 19:00 EDT
    # And the week the US leaves DST: 00:00 Berlin is back to 18:00 ET.
    ((2026, 11, 1), FULL_SYNC_HOUR, "Sunday", False),
    ((2026, 11, 2), RECOVERY_HOUR, "Sunday", False),
    ((2026, 11, 3), RECOVERY_HOUR, "Monday", False),
    ((2026, 11, 4), RECOVERY_HOUR, "Tuesday", True),
    # Spring: the US enters DST on 2026-03-08, Europe on 03-29.
    ((2026, 3, 8), FULL_SYNC_HOUR, "Sunday", False),
    ((2026, 3, 10), RECOVERY_HOUR, "Monday", False),
    ((2026, 3, 11), RECOVERY_HOUR, "Tuesday", True),
    ((2026, 3, 28), FULL_SYNC_HOUR, "Saturday", True),
    ((2026, 3, 29), RECOVERY_HOUR, "Saturday", True),   # an hour before Europe's change
    ((2026, 3, 30), RECOVERY_HOUR, "Sunday", False),
    ((2026, 3, 31), RECOVERY_HOUR, "Monday", False),
]


@pytest.mark.parametrize("berlin_day,hour,et_weekday,asks", SLOTS)
def test_each_slot_is_judged_by_its_et_weekday(berlin_day, hour, et_weekday, asks):
    moment = _berlin(*berlin_day, hour)

    assert f"{et_date(moment):%A}" == et_weekday, "fixture no longer lands on that ET day"
    assert generation_brings_new_trading_day(moment) is asks


def test_the_boundary_is_midnight_new_york():
    assert generation_brings_new_trading_day(_et(2026, 10, 12, 23, 59, 59)) is False  # Mon
    assert generation_brings_new_trading_day(_et(2026, 10, 13, 0, 0, 0)) is True      # Tue
    assert generation_brings_new_trading_day(_et(2026, 10, 10, 23, 59, 59)) is True  # Sat
    assert generation_brings_new_trading_day(_et(2026, 10, 11, 0, 0, 0)) is False     # Sun


@pytest.mark.parametrize("as_of", [
    _et(2026, 10, 10, 13),  # Saturday afternoon: Sunday and Monday are both passed over
    _et(2026, 10, 11, 12),  # Sunday
    _et(2026, 10, 12, 18),  # Monday evening
])
def test_the_next_attempt_after_a_weekend_is_tuesday_midnight_et(as_of):
    assert next_new_trading_day_generation_opens_at(as_of) == _et(2026, 10, 13, 0)


def test_the_next_attempt_midweek_is_the_next_midnight_et():
    assert next_new_trading_day_generation_opens_at(_et(2026, 10, 13, 12)) == _et(2026, 10, 14, 0)


@pytest.mark.parametrize("generated_on,expected", [
    (date(2026, 10, 10), date(2026, 10, 12)),  # Saturday reaches Friday → Monday at risk
    (date(2026, 10, 11), date(2026, 10, 12)),  # Sunday
    (date(2026, 10, 12), date(2026, 10, 12)),  # Monday reaches Friday → Monday itself
    (date(2026, 10, 14), date(2026, 10, 14)),  # Wednesday reaches Tuesday → Wednesday
])
def test_the_oldest_trading_day_a_statement_misses(generated_on, expected):
    assert oldest_uncovered_trading_day(generated_on) == expected


# ── the scheduled paths: zero IBKR calls, the market half untouched ───────────

@pytest_asyncio.fixture
async def maker(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.services.scheduler_service.AsyncSessionLocal", sessions)
    monkeypatch.setattr("app.services.scheduler_service.IBKRService", _ExplodingFlex)
    try:
        yield sessions
    finally:
        await engine.dispose()


def _pin_clock(monkeypatch, moment):
    monkeypatch.setattr("app.services.scheduler_service.utcnow", lambda: moment)


@pytest.mark.asyncio
@pytest.mark.parametrize("moment", [
    _berlin(2026, 10, 11, FULL_SYNC_HOUR),   # Sunday 12:00 ET
    _berlin(2026, 10, 12, RECOVERY_HOUR),    # Sunday 18:00 ET
    _berlin(2026, 10, 12, FULL_SYNC_HOUR),   # Monday 12:00 ET
    _berlin(2026, 10, 13, RECOVERY_HOUR),    # Monday 18:00 ET
])
async def test_a_skipped_day_makes_no_ibkr_request(maker, monkeypatch, moment):
    _pin_clock(monkeypatch, moment)

    result = await SchedulerService().sync_ibkr_data()

    assert result["status"] == "skipped"
    assert result["reason"] == "no_new_trading_day"
    assert result["next_attempt_after"].startswith("2026-10-13T04:00")  # Tue 00:00 EDT


@pytest.mark.asyncio
async def test_a_tuesday_still_asks(maker, monkeypatch):
    """The other side of the boundary: reaching the exploding client is the assertion."""
    _pin_clock(monkeypatch, _berlin(2026, 10, 13, FULL_SYNC_HOUR))

    result = await SchedulerService().sync_ibkr_data()

    assert result["status"] == "error"
    assert "must not ask IBKR" in result["message"]


@pytest.mark.asyncio
async def test_force_still_asks_on_a_monday(maker, monkeypatch):
    _pin_clock(monkeypatch, _berlin(2026, 10, 12, FULL_SYNC_HOUR))

    result = await SchedulerService().sync_ibkr_data(force=True)

    assert result["status"] == "error"
    assert "must not ask IBKR" in result["message"]


def _spy_on_the_other_steps(monkeypatch, svc):
    called, recorded = [], []

    def step(name):
        async def fake(*a, **kw):
            called.append(name)
            return {"status": "success"}
        return fake

    for name in ("sync_exchange_rates", "sync_market_data", "sync_dividends",
                 "refresh_lookthrough_data", "record_dividend_forecast"):
        monkeypatch.setattr(svc, name, step(name))

    async def record(result, started_at):
        recorded.append(result)
    monkeypatch.setattr(svc, "_record_run", record)
    return called, recorded


@pytest.mark.asyncio
async def test_the_full_sync_still_prices_on_a_skipped_day(maker, monkeypatch):
    _pin_clock(monkeypatch, _berlin(2026, 10, 12, FULL_SYNC_HOUR))  # Monday 12:00 ET
    svc = SchedulerService()
    called, recorded = _spy_on_the_other_steps(monkeypatch, svc)

    result = await svc.full_sync_job()

    assert "sync_market_data" in called
    assert "sync_dividends" in called
    assert result["status"] == "skipped"
    assert result["reason"] == "no_new_trading_day"
    assert recorded and recorded[0]["reason"] == "no_new_trading_day"


@pytest.mark.asyncio
async def test_the_recovery_slot_records_the_skip(maker, monkeypatch):
    _pin_clock(monkeypatch, _berlin(2026, 10, 13, RECOVERY_HOUR))  # Monday 18:00 ET
    svc = SchedulerService()
    called, recorded = _spy_on_the_other_steps(monkeypatch, svc)

    result = await svc.ibkr_only_sync_job()

    assert result["status"] == "skipped"
    assert result["reason"] == "no_new_trading_day"
    assert "sync_market_data" not in called
    assert recorded and recorded[0]["status"] == "skipped"


# ── the gap alarm counts from the oldest trading day not yet fetched ──────────

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


def _span(days):
    end = date(2026, 10, 9)
    return {"ibkr_result": {
        "data_from": (end - timedelta(days=days - 1)).isoformat(),
        "data_to": end.isoformat(),
    }}


async def _success(db, at, window):
    db.add(SyncRun(sync_type="full_sync", status="success", finished_at=at,
                   details=_span(window)))
    await db.flush()


# The 08:00 Berlin market-data slot, where the alarm first runs each ET day.
def _morning_check(y, m, d):
    return _et(y, m, d, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("check_day", [11, 12, 13])  # Sunday, Monday, Tuesday
async def test_a_saturday_success_does_not_warn_through_the_skipped_days(db, check_day):
    """At the narrowest window ever run, where the floor sits: still quiet."""
    await _success(db, _et(2026, 10, 10, 12, 30), window=3)

    warnings = await SchedulerService().find_flex_generation_gap(
        db, as_of=_morning_check(2026, 10, check_day))

    assert warnings == []


@pytest.mark.asyncio
async def test_a_friday_to_wednesday_outage_warns(db):
    await _success(db, _et(2026, 10, 9, 12, 30), window=3)

    warnings = await SchedulerService().find_flex_generation_gap(
        db, as_of=_morning_check(2026, 10, 14))

    assert len(warnings) == 1
    assert "2026-10-09" in warnings[0]


@pytest.mark.asyncio
async def test_a_failed_saturday_under_a_narrow_window_warns_on_sunday(db):
    """
    The case the skip makes sharper: with Sunday and Monday not asked, Saturday's two
    slots are the only scheduled route to Friday's trades under a 3-day window. Counting
    only Tue–Sat would keep this quiet until Tuesday, the day those trades are gone.
    """
    await _success(db, _et(2026, 10, 9, 12, 30), window=3)

    warnings = await SchedulerService().find_flex_generation_gap(
        db, as_of=_morning_check(2026, 10, 11))

    assert len(warnings) == 1
    assert "oldest trading day not yet fetched 2026-10-09" in warnings[0]


@pytest.mark.asyncio
async def test_the_same_weekend_is_quiet_under_the_live_window(db):
    await _success(db, _et(2026, 10, 9, 12, 30), window=30)

    assert await SchedulerService().find_flex_generation_gap(
        db, as_of=_morning_check(2026, 10, 14)) == []


@pytest.mark.asyncio
async def test_a_wide_window_counts_calendar_days_from_the_monday(db):
    """
    Calendar days, not generation days: the window is N calendar days, so counting
    only Tue–Sat would put the warning past the day Monday's trades fall out.
    """
    await _success(db, _et(2026, 10, 10, 12, 30), window=30)  # Saturday → Monday at risk

    svc = SchedulerService()
    assert await svc.find_flex_generation_gap(db, as_of=_morning_check(2026, 11, 9)) == []
    warnings = await svc.find_flex_generation_gap(db, as_of=_morning_check(2026, 11, 10))
    assert len(warnings) == 1  # 29 days after Monday 10-12
