"""
Tests for the scheduler's job wiring.

The load-bearing invariant here is a Yahoo Finance one: CLAUDE.md forbids calling
Yahoo without explicit user permission (it rate-limits aggressively, and a full
sync is 50-150+ requests over 730 days). The IBKR-only recovery attempt exists to
recover from a transient IBKR error *without* dragging Yahoo along, so that
separation must not quietly regress into calling the full sync.

Its *hour* is load-bearing too, in two ways. IBKR generates a statement from
finalised daily data, so `SendRequest` succeeds overnight and fails during US
market hours — the retries sat at 13:00 and 20:00 Berlin once and went 0-for-6 and
1-for-8. And IBKR issues only about one generation per ET calendar day, so a
recovery slot is worth having only where it falls after the primary within the
same ET day.

Also covers the stale-price detector, which exists because a price that simply never
arrives is otherwise silent: the position is valued at 0.00 and the portfolio total drops
with nothing reporting it (SBI@TSE, -446.93 CHF, 2026-07-27).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.services.scheduler_service import (
    ALL_SYNC_HOURS,
    FULL_SYNC_HOUR,
    IBKR_ONLY_HOURS,
    MARKET_DATA_HOURS,
    MISFIRE_GRACE_SECONDS,
    STALE_PRICE_DAYS,
    SchedulerService,
    _collect_warnings,
    full_sync_job_entry,
    ibkr_only_sync_job_entry,
    market_data_only_sync_job_entry,
)
from app.services.scheduler_service import MANUAL_PRICE_STALE_DAYS
from app.models.security import PRICE_SOURCE_MANUAL
from app.services.finpension_ingest import (
    CARRY_HORIZON_DAYS, PRICE_SOURCE_CARRY, PRICE_SOURCE_STATEMENT,
)
from app.services.portfolio_service import PRICE_LOOKBACK_DAYS

# One job per declared hour — pinned by test_scheduler_registers_every_declared_job,
# which asserts no two of the three groups share one.
EXPECTED_JOB_COUNT = len(ALL_SYNC_HOURS)


class _Spy:
    """Records which sync halves a job invoked."""

    def __init__(self):
        self.called = []

    def make(self, name, result):
        async def fake(*args, **kwargs):
            self.called.append(name)
            return result
        return fake


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    svc = SchedulerService()
    monkeypatch.setattr(svc, 'sync_ibkr_data',
                        s.make('ibkr', {"status": "success", "securities_synced": 38}))
    monkeypatch.setattr(svc, 'sync_exchange_rates',
                        s.make('fx', {"status": "success"}))
    monkeypatch.setattr(svc, 'sync_market_data',
                        s.make('market_data', {"status": "success"}))
    monkeypatch.setattr(svc, 'sync_dividends',
                        s.make('dividends', {"status": "success"}))
    monkeypatch.setattr(svc, 'sync_benchmark_prices',
                        s.make('benchmarks', {"status": "success"}))
    monkeypatch.setattr(svc, 'refresh_lookthrough_data',
                        s.make('lookthrough', {"status": "success"}))
    return svc, s


@pytest.mark.asyncio
async def test_ibkr_only_job_never_touches_yahoo(spy):
    """The whole point of this job: retry IBKR without spending Yahoo quota."""
    svc, s = spy

    await svc.ibkr_only_sync_job()

    assert 'ibkr' in s.called
    assert 'market_data' not in s.called   # Yahoo — 50-150+ requests
    assert 'dividends' not in s.called     # also yfinance
    assert 'benchmarks' not in s.called    # also yfinance


@pytest.mark.asyncio
async def test_ibkr_only_job_records_its_result_for_the_status_endpoint(spy):
    """/api/scheduler/status (and the cloud validator) read last_sync_result."""
    svc, s = spy

    await svc.ibkr_only_sync_job()

    result = svc.last_sync_result
    assert result["type"] == "ibkr_sync"
    assert result["status"] == "success"
    assert result["ibkr_result"]["securities_synced"] == 38
    datetime.fromisoformat(result["timestamp"])  # parseable


@pytest.mark.asyncio
async def test_ibkr_only_job_reports_failure_status(spy, monkeypatch):
    """A failed IBKR half must surface as status=error, not be masked by FX success."""
    svc, s = spy
    monkeypatch.setattr(svc, 'sync_ibkr_data',
                        s.make('ibkr', {"status": "error", "message": "Code=1025: locked"}))

    await svc.ibkr_only_sync_job()

    assert svc.last_sync_result["status"] == "error"
    assert 'market_data' not in s.called  # still no Yahoo on the failure path


# --- full_sync's market-data half is not hostage to its IBKR half --------------------
#
# IBKR generates this account's statement about once per ET calendar day. Measured on
# 2026-08-07 over the six preceding days: exactly one success each, every later attempt
# refused with Code=1001. The 06:00 Berlin ibkr_only job usually takes that one
# generation, so full_sync's IBKR half usually fails at 08:00 — and while the market
# data step was gated on it, the 730-day pass simply did not run. It had last run on
# 2026-08-03. The six 7-day market_data_only slots hid it by keeping current value
# fresh, so only the deep backfill lapsed and nothing reported that.


@pytest.mark.asyncio
async def test_full_sync_still_prices_when_the_ibkr_half_fails(spy, monkeypatch):
    """The regression that mattered: a refused Flex statement says nothing about Yahoo."""
    svc, s = spy
    monkeypatch.setattr(svc, 'sync_ibkr_data',
                        s.make('ibkr', {"status": "error", "message": "Code=1001"}))
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.full_sync_job()

    assert 'market_data' in s.called, "the 730-day pass was skipped because IBKR refused"
    assert svc.last_sync_result["market_result"] == {"status": "success"}


@pytest.mark.asyncio
async def test_full_sync_still_reports_the_ibkr_failure_it_no_longer_stops_for(spy, monkeypatch):
    """Decoupling must not let a successful Yahoo half paper over a refused statement —
    `status` is the IBKR verdict, which is what find_stale_ibkr_sync counts."""
    svc, s = spy
    monkeypatch.setattr(svc, 'sync_ibkr_data',
                        s.make('ibkr', {"status": "error", "message": "Code=1001"}))
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.full_sync_job()

    assert svc.last_sync_result["status"] == "error"


@pytest.mark.asyncio
async def test_full_sync_prices_after_ibkr_never_before(spy, monkeypatch):
    """Order still carries weight on the days IBKR works: a security created by the
    statement must exist before the market data step tries to price it."""
    svc, s = spy
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.full_sync_job()

    assert s.called.index('ibkr') < s.called.index('market_data')


@pytest.mark.asyncio
async def test_market_warnings_survive_a_failed_ibkr_half(spy, monkeypatch):
    """A skipped step reports nothing, so an unpriced holding found on a morning IBKR
    refused was structurally unreachable — the surface `warnings[]` exists for."""
    svc, s = spy
    monkeypatch.setattr(svc, 'sync_ibkr_data',
                        s.make('ibkr', {"status": "error", "message": "Code=1001"}))
    monkeypatch.setattr(svc, 'sync_market_data', s.make('market_data', {
        "status": "success", "warnings": ["VT@ARCA: no cached price at all"],
    }))
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.full_sync_job()

    assert svc.last_sync_result["warnings"] == ["VT@ARCA: no cached price at all"]


@pytest.mark.asyncio
async def test_a_jobs_warnings_reach_the_top_of_its_result(spy, monkeypatch):
    """_record_run persists result["warnings"], and a job's own dict never had that key —
    so a step's warnings were buried inside `details` and never rendered as warnings."""
    svc, s = spy
    monkeypatch.setattr(svc, 'sync_market_data', s.make('market_data', {
        "status": "success", "warnings": ["SBI@TSE: no cached price at all"],
    }))
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.market_data_only_sync_job()

    assert svc.last_sync_result["warnings"] == ["SBI@TSE: no cached price at all"]


async def _noop():
    return None


def test_collect_warnings_merges_steps_and_stays_absent_when_there_are_none():
    merged = {}
    _collect_warnings(merged, {"warnings": ["a"]}, None, {"warnings": ["b"]})
    assert merged["warnings"] == ["a", "b"]

    quiet = {}
    _collect_warnings(quiet, {"status": "success"}, None)
    assert "warnings" not in quiet   # a clean run must not grow an empty list


# --- the Yahoo rate-limit circuit breaker -------------------------------------------


@pytest.mark.asyncio
async def test_a_yahoo_rate_limit_abandons_the_rest_of_the_pass(monkeypatch):
    """
    CLAUDE.md's recovery for a Yahoo rate limit is "stop immediately, wait 30-60 min",
    and until now the run did the opposite: `fetch_prices_from_yahoo` aborted only the
    *ticker variations* for the security in hand, so the caller logged a failure and
    moved straight on to the next of ~40, asking the same IP again seconds later for
    several more minutes.

    Cheap to ignore at three market-data passes a day. Not at seven, which is why this
    lands with the intraday slots rather than separately.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy.ext.asyncio import async_sessionmaker
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.services.scheduler_service.AsyncSessionLocal", maker)

    try:
        async with maker() as seed:
            for i in range(1, 5):
                seed.add(Security(
                    id=i, isin=f"US000000000{i}", symbol=f"S{i}", description=f"S{i}",
                    currency="USD", conid=1000 + i, asset_category="STK",
                    exchange="NASDAQ",
                ))
            await seed.commit()

        attempted = []

        async def _fetch(self, security, days_back=730):
            attempted.append(security.symbol)
            if len(attempted) == 2:
                self.rate_limited = True     # Yahoo said 429 on the second security
                return 0
            return 5

        monkeypatch.setattr(
            "app.services.market_data_service.MarketDataService.sync_security_prices",
            _fetch,
        )

        result = await SchedulerService().sync_market_data(days_back=7)

        assert len(attempted) == 2, f"kept asking after a rate limit: {attempted}"
        assert result["securities_processed"] == 2
        assert result["prices_fetched"] == 5, "the first security's prices were kept"
        assert result["status"] == "partial_success"
        assert result["rate_limited"] is True
        # The warning has to survive the diagnostics block below it, which used to
        # assign `warnings` outright and would have dropped the one line explaining
        # why two thirds of the portfolio has no fresh price.
        assert any("rate-limited" in w for w in result["warnings"])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_every_market_data_sweep_stops_on_a_rate_limit():
    """
    The family check, not the instance.

    `SchedulerService.sync_market_data` and `POST /api/market-data/sync` were two copies
    of one loop, and the breaker went into the scheduler's copy first — leaving the
    *public* route asking Yahoo for another ~38 securities after being told to back off.
    Exactly CLAUDE.md's dominant failure mode, in the half a stranger can reach.

    Both now delegate to `MarketDataService.sync_securities`, so this asserts the shared
    sweep stops, and `test_both_sync_paths_share_one_sweep` below asserts neither caller
    has grown its own loop again.
    """
    from app.services.market_data_service import MarketDataService

    svc = MarketDataService.__new__(MarketDataService)
    svc.rate_limited = False
    attempted = []

    async def _fetch(security, days_back=730):
        attempted.append(security.symbol)
        if len(attempted) == 2:
            svc.rate_limited = True
        return 3

    svc.sync_security_prices = _fetch
    securities = [SimpleNamespace(symbol=f"S{i}", exchange="NASDAQ") for i in range(1, 6)]

    swept = await svc.sync_securities(securities, days_back=7)

    assert attempted == ["S1", "S2"], f"kept asking after a rate limit: {attempted}"
    assert swept["processed"] == 2
    assert swept["total_prices"] == 6, "prices already fetched must be kept"
    assert swept["rate_limited_after"] == "S2"
    assert swept["errors"] == []


def test_both_sync_paths_share_one_sweep():
    """
    Neither caller may re-grow the per-security loop.

    A source check because that is the only thing that can see it: both copies worked
    perfectly well while disagreeing about whether to stop on a 429, and a behavioural
    test of one says nothing about the other. Keyed on `sync_securities` appearing in
    both and `sync_security_prices` in neither — the latter is the inner call a
    hand-rolled loop would have to make.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in ("app/services/scheduler_service.py", "app/routers/market_data.py"):
        source = (root / rel).read_text(encoding="utf-8")
        assert "sync_securities(" in source, (
            f"{rel} no longer delegates to MarketDataService.sync_securities — if it "
            f"has its own securities loop again, the rate-limit breaker is optional "
            f"in exactly one of the two paths."
        )
        assert "sync_security_prices(" not in source, (
            f"{rel} calls sync_security_prices directly, i.e. it is iterating "
            f"securities itself. Use sync_securities so the breaker and the pacing "
            f"cannot drift between the scheduled job and the public route."
        )


# --- stale / missing prices ---------------------------------------------------------


@pytest_asyncio.fixture
async def price_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
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


async def _seed(db, security_id, symbol, latest_price_age=None, is_open=True):
    """A security with one tax lot and, optionally, a newest price N days old."""
    db.add(Security(
        id=security_id, isin=f"US000000000{security_id}", symbol=symbol,
        description=symbol, currency="USD", conid=1000 + security_id,
        asset_category="STK", exchange="NASDAQ",
    ))
    db.add(TaxLot(
        security_id=security_id, open_date=date(2026, 1, 5), quantity=Decimal("10"),
        cost_basis=Decimal("1000"), price_per_unit=Decimal("100"), currency="USD",
        cost_basis_eur=Decimal("900"), is_open=is_open,
        close_date=None if is_open else date(2026, 6, 1),
    ))
    if latest_price_age is not None:
        db.add(MarketPrice(
            security_id=security_id, date=date.today() - timedelta(days=latest_price_age),
            close_price=Decimal("120"), currency="USD", source="yahoo_finance",
        ))
    await db.flush()


@pytest.mark.asyncio
async def test_a_security_with_no_price_at_all_is_reported(price_db):
    """The case that actually cost money: no price means the position is valued at 0.00,
    and every other layer treats that as a legitimate number."""
    await _seed(price_db, 1, "SBI", latest_price_age=None)

    warnings = await SchedulerService().find_stale_priced_securities(price_db)

    assert len(warnings) == 1
    assert "SBI@NASDAQ" in warnings[0]
    assert "no cached price" in warnings[0]


@pytest.mark.asyncio
async def test_a_stale_price_is_reported_with_its_age(price_db):
    await _seed(price_db, 1, "OLD", latest_price_age=STALE_PRICE_DAYS + 3)

    warnings = await SchedulerService().find_stale_priced_securities(price_db)

    assert len(warnings) == 1
    assert f"{STALE_PRICE_DAYS + 3} days old" in warnings[0]


@pytest.mark.asyncio
async def test_a_fresh_price_is_not_reported(price_db):
    """The 15:00 market-data job runs before the US close, so a day or two of lag on a
    price is routine. Warning on it would train the reader to ignore this."""
    await _seed(price_db, 1, "FRESH", latest_price_age=1)

    assert await SchedulerService().find_stale_priced_securities(price_db) == []


@pytest.mark.asyncio
async def test_a_fully_sold_security_going_quiet_is_not_a_fault(price_db):
    """Only open lots move a number the user sees; a closed-out holding legitimately
    stops getting prices, and reporting it would be permanent noise."""
    await _seed(price_db, 1, "GONE", latest_price_age=None, is_open=False)

    assert await SchedulerService().find_stale_priced_securities(price_db) == []


@pytest.mark.asyncio
async def test_each_held_security_is_reported_at_most_once(price_db):
    """Several open lots per security is the norm (972 lots over 38 securities), so the
    grouping has to collapse them or one bad feed becomes dozens of warnings."""
    await _seed(price_db, 1, "MANYLOTS", latest_price_age=None)
    price_db.add(TaxLot(
        security_id=1, open_date=date(2026, 2, 9), quantity=Decimal("5"),
        cost_basis=Decimal("500"), price_per_unit=Decimal("100"), currency="USD",
        cost_basis_eur=Decimal("450"), is_open=True,
    ))
    await price_db.flush()

    warnings = await SchedulerService().find_stale_priced_securities(price_db)

    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_an_old_price_alongside_a_recent_one_is_not_stale(price_db):
    """The check is on the *newest* price, not any price — otherwise every security with
    two years of history would look broken."""
    await _seed(price_db, 1, "HISTORY", latest_price_age=1)
    price_db.add(MarketPrice(
        security_id=1, date=date.today() - timedelta(days=400),
        close_price=Decimal("80"), currency="USD", source="yahoo_finance",
    ))
    await price_db.flush()

    assert await SchedulerService().find_stale_priced_securities(price_db) == []


@pytest.fixture()
def jobstore_url(tmp_path, monkeypatch):
    """A throwaway persistent store per test, so nothing touches the real one."""
    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    monkeypatch.setattr(settings, "scheduler_jobstore_url", url, raising=False)
    return url


def _trigger_hour(job) -> int:
    return int(str(job.trigger).split("hour='")[1].split("'")[0])


def _declared_job_ids() -> set:
    """
    The job ids this build should register, derived from the hour constants.

    Derived rather than listed, because the literal set was written out twice and both
    copies had to be edited by hand when the IBKR slots went from two to one — which is
    the same "one fact, two copies" shape the rest of this codebase keeps getting caught
    by, in the file whose whole job is to notice schedule drift.
    """
    return {
        'full_sync_job',
        *(f'ibkr_retry_{i}' for i in range(1, len(IBKR_ONLY_HOURS) + 1)),
        *(f'market_sync_{i}' for i in range(1, len(MARKET_DATA_HOURS) + 1)),
    }


@pytest.mark.asyncio
async def test_scheduler_registers_every_declared_job(jobstore_url):
    # async so AsyncIOScheduler has a running loop to attach to (it only reports
    # itself as running inside one, and shutdown() raises otherwise).
    svc = SchedulerService()
    try:
        svc.start()
        jobs = {job.id: job for job in svc.scheduler.get_jobs()}

        assert set(jobs) == _declared_job_ids()
        # The IBKR jobs must not collide with a market-data job, or a Yahoo sync and
        # an IBKR sync run concurrently against the same DB.
        for index, hour in enumerate(IBKR_ONLY_HOURS, start=1):
            assert _trigger_hour(jobs[f'ibkr_retry_{index}']) == hour
        assert _trigger_hour(jobs['full_sync_job']) == FULL_SYNC_HOUR
        assert not set(IBKR_ONLY_HOURS) & set(MARKET_DATA_HOURS)
        assert FULL_SYNC_HOUR not in MARKET_DATA_HOURS
    finally:
        svc.shutdown()


@pytest.mark.asyncio
async def test_the_registered_slots_are_exactly_the_declared_ones(jobstore_url):
    """
    ALL_SYNC_HOURS is what `ops/auto-deploy.sh` is checked against, so it has to be
    what actually runs rather than a comment that agrees with the code today.

    This is the half `test_deploy_guard_hours.py` cannot see: that file compares the
    shell script to the constant, and this one compares the constant to the triggers
    APScheduler ends up holding. Together they mean a new slot cannot be added
    without the deploy guard learning about it — the previous arrangement regexed
    literal `hour=` digits out of the source, which a slot registered from a loop or
    at a half-hour would have slipped straight past.
    """
    svc = SchedulerService()
    try:
        svc.start()
        registered = {_trigger_hour(job) for job in svc.scheduler.get_jobs()}
        assert registered == set(ALL_SYNC_HOURS)

        for job in svc.scheduler.get_jobs():
            assert "minute='0'" in str(job.trigger), (
                f"{job.id} does not run on the hour. The deploy guard reasons in whole "
                f"hours, so it could not defer a deploy away from this slot."
            )
    finally:
        svc.shutdown()


@pytest.mark.asyncio
async def test_market_data_repriced_at_least_every_three_hours_intraday(jobstore_url):
    """
    The point of the 2026-08-04 change: the portfolio was repriced at 08:00, 15:00 and
    22:00 Berlin only, so a value read mid-morning could be seven hours stale, and
    Xetra's *close* was never captured at all — the 15:00 job ran 2.5 hours before it.

    Asserted as a coverage property rather than as a list of hours, so re-timing a slot
    stays free while quietly dropping back to two-a-day does not. 09:00-22:00 Berlin
    spans the European session and the US session end to end.
    """
    svc = SchedulerService()
    try:
        svc.start()
        market_hours = sorted(
            _trigger_hour(job) for job in svc.scheduler.get_jobs()
            if job.id.startswith('market_sync_') or job.id == 'full_sync_job'
        )
        covering = [h for h in market_hours if 8 <= h <= 22]
        gaps = [b - a for a, b in zip(covering, covering[1:])]
        assert gaps and max(gaps) <= 3, (
            f"market data reprices at {covering}:00 Berlin, leaving a {max(gaps)}h gap "
            f"inside market hours"
        )
        # Europe closes at 17:30 and the US at 22:00; each needs a slot after it, or
        # the day's real close is never fetched and the last provisional one sticks
        # once it ages past PROVISIONAL_PRICE_DAYS.
        assert any(18 <= h <= 21 for h in market_hours), "no slot after the European close"
        assert any(h >= 22 for h in market_hours), "no slot at or after the US close"
    finally:
        svc.shutdown()


# Every job that reaches IBKR. Market-data jobs are excluded: they touch Yahoo, not Flex.
IBKR_JOB_IDS = ('full_sync_job', *(f'ibkr_retry_{i}' for i in range(1, len(IBKR_ONLY_HOURS) + 1)))

# The Berlin hours an IBKR job is *allowed* to run at.
#
# This used to be the range 22:00-09:00, asserted as a rule, because IBKR builds this
# statement from *finalised* daily data: `SendRequest` succeeds overnight and fails
# during the US session, as `Code=1001` at the request step — the fatal-fast kind.
# Measured on this account's own `sync_runs` (2026-07-31): overnight 8/9, afternoon and
# evening 1/15, with 13:00 Berlin at 0-for-6 and 20:00 at 1-for-8.
#
# **18:00 is in this list against that evidence, by the account owner's explicit
# decision** (2026-08-08, reaffirmed after the trade-off was put to them twice). It is
# 12:00 ET, mid-session. It captures no additional trades — the Flex window ends
# yesterday measured in US Eastern and rolls at midnight ET, so 12:00 ET covers exactly
# what 00:00 ET does — and it is unproven at that hour. Recorded here rather than in a
# commit message so the next person to read a run of `1001`s knows this was chosen, and
# knows what moving it back to 06:00 Berlin would buy.
IBKR_ALLOWED_HOURS = frozenset({0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 18, 22, 23})


@pytest.mark.asyncio
async def test_ibkr_jobs_run_at_the_declared_hours(jobstore_url):
    """
    An IBKR slot may not drift to an hour nobody chose.

    The check this replaced asserted a *rule* (overnight only) that the schedule no
    longer follows, so it had to become an allowlist — but an allowlist is still the
    thing that matters here: what has actually hurt this account is a slot moving
    without anyone re-deriving whether it can succeed there. Every failed attempt is a
    failed statement *generation*, which is precisely what the `Code=1025` token lockout
    counts, so a badly placed slot is not merely useless but actively harmful.

    See IBKR_ALLOWED_HOURS for why 18:00 is on the list despite the measurements.
    """
    svc = SchedulerService()
    try:
        svc.start()
        jobs = {job.id: job for job in svc.scheduler.get_jobs()}

        offenders = {
            jid: _trigger_hour(jobs[jid])
            for jid in IBKR_JOB_IDS
            if _trigger_hour(jobs[jid]) not in IBKR_ALLOWED_HOURS
        }

        assert not offenders, (
            f"IBKR job(s) at an unvetted hour: {offenders}. Statement generation fails "
            f"reliably inside the US session and each failure spends Code=1025 lockout "
            f"budget. Add the hour to IBKR_ALLOWED_HOURS only with a reason."
        )
    finally:
        svc.shutdown()


@pytest.mark.asyncio
async def test_only_one_ibkr_slot_can_win_a_given_et_day(jobstore_url):
    """
    IBKR issues about one statement generation per ET calendar day, so a second slot is
    only ever a *recovery* attempt — it earns its place by falling in the same ET day as
    the primary, after it, and before midnight ET rolls the window.

    A slot placed outside that span is not redundancy, it is a second doomed generation
    the day after. 18:00 Berlin is 12:00 ET and 00:00 Berlin is 18:00 ET *of the same ET
    day*, which is the property this pins — six Berlin hours later, still six hours
    before the ET day ends.

    An empty IBKR_ONLY_HOURS passes vacuously, which is correct: dropping the recovery
    slot entirely is a legitimate choice, just a thinner one.
    """
    # Berlin is UTC+1/+2 and New York UTC-5/-4, so the gap is six hours for all but the
    # ~3 weeks a year the two DST changeovers are out of step, when it is five. Both are
    # checked rather than assuming the common case, because a slot ordering that only
    # holds for 49 weeks of the year is exactly the kind of thing nobody would catch in
    # the other three.
    for offset in (5, 6):
        primary_et = (FULL_SYNC_HOUR - offset) % 24

        for hour in IBKR_ONLY_HOURS:
            recovery_et = (hour - offset) % 24
            assert primary_et < recovery_et, (
                f"at a {offset}h Berlin-to-ET offset the {hour:02d}:00 Berlin recovery "
                f"slot is {recovery_et}:00 ET, which is *before* the {primary_et}:00 ET "
                f"primary — so it would take the day's one generation and the primary "
                f"would skip behind it"
            )


@pytest.mark.asyncio
async def test_a_missed_slot_is_still_worth_running_for_half_an_hour(jobstore_url):
    """
    A deploy overlapping a Berlin slot used to lose that sync outright — the 08:00
    full_sync on 2026-07-30 went that way. The grace window has to cover a
    `build --no-cache` rebuild and no more: a genuinely long outage must not dump four
    stale slots onto a cold container.
    """
    svc = SchedulerService()
    try:
        svc.start()
        for job in svc.scheduler.get_jobs():
            assert job.misfire_grace_time == MISFIRE_GRACE_SECONDS
            # One catch-up run per job, not one per slot the outage covered.
            assert job.coalesce is True
            assert job.max_instances == 1
    finally:
        svc.shutdown()


@pytest.mark.asyncio
async def test_a_restart_preserves_the_run_time_it_is_meant_to_recover(jobstore_url):
    """
    The trap that makes a persistent store useless: `add_job(replace_existing=True)`
    recomputes next_run_time from now, so the missed timestamp is overwritten on the way
    in and the misfire is never noticed. An unchanged schedule must leave it alone.
    """
    first = SchedulerService()
    first.start()
    before = {j.id: j.next_run_time for j in first.scheduler.get_jobs()}
    first.shutdown()

    second = SchedulerService()
    try:
        second.start()
        after = {j.id: j.next_run_time for j in second.scheduler.get_jobs()}
    finally:
        second.shutdown()

    assert after == before


@pytest.mark.asyncio
async def test_a_job_this_build_no_longer_registers_is_removed_from_the_store(jobstore_url):
    """
    The job store outlives the code, so a renamed or retired job does not disappear — it
    keeps its trigger and keeps firing. For the IBKR retries that is not cosmetic: the
    old id would run a *second* sync alongside the new one, and each extra run burns a
    Flex statement generation, which is exactly what `Code=1025` counts.

    This is not hypothetical — the retries were renamed from `ibkr_sync_midday` /
    `ibkr_sync_evening` on 2026-07-31 when their hours moved and the names became lies.
    """
    first = SchedulerService()
    first.start()
    # A job from a hypothetical earlier build, persisted in the same store.
    first.scheduler.add_job(
        ibkr_only_sync_job_entry,
        trigger=CronTrigger(hour=13, minute=0, timezone='Europe/Berlin'),
        id='ibkr_sync_midday', name='IBKR-only Sync (13:00 Europe/Berlin)',
        replace_existing=True,
    )
    assert first.scheduler.get_job('ibkr_sync_midday') is not None
    first.shutdown()

    second = SchedulerService()
    try:
        second.start()
        ids = {j.id for j in second.scheduler.get_jobs()}
    finally:
        second.shutdown()

    assert 'ibkr_sync_midday' not in ids, "a retired job kept firing from the store"
    assert ids == _declared_job_ids()


@pytest.mark.asyncio
async def test_a_schedule_change_still_takes_effect(jobstore_url, monkeypatch):
    """
    The other half: keeping a stored job must not mean a stored job can never be
    rescheduled. Editing an hour has to replace it, stale run time and all.
    """
    first = SchedulerService()
    first.start()
    original = first.scheduler.get_job('full_sync_job').next_run_time
    first.shutdown()

    second = SchedulerService()
    try:
        # Same id, a different hour: the trigger repr differs, so it is replaced.
        real_add_or_keep = SchedulerService._add_or_keep

        def shifted(self, job_id, func, trigger, name):
            if job_id == 'full_sync_job':
                trigger = CronTrigger(hour=9, minute=0, timezone='Europe/Berlin')
            return real_add_or_keep(self, job_id, func, trigger, name)

        monkeypatch.setattr(SchedulerService, "_add_or_keep", shifted)
        second.start()
        job = second.scheduler.get_job('full_sync_job')
        assert "hour='9'" in str(job.trigger)
        assert job.next_run_time != original
    finally:
        second.shutdown()


@pytest.mark.asyncio
async def test_jobs_are_serializable_into_the_persistent_store(jobstore_url):
    """
    The registered targets must be importable by name. They were bound methods, which
    an in-memory store holds happily and a persistent one cannot: pickling
    `self.full_sync_job` drags the live AsyncIOScheduler in with it. A store round-trip
    through a *fresh* service proves the references survive a process boundary.
    """
    first = SchedulerService()
    first.start()
    first.shutdown()

    second = SchedulerService()
    try:
        second.start()
        funcs = {job.id: job.func for job in second.scheduler.get_jobs()}
        assert funcs['full_sync_job'] is full_sync_job_entry
        assert funcs['ibkr_retry_1'] is ibkr_only_sync_job_entry
        assert funcs['market_sync_1'] is market_data_only_sync_job_entry
    finally:
        second.shutdown()


@pytest.mark.asyncio
async def test_an_empty_jobstore_url_keeps_the_scheduler_in_memory(monkeypatch):
    """The escape hatch, so a read-only or ephemeral filesystem can still run."""
    monkeypatch.setattr(settings, "scheduler_jobstore_url", "", raising=False)
    svc = SchedulerService()
    try:
        svc.start()
        assert len(svc.scheduler.get_jobs()) == EXPECTED_JOB_COUNT
    finally:
        svc.shutdown()


def test_scheduler_is_enabled_by_default():
    """
    The gate that keeps a local uvicorn from arming real IBKR/Yahoo syncs must
    default to ON, or production silently stops syncing and looks perfectly
    healthy while doing it. Read from the field default rather than the loaded
    settings object, which picks up whatever the local .env says.
    """
    from app.config import Settings

    assert Settings.model_fields["scheduler_enabled"].default is True


@pytest.mark.asyncio
async def test_an_unusable_job_store_degrades_instead_of_killing_the_container(
    monkeypatch, caplog
):
    """
    The store is a bind-mounted sqlite file, so corrupt or unwritable is possible — and
    an exception out of start() propagates through the lifespan handler and the
    container never boots. Losing misfire recovery costs one late sync; failing to
    start costs the whole site.

    The guard has to wrap start(), not the constructor: SQLAlchemyJobStore connects
    lazily, so a bad file surfaces when the scheduler starts the store.
    """
    import logging
    from apscheduler.jobstores import sqlalchemy as js_module

    monkeypatch.setattr(settings, "scheduler_jobstore_url", "sqlite:///:memory:", raising=False)

    class Exploding(js_module.SQLAlchemyJobStore):
        def start(self, scheduler, alias):
            raise RuntimeError("database disk image is malformed")

    monkeypatch.setattr(
        "app.services.scheduler_service.SQLAlchemyJobStore", Exploding
    )

    svc = SchedulerService()
    try:
        with caplog.at_level(logging.ERROR):
            svc.start()

        # Came up anyway, with the full schedule.
        assert svc.scheduler is not None
        assert len(svc.scheduler.get_jobs()) == EXPECTED_JOB_COUNT
        # And said so, rather than degrading silently.
        assert any("running in memory" in r.getMessage() for r in caplog.records)
    finally:
        svc.shutdown()


# ── Staleness for a security priced from provider statements ────────────────

async def _seed_manual(db, security_id, symbol, *, observed_age, carry_to):
    """
    A statement-priced fund: one observed NAV `observed_age` days ago, plus the carry
    the importer materialises forward from it.
    """
    db.add(Security(
        id=security_id, isin=f"CH000000000{security_id}", symbol=symbol,
        description=symbol, currency="CHF", conid=None, asset_category="STK",
        exchange="FUND", account="pillar3a", price_source=PRICE_SOURCE_MANUAL,
    ))
    db.add(TaxLot(
        security_id=security_id, open_date=date(2026, 1, 5), quantity=Decimal("10"),
        cost_basis=Decimal("1000"), price_per_unit=Decimal("100"), currency="CHF",
        cost_basis_eur=Decimal("1000"), is_open=True,
    ))
    observed = date.today() - timedelta(days=observed_age)
    db.add(MarketPrice(security_id=security_id, date=observed,
                       close_price=Decimal("120"), currency="CHF",
                       source=PRICE_SOURCE_STATEMENT))
    for offset in range(1, carry_to + 1):
        db.add(MarketPrice(security_id=security_id, date=observed + timedelta(days=offset),
                           close_price=Decimal("120"), currency="CHF",
                           source=PRICE_SOURCE_CARRY))
    await db.flush()


@pytest.mark.asyncio
async def test_a_carried_price_does_not_hide_a_stale_statement(price_db):
    """
    The defect this exists to prevent. The importer carries the last NAV forward 45
    days, so `max(market_prices.date)` sits in the **future** and a staleness alarm
    keyed on it could never fire — the fund would silently coast on a months-old NAV
    with nothing asking for a newer export. The alarm must read the newest *observed*
    row, which is the whole reason the two source tags exist.
    """
    await _seed_manual(price_db, 1, "CH1529078078",
                       observed_age=MANUAL_PRICE_STALE_DAYS + 5, carry_to=45)

    warnings = await SchedulerService().find_stale_priced_securities(price_db)

    assert len(warnings) == 1
    assert "CH1529078078" in warnings[0]
    # Actionable for the source that actually feeds it — "check the ticker mapping"
    # would send the reader hunting a Yahoo symbol this security does not have.
    assert "Upload a newer statement export" in warnings[0]
    assert "ticker_mappings" not in warnings[0]


@pytest.mark.asyncio
async def test_a_recent_statement_is_quiet_even_though_a_daily_feed_would_warn(price_db):
    """
    A monthly upload must not warn every week. Reusing STALE_PRICE_DAYS here would
    badge it permanently, which is the always-present-never-actionable warning that
    teaches the reader to skip the banner carrying something real.
    """
    await _seed_manual(price_db, 1, "CH1529078078",
                       observed_age=STALE_PRICE_DAYS + 10, carry_to=45)

    assert await SchedulerService().find_stale_priced_securities(price_db) == []


@pytest.mark.asyncio
async def test_the_warning_arrives_before_the_holding_drops_out_of_the_total():
    """
    The ordering, asserted as an invariant rather than as two constants.

    A carried price runs out `CARRY_HORIZON_DAYS` past the last observed NAV, and the
    read path walks back `PRICE_LOOKBACK_DAYS` beyond that — so the position vanishes
    from the total on day 59. If the alarm fired later, `unpriced_holdings` would be
    the first signal, and it points at a broken ticker mapping rather than at a stale
    upload. There must be no day on which the holding is unvaluable and this is silent.
    """
    drops_out_on = CARRY_HORIZON_DAYS + PRICE_LOOKBACK_DAYS
    assert MANUAL_PRICE_STALE_DAYS < drops_out_on, (
        f"a statement-priced holding goes unvaluable on day {drops_out_on} but the "
        f"alarm does not speak until day {MANUAL_PRICE_STALE_DAYS}"
    )


@pytest.mark.asyncio
async def test_a_yahoo_security_is_unaffected_by_the_manual_threshold(price_db):
    """The ordinary case keeps the tight threshold: a daily feed going quiet for a week
    really is broken."""
    await _seed(price_db, 1, "OLD", latest_price_age=STALE_PRICE_DAYS + 3)
    warnings = await SchedulerService().find_stale_priced_securities(price_db)
    assert len(warnings) == 1
    assert "price feed looks broken" in warnings[0]


# --- the look-through keeps itself current from the 18:00 job, and only from there -------
#
# Issuer sites plus OpenFIGI/GLEIF: no Yahoo, no IBKR. The step exists because stale
# baskets warned on every market-data run for two weeks in 2026-08/09 with nobody running
# the CLI; it runs once a day because the issuers publish once a day, and seven third-party
# sites are not a thing to poll at every slot.


@pytest.mark.asyncio
async def test_full_sync_refreshes_the_lookthrough_last(spy, monkeypatch):
    """After market data and dividends, so an issuer hanging on its timeout delays nothing
    the portfolio's own figures depend on."""
    svc, s = spy
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.full_sync_job()

    assert 'lookthrough' in s.called
    assert s.called.index('lookthrough') > s.called.index('market_data')
    assert s.called.index('lookthrough') > s.called.index('dividends')
    assert svc.last_sync_result["lookthrough_result"] == {"status": "success"}


@pytest.mark.asyncio
async def test_lookthrough_upkeep_runs_when_ibkr_refused(spy, monkeypatch):
    """Same reasoning as the market-data half: a refused Flex statement says nothing about
    an issuer's website."""
    svc, s = spy
    monkeypatch.setattr(svc, 'sync_ibkr_data',
                        s.make('ibkr', {"status": "error", "message": "Code=1001"}))
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.full_sync_job()

    assert 'lookthrough' in s.called
    assert svc.last_sync_result["status"] == "error", "the IBKR verdict is still the status"


@pytest.mark.asyncio
async def test_lookthrough_warnings_reach_the_top_of_the_result(spy, monkeypatch):
    """A basket that failed to refresh is exactly the kind of thing `warnings[]` exists
    for — and a step's warnings buried in `details` are never rendered."""
    svc, s = spy
    monkeypatch.setattr(svc, 'refresh_lookthrough_data', s.make('lookthrough', {
        "status": "success", "warnings": ["XAIX: basket refresh failed (ConnectError)"],
    }))
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.full_sync_job()

    assert svc.last_sync_result["warnings"] == ["XAIX: basket refresh failed (ConnectError)"]


@pytest.mark.asyncio
async def test_the_lighter_jobs_leave_the_issuer_sites_alone(spy, monkeypatch):
    svc, s = spy
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.ibkr_only_sync_job()
    await svc.market_data_only_sync_job()

    assert 'lookthrough' not in s.called


# --- every Yahoo step reports what it did not finish --------------------------------
#
# The dividend and benchmark steps both latched a rate limit correctly and then
# reported nothing: no `rate_limited`, no `warnings`, and neither result was handed to
# `_collect_warnings`. A pass Yahoo killed at 5 of 40 was recorded as `success` and
# looked exactly like a complete one.


class _FakeSession:
    """Just enough of an AsyncSession for the two scheduler steps below."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, *_a, **_k):
        rows = self._rows

        class _Result:
            def all(self_inner):
                return list(rows)

        return _Result()

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_dividend_warnings_reach_the_top_of_the_result(spy, monkeypatch):
    """`div_result` was the one step result the full sync never passed to
    `_collect_warnings`, so its warnings were buried in `details` for every run."""
    svc, s = spy
    monkeypatch.setattr(svc, 'sync_dividends', s.make('dividends', {
        "status": "success",
        "warnings": ["Yahoo Finance rate limit reached; the rest of this pass was abandoned."],
    }))
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.full_sync_job()

    assert svc.last_sync_result["warnings"] == [
        "Yahoo Finance rate limit reached; the rest of this pass was abandoned."
    ]


@pytest.mark.asyncio
async def test_benchmark_warnings_reach_the_top_of_the_result(spy, monkeypatch):
    """The market-data-only job collected only `market_result`; the benchmark warm-up
    beside it is the fastest Yahoo loop at burning a limit and was silent."""
    svc, s = spy
    monkeypatch.setattr(svc, 'sync_benchmark_prices', s.make('benchmarks', {
        "status": "success", "benchmarks_synced": 2, "benchmarks_total": 8,
        "rate_limited": True,
        "warnings": ["Yahoo Finance rate limit reached during the benchmark warm-up"],
    }))
    monkeypatch.setattr(svc, '_record_run', lambda *a, **k: _noop())

    await svc.market_data_only_sync_job()

    assert svc.last_sync_result["warnings"] == [
        "Yahoo Finance rate limit reached during the benchmark warm-up"
    ]


@pytest.mark.asyncio
async def test_sync_dividends_hoists_its_childrens_warnings_and_the_rate_limit(monkeypatch):
    """Two children, two places a warning could hide: the fetch's abandoned pass and the
    compute step's FX-skipped rows. Both must surface on the step result itself."""
    import app.services.scheduler_service as sched_mod
    from app.services.dividend_service import DividendService

    monkeypatch.setattr(sched_mod, "AsyncSessionLocal", lambda: _FakeSession())

    async def _sync(self):
        return {"securities_processed": 5, "rate_limited": True,
                "warnings": ["Yahoo Finance rate limit reached"]}

    async def _compute(self):
        return {"computed": 3, "fx_skipped": 1,
                "warnings": ["1 dividend(s) left uncomputed: no exchange rate"]}

    monkeypatch.setattr(DividendService, "sync_dividend_data", _sync)
    monkeypatch.setattr(DividendService, "compute_dividend_income", _compute)

    result = await SchedulerService().sync_dividends()

    assert result["status"] == "success"
    assert result["rate_limited"] is True
    assert result["warnings"] == [
        "Yahoo Finance rate limit reached",
        "1 dividend(s) left uncomputed: no exchange rate",
    ]


@pytest.mark.asyncio
async def test_a_quiet_dividend_step_grows_no_warnings_key(monkeypatch):
    import app.services.scheduler_service as sched_mod
    from app.services.dividend_service import DividendService

    monkeypatch.setattr(sched_mod, "AsyncSessionLocal", lambda: _FakeSession())

    async def _sync(self):
        return {"securities_processed": 5, "rate_limited": False}

    async def _compute(self):
        return {"computed": 3, "fx_skipped": 0}

    monkeypatch.setattr(DividendService, "sync_dividend_data", _sync)
    monkeypatch.setattr(DividendService, "compute_dividend_income", _compute)

    result = await SchedulerService().sync_dividends()

    assert result["rate_limited"] is False
    assert "warnings" not in result   # a clean run must not grow an empty list


@pytest.mark.asyncio
async def test_the_benchmark_warmup_reports_how_far_it_got_when_yahoo_refused(monkeypatch):
    """A warm-up that stopped at the first of two benchmarks used to return the same
    `{"status": "success"}` as one that finished."""
    import app.services.scheduler_service as sched_mod
    from app.services.benchmark_service import BenchmarkService

    monkeypatch.setattr(
        sched_mod, "AsyncSessionLocal",
        lambda: _FakeSession(rows=[("^GSPC",), ("^IXIC",)]),
    )

    async def _refuse(self, *_a, **_k):
        raise Exception("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(BenchmarkService, "_ensure_prices_available", _refuse)

    result = await SchedulerService().sync_benchmark_prices()

    assert result["status"] == "success"          # the step ran; the job is not a failure
    assert result["rate_limited"] is True
    assert result["benchmarks_synced"] == 0
    assert result["benchmarks_total"] == 2
    assert len(result["warnings"]) == 1
    assert "0 of 2" in result["warnings"][0]
    assert "Do not retry manually" in result["warnings"][0]


@pytest.mark.asyncio
async def test_a_complete_benchmark_warmup_is_not_flagged(monkeypatch):
    import app.services.scheduler_service as sched_mod
    from app.services.benchmark_service import BenchmarkService

    monkeypatch.setattr(
        sched_mod, "AsyncSessionLocal",
        lambda: _FakeSession(rows=[("^GSPC",), ("^IXIC",)]),
    )

    async def _fine(self, *_a, **_k):
        return 3

    monkeypatch.setattr(BenchmarkService, "_ensure_prices_available", _fine)

    result = await SchedulerService().sync_benchmark_prices()

    assert result["rate_limited"] is False
    assert result["benchmarks_synced"] == result["benchmarks_total"] == 2
    assert "warnings" not in result
