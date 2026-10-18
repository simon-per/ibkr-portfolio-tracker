"""
Scheduler Service
Handles automated daily synchronization of IBKR data and market prices.
"""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import date, timedelta
from app.clock import utc_today, utcnow

from app.config import settings
from app.database import AsyncSessionLocal
from app.single_flight import SYNC_PIPELINE, SyncBusy, is_running, single_flight
from app.services.ibkr_service import IBKRService, FLEX_RETRY_DELAYS_PATIENT
from app.services.flex_generation import (
    FLEX_WINDOW_DAYS_FALLBACK,
    IBKR_SYNC_TYPES,
    describe_refusal_hold,
    et_date,
    flex_refusal_in_force,
    flex_window_days,
    generation_already_spent,
    generation_brings_new_trading_day,
    is_trading_day,
    next_generation_opens_at,
    newest_coverage,
    next_new_trading_day_generation_opens_at,
)
from app.services.market_data_service import MarketDataService
from app.services.currency_service import CurrencyService
from app.services.sync_helper import ingest_flex_statement
from app.repositories.security_repository import SecurityRepository, held
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.sync_run_repository import SyncRunRepository, utc_iso
from app.services.benchmark_service import (
    BENCHMARKS, BenchmarkService, last_finished_session,
)
from app.models.benchmark_price import BenchmarkPrice
from app.models.dividend_payment import DividendPayment
from app.repositories.dividend_repository import YFINANCE_SOURCE
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.sync_run import SyncRun
from app.models.ticker_mapping import TickerMapping
from app.models.security import PRICE_SOURCE_MANUAL
from app.services.finpension_ingest import CARRIED_PRICE_SOURCES, CARRY_HORIZON_DAYS
from app.services.portfolio_service import PRICE_LOOKBACK_DAYS
from sqlalchemy import select, distinct, func, and_

logger = logging.getLogger(__name__)


def _sqlite_path(url: str) -> Optional[Path]:
    """
    The filesystem path a `sqlite:///` URL points at, or None if it has none.

    None covers both the non-sqlite case and `:memory:` — neither has a directory
    to create, and `tests/conftest.py` runs the whole suite on the latter.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    target = url[len(prefix):]
    if not target or target.startswith(":"):
        return None
    return Path(target)


def ensure_jobstore_parent(url: str) -> None:
    """
    Create the directory the job store's sqlite file lives in.

    sqlite creates the *file* on first connect but never its parent, so a job store
    addressed inside a directory that does not exist yet fails with the same
    `unable to open database file` as a genuinely unwritable path. That matters on a
    fresh clone and on any host where the compose mount has not been created yet.

    Best-effort: a failure here is reported by the caller's existing fallback to an
    in-memory store, which is strictly better than refusing to boot.
    """
    path = _sqlite_path(url)
    if path is None or not path.parent.name:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Could not create the scheduler job store directory %s", path.parent)

# How old the newest cached price for a held security may be before we say so.
# Generous enough to absorb a weekend plus a public holiday, and a slot that runs
# before a market opens, so a warning means something is actually wrong rather than
# "the market was shut". Five days is roughly one trading week of silence.
STALE_PRICE_DAYS = 5

#: The same alarm for a security priced from provider statements rather than a feed.
#:
#: Per-source rather than one global number, the same judgement `ADAPTER_STALE_DAYS`
#: makes: five days is right for a daily feed and would badge a monthly upload
#: permanently, which is the always-present-never-actionable warning CLAUDE.md calls
#: worse than none.
#:
#: **Forty is chosen so the warning arrives before the holding disappears.** The
#: importer carries a NAV forward `CARRY_HORIZON_DAYS` (45) and the read path walks
#: back `PRICE_LOOKBACK_DAYS` (14) beyond that, so the position drops out of the
#: total on day 59. A warning after that would be the wrong alarm: `unpriced_holdings`
#: would fire first and send the reader hunting a broken ticker mapping.
MANUAL_PRICE_STALE_DAYS = 40

#: How many ECB publication days a held currency's (or the base currency's) rate may
#: miss before `find_stale_exchange_rates` says so. Not one: the warm-up re-asks at
#: every slot, so a provider blip the next slot heals is recorded in the run's
#: `fx_result` (`frankfurter_failed`, `fallback_available`) and never warns. Three is
#: three working days of every slot failing, well inside `fx_policy.FX_MAX_CARRY_DAYS`.
FX_STALE_PUBLICATION_DAYS = 3

# How long IBKR may go without a *successful* sync before we say so.
#
# Nothing watched this until 2026-07-31, and under a Year-to-Date Flex Query it did not
# much matter: every statement re-delivers the whole year, so a gap self-heals the moment
# one sync lands. **It becomes load-bearing the moment the query period is bounded.**
# With a 30-day window, an outage longer than the window loses those trades permanently
# and silently — the rows simply never appear in any future statement.
#
# Seven days against two IBKR attempts on each of the five ET days that schedule one
# (Tuesday to Saturday; Sunday and Monday skip, see `generation_brings_new_trading_day`)
# means ~10 consecutive failures before it fires, so it cannot cry wolf over a `1025`
# lockout (~14h) or a bad night. The longest healthy gap is Saturday to Tuesday, ~3 days.
#
# **Which of the two alarms fires first depends on the query period, and both orders
# are correct.** `find_flex_generation_gap` warns at N-1 ET days for an N-day window, so
# under the `Last 3 Calendar Days` period this account ran in August it fired at 2 — days
# before this one, because seven days is far too slow to protect a 3-day window. Under
# the `Last 30 Calendar Days` period actually in force it fires at 29, and *this* one
# leads. That is not a regression in either: they answer different questions. This one
# says the token or the schedule is broken and somebody should look; that one says the
# trades themselves are about to become unrecoverable. Under a wide window the operational
# problem genuinely does deserve attention weeks before the data is at risk.
IBKR_SYNC_STALE_DAYS = 7

# The floor under `find_flex_generation_gap`'s threshold, in ET days.
#
# The threshold itself is **derived from the window IBKR actually served** — see
# `flex_generation.flex_window_days` — because the period is a portal setting no constant
# here can track. A statement covering N calendar days makes a trade on day T reachable
# from T+1..T+N and gone from T+N+1, and one generation is available per ET day, so the
# margin is N and the alarm belongs at N-1: one day of slack to notice and recover by
# hand, since the recovery is a browser download through `app/cli/ingest_flex_xml.py`
# and needs a human awake.
#
# Two is the floor rather than the answer. It is what N=3 produces, and it keeps a
# pathologically narrow window off a hair trigger: at 1 it would warn every weekday
# morning with nothing wrong, because the market-data checks before 12:00 ET already see
# yesterday's success as one day old. An ordinary weekend no longer needs the floor —
# the alarm counts from the oldest trading day the last statement missed, so a Saturday
# success is nothing until Monday's trades.
FLEX_GENERATION_GAP_WARN_DAYS_FLOOR = 2

# Kept under the old name because `tests/test_flex_daily_generation.py` and
# `tests/test_stale_etf_baskets.py` import it, and because it is still the threshold
# whenever nothing is on record to measure.
FLEX_GENERATION_GAP_WARN_DAYS = FLEX_GENERATION_GAP_WARN_DAYS_FLOOR

# `IBKR_SYNC_TYPES` — the sync types that actually reach IBKR — now comes from
# `app/services/flex_generation.py` (imported at the top of this module, and still
# importable from here for the callers that already do).
#
# It lives there because there are two overlapping sets and keeping them apart matters.
# `IBKR_SYNC_TYPES` answers *"was the data refreshed?"*, so it **includes** the offline
# `ibkr_manual_xml` path — ingesting a browser download genuinely updates the portfolio.
# `FLEX_API_SYNC_TYPES` answers *"was today's one statement generation spent?"*, and
# **excludes** it, because that download arrives over an independent channel and costs
# no generation. Same table, two questions; declaring the tuples separately in two
# modules is how this codebase's duplicated logic has always drifted.

# How late a missed run may still be honoured.
#
# APScheduler runs in-process with the app, so a `docker compose down` that overlaps a
# Berlin slot loses that slot outright — which is what happened to the 2026-07-30 08:00
# full_sync, and why CLAUDE.md tells humans not to push near a slot. With a persistent
# job store the missed run time survives the restart, and this is the window in which
# it is still worth running.
#
# Thirty minutes is chosen from both ends. A full uncached rebuild takes a couple
# of minutes, so it comfortably covers a deploy. And it is short enough that a genuinely
# long outage does *not* dump four missed slots onto a cold container: anything older is
# dropped, so only the slot the outage actually straddled is recovered. `coalesce` then
# collapses repeats of the same job into one.
MISFIRE_GRACE_SECONDS = 30 * 60

# How long a *scheduled* job waits for a held SYNC_PIPELINE gate before recording
# `skipped / pipeline_busy` (GitHub issue #16). Public GETs hold that gate too — the
# dividend card's background pass, the benchmark's lazy fetch — so a dashboard load just
# before 18:00 used to cost the day its 730-day pass and look-through upkeep. Manual
# triggers never wait: they answer 429 at once.
#
# Strictly under `SLOT_MARGIN_MIN` in ops/auto-deploy.sh (10): a job that only starts
# its work after the deploy guard's window has closed can be killed by a deploy
# (`test_the_gate_wait_ends_inside_the_deploy_guard_window`). Slots are an hour or more
# apart, so a waiting job never reaches the next one, and APScheduler's max_instances=1
# counts the wait as the running instance of a job that next fires hours later.
SCHEDULED_GATE_WAIT_SECONDS = 9 * 60
SCHEDULED_GATE_POLL_SECONDS = 15
# The wait's sleep, a module name so tests replace it instead of sleeping for real.
_gate_sleep = asyncio.sleep

# ── The schedule, in Europe/Berlin hours ──────────────────────────────────────
#
# The constants below are the single source of truth for when anything runs. They
# exist as constants rather than as literals inside the `CronTrigger` calls because
# `ops/auto-deploy.sh` carries its own copy of the same hours — it defers a deploy
# that would land in a slot — and that copy has already drifted once, in the worst
# possible direction (it guarded two retired slots and left the live ones bare).
# `tests/test_deploy_guard_hours.py` now compares the shell script against
# ALL_SYNC_HOURS, and `test_the_registered_slots_are_exactly_the_declared_ones`
# checks the triggers really come from here, so the chain is closed end to end.
#
# Whole hours only, and that is a constraint rather than a preference: the guard
# reasons in hours, so a half-hour slot could not be expressed on its side and
# would run unprotected.
#
# ── Why IBKR sits at 18:00 Berlin, against the evidence ───────────────────────
#
# The account owner asked for the single daily Flex generation to be aimed at 18:00
# Berlin (12:00 ET), and reaffirmed it after the trade-off was put to them twice. It
# is written down here because the code now contradicts what the measurements argue
# for, and a later reader must not "fix" it back without knowing that:
#
# - **It captures no additional trades.** The Flex window ends yesterday *measured in
#   US Eastern* and rolls at midnight ET, not at generation time — so 12:00 ET and
#   00:00 ET on the same day both cover D-3..D-1. It is the same statement, ~12 hours
#   later. Reaching *today's* trades needs a custom date range set by hand in the
#   portal, which is not a schedule change.
# - **12:00 ET is mid-US-session**, where IBKR builds this statement from unfinalised
#   daily data. The historical mid-session slots ran 13:00 Berlin 0-for-6 and 20:00
#   Berlin 1-for-8. `test_ibkr_jobs_run_at_the_declared_hours` records this as a
#   deliberate exception rather than asserting the old overnight rule.
# - **Attempts drop from three per ET day to two**, both in the weaker band, against a
#   3-day Flex window whose whole margin is two consecutive failed days. That is what
#   `find_flex_generation_gap` exists to catch; `find_stale_ibkr_sync` at 7 days
#   cannot see it in time.
#
# What makes this survivable at all is the guard in `flex_generation.py`: whichever
# slot runs first in an ET day takes the generation and the rest skip without asking,
# so the cost of a doomed attempt is now zero rather than a spent `Code=1025` budget.
IBKR_ONLY_HOURS = (0,)
FULL_SYNC_HOUR = 18
# 18:00 is absent here because the full sync already reprices then; these are the
# additional market-data touches. Why six and not two: see `start()`.
#
# The seven Yahoo repricing hours are 8, 11, 13, 15, 18, 20, 22 and are **unchanged**
# by the IBKR move — only which job performs the 08:00 and 18:00 touches changed, so
# 08:00 is now a plain 7-day pass and the 730-day one rides with the 18:00 full sync.
MARKET_DATA_HOURS = (8, 11, 13, 15, 20, 22)
# On trading days only (GitHub issue #9). No exchange the book holds trades on a
# weekend, yet every weekend pass re-asked Yahoo for each security, because
# PROVISIONAL_PRICE_DAYS keeps Wed-Fri (Saturday) or Thu-Fri (Sunday) "missing" even
# when cached — ~650 redundant requests a weekend against an IP-based limit (rule 1).
MARKET_DATA_DAYS = 'mon-fri'
# The one weekend pass that changes anything: Saturday 08:00 settles Friday's US
# close, which the 22:00 pass wrote seconds after the bell. Monday 08:00 would settle
# it too, but the weekend would show the provisional price until then.
MARKET_DATA_SETTLE_HOUR = 8
MARKET_DATA_SETTLE_DAYS = 'mon-sat'
# The slots the portfolio itself is synced at: IBKR, FX, prices.
PORTFOLIO_SYNC_HOURS = tuple(sorted({FULL_SYNC_HOUR, *IBKR_ONLY_HOURS, *MARKET_DATA_HOURS}))

# ── The watchlist's own slot ──────────────────────────────────────────────────
#
# Nothing scheduled the watchlist until 2026-10-13: it refreshed when someone pressed
# *Refresh All* and aged silently otherwise. The owner asked for a scheduled refresh
# **away from the portfolio slots**, because it is the most expensive Yahoo loop per
# row — up to six requests an item (`WatchlistService._fetch_ticker_data`; `.info` alone
# is three), four on the nights its weekly estimates are not due — and stacking it on a
# repricing pass is how an IP-based rate limit gets spent.
#
# 04:00 Berlin: Yahoo's last scheduled touch is 22:00 and the next 08:00 (00:00 is
# IBKR-only), so this leaves six quiet hours before it and four after. Tuesday to
# Saturday, i.e. once after each US close and never on a day no market moved.
WATCHLIST_REFRESH_HOUR = 4
WATCHLIST_REFRESH_DAYS = 'tue-sat'

# ── The fundamentals' slot ────────────────────────────────────────────────────
#
# The Fundamentals tab and the analyst ratings refreshed only on a click until
# 2026-10-14, then weekly; since 2026-10-17 daily, at the owner's request — the tab's
# ratios are read beside prices that move every day. One pass is six Yahoo requests per
# company (seven when its rating is due) and three per fund (`.info` is three of them;
# a fund's rating is never asked), **held securities only**
# (`security_repository.held`), and only rows older than `STALE_AFTER`
# (docs/flex-and-sync.md, *Sync schedule*).
#
# Tuesday to Saturday, once after each US close, like the watchlist — and an hour after
# it, so the two most expensive Yahoo loops never share a pass (a watchlist pass takes minutes,
# and the shared SYNC_PIPELINE gate would turn an overlap longer than
# SCHEDULED_GATE_WAIT_SECONDS into a skipped day). 05:00
# still leaves three quiet hours before the 08:00 repricing. A new hour, so the deploy
# guard's copies in ops/ carry it too (`tests/test_deploy_guard_hours.py`).
FUNDAMENTALS_REFRESH_HOUR = 5
FUNDAMENTALS_REFRESH_DAYS = 'tue-sat'

# Every hour at which anything is registered, and so what the deploy guard is checked
# against. Not the same thing as PORTFOLIO_SYNC_HOURS since the upkeep slots exist.
ALL_SYNC_HOURS = tuple(sorted({
    *PORTFOLIO_SYNC_HOURS, WATCHLIST_REFRESH_HOUR, FUNDAMENTALS_REFRESH_HOUR,
}))

# The crypto sync (docs/crypto.md) rides the portfolio's eight slots **by derivation,
# not by copy**, so the deploy guard — which is checked against ALL_SYNC_HOURS, a
# superset — already covers every crypto run and no ops script needs to learn a new
# hour. Derived from PORTFOLIO_SYNC_HOURS rather than ALL_SYNC_HOURS on purpose: a slot
# added for some other upkeep must not become one more CoinStats run a day, which is
# credits (`tests/test_crypto_budget.py`). It shares minute :00 with the stock jobs on
# purpose: its only upstream is CoinStats, it holds no gate the stock jobs use, and it
# never opens a write transaction during an HTTP call.
CRYPTO_SYNC_HOURS = PORTFOLIO_SYNC_HOURS

# Which pipeline a registered job belongs to. The public `/api/scheduler/status` lists
# the stock group only — the dashboard reads `jobs[0]` as its "Next:" and the daily
# validator counts the jobs — while `/api/crypto/status` reads the crypto group. The
# upkeep group is the watchlist refresh: it is not a sync of the portfolio, so it is in
# neither list and never becomes "the last sync" (it is absent from SCHEDULED_JOB_TYPES
# and never sets `last_sync_result`). The daily fundamentals refresh sits there too.
STOCK_JOB_GROUP = "stock"
CRYPTO_JOB_GROUP = "crypto"
UPKEEP_JOB_GROUP = "upkeep"

# The `sync_runs.sync_type` values the stock scheduler's own jobs record — the
# `_gated_job` names below and the `type` of each job's result. The status endpoint's
# "last sync" means the newest of these, never a crypto run that finished later.
SCHEDULED_JOB_TYPES = ("full_sync", "market_data_only", "ibkr_sync")

# The timezone every CronTrigger here is written in.
BERLIN = ZoneInfo('Europe/Berlin')


def is_berlin_trading_day(moment: Optional[datetime] = None) -> bool:
    """
    Whether the Berlin calendar date of `moment` (naive UTC, default now) is a trading
    day — `flex_generation.is_trading_day`, so US holidays stay unmodelled in one place.

    Berlin rather than ET because it answers "does this slot fall on a day the market
    moved", and the slots are Berlin hours: the IBKR half keys on ET for its own reason.
    """
    aware = (moment or utcnow()).replace(tzinfo=timezone.utc)
    return is_trading_day(aware.astimezone(BERLIN).date())


def _collect_warnings(job_result: dict, *step_results) -> None:
    """
    Hoist the steps' warnings to the top of a job's result dict.

    `_record_run` reads `result["warnings"]`, and a job's own dict never had that key —
    so anything a step reported was buried inside `details` and never rendered as a
    warning. The steps that produce them (`ingest_flex_statement`'s skipped currencies
    and split purges, `sync_market_data`'s stale prices) are exactly the events that
    should be visible without opening the JSON.
    """
    warnings = []
    for step in step_results:
        warnings.extend((step or {}).get("warnings") or [])
    if warnings:
        job_result["warnings"] = warnings


class SchedulerService:
    """
    Service for scheduling automated data synchronization tasks.

    Runs 8 times each weekday (Europe/Berlin):
    - 08:00, 11:00, 13:00, 15:00, 20:00, 22:00: Market data only (7 days); weekends
      only Saturday 08:00, which settles Friday's US close (MARKET_DATA_DAYS)
    - 18:00: Full sync (IBKR + 730 days market data + look-through upkeep) — fills
      historical gaps gradually and refreshes any stale issuer basket. Daily; on a
      Berlin weekend its price pass asks only for dates with no row at all
    - 00:00: IBKR + FX only — no Yahoo

    **Only one of the two IBKR slots can ever succeed in a day.** IBKR generates this
    statement about once per US-Eastern calendar day, so 18:00 Berlin (12:00 ET) takes
    it and 00:00 Berlin (18:00 ET, the same ET day) skips without asking — unless
    18:00 failed, which is the only case the second slot exists for. The guard is in
    `app/services/flex_generation.py`; the reasoning for the hour is beside
    IBKR_ONLY_HOURS.

    Yahoo is repriced at seven hours spread *through* both sessions rather than sitting
    after each close — see MARKET_DATA_HOURS and `start()`.
    """

    def __init__(self):
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.last_sync_result: Optional[dict] = None
        # False until `start()` proves the configured store actually opened.
        self.jobstore_persistent: bool = False
        # Ids registered by this build, filled in by `_add_or_keep` and consumed by
        # `_prune_unknown_jobs` to evict jobs the persistent store outlived.
        self._registered_job_ids: set = set()
        # job id -> STOCK_JOB_GROUP | CRYPTO_JOB_GROUP, from the same registration.
        self._job_groups: dict = {}
        # ticker -> the finished session the benchmark warm-up last had answered for it;
        # see `sync_benchmark_prices`.
        self._benchmark_sessions_asked: dict = {}

    async def sync_ibkr_data(self, force: bool = False) -> dict:
        """
        Sync securities and tax lots from IBKR Flex Query.

        Returns `status="skipped"` without touching the network when IBKR has already
        generated today's statement — see `app/services/flex_generation.py` for the
        evidence. Asking anyway cannot succeed and is not free: the refusal arrives as
        `Code=1001` at the SendRequest step, which is a *failed generation*, and failed
        generations are exactly what the `Code=1025` token lockout counts.

        Also `status="skipped"`, `reason="no_new_trading_day"`, on an ET Sunday or ET
        Monday: those statements hold no trading day Saturday's did not, and were where
        the failed generations clustered (`flex_generation.generation_brings_new_trading_day`).
        Every scheduled IBKR path comes through here, so this is the one place it is asked.

        Also `status="skipped"`, `reason="flex_refusal_hold"`, while the newest Flex
        outcome on record is a `Code=1025` younger than `FLEX_LOCKOUT_HOLD_HOURS` — the
        lockout half of the rule `POST /api/sync/ibkr` answers 429 on (GitHub issue #55).
        Not the request-step `1001` half: after an 18:00 refusal the 00:00 slot keeps its
        one fresh SendRequest, the recovery this slot exists for.

        `force` exists for the one case the history shows really does allow a second
        generation in a day: editing the query definition in the IBKR portal (07-31).
        Nothing scheduled passes it.

        Returns:
            Summary of synced data
        """
        logger.info("Starting scheduled IBKR data sync...")

        async with AsyncSessionLocal() as db:
            if not force:
                spent = await generation_already_spent(db)
                if spent is not None:
                    message = (
                        f"IBKR already generated today's statement (generation spent "
                        f"{utc_iso(spent)}). It issues one per US-Eastern day; the next is "
                        f"available from {utc_iso(next_generation_opens_at())}."
                    )
                    logger.info(f"IBKR sync skipped: {message}")
                    return {
                        "status": "skipped",
                        "reason": "already_generated_today",
                        "message": message,
                        "last_success_at": utc_iso(spent),
                        "next_attempt_after": utc_iso(next_generation_opens_at()),
                        "timestamp": utc_iso(utcnow()),
                    }

                hold = await flex_refusal_in_force(db, request_refusals=False)
                if hold is not None:
                    message = describe_refusal_hold(hold)
                    logger.info(f"IBKR sync skipped: {message}")
                    return {
                        "status": "skipped",
                        "reason": "flex_refusal_hold",
                        "message": message,
                        "next_attempt_after": utc_iso(hold.until),
                        "timestamp": utc_iso(utcnow()),
                    }

                # ET Sunday and ET Monday statements reach the same Friday Saturday's
                # did, and were ~90% of all failed generations — see
                # `generation_brings_new_trading_day`. No request at all.
                now = utcnow()
                if not generation_brings_new_trading_day(now):
                    next_at = next_new_trading_day_generation_opens_at(now)
                    message = (
                        f"No new trading day to fetch: a statement generated on an ET "
                        f"{et_date(now):%A} reaches the same Friday Saturday's did. The next "
                        f"scheduled generation is from {utc_iso(next_at)}."
                    )
                    logger.info(f"IBKR sync skipped: {message}")
                    return {
                        "status": "skipped",
                        "reason": "no_new_trading_day",
                        "message": message,
                        "next_attempt_after": utc_iso(next_at),
                        "timestamp": utc_iso(now),
                    }

            try:
                # Initialize services and repositories
                # Step 1: Fetch data from IBKR. Nobody is waiting on a scheduled run,
                # so wait longer between attempts rather than risking IBKR's rate cap.
                logger.info("Fetching data from IBKR Flex Query...")
                flex_data = await IBKRService().fetch_flex_data(
                    retry_delays=FLEX_RETRY_DELAYS_PATIENT
                )

                # Steps 2-6, shared with POST /api/sync/ibkr and the offline XML ingest so
                # every path reconciles in exactly the same order.
                ingested = await ingest_flex_statement(db, flex_data)

                # Commit transaction
                await db.commit()

                # Same warnings as the manual endpoint: unsupported currencies plus any
                # Flex XML schema drift the sanitizer worked around, so a scheduled sync
                # surfaces it in /api/scheduler/status instead of only in the logs.
                warnings = ingested.pop("warnings", [])
                result = {
                    "status": "success",
                    "message": "Successfully synced data from IBKR",
                    **ingested,
                    "timestamp": utc_iso(utcnow()),
                }
                if warnings:
                    result["warnings"] = warnings

                logger.info(
                    f"IBKR sync completed: {ingested['securities_synced']} securities, "
                    f"{ingested['taxlots_synced']} taxlots"
                )
                return result

            except Exception as e:
                await db.rollback()
                logger.error(f"Failed to sync IBKR data: {str(e)}", exc_info=True)
                return {
                    "status": "error",
                    "message": f"Failed to sync IBKR data: {str(e)}",
                    "timestamp": utc_iso(utcnow())
                }

    async def collect_market_data_diagnostics(
        self, db, stale_baskets: bool = True,
    ) -> list[str]:
        """
        Every "nobody would otherwise notice" check that belongs after a market-data
        pass, in one place: `sync_market_data` runs it, and so does every path that
        prices — the scheduled jobs and `POST /api/market-data/sync` alike.

        The scheduled job ran all five; `POST /api/market-data/sync` ran none. The loop
        itself was extracted to `MarketDataService.sync_securities` after the 2026-08-04
        divergence, and the detectors hanging off it were not — so the *public* route
        answered `{"status": "success", "securities_processed": 40}` and said nothing,
        at the exact moment someone reaches for it: seeing a holding at 0.00 and
        clicking Sync Market Data is precisely when `find_stale_priced_securities` would
        have named the security and its `ticker_mappings` row. Since GitHub issue #65
        the route runs `sync_market_data` itself rather than a copy of it.

        `stale_baskets=False` leaves out the basket check, for the one caller that
        refreshes the baskets *after* its price pass: the 18:00 full sync asks
        `check_stale_etf_baskets` once its look-through upkeep is done (issue #79).

        They belong to the **market-data** pass specifically, and that is not an
        accident of where they were written: those slots succeed while Flex is refusing,
        so hanging the IBKR-freshness ones off an IBKR job would silence them in exactly
        the outage they exist to report.

        Each is guarded separately. These are diagnostics — one of them raising must
        never be the reason a sync that actually fetched prices reports failure, and it
        must never hide the other four.
        """
        diagnostics = []
        try:
            diagnostics += await self.find_stale_priced_securities(db)
        except Exception as e:
            logger.warning(f"Could not check for stale prices: {e}")
        # Same shape, same reasoning, for the dividend side: an estimate row
        # older than its mapping came from a different ticker and silently
        # keeps driving the forecast. Separately guarded so one diagnostic
        # failing cannot hide the other.
        try:
            diagnostics += await self.find_dividends_predating_their_mapping(db)
        except Exception as e:
            logger.warning(f"Could not check dividend provenance: {e}")
        # IBKR freshness is checked from the *market-data* job on purpose: those
        # slots succeed even while Flex is refusing, so the warning still reaches
        # `warnings[]`. Hanging it off the IBKR job instead would silence it in
        # exactly the outage it exists to report.
        #
        # Two detectors, two horizons, and they are not redundant. The 7-day one
        # is the backstop for a broken token; the other is what protects the
        # data, at N-1 ET days for the window `flex_window_days` measures. Which
        # fires first depends on N and both orders are right: at N=3 the gap one
        # leads (a statement loses trades four days before the 7-day one speaks),
        # at N=30 this one leads (an operational fault deserves attention weeks
        # before the trades are actually at risk).
        try:
            diagnostics += await self.find_flex_generation_gap(db)
        except Exception as e:
            logger.warning(f"Could not check the Flex generation gap: {e}")
        try:
            diagnostics += await self.find_stale_ibkr_sync(db)
        except Exception as e:
            logger.warning(f"Could not check IBKR sync freshness: {e}")
        # The look-through's baskets are the one decaying dataset with no alarm:
        # a CLI snapshot that ages in place while the tab keeps serving it. Same
        # slot and same separate guard as the four above.
        if stale_baskets:
            try:
                diagnostics += await self.find_stale_etf_baskets(db)
            except Exception as e:
                logger.warning(f"Could not check ETF basket freshness: {e}")
        return diagnostics

    async def sync_market_data(
        self, days_back: int = 730, refresh_provisional: Optional[bool] = None,
        stale_baskets: bool = True,
    ) -> dict:
        """
        Sync market prices from Yahoo Finance for all securities.
        Only fetches missing dates (incremental sync).

        The price step of the scheduled jobs **and** the whole body of
        `POST /api/market-data/sync`, which adds only the shared gate and the public
        cooldown (GitHub issue #65). The route used to carry its own copy of this body —
        status rule, result dict, rate-limit warning, diagnostics — and it had drifted:
        it never passed `refresh_provisional`, so a weekend manual sync re-downloaded
        closes that could not change.

        Args:
            days_back: Number of days to look back (default 730 = 2 years)
            refresh_provisional: False asks only for dates with no row at all. None (the
                default) is the calendar's answer, `is_berlin_trading_day()`: on a Berlin
                non-trading day no cached close can still change (issue #9). Only the
                Saturday 08:00 market-data slot overrides it, with True — it is the one
                that settles Friday's US close.
            stale_baskets: False leaves the basket check out of the diagnostics, for the
                full sync, which takes it after its look-through refresh (issue #79)

        Returns:
            Summary of synced data
        """
        logger.info("Starting market data sync...")
        if refresh_provisional is None:
            refresh_provisional = is_berlin_trading_day()

        async with AsyncSessionLocal() as db:
            try:
                market_data_service = MarketDataService(db)
                security_repo = SecurityRepository(db)

                # Get all securities
                securities = await security_repo.get_all(limit=1000)

                if not securities:
                    logger.info("No securities found to sync")
                    return {
                        "status": "success",
                        "message": "No securities found to sync",
                        "securities_processed": 0,
                        "prices_fetched": 0,
                        "timestamp": utc_iso(utcnow())
                    }

                # The loop itself lives on the service — it fetches the dates we are
                # missing plus the trailing few whose cached close may still be a
                # mid-session price, and stops early if Yahoo rate-limits.
                swept = await market_data_service.sync_securities(
                    securities, days_back=days_back,
                    refresh_provisional=refresh_provisional,
                )
                total_prices = swept["total_prices"]
                processed = swept["processed"]
                errors = swept["errors"]
                rate_limited_after = swept["rate_limited_after"]

                # Commit all price data
                await db.commit()

                result = {
                    "status": "success" if not (errors or rate_limited_after) else "partial_success",
                    "message": f"Synced market data for {processed} of {swept['planned']} securities",
                    "securities_processed": processed,
                    "prices_fetched": total_prices,
                    # Not asked this pass by design (issue #10): sold outside the short
                    # passes' grace, or backing off after every provider came back empty.
                    "securities_skipped_sold": swept["skipped_sold"],
                    "securities_backing_off": swept["skipped_backoff"],
                    "timestamp": utc_iso(utcnow())
                }

                if errors:
                    result["errors"] = errors
                    result["errors_count"] = len(errors)

                if rate_limited_after:
                    result["rate_limited"] = True
                    result["warnings"] = [
                        f"Yahoo rate-limited this run at {rate_limited_after}; "
                        f"{swept['planned'] - processed} securities were skipped and "
                        f"keep their previous prices. Do not trigger a manual sync — "
                        f"the next scheduled slot resumes"
                    ]

                # A fetch that returned nothing is not an error anywhere: the position
                # simply gets no price, and _calculate_daily_value / the positions
                # endpoint value it at 0.00 and move on. SBI@TSE lost 446.93 CHF off the
                # portfolio total that way with nothing, anywhere, saying so.
                #
                # Guarded on its own: this is a diagnostic, and it must never be the
                # reason a sync that actually fetched prices reports failure.
                diagnostics = await self.collect_market_data_diagnostics(
                    db, stale_baskets=stale_baskets,
                )
                if diagnostics:
                    # Append: the rate-limit abort above may already have put a
                    # warning here, and a plain assignment would drop the one
                    # warning that explains why the rest of the run is missing.
                    result["warnings"] = (result.get("warnings") or []) + diagnostics

                logger.info(f"Market data sync completed: {total_prices} prices fetched")
                return result

            except Exception as e:
                await db.rollback()
                logger.error(f"Failed to sync market data: {str(e)}", exc_info=True)
                return {
                    "status": "error",
                    "message": f"Failed to sync market data: {str(e)}",
                    "timestamp": utc_iso(utcnow())
                }

    async def find_stale_exchange_rates(
        self, db: AsyncSession, as_of: Optional[date] = None
    ) -> list:
        """
        Held currencies, and the selected base currency's `EUR -> base` pair, whose
        newest published rate is FX_STALE_PUBLICATION_DAYS ECB publication days behind.

        Database only, so it judges the outcome of every slot's warm-up rather than one
        run's failure. A currency warmed only on spec (`WARM_CURRENCIES`, the market-cap
        quote currencies) never warns: no position depends on it. Strings for
        `warnings[]`, like the other `find_*` checks.
        """
        held_currencies = (await db.execute(
            select(distinct(Security.currency)).where(held(), Security.currency.isnot(None))
        )).scalars().all()
        pairs = [(c, "EUR") for c in sorted(held_currencies) if c != "EUR"]
        base = await AppSettingsRepository(db).get_stored_base_currency()
        if base != "EUR":
            pairs.append(("EUR", base))

        as_of = as_of or utc_today()
        stale = await CurrencyService(db).stale_pairs(
            pairs, FX_STALE_PUBLICATION_DAYS, as_of=as_of,
        )
        return [
            f"{f}->{t}: no published exchange rate on record — both FX providers have "
            f"failed for it" if newest is None else
            f"{f}->{t}: no published exchange rate since {newest} "
            f"({(as_of - newest).days} days) — positions run on a carried rate"
            for (f, t), newest in stale
        ]

    async def find_stale_priced_securities(
        self, db: AsyncSession, as_of: Optional[date] = None
    ) -> list:
        """
        Held securities whose newest cached price is missing or too old.

        Restricted to securities with open tax lots, because those are the only ones
        whose price actually moves a number the user sees — a fully-sold security going
        quiet is expected, not a fault.

        Returns human-readable strings, ready for `warnings[]`, so the dashboard and
        `/api/scheduler/history` surface them without needing to know the shape.
        """
        as_of = as_of or utc_today()
        cutoff = as_of - timedelta(days=STALE_PRICE_DAYS)
        manual_cutoff = as_of - timedelta(days=MANUAL_PRICE_STALE_DAYS)

        # Deliberately two queries rather than one join. Joining taxlots to
        # market_prices multiplies them — a security with 100 open lots and 730 cached
        # closes is 73,000 rows to aggregate, and the portfolio holds 972 lots against
        # 21,000 prices. Two indexed scans and a dict lookup cost nothing by comparison.
        held_rows = (await db.execute(
            select(Security.id, Security.symbol, Security.exchange,
                   Security.price_source)
            .where(held())
        )).all()
        if not held_rows:
            return []

        newest = await db.execute(
            select(MarketPrice.security_id, func.max(MarketPrice.date))
            .where(MarketPrice.security_id.in_([row[0] for row in held_rows]))
            .group_by(MarketPrice.security_id)
        )
        newest_by_security = dict(newest.all())

        # For a statement-priced security the newest row of *any* kind is useless
        # here: the importer materialises a carry forward of the last observed NAV,
        # so `max(date)` sits ~45 days in the FUTURE and this alarm could never fire.
        # The carry is exactly what is being bridged, so ask when the provider last
        # actually published something.
        newest_observed = dict((await db.execute(
            select(MarketPrice.security_id, func.max(MarketPrice.date))
            .where(
                MarketPrice.security_id.in_([row[0] for row in held_rows]),
                MarketPrice.source.notin_(CARRIED_PRICE_SOURCES),
            )
            .group_by(MarketPrice.security_id)
        )).all())

        warnings = []
        for security_id, symbol, exchange, price_source in held_rows:
            name = f"{symbol}@{exchange}" if exchange else str(symbol)
            manual = price_source == PRICE_SOURCE_MANUAL
            latest = (newest_observed if manual else newest_by_security).get(security_id)
            if latest is None:
                warnings.append(
                    f"{name}: no published NAV on record at all — the position is "
                    f"being valued at 0.00. Import a provider statement for it"
                    if manual else
                    f"{name}: no cached price at all — the position is being "
                    f"valued at 0.00. Check the ticker_mappings row and the Yahoo symbol"
                )
            elif latest < (manual_cutoff if manual else cutoff):
                age = (as_of - latest).days
                warnings.append(
                    # Actionable for the source that actually feeds it. "Check the
                    # ticker mapping" would send the reader hunting a Yahoo symbol
                    # this security deliberately does not have.
                    f"{name}: the newest NAV its provider published is {latest} "
                    f"({age} days old), and the carried price runs out before day "
                    f"{CARRY_HORIZON_DAYS + PRICE_LOOKBACK_DAYS}. Upload a newer "
                    f"statement export"
                    if manual else
                    f"{name}: newest price is {latest} "
                    f"({age} days old) — the price feed looks broken"
                )

        if warnings:
            logger.warning(f"{len(warnings)} security(ies) with missing or stale prices")
        return warnings

    async def find_flex_generation_gap(
        self, db: AsyncSession, as_of: Optional[datetime] = None
    ) -> list:
        """
        Warn once a gap in successful IBKR syncs threatens the Flex window's margin.

        **This is the alarm that protects the data; `find_stale_ibkr_sync` is only the
        backstop.** A statement covering N calendar days is generated on day D for
        D-N..D-1, so a trade on day T is reachable from T+1..T+N and unreachable from
        T+N+1 onward. One generation is available per ET day, so N consecutive missed
        days is the entire margin and the alarm fires at N-1.

        **N is measured, not declared.** It comes from `flex_window_days`, which reads
        the span off the last statement IBKR actually served. The period is a portal
        setting, so a constant in this file tracks it only for as long as nobody edits
        the portal — and on 2026-08-24 nobody had edited this file: the live query was
        `Last 30 Calendar Days` while the threshold was still the 2 that N=3 implies, so
        this fired on the first ordinary two-day gap and told the reader trades were
        "about to become unreachable" with four weeks of slack in hand. A false alarm
        that names a data-loss risk is worse than silence, because the next true one
        reads the same.

        Bounded below by `FLEX_GENERATION_GAP_WARN_DAYS_FLOOR` so a narrow window cannot
        put it on a hair trigger over a weekend, and the message states the window it
        measured so a wrong reading is visible rather than inferred.

        Counted in **ET days**, not in elapsed hours, because that is the unit IBKR
        issues statements in: a failure at 23:00 ET and one at 01:00 ET the next day are
        two missed generations two hours apart, and an hours-based age would call that
        one.

        **Counted from the oldest trading day the last statement does not cover**, not
        from the day it was generated (`oldest_uncovered_trading_day`). A Saturday,
        Sunday or Monday statement reaches Friday, so nothing is at risk until Monday's
        trades — which is what keeps the scheduler's ET Sunday/Monday skip from raising
        this by itself (a Saturday success read on Tuesday morning is one day old, not
        three). Calendar days from there, never "generation days": the window is N
        *calendar* days, so counting only Tue–Sat would warn after the trades were gone.
        A real outage spanning a weekend still counts every day of it — a Friday success
        with a failed Saturday is two days old by Sunday, and under a narrow window the
        skipped Sunday and Monday make that warning the one that matters.

        Read through `newest_coverage`, which keys on the newest `data_to` a run covered
        rather than on when it finished, so an old-range download ingested today does not
        silence it.

        Uses `IBKR_SYNC_TYPES`, so an offline `ibkr_manual_xml` ingest **closes** the
        gap when it covers it. That is the opposite choice from the guard in `flex_generation.py`, which
        excludes it — deliberately, and the two questions are genuinely different. A
        browser download refreshes the data (so the alarm should quieten) while costing
        no generation (so the guard must not skip the day's attempt). Getting this
        backwards in either direction is a real bug: the alarm would blare through a
        correct recovery, or the recovery would suppress the next real sync.

        Silent on a fresh install with no successful sync ever — that is
        `find_stale_ibkr_sync`'s case, and duplicating it here would put two warnings on
        one condition.
        """
        as_of = as_of or utcnow()

        coverage = await newest_coverage(db)
        if coverage is None:
            return []

        newest, at_risk_from = coverage
        missed = (et_date(as_of) - at_risk_from).days

        window = await flex_window_days(db)
        measured = window is not None
        window = window if measured else FLEX_WINDOW_DAYS_FALLBACK
        threshold = max(FLEX_GENERATION_GAP_WARN_DAYS_FLOOR, window - 1)
        if missed < threshold:
            return []

        # `window - missed` is how many more ET days the oldest at-risk trade survives.
        # Zero or less means it is already gone from every future statement, which is a
        # different instruction to the reader than "act now", so say which.
        remaining = window - missed
        provenance = (
            f"reaches back {window} calendar days"
            if measured
            else f"window length is unknown, assuming {window} calendar days"
        )
        urgency = (
            f"about {remaining} day(s) of margin left"
            if remaining > 0
            else "that margin is already gone"
        )
        # The count runs from `at_risk_from`, not from the last success, so the sentence
        # names that day: "no statement for 2 days" beside a success four days back (a
        # Saturday success, the skipped Sunday and Monday, a failed Tuesday) reads as a bug.
        return [
            f"IBKR: the oldest trading day not yet fetched is {at_risk_from:%Y-%m-%d}, "
            f"{missed} US-Eastern days ago (last success {et_date(newest):%Y-%m-%d} ET). "
            f"The Flex Query {provenance}, so "
            f"{urgency} before those trades become unreachable from every future "
            f"statement — download the statement from Client Portal with a wider period "
            f"and ingest it via app/cli/ingest_flex_xml.py"
        ]

    async def find_stale_ibkr_sync(
        self, db: AsyncSession, as_of: Optional[datetime] = None
    ) -> list:
        """
        Warn when no IBKR sync has *succeeded* for `IBKR_SYNC_STALE_DAYS`.

        The gap this closes: a run that fails is recorded and visible in
        `/api/scheduler/history`, but nobody reads that daily, and a portfolio quietly
        serving last week's positions looks exactly like one serving today's. The
        failures are individually unremarkable — `1001` is routine — so the thing worth
        alarming on is not any single failure but the *absence of a success*.

        **This is a precondition for shortening the Flex Query period.** A Year-to-Date
        query re-delivers everything, so a gap costs freshness and nothing else. A bounded
        window does not: trades that fall out of it before a sync succeeds are gone from
        every future statement, and the only recovery is a manual period change and an
        offline XML ingest. So the alarm has to exist before the window shrinks, not after
        someone notices a hole in the ledger.

        Counts only sync types that actually reach IBKR (`IBKR_SYNC_TYPES`), including the
        offline `ibkr_manual_xml` path — ingesting a browser download genuinely refreshes
        the data, so it should reset the clock exactly like an API sync. Read through
        `newest_coverage`, like the gap alarm: the clock is the run that brought the
        newest data, so a prior-range download ingested today does not reset it.

        Returns `warnings[]`-ready strings, like the sibling detectors.
        """
        as_of = as_of or utcnow()

        coverage = await newest_coverage(db)

        if coverage is None:
            # No successful IBKR sync on record at all. Silent on a fresh install, where
            # an empty history is expected rather than alarming — the first sync fixes it.
            has_any = await db.execute(
                select(func.count(SyncRun.id)).where(SyncRun.sync_type.in_(IBKR_SYNC_TYPES))
            )
            if (has_any.scalar_one_or_none() or 0) == 0:
                return []
            return [
                "IBKR: no sync has ever succeeded, only failures. The portfolio is not "
                "being updated — check the Flex token and the query configuration"
            ]

        newest = coverage[0]
        age = (as_of - newest).days
        if age < IBKR_SYNC_STALE_DAYS:
            return []

        return [
            f"IBKR: the newest data is from the sync of {newest:%Y-%m-%d} ({age} days ago). "
            f"Positions and trades are stale. If the Flex Query period is bounded, "
            f"trades older than that window will be lost permanently — ingest a browser "
            f"download via app/cli/ingest_flex_xml.py rather than waiting"
        ]

    async def find_dividends_predating_their_mapping(self, db: AsyncSession) -> list:
        """
        Held securities whose dividend estimates were fetched under an older ticker.

        The price side of this class has been watched since
        `find_stale_priced_securities` — a missing price now warns. Nothing watched
        the dividend side, which is why SBI's two rows survived the mapping's
        correction to SBI.TO, the price purge, and a month of syncs: estimates are
        keyed to whatever Yahoo ticker resolved when they were written, and
        `_forecast_inputs()` then let them alone define a gold miner's payout
        schedule while the real IBKR payment was skipped.

        The signal is specific rather than a staleness guess: a row computed before
        its mapping's `updated_at` came from a *different* ticker. A genuine
        non-payer has no estimate rows at all, so it cannot trip this; a payer that
        simply hasn't declared lately has rows newer than the mapping.

        Returns `warnings[]`-ready strings, like its price-side counterpart.
        """
        held_rows = (await db.execute(
            select(Security.id, Security.symbol, Security.exchange).where(held())
        )).all()
        if not held_rows:
            return []

        # Same two-queries-not-a-join reasoning as above: joining lots to dividend
        # rows multiplies them for no benefit.
        newest = await db.execute(
            select(DividendPayment.security_id, func.max(DividendPayment.last_computed))
            .where(
                and_(
                    DividendPayment.security_id.in_([row[0] for row in held_rows]),
                    DividendPayment.source == YFINANCE_SOURCE,
                )
            )
            .group_by(DividendPayment.security_id)
        )
        newest_by_security = dict(newest.all())

        mappings = await db.execute(
            select(TickerMapping.ibkr_symbol, TickerMapping.ibkr_exchange,
                   TickerMapping.yahoo_ticker, TickerMapping.updated_at)
            .where(TickerMapping.is_active == True)  # noqa: E712
        )
        mapping_by_key = {
            (sym, exch): (ticker, updated) for sym, exch, ticker, updated in mappings.all()
        }

        warnings = []
        for security_id, symbol, exchange in held_rows:
            newest_row = newest_by_security.get(security_id)
            if newest_row is None:
                continue  # no estimates: a non-payer, or IBKR-only. Nothing to suspect.
            mapping = mapping_by_key.get((symbol, exchange))
            if not mapping:
                continue  # resolved by suffix or bare symbol; no mapping row to compare
            ticker, updated_at = mapping
            if updated_at is None or newest_row >= updated_at:
                continue

            name = f"{symbol}@{exchange}" if exchange else str(symbol)
            warnings.append(
                f"{name}: dividend estimates were last computed "
                f"{newest_row:%Y-%m-%d} but the mapping to {ticker} changed "
                f"{updated_at:%Y-%m-%d} — those rows came from a different ticker and "
                f"still drive the forecast. Clear them with "
                f"`python -m app.cli.purge_dividend_estimates {symbol} {exchange}`"
            )

        if warnings:
            logger.warning(
                f"{len(warnings)} security(ies) hold dividend estimates older than "
                f"their ticker mapping"
            )
        return warnings

    async def find_stale_etf_baskets(
        self, db: AsyncSession, as_of: Optional[date] = None
    ) -> list:
        """
        Held funds whose constituent basket is missing or older than its issuer's cadence.

        Every other decaying dataset here has a detector — prices, IBKR freshness, the
        Flex generation gap, dividend provenance — and the look-through's baskets had
        none. Runs from the **market-data** job, like its siblings and for the same
        reason: those slots succeed while Flex is refusing.

        **The verdict comes from `etf_basket_refresh.stale_basket_verdicts`, the same
        list the 18:00 refresh fetches from.** This method only formats its `stale` half;
        the refresh fetches its `due` half. Two definitions of "stale" — one that warns
        and one that acts — would be this codebase's dominant failure mode, and the
        version that lived here had already been wrong once about which basket a proxied
        fund reads. The four rules (held funds only, the proxy source's file and cadence,
        the per-adapter threshold, nothing unclearable) are documented on the predicate.

        The refresh asks a daily issuer every day, long before the warn threshold, so a
        warning here means one of three things and says which: a newly held fund the
        next evening will fetch; a fund whose refresh has kept failing or whose issuer
        has published nothing newer for the whole threshold, in which case the 18:00
        run's own warning names any error; or a fund with no automated route.
        """
        from app.services.etf_basket_refresh import stale_basket_verdicts

        warnings = []
        for v in await stale_basket_verdicts(db, as_of):
            if not v.stale:
                continue  # due a fetch, not yet worth telling anyone about
            if v.missing:
                warnings.append(
                    f"{v.name}: no constituent basket has ever been fetched, so its whole "
                    f"value is missing from the look-through's company rows. The 18:00 "
                    f"full sync fetches it automatically; if this persists past a day, run "
                    f"`python -m app.cli.fetch_etf_baskets --all` and the import line it "
                    f"prints, then `python -m app.cli.resolve_identities --constituents`"
                )
            elif v.fetchable:
                warnings.append(
                    f"{v.name}: its basket is dated {v.as_of_date:%Y-%m-%d} ({v.age_days} "
                    f"days old against a {v.limit_days}-day expectation for "
                    f"{v.basket_adapter}), so every company figure it contributes describes "
                    f"an older index. The 18:00 full sync asks its issuer for it on a "
                    f"schedule, so either that fetch keeps failing (its warning names the "
                    f"error) or the issuer has published nothing newer. To look, run "
                    f"`python -m app.cli.fetch_etf_baskets --all` and the import lines it "
                    f"prints, then `python -m app.cli.resolve_identities --constituents` "
                    f"for any new CINS/SEDOL rows"
                )
            else:
                warnings.append(
                    f"{v.name}: its basket is dated {v.as_of_date:%Y-%m-%d} ({v.age_days} "
                    f"days old against a {v.limit_days}-day expectation for "
                    f"{v.basket_adapter}), so every company figure it contributes describes "
                    f"an older index. It has no automated route, so download a newer file "
                    f"by hand and import it with `python -m app.cli.import_etf_basket`"
                )

        if warnings:
            logger.warning(f"{len(warnings)} held fund(s) with a missing or stale basket")
        return warnings

    async def refresh_lookthrough_data(self) -> dict:
        """
        Step 5 of the 18:00 full sync: keep the look-through's baskets and identities current.

        Issuer sites, OpenFIGI and GLEIF only — no Yahoo, no IBKR — so neither rule at
        the top of CLAUDE.md applies, and it runs whether or not the Flex half succeeded,
        for the same reason the market-data half does. Once a day rather than at every
        market-data slot: the issuers publish once a day, and seven of them is not a
        thing to poll. The service refuses everything the CLIs refuse and keeps the
        previous basket on any failure; its failures are this step's warnings, never the
        job's status. See `app/services/etf_basket_refresh.py`.
        """
        from app.services.etf_basket_refresh import refresh_lookthrough_data

        try:
            async with AsyncSessionLocal() as db:
                return await refresh_lookthrough_data(db)
        except Exception as e:
            logger.error(f"Look-through upkeep failed: {e}")
            return {
                "status": "error",
                "message": str(e),
                "warnings": [
                    f"Look-through upkeep failed ({type(e).__name__}: {e}); baskets and "
                    f"identities are as they were"
                ],
            }

    async def check_stale_etf_baskets(self) -> dict:
        """
        `find_stale_etf_baskets` as a step of its own, which the 18:00 full sync takes
        **after** `refresh_lookthrough_data` (GitHub issue #79).

        The full sync's price pass runs first, and its diagnostics used to carry this
        check — so the run record warned about baskets the same run refreshed minutes
        later (production, 2026-10-18 18:00: every fund the warnings named was listed in
        that record's `lookthrough_result.baskets.refreshed`, and the tab showed none of
        them stale). Asked after the refresh, a warning means what it says: the refresh
        failed, the issuer served nothing newer, or there is no automated route. The
        market-data slots keep the check inside their diagnostics; no refresh follows them.

        Guarded like its siblings: a diagnostic never decides a job's status.
        """
        try:
            async with AsyncSessionLocal() as db:
                warnings = await self.find_stale_etf_baskets(db)
        except Exception as e:
            logger.warning(f"Could not check ETF basket freshness: {e}")
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
        result = {"status": "success"}
        if warnings:
            result["warnings"] = warnings
        return result

    async def sync_exchange_rates(self, days_back: int = 30) -> dict:
        """
        Sync exchange rates from Frankfurter API for all non-EUR currencies
        used by securities in the portfolio.

        Args:
            days_back: Number of days to fetch rates for

        Returns:
            Summary of synced rates
        """
        logger.info("Starting exchange rate sync...")

        async with AsyncSessionLocal() as db:
            try:
                security_repo = SecurityRepository(db)
                currency_service = CurrencyService(db)

                # Currencies actually held, plus the ones we keep warm on spec.
                securities = await security_repo.get_all(limit=1000)
                held = {
                    sec.currency for sec in securities
                    if sec.currency and sec.currency != "EUR"
                }

                # WARM_CURRENCIES are the ones the fallback provider has to serve, and it
                # is latest-only: their history exists only because this job records a
                # rate every day. Warming them costs one extra request for the whole set,
                # and it is what lets a position in a new currency be valued from the day
                # its statement lands rather than from the day we happen to notice.
                today = utc_today()

                # And the currencies a market cap is quoted in (`market_cap.py`): every
                # read converts them from the cache only, so they must be in it.
                from app.services.market_cap import quoted_currencies
                quoted = await quoted_currencies(db)
                currencies = held | currency_service.WARM_CURRENCIES | quoted

                logger.info(f"Syncing exchange rates for currencies: {sorted(currencies)}")

                # The EUR->base rates for the selectable base currencies (CHF/USD) ride in
                # the same request, so switching the display currency is instant. Every
                # ECB pair costs one request between them, and none once current.
                warmed = await currency_service.warm_rates(
                    currencies, target_date=today, days_back=days_back,
                    extra_pairs=[("EUR", base) for base in ("CHF", "USD")],
                )
                total_rates = warmed["frankfurter"] + warmed["fallback"]

                await db.commit()

                result = {
                    "status": "success",
                    "currencies_synced": total_rates,
                    "currencies": sorted(currencies),
                    "held_currencies": sorted(held),
                    "quoted_currencies": sorted(quoted),
                    "timestamp": utc_iso(utcnow())
                }
                # Recorded, not warned: one run's failure is healed by the next slot's
                # re-ask. The warning is the staleness those failures add up to.
                for key in ("frankfurter_failed", "fallback_available"):
                    if warmed.get(key) is not None:
                        result[key] = warmed[key]
                try:
                    stale = await self.find_stale_exchange_rates(db)
                except Exception as e:
                    logger.warning(f"Could not check exchange-rate staleness: {e}")
                    stale = []
                if stale:
                    result["warnings"] = stale
                logger.info(f"Exchange rate sync completed: {total_rates} currencies updated")
                return result

            except Exception as e:
                await db.rollback()
                logger.error(f"Failed to sync exchange rates: {str(e)}", exc_info=True)
                return {
                    "status": "error",
                    "message": f"Failed to sync exchange rates: {str(e)}",
                    "timestamp": utc_iso(utcnow())
                }

    async def sync_benchmark_prices(self) -> dict:
        """
        Fetch the last finished session for every benchmark already in the DB.

        Only benchmarks the user has previously selected (i.e., have cached prices); the
        chart's own lazy fetch bootstraps a new one. Runs inside `_market_data_only_locked`,
        so it already holds SYNC_PIPELINE and passes `gate_held=True` — re-entering the
        gate refused every ticker, and until 2026-10-17 this pass never reached Yahoo.

        The range ends at `last_finished_session`, never today: today's bar is missing
        until the close and would be asked for at every slot. Each benchmark is asked at
        most once per finished session (`_benchmark_sessions_asked`), because a holiday at
        the range end, or inside it and younger than HOLIDAY_GRACE_DAYS, stays missing
        after Yahoo answers. So: one request per warm benchmark at the first slot after a
        session, none at the others. The memo is in-process; a restart costs at most one more.
        """
        logger.info("Starting benchmark price sync...")

        async with AsyncSessionLocal() as db:
            try:
                # Find which benchmark tickers already have data in the DB
                result = await db.execute(
                    select(distinct(BenchmarkPrice.ticker))
                )
                existing_tickers = sorted(row[0] for row in result.all())

                if not existing_tickers:
                    logger.info("No benchmarks in DB to refresh")
                    return {
                        "status": "success", "benchmarks_fetched": 0,
                        "benchmark_prices_written": 0,
                        "benchmarks_total": 0, "rate_limited": False,
                    }

                # Map tickers back to benchmark info for currency
                ticker_to_benchmark = {
                    info["ticker"]: info for info in BENCHMARKS.values()
                }

                bench_service = BenchmarkService(db)
                end = last_finished_session(utc_today())
                start = end - timedelta(days=7)
                fetched = 0
                written = 0
                rate_limited = False
                warnings = []

                for ticker in existing_tickers:
                    if self._benchmark_sessions_asked.get(ticker) == end:
                        continue
                    bench_info = ticker_to_benchmark.get(ticker)
                    currency = bench_info["currency"] if bench_info else "USD"
                    try:
                        # No provisional refresh: the range ends at a finished session,
                        # so every row this pass writes is already a close. See
                        # _ensure_prices_available.
                        count = await bench_service._ensure_prices_available(
                            ticker, start, end, currency=currency,
                            refresh_provisional=False, gate_held=True,
                        )
                    except Exception as e:
                        # The fetch swallows its own upstream errors, so what lands here
                        # is a bug (a DB error, a false gate_held) — say so on the result.
                        logger.error(f"Failed to sync benchmark {ticker}: {e}")
                        warnings.append(f"Benchmark {ticker} was not refreshed: {e}")
                        continue
                    # Rule 1: stop on a rate limit rather than working through the
                    # remaining benchmarks against an IP that has already refused us.
                    # The fetch latches it (it catches every exception), so the latch is
                    # what this loop reads. A benchmark this pass skipped keeps its
                    # cached prices, is not marked asked, and the next slot resumes it.
                    if bench_service.rate_limited:
                        rate_limited = True
                        logger.warning(
                            f"Yahoo rate limit on benchmark {ticker}; "
                            f"abandoning the rest of the warm-up"
                        )
                        break
                    # None: Yahoo was not asked (nothing missing, throttled) or did not
                    # answer. Neither is a fetch, and neither uses up this session.
                    if count is not None:
                        self._benchmark_sessions_asked[ticker] = end
                        fetched += 1
                        written += count
                        logger.info(f"Synced {count} benchmark prices for {ticker}")

                await db.commit()
                logger.info(
                    f"Benchmark price sync completed: {fetched} benchmarks fetched, "
                    f"{written} prices written"
                )
                result = {
                    "status": "success",
                    "benchmarks_fetched": fetched,
                    "benchmark_prices_written": written,
                    "benchmarks_total": len(existing_tickers),
                    "rate_limited": rate_limited,
                }
                if rate_limited:
                    # Without this a warm-up that stopped at 2 of 8 was byte-identical
                    # in the API to one that finished — and this loop is the fastest of
                    # the Yahoo loops at burning a limit, so it is the leading indicator
                    # for the market-data half that runs beside it.
                    warnings.append(
                        f"Yahoo Finance rate limit reached during the benchmark warm-up; "
                        f"{fetched} of {len(existing_tickers)} benchmarks refreshed, the rest "
                        f"keep their cached prices. Do not retry manually — the next "
                        f"scheduled run resumes where it stopped."
                    )
                if warnings:
                    result["warnings"] = warnings
                return result

            except Exception as e:
                await db.rollback()
                logger.error(f"Failed to sync benchmark prices: {e}", exc_info=True)
                return {"status": "error", "message": str(e)}

    async def sync_dividends(self) -> dict:
        """
        Sync dividend ex-dates from Yahoo Finance and compute EUR income.

        Independent of IBKR (yfinance-based); computes against existing tax lots.
        Cheap to run daily: the pass asks only about held, Yahoo-eligible securities
        whose `dividends_checked_at` is NULL, a month old, or older than a mapping change
        or a split (`needs_dividend_check`) — the price passes capture new ex-dates in
        between — so most days it asks nothing.

        Returns:
            Summary of the dividend sync + compute steps
        """
        logger.info("Starting scheduled dividend sync...")

        async with AsyncSessionLocal() as db:
            try:
                # Imported here to avoid any import cycle at module load
                from app.services.dividend_service import DividendService

                service = DividendService(db)
                sync_res = await service.sync_dividend_data()
                compute_res = await service.compute_dividend_income()

                logger.info(f"Dividend sync completed: {sync_res}; compute: {compute_res}")
                result = {
                    "status": "success",
                    "sync": sync_res,
                    "compute": compute_res,
                    "rate_limited": bool(sync_res.get("rate_limited")),
                    "timestamp": utc_iso(utcnow())
                }
                # Hoisted so `_collect_warnings` can see them: a rate limit that
                # abandoned the fetch, and `compute_dividend_income`'s FX-skipped
                # rows, both sat two levels down in `details` where nothing read them.
                warnings = (
                    list(sync_res.get("warnings") or [])
                    + list(compute_res.get("warnings") or [])
                )
                if warnings:
                    result["warnings"] = warnings
                return result

            except Exception as e:
                logger.error(f"Failed to sync dividends: {str(e)}", exc_info=True)
                return {
                    "status": "error",
                    "message": f"Failed to sync dividends: {str(e)}",
                    "timestamp": utc_iso(utcnow())
                }

    async def dividend_sync_locked(self) -> dict:
        """
        `sync_dividends` as a run of its own, with a `sync_runs` row: the body of
        `POST /api/dividends/sync` and of the pass `GET /api/dividends/summary` enqueues.
        The caller holds `SYNC_PIPELINE` with the public cooldown; the gate and the
        cooldown are the only things the route adds (GitHub issue #65).

        The route used to run sync-then-compute itself and drop what `sync_dividends`
        hoists — `rate_limited` and the warnings — with no row recorded, so a pass Yahoo
        cut short left no trace anywhere. Recorded as `dividend_sync`, which is not in
        SCHEDULED_JOB_TYPES and never sets `last_sync_result`: it is not the portfolio's
        "last sync".
        """
        started_at = utcnow()
        div_result = await self.sync_dividends()
        result = {
            "type": "dividend_sync",
            "timestamp": utc_iso(started_at),
            "status": div_result.get("status", "error"),
            "message": div_result.get("message")
            or (div_result.get("sync") or {}).get("message"),
            "rate_limited": bool(div_result.get("rate_limited")),
            "dividend_result": div_result,
        }
        _collect_warnings(result, div_result)
        await self._record_run(result, started_at)
        return result

    async def record_dividend_forecast(self) -> dict:
        """
        Record today's dividend forecast figures (docs/dividends.md, *The forecast's
        history*). Pure DB — no Yahoo, no IBKR.

        Called by every job that reprices, so one missed slot does not lose the day;
        the row is keyed on the UTC date and the last write wins. Best-effort like
        `_record_run`: a failure is this step's warning and never the job's status.
        """
        try:
            async with AsyncSessionLocal() as db:
                # Imported here to avoid any import cycle at module load
                from app.services.dividend_service import DividendService

                snapshot = await DividendService(db).record_forecast_snapshot(
                    utcnow().date()
                )
                await db.commit()
            return {"status": "success", "snapshot": snapshot}
        except Exception as e:
            logger.warning(f"Could not record the dividend forecast snapshot: {e}")
            return {
                "status": "error",
                "warnings": [
                    "The dividend forecast snapshot was not recorded for today: "
                    f"{type(e).__name__}"
                ],
            }

    async def _gated_job(self, job_type: str, run, scheduled: bool = False) -> Optional[dict]:
        """
        Run one pipeline job under the shared sync-pipeline gate.

        A collision (e.g. a manual trigger already in flight at a scheduled slot)
        records itself as 'skipped' instead of running concurrently — two
        pipelines would race the Flex budget toward a Code=1025 lockout and
        double-hit Yahoo. The next scheduled slot recovers the freshness.

        `scheduled=True` (the APScheduler entry points only) first polls the gate every
        SCHEDULED_GATE_POLL_SECONDS for up to SCHEDULED_GATE_WAIT_SECONDS, so a pass a
        public GET started moments before the slot delays the job instead of skipping
        it. The default fails fast, which `trigger_sync_now`'s 429 depends on. Polling
        `is_running` rather than retrying `single_flight` keeps a SyncBusy raised
        *inside* `run()` from re-running the job; nothing awaits between the last poll
        and the entry, so the hand-over is atomic.

        `reason` distinguishes this skip from the other one a job can now report.
        `sync_ibkr_data` also returns `status="skipped"` when IBKR has already
        generated today's statement, and the two mean opposite things: this one is
        "come back shortly", that one is "there is nothing more to fetch today".
        `trigger_sync_now` keys on the reason, because answering 429 *busy* to a day
        that simply had nothing left to sync is a misleading error.
        """
        waited = 0
        while scheduled and is_running(SYNC_PIPELINE) and waited < SCHEDULED_GATE_WAIT_SECONDS:
            await _gate_sleep(SCHEDULED_GATE_POLL_SECONDS)
            waited += SCHEDULED_GATE_POLL_SECONDS
        try:
            with single_flight(SYNC_PIPELINE):
                return await run()
        except SyncBusy as e:
            logger.warning(f"{job_type} skipped: {e}")
            skipped = {
                "type": job_type,
                "status": "skipped",
                "reason": "pipeline_busy",
                "message": str(e),
                "timestamp": utc_iso(utcnow()),
            }
            await self._record_run(skipped, utcnow())
            return skipped

    async def full_sync_job(self, scheduled: bool = False) -> Optional[dict]:
        """
        Full sync job that runs once daily at 18:00 Europe/Berlin.

        This is the slot that carries the day's one IBKR statement generation — see
        IBKR_ONLY_HOURS for why it sits at 18:00 rather than overnight. It also keeps
        the 730-day market-data pass, which moved here with it: pairing them means a
        security the statement creates is priced by the same job, and 08:00 keeps its
        Yahoo touch as a plain 7-day pass so the seven repricing hours are unchanged.

        Executes in sequence:
        1. IBKR data sync (securities and tax lots)
        2. Market data sync (prices for all securities) — runs whether or not step 1
           succeeded, because IBKR refusing a statement says nothing about Yahoo. The
           ordering still matters on the days step 1 works: a security created there
           must exist before this prices it. Missing dates only on a Berlin weekend.
        3. Dividend sync (Yahoo Finance ex-dates + EUR income)
        4. Look-through upkeep (issuer baskets, identities), then the stale-basket
           check — after the refresh, not among step 2's diagnostics (issue #79)
        """
        return await self._gated_job("full_sync", self._full_sync_job_locked, scheduled=scheduled)

    async def _full_sync_job_locked(self) -> dict:
        logger.info("=" * 80)
        logger.info("STARTING FULL SYNC JOB (IBKR + MARKET DATA)")
        logger.info("=" * 80)

        started_at = utcnow()

        # Step 1: Sync IBKR data
        ibkr_result = await self.sync_ibkr_data()
        logger.info(f"IBKR Sync Result: {ibkr_result}")

        # Step 2: Sync exchange rates (always, even if IBKR sync fails)
        logger.info("Syncing exchange rates...")
        fx_result = await self.sync_exchange_rates(days_back=30)
        logger.info(f"Exchange Rate Sync Result: {fx_result}")

        # Step 3: Sync market data (always — Yahoo does not care that Flex refused).
        #
        # This used to be gated on `ibkr_result["status"] == "success"`, and that gate
        # made the 730-day pass the rarest job in the schedule rather than a daily one.
        # IBKR generates this statement roughly **once per ET calendar day**: read off
        # sync_runs on 2026-08-07, every one of the six preceding days had exactly one
        # success and refused every later attempt with Code=1001. The 06:00 Berlin
        # ibkr_only job runs two hours before this one and usually wins that single
        # generation, so this job's IBKR half usually fails — and took the deep price
        # history down with it. Nothing surfaced that, because the six 7-day
        # market_data_only slots keep *current* value fresh; only the two-year backfill
        # lapsed, and it had not run since 2026-08-03.
        #
        # Decoupling costs one Yahoo request per security on the days it now runs where
        # it previously skipped. The span narrowing in fetch_and_cache_prices means a
        # 730-day window is not 730 days of download — it starts a few days before
        # min(missing), so a security already backfilled re-requests almost nothing.
        # A newly bought one pulls its whole history, which is the point.
        #
        # On a Berlin weekend, missing dates only (GitHub issue #9): no cached close can
        # still change, and re-fetching the provisional ones was a request per security
        # for nothing. A security new in Saturday's statement, or a split purge, still
        # gets its history. Keyed on the calendar — `sync_market_data`'s default, so
        # `POST /api/market-data/sync` gets the same answer — never on the IBKR result
        # above: gating this half on that was a silent outage (see above).
        #
        # Without the stale-basket check: step 5 refreshes the baskets, and a verdict
        # taken now would warn about baskets this run brings current (issue #79).
        logger.info("Syncing market data (730 days)...")
        market_result = await self.sync_market_data(days_back=730, stale_baskets=False)
        logger.info(f"Market Data Sync Result: {market_result}")

        # Step 4: Sync dividends (always — yfinance-based, computes against existing tax lots)
        #
        # Weekends included, on purpose: the pass asks only about securities due a full
        # read (`needs_dividend_check`), so skipping it would not save a request,
        # only move them to Monday — or to a weekend dashboard load, since
        # `GET /api/dividends/summary` enqueues a pass while anything is due.
        logger.info("Syncing dividends...")
        div_result = await self.sync_dividends()
        logger.info(f"Dividend Sync Result: {div_result}")

        # Before the look-through upkeep, which can sit on an issuer's timeout.
        forecast_result = await self.record_dividend_forecast()

        # Step 5: Look-through upkeep — stale issuer baskets, then pending identities.
        # Issuer sites and OpenFIGI/GLEIF only, no Yahoo and no IBKR, and not gated on
        # either. Last, so an issuer hanging on its 60 s timeout delays nothing the
        # portfolio's own figures depend on. Once a day, here, because the issuers
        # publish once a day and seven third-party sites are not a thing to poll at
        # every market-data slot.
        logger.info("Refreshing look-through baskets and identities...")
        lookthrough_result = await self.refresh_lookthrough_data()
        logger.info(f"Look-through Upkeep Result: {lookthrough_result}")

        # The stale-basket verdict the price pass left out, now that the refresh has run.
        basket_check = await self.check_stale_etf_baskets()

        # Track result
        self.last_sync_result = {
            "type": "full_sync",
            "timestamp": utc_iso(utcnow()),
            "ibkr_result": ibkr_result,
            "fx_result": fx_result,
            "market_result": market_result,
            "dividend_result": div_result,
            "lookthrough_result": lookthrough_result,
            "dividend_forecast_result": forecast_result,
            "status": ibkr_result.get("status", "error"),
        }
        # A skip is only useful in the history if it says which kind it was; `status`
        # alone reads the same as a pipeline collision.
        if ibkr_result.get("reason"):
            self.last_sync_result["reason"] = ibkr_result["reason"]
        _collect_warnings(
            self.last_sync_result, ibkr_result, fx_result, market_result, div_result,
            lookthrough_result, basket_check, forecast_result,
        )
        await self._record_run(self.last_sync_result, started_at)

        logger.info("=" * 80)
        logger.info("FULL SYNC JOB COMPLETED")
        logger.info("=" * 80)
        return self.last_sync_result

    async def market_data_only_sync_job(self, scheduled: bool = False) -> Optional[dict]:
        """
        Market-data-only sync job, at each of MARKET_DATA_HOURS (Europe/Berlin).

        Only checks the last 7 days — lightweight, one Yahoo request per security. Most
        of those slots land *inside* a session, so the trailing rows it writes are
        mid-session prices that a later slot restates; that is by design and rests on
        MarketPriceRepository.PROVISIONAL_PRICE_DAYS. Also syncs exchange rates.
        """
        return await self._gated_job("market_data_only", self._market_data_only_locked, scheduled=scheduled)

    async def _market_data_only_locked(self) -> dict:
        logger.info("=" * 80)
        logger.info("STARTING MARKET DATA + EXCHANGE RATE SYNC (7 days)")
        logger.info("=" * 80)

        started_at = utcnow()

        # Sync exchange rates first (needed for portfolio value calculation)
        fx_result = await self.sync_exchange_rates(days_back=7)
        logger.info(f"Exchange Rate Sync Result: {fx_result}")

        # Always refreshing provisional closes, weekend or not: this job runs on a
        # Saturday only at 08:00, and that slot exists to settle Friday's US close,
        # which the 22:00 pass wrote seconds after the bell (issue #9).
        market_result = await self.sync_market_data(days_back=7, refresh_provisional=True)
        logger.info(f"Market Data Sync Result: {market_result}")

        # Sync benchmark prices (only previously used benchmarks)
        bench_result = await self.sync_benchmark_prices()
        logger.info(f"Benchmark Price Sync Result: {bench_result}")

        forecast_result = await self.record_dividend_forecast()

        # Track result
        self.last_sync_result = {
            "type": "market_data_only",
            "timestamp": utc_iso(utcnow()),
            "fx_result": fx_result,
            "market_result": market_result,
            "benchmark_result": bench_result,
            "dividend_forecast_result": forecast_result,
            "status": market_result.get("status", "error"),
        }
        _collect_warnings(
            self.last_sync_result, fx_result, market_result, bench_result,
            forecast_result,
        )
        await self._record_run(self.last_sync_result, started_at)

        logger.info("=" * 80)
        logger.info("MARKET DATA ONLY SYNC COMPLETED")
        logger.info("=" * 80)
        return self.last_sync_result

    async def watchlist_refresh_job(self, scheduled: bool = False) -> Optional[dict]:
        """
        The watchlist's scheduled refresh, at WATCHLIST_REFRESH_HOUR (Europe/Berlin).

        Under the same `SYNC_PIPELINE` gate as every other Yahoo job and the manual
        *Refresh All*, so it can never run beside one. The loop is
        `WatchlistService.sync_all` — its pacing, its per-item cache and its
        abandon-the-pass-on-429 latch — and nothing here re-implements any of it.
        """
        return await self._gated_job("watchlist_refresh", self._watchlist_refresh_locked, scheduled=scheduled)

    async def _watchlist_refresh_locked(self) -> dict:
        started_at = utcnow()
        result = {"type": "watchlist_refresh", "timestamp": utc_iso(started_at)}
        try:
            async with AsyncSessionLocal() as db:
                # Imported here to avoid any import cycle at module load
                from app.services.watchlist_service import WatchlistService

                watchlist_result = await WatchlistService(db).sync_all(force=False)
        except Exception as e:
            logger.error(f"Watchlist refresh failed: {e}", exc_info=True)
            result.update(
                status="error",
                message=f"Watchlist refresh failed: {type(e).__name__}: {e}",
            )
        else:
            failed = watchlist_result.get("errors", 0)
            # A ticker Yahoo could not serve is that row's warning, never the job's
            # status — and it has to be said, because the row keeps its old
            # `last_synced` and nothing else would report why. The rate-limit case
            # already carries its own warning from `sync_all`.
            if failed and not watchlist_result.get("rate_limited"):
                watchlist_result.setdefault("warnings", []).append(
                    f"{failed} watchlist item(s) could not be refreshed and keep "
                    f"their previous data."
                )
            result.update(
                status="success",
                message=watchlist_result.get("message"),
                watchlist_result=watchlist_result,
            )
            _collect_warnings(result, watchlist_result)

        # Deliberately not `self.last_sync_result`: that is the portfolio's "last sync".
        await self._record_run(result, started_at)
        logger.info(f"Watchlist refresh: {result.get('status')} — {result.get('message')}")
        return result

    async def fundamentals_refresh_job(self, scheduled: bool = False) -> Optional[dict]:
        """
        The daily fundamentals and analyst-ratings refresh, at FUNDAMENTALS_REFRESH_HOUR
        on FUNDAMENTALS_REFRESH_DAYS (Europe/Berlin).

        Under `SYNC_PIPELINE` like every Yahoo job and the tab's own Sync buttons. The
        loops are the services' stale passes — their pacing, held-and-`yahoo_eligible()`
        filter and abandon-on-429 latch — and nothing here re-implements any of them. A rate
        limit in the fundamentals pass skips the ratings pass: it is the same IP. The logo
        pass (`LogoService.refresh`, logo.dev) follows either way.
        """
        return await self._gated_job("fundamentals_refresh", self._fundamentals_refresh_locked, scheduled=scheduled)

    async def _fundamentals_refresh_locked(self) -> dict:
        # Imported here to avoid any import cycle at module load
        from app.services.analyst_rating_service import AnalystRatingService
        from app.services.fundamentals_service import FundamentalsService

        started_at = utcnow()
        result = {"type": "fundamentals_refresh", "timestamp": utc_iso(started_at)}
        try:
            async with AsyncSessionLocal() as db:
                fundamentals_result = await FundamentalsService(db).sync_stale_fundamentals()
            if fundamentals_result.get("rate_limited"):
                ratings_result = {
                    "status": "skipped",
                    "message": "Not attempted after the fundamentals pass hit a rate limit",
                    "warnings": [
                        "Analyst ratings were not refreshed: Yahoo rate-limited the "
                        "fundamentals pass first."
                    ],
                }
            else:
                async with AsyncSessionLocal() as db:
                    ratings_result = await AnalystRatingService(db).sync_stale_ratings()
        except Exception as e:
            logger.error(f"Fundamentals refresh failed: {e}", exc_info=True)
            result.update(
                status="error",
                message=f"Fundamentals refresh failed: {type(e).__name__}: {e}",
            )
        else:
            # A security Yahoo could not serve is that row's warning, never the job's
            # status. The rate-limit case already carries its own warning.
            for step, noun in ((fundamentals_result, "fundamentals"),
                               (ratings_result, "analyst ratings")):
                failed = step.get("errors", 0)
                if failed and not step.get("rate_limited"):
                    step.setdefault("warnings", []).append(
                        f"{failed} securit{'y' if failed == 1 else 'ies'} could not "
                        f"refresh {noun} and keep their previous data."
                    )
            result.update(
                status="success",
                message=f"{fundamentals_result.get('message')}; {ratings_result.get('message')}",
                fundamentals_result=fundamentals_result,
                ratings_result=ratings_result,
            )
            _collect_warnings(result, fundamentals_result, ratings_result)

        # Holding logos (docs/logos.md): logo.dev, not Yahoo, so it runs after a Yahoo rate
        # limit or a crashed fundamentals pass too, and its own failure is a warning, never
        # this job's status. No key, or nothing due, makes no request.
        from app.services.logo_service import LogoService
        try:
            async with AsyncSessionLocal() as db:
                logos_result = await LogoService(db).refresh()
        except Exception as e:
            logger.error(f"Logo pass failed: {e}", exc_info=True)
            logos_result = {
                "status": "error", "message": f"Logo pass failed: {type(e).__name__}: {e}",
                "warnings": [f"Logo pass failed: {type(e).__name__}: {e}"],
            }
        result["logos_result"] = logos_result
        # Appended, not `_collect_warnings`, which would replace the passes' warnings above.
        if logos_result.get("warnings"):
            result.setdefault("warnings", []).extend(logos_result["warnings"])

        # Not `self.last_sync_result` either: that is the portfolio's "last sync".
        await self._record_run(result, started_at)
        logger.info(f"Fundamentals refresh: {result.get('status')} — {result.get('message')}")
        return result

    async def _record_run(self, result: dict, started_at: datetime) -> None:
        """
        Persist a job's outcome to sync_runs.

        last_sync_result alone is in-memory, so it disappears on every container
        restart — and auto-deploy restarts on each push, which would leave the daily
        validator blind.

        Best-effort end to end: the session itself may fail to open, so the whole thing
        is guarded. Bookkeeping must never be the reason a sync reports failure.
        """
        try:
            async with AsyncSessionLocal() as db:
                await SyncRunRepository(db).record(
                    sync_type=result.get("type", "unknown"),
                    status=result.get("status", "error"),
                    message=result.get("message") or (result.get("ibkr_result") or {}).get("message"),
                    details={k: v for k, v in result.items() if k not in ("warnings",)},
                    warnings=result.get("warnings"),
                    started_at=started_at,
                )
        except Exception as e:
            logger.warning(f"Could not persist sync run: {e}")

    async def ibkr_only_sync_job(self, scheduled: bool = False) -> Optional[dict]:
        return await self._gated_job("ibkr_sync", self._ibkr_only_sync_locked, scheduled=scheduled)

    async def _ibkr_only_sync_locked(self) -> dict:
        """
        IBKR-only sync, at 00:00 Europe/Berlin — the day's *recovery* attempt.

        Exists because the full sync used to be the only job that talks to IBKR, so a
        transient `Code=1001` ("statement could not be generated") cost a whole day of
        freshness — which got likelier once the Flex Query grew from one section to six
        (Trades/CorporateActions/CashTransactions, then Deposits & Withdrawals and
        Transfers), five of which scan the whole period.

        **It is now almost always a no-op, and that is the point.** 00:00 Berlin is
        18:00 ET — the *same* ET calendar day as the 18:00 Berlin full sync six hours
        earlier — so when that one succeeded, the guard in `flex_generation.py` makes
        this return `skipped` without touching the network. It fires only on a day the
        primary attempt failed, which is exactly the case that saved the data on
        2026-08-02 and 08-03. Before the guard existed this slot failed every single
        day of its recorded history, spending `Code=1025` budget each time.

        **This ran at 13:00 and 20:00 until 2026-07-31 and must not go back there.**
        IBKR builds the statement from *finalised* daily data, so SendRequest succeeds
        overnight and fails mid-session — as `Code=1001` at the request step, the
        fatal-fast kind. Measured on this account's own `sync_runs`: overnight 8/9,
        afternoon and evening 1/15 (13:00 was 0-for-6, 20:00 1-for-8). That evidence
        also argues against the 18:00 Berlin primary slot, which the account owner
        chose anyway with the trade-off stated; see IBKR_ONLY_HOURS.

        Deliberately does NOT call sync_market_data() or sync_dividends(): both hit Yahoo
        Finance, which is rate-limited and, per CLAUDE.md, must never be called without
        explicit user permission. Re-running just the IBKR half is safe and cheap —
        ingestion is idempotent (upserts keyed on ib_key) — and it also picks up intraday
        trades. Exchange rates come from Frankfurter (free, unmetered), so they ride along.
        """
        logger.info("=" * 80)
        logger.info("STARTING IBKR-ONLY SYNC (no market data)")
        logger.info("=" * 80)

        started_at = utcnow()
        ibkr_result = await self.sync_ibkr_data()
        logger.info(f"IBKR Sync Result: {ibkr_result}")

        fx_result = await self.sync_exchange_rates(days_back=7)
        logger.info(f"Exchange Rate Sync Result: {fx_result}")

        self.last_sync_result = {
            "type": "ibkr_sync",
            "timestamp": utc_iso(utcnow()),
            "ibkr_result": ibkr_result,
            "fx_result": fx_result,
            "status": ibkr_result.get("status", "error"),
        }
        if ibkr_result.get("reason"):
            self.last_sync_result["reason"] = ibkr_result["reason"]
        _collect_warnings(self.last_sync_result, ibkr_result, fx_result)
        await self._record_run(self.last_sync_result, started_at)

        logger.info("=" * 80)
        logger.info("IBKR-ONLY SYNC COMPLETED")
        logger.info("=" * 80)
        return self.last_sync_result

    def _add_or_keep(
        self, job_id: str, func, trigger: CronTrigger, name: str,
        group: str = STOCK_JOB_GROUP,
    ) -> None:
        """
        Register a job, **preserving a stored run time when the schedule is unchanged**.

        `add_job(..., replace_existing=True)` looks like the obvious call and is the
        wrong one here: replacing a job recomputes `next_run_time` from now, so the
        missed run time a restart was supposed to recover is overwritten on the way in
        and the persistent store buys nothing. Leaving an identically-triggered job
        alone keeps that timestamp, which is what lets APScheduler notice the misfire.

        Comparing `str(trigger)` is how a schedule *change* still lands: CronTrigger's
        repr is its full field set, so editing an hour replaces the job (and its stale
        run time) while a restart with the same schedule does not.
        """
        # Feeds `_prune_unknown_jobs`, so the set of live jobs is defined by what was
        # actually registered rather than by a second list free to fall out of step.
        self._registered_job_ids.add(job_id)
        self._job_groups[job_id] = group

        existing = self.scheduler.get_job(job_id)
        if existing is not None and str(existing.trigger) == str(trigger):
            logger.info(f"  {name} — kept, next run: {existing.next_run_time}")
            return

        if existing is not None:
            logger.info(f"  {name} — schedule changed, rescheduling")

        self.scheduler.add_job(
            func, trigger=trigger, id=job_id, name=name, replace_existing=True
        )

    def _prune_unknown_jobs(self) -> None:
        """
        Drop persisted jobs this build no longer registers.

        The job store outlives the code. Without this, **renaming a job id silently
        doubles the work instead of replacing it**: `_add_or_keep` adds the new id, and
        the row under the old id keeps its trigger and keeps firing — two IBKR syncs a
        night, each burning a Flex statement generation, which is what `Code=1025`
        counts. Retiring a job entirely has the same shape: the code forgets it, the
        store does not.

        Registration is the single source of truth: `_add_or_keep` records each id it
        handles, so this cannot drift from the list above the way a second hardcoded set
        would. Safe because jobs are only ever added there — nothing schedules at runtime.
        """
        for job in self.scheduler.get_jobs():
            if job.id not in self._registered_job_ids:
                logger.info(f"  removing retired job '{job.id}' from the persistent store")
                job.remove()

    def _try_start(self, jobstores: dict, fatal: bool = False) -> bool:
        """
        Build and start a paused scheduler over `jobstores`. True if it came up.

        Paused so the job store is live for `_add_or_keep`'s `get_job()` lookups —
        before `start()` those see only APScheduler's pending-jobs list — while nothing
        can fire mid-registration.

        `fatal=True` for the in-memory attempt: if even that fails there is nothing left
        to fall back to, and swallowing it would leave `self.scheduler` None for
        `shutdown()` and `/api/scheduler/status` to trip over later.
        """
        try:
            self.scheduler = AsyncIOScheduler(
                jobstores=jobstores,
                job_defaults={
                    # One catch-up run, not one per slot the outage covered.
                    'coalesce': True,
                    'misfire_grace_time': MISFIRE_GRACE_SECONDS,
                    # Belt and braces beside single_flight, which is the real guard.
                    'max_instances': 1,
                },
            )
            self.scheduler.start(paused=True)
            return True
        except Exception:
            self.scheduler = None
            if fatal:
                raise
            logger.exception("Scheduler failed to start with the configured job store")
            return False

    def start(self):
        """
        Start the scheduler with 8 daily stock syncs (Europe/Berlin):
        - 18:00: Full sync (IBKR + 730 days market data) — the day's one generation
        - 00:00: IBKR only — the recovery attempt, a no-op unless 18:00 failed
        - MARKET_DATA_HOURS: market data only (7 days), through both sessions, on
          MARKET_DATA_DAYS (Monday to Friday, and Saturday at 08:00)

        plus the crypto sync at the same eight slots (CRYPTO_SYNC_HOURS), in its own
        job group, and the watchlist refresh on a slot of its own
        (WATCHLIST_REFRESH_HOUR, Tuesday to Saturday), in a third — beside the daily
        fundamentals refresh (FUNDAMENTALS_REFRESH_HOUR, Tuesday to Saturday).

        Jobs are persisted to `settings.scheduler_jobstore_url` so a restart that
        overlaps a slot recovers it instead of losing it — see MISFIRE_GRACE_SECONDS.
        """
        if self.scheduler is not None:
            logger.warning("Scheduler is already running")
            return

        logger.info("Starting scheduler service...")

        # A persistent store serializes each job, so the target must be importable by
        # name. That is why the five entry points below are module-level functions
        # rather than the bound methods they used to be: a bound method of this instance
        # would drag the live AsyncIOScheduler into the pickle.
        #
        # It degrades to in-memory rather than raising, and the guard wraps `start()`
        # rather than the constructor because SQLAlchemyJobStore connects lazily — a
        # corrupt or unwritable file surfaces when the scheduler starts the store, not
        # when it is built. The store is a bind-mounted sqlite file, so both are
        # possible, and an exception here propagates out of the lifespan handler and
        # **the container never boots**. Losing misfire recovery costs one late sync;
        # failing to start costs the whole site.
        started = False
        if settings.scheduler_jobstore_url:
            ensure_jobstore_parent(settings.scheduler_jobstore_url)
            try:
                store = SQLAlchemyJobStore(url=settings.scheduler_jobstore_url)
            except Exception:
                logger.exception("Could not build the scheduler job store")
            else:
                started = self._try_start({'default': store})
            if not started:
                logger.error(
                    "Scheduler job store at %s is unusable - running in memory. Jobs "
                    "still run on schedule, but a restart overlapping a slot loses it.",
                    settings.scheduler_jobstore_url,
                )

        # Reported by /health. The fallback re-registers all five jobs, so
        # `/api/scheduler/status` cannot tell it from a working store — which is exactly
        # how a job store that never once opened looked healthy for two days.
        self.jobstore_persistent = started

        if not started:
            self._try_start({}, fatal=True)

        self._add_or_keep(
            'full_sync_job', full_sync_job_entry,
            CronTrigger(hour=FULL_SYNC_HOUR, minute=0, timezone='Europe/Berlin'),
            f'Full IBKR + Market Data Sync ({FULL_SYNC_HOUR:02d}:00 Europe/Berlin)',
        )
        # One IBKR-only second chance, so a transient Code=1001 at 18:00 does not cost a
        # full day of freshness.
        #
        # **Adding more would not add freshness.** IBKR generates this statement about
        # once per ET calendar day and refuses every later attempt, so extra slots buy
        # failed generations — which is precisely what `Code=1025` counts. What makes
        # even this one free is the guard in `flex_generation.py`: it skips without
        # asking whenever the ET day's generation is already spent, so it costs nothing
        # on the ~everyday case and only reaches the network when 18:00 failed.
        #
        # A slot is useful only if it falls in the same ET day *after* the primary and
        # before midnight ET. 00:00 Berlin is 18:00 ET, which satisfies both.
        #
        # The historical hour evidence, kept because it still constrains where a future
        # slot may go (measured on this account's own `sync_runs`, 2026-07-31):
        #
        #     Berlin   ET       ok/total
        #     00:00    18:00     1/1      after the US close
        #     06:00    00:00     2/2
        #     08:00    02:00     4/5
        #     09:00    03:00     1/1
        #     13:00    07:00     0/6      pre-market
        #     20:00    14:00     1/8      mid-session
        #
        # Overnight 8/9; afternoon and evening 1/15. Those old midday slots were not
        # merely weak, they were **negative** — each failure a failed generation. That
        # is the evidence the 18:00 Berlin primary slot runs against, by the owner's
        # explicit decision; see IBKR_ONLY_HOURS for the full trade-off.
        #
        # The ids are **numbered, not named for their hour**. They used to be
        # `ibkr_sync_midday` / `ibkr_sync_evening`, which became lies the moment the
        # hours moved — and an id is the one thing a persistent job store cannot cheaply
        # rename (see `_prune_unknown_jobs`). Encoding the schedule in the identity
        # guarantees exactly this problem on the next change; the human-readable hour
        # lives in `name`, which is free to change.
        for index, hour in enumerate(IBKR_ONLY_HOURS, start=1):
            self._add_or_keep(
                f'ibkr_retry_{index}', ibkr_only_sync_job_entry,
                CronTrigger(hour=hour, minute=0, timezone='Europe/Berlin'),
                f'IBKR-only Sync ({hour:02d}:00 Europe/Berlin)',
            )
        # Market data runs seven times a trading day, and five of those are *inside* a
        # session on purpose — the portfolio was only ever repriced at 08:00, 15:00
        # and 22:00, so a figure looked at mid-morning was up to seven hours old.
        #
        # **Intraday slots are only correct because a recent close is now
        # re-fetched** (MarketPriceRepository.PROVISIONAL_PRICE_DAYS). Yahoo answers
        # a daily request with a bar for the live session, and until 2026-08-04 the
        # first row of the day was kept forever: production held every European
        # close at its 15:00 Berlin mid-session value. Adding an earlier slot on top
        # of that would have frozen an *earlier* price, making the number worse
        # rather than fresher. If that refresh is ever removed, these slots have to
        # go with it.
        #
        # Berlin hours against the sessions they serve (both EU and US shift with
        # DST together, so the coverage holds year-round):
        #
        #     08:00  settles yesterday's US close and Asia's (also Saturdays)
        #     11:00  Xetra/Euronext/LSE ~2h in — also settles Tokyo and Korea,
        #            whose closes the 08:00 run catches mid-session
        #     13:00  Europe ~4h in
        #     15:00  Europe ~6h in, US pre-open
        #     18:00  Europe settled (closes 17:30), US ~2.5h in
        #     20:00  US ~4.5h in
        #     22:00  US close
        #
        # Worst gap inside either session is ~2.5h, down from ~7h.
        #
        # 13:00 and 20:00 were IBKR slots until 2026-07-31 and were retired for
        # failing mid-session. **These are not those slots coming back** — market
        # data touches only Yahoo, and Yahoo has no statement to generate and no
        # `Code=1025` budget to spend. The prohibition is specific to Flex; see
        # `_ibkr_only_sync_locked` and `test_every_ibkr_job_avoids_us_market_hours`.
        #
        # The ids are numbered rather than named for their hour, for the reason the
        # IBKR pair above is: an id is the one thing a persistent job store cannot
        # cheaply rename, so encoding a schedule in it guarantees a lie on the next
        # change. `market_sync_eu_close` / `market_sync_us_close` were exactly that
        # — the first named itself after a close it ran 2.5 hours before.
        #
        # Weekdays only, plus Saturday 08:00 — see MARKET_DATA_DAYS. A persisted job
        # with the old daily trigger is replaced on the next start: `_add_or_keep`
        # compares `str(trigger)`, which includes `day_of_week`.
        for index, hour in enumerate(MARKET_DATA_HOURS, start=1):
            days = (
                MARKET_DATA_SETTLE_DAYS if hour == MARKET_DATA_SETTLE_HOUR
                else MARKET_DATA_DAYS
            )
            self._add_or_keep(
                f'market_sync_{index}', market_data_only_sync_job_entry,
                CronTrigger(day_of_week=days, hour=hour, minute=0, timezone='Europe/Berlin'),
                f'Market Data Sync ({hour:02d}:00 Europe/Berlin)',
            )

        # The crypto sync: CoinStats only, under its own gate, never touching
        # `last_sync_result`. Numbered ids for the reason the groups above are.
        for index, hour in enumerate(CRYPTO_SYNC_HOURS, start=1):
            self._add_or_keep(
                f'crypto_sync_{index}', crypto_sync_job_entry,
                CronTrigger(hour=hour, minute=0, timezone='Europe/Berlin'),
                f'Crypto Sync ({hour:02d}:00 Europe/Berlin)',
                group=CRYPTO_JOB_GROUP,
            )

        # The watchlist, on its own slot and in its own group — see
        # WATCHLIST_REFRESH_HOUR for why it rides none of the slots above.
        self._add_or_keep(
            'watchlist_refresh', watchlist_refresh_job_entry,
            CronTrigger(
                day_of_week=WATCHLIST_REFRESH_DAYS, hour=WATCHLIST_REFRESH_HOUR,
                minute=0, timezone='Europe/Berlin',
            ),
            f'Watchlist Refresh ({WATCHLIST_REFRESH_HOUR:02d}:00 Europe/Berlin)',
            group=UPKEEP_JOB_GROUP,
        )
        # Fundamentals and analyst ratings, on the watchlist's days an hour after it —
        # see FUNDAMENTALS_REFRESH_HOUR.
        self._add_or_keep(
            'fundamentals_refresh', fundamentals_refresh_job_entry,
            CronTrigger(
                day_of_week=FUNDAMENTALS_REFRESH_DAYS, hour=FUNDAMENTALS_REFRESH_HOUR,
                minute=0, timezone='Europe/Berlin',
            ),
            f'Fundamentals Refresh ({FUNDAMENTALS_REFRESH_HOUR:02d}:00 Europe/Berlin)',
            group=UPKEEP_JOB_GROUP,
        )

        self._prune_unknown_jobs()
        self.scheduler.resume()

        logger.info("Scheduler started successfully")
        for job in self.scheduler.get_jobs():
            logger.info(f"  {job.name} — next run: {job.next_run_time}")

    def shutdown(self):
        """
        Shutdown the scheduler gracefully.
        """
        if self.scheduler is None:
            logger.warning("Scheduler is not running")
            return

        logger.info("Shutting down scheduler service...")
        self.scheduler.shutdown(wait=True)
        self.scheduler = None
        logger.info("Scheduler shut down successfully")

    def jobs_in_group(self, group: str) -> list:
        """The live jobs this build registered under `group`, in APScheduler's order."""
        if self.scheduler is None:
            return []
        return [
            job for job in self.scheduler.get_jobs()
            if self._job_groups.get(job.id) == group
        ]

    def next_run_time(self, group: str) -> Optional[datetime]:
        """The soonest run of any job in `group`, or None when nothing is scheduled."""
        times = [job.next_run_time for job in self.jobs_in_group(group) if job.next_run_time]
        return min(times) if times else None

    async def crypto_sync_job(self) -> Optional[dict]:
        """
        One scheduled crypto sync (docs/crypto.md).

        Its own gate, never `SYNC_PIPELINE`: the stock pipeline and this one share no
        upstream, and entering that gate would put the stock Sync buttons on cooldown.
        Never sets `last_sync_result`, which is the stock pipeline's "last sync". Nothing
        at all when CoinStats is not configured — not even a skipped row.
        """
        from app.services.coinstats_client import is_configured
        from app.services.crypto_service import CRYPTO_GATE, CryptoSyncService

        if not is_configured():
            return None
        try:
            with single_flight(CRYPTO_GATE):
                return await CryptoSyncService().sync()
        except SyncBusy as e:
            # The manual button is mid-run; its result is this slot's result.
            logger.info(f"crypto sync skipped: {e}")
            return None

    async def trigger_sync_now(self) -> dict:
        """
        Manually trigger the sync job immediately (for testing).

        Raises SyncBusy (a 429 at the router) when the pipeline is already
        running, instead of pretending a skipped run completed.

        Keyed on the *reason*, not on the bare `skipped` status: a job whose IBKR half
        was skipped because today's statement is already generated is not busy, it is
        finished, and answering 429 "try again shortly" to that would send the caller
        back for something that cannot arrive before midnight ET.
        """
        logger.info("Manually triggering full sync job...")
        result = await self.full_sync_job()
        if result and result.get("reason") == "pipeline_busy":
            raise SyncBusy(result.get("message") or "sync pipeline is busy")
        # last_sync_result is already set by full_sync_job
        return {"status": "completed", "message": "Manual sync triggered successfully"}


# Global scheduler instance
_scheduler_service: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    """
    Get the global scheduler service instance.

    Returns:
        SchedulerService instance
    """
    global _scheduler_service
    if _scheduler_service is None:
        _scheduler_service = SchedulerService()
    return _scheduler_service


# ── Job entry points ──────────────────────────────────────────────────────────
#
# The scheduled jobs used to be registered as bound methods (`self.full_sync_job`),
# which an in-memory job store is happy to hold onto. A persistent store serializes
# each job instead, and a bound method would drag the whole SchedulerService — the live
# AsyncIOScheduler included — into the pickle.
#
# Module-level functions serialize as `module:name` references, so these thin wrappers
# resolve the singleton at fire time. They are also the reason the scheduler survives a
# code change: a stored reference is re-imported, not un-pickled from old bytes.


async def full_sync_job_entry() -> dict:
    return await get_scheduler().full_sync_job(scheduled=True)


async def ibkr_only_sync_job_entry() -> dict:
    return await get_scheduler().ibkr_only_sync_job(scheduled=True)


async def market_data_only_sync_job_entry() -> dict:
    return await get_scheduler().market_data_only_sync_job(scheduled=True)


async def crypto_sync_job_entry() -> Optional[dict]:
    return await get_scheduler().crypto_sync_job()


async def watchlist_refresh_job_entry() -> Optional[dict]:
    return await get_scheduler().watchlist_refresh_job(scheduled=True)


async def fundamentals_refresh_job_entry() -> Optional[dict]:
    return await get_scheduler().fundamentals_refresh_job(scheduled=True)
