"""
Scheduler Service
Handles automated daily synchronization of IBKR data and market prices.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import date, timedelta
from app.clock import utcnow

from app.config import settings
from app.database import AsyncSessionLocal
from app.single_flight import SYNC_PIPELINE, SyncBusy, single_flight
from app.services.ibkr_service import IBKRService, FLEX_RETRY_DELAYS_PATIENT
from app.services.flex_generation import (
    IBKR_SYNC_TYPES,
    et_date,
    generation_already_spent,
    next_generation_opens_at,
)
from app.services.market_data_service import MarketDataService
from app.services.currency_service import CurrencyService
from app.services.sync_helper import ingest_flex_statement
from app.repositories.security_repository import SecurityRepository
from app.repositories.sync_run_repository import SyncRunRepository, utc_iso
from app.services.benchmark_service import BenchmarkService, BENCHMARKS
from app.services.yahoo_rate_limit import is_rate_limit
from app.models.benchmark_price import BenchmarkPrice
from app.models.dividend_payment import DividendPayment
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.sync_run import SyncRun
from app.models.taxlot import TaxLot
from app.models.ticker_mapping import TickerMapping
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

# How long IBKR may go without a *successful* sync before we say so.
#
# Nothing watched this until 2026-07-31, and under a Year-to-Date Flex Query it did not
# much matter: every statement re-delivers the whole year, so a gap self-heals the moment
# one sync lands. **It becomes load-bearing the moment the query period is bounded.**
# With a 30-day window, an outage longer than the window loses those trades permanently
# and silently — the rows simply never appear in any future statement.
#
# Seven days against two IBKR attempts a day (18:00 and 00:00 Berlin) means ~14
# consecutive failures before it fires, so it cannot cry wolf over a `1025` lockout
# (~14h) or a bad night.
#
# **Seven days is far too slow to be the alarm for a 3-day Flex window**, and that is
# not a flaw in this constant but the reason `find_flex_generation_gap` exists beside
# it: trades fall out of a `Last 3 Calendar Days` statement after two consecutive
# missed days, four days before this one says a word. This stays as the backstop for a
# genuinely broken token; the two-day one is the alarm that protects the data.
IBKR_SYNC_STALE_DAYS = 7

# ET days without a successful IBKR sync before `find_flex_generation_gap` warns.
#
# Two, because that is the actual margin. The Flex Query period is `Last N Calendar
# Days` with N=3, so a statement generated on day D covers D-3..D-1 and a trade on day
# T is reachable from T+1, T+2 or T+3 and **gone from T+4**. One generation is
# available per ET day, so two consecutive missed days leaves exactly one day of slack
# to notice and recover by hand.
#
# It fires one day earlier than strictly necessary on purpose: the recovery is manual
# (a browser download through `app/cli/ingest_flex_xml.py`) and needs a human awake.
# The cost of a day's false alarm is one warning line; the cost of being a day late is
# trades that no future statement contains. **Re-derive this if the Flex Query period
# changes** — it is N-1, not a preference.
FLEX_GENERATION_GAP_WARN_DAYS = 2

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
# Thirty minutes is chosen from both ends. A `build --no-cache` rebuild takes a couple
# of minutes, so it comfortably covers a deploy. And it is short enough that a genuinely
# long outage does *not* dump four missed slots onto a cold container: anything older is
# dropped, so only the slot the outage actually straddled is recovered. `coalesce` then
# collapses repeats of the same job into one.
MISFIRE_GRACE_SECONDS = 30 * 60

# ── The schedule, in Europe/Berlin hours ──────────────────────────────────────
#
# These four constants are the single source of truth for when anything runs. They
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
ALL_SYNC_HOURS = tuple(sorted({FULL_SYNC_HOUR, *IBKR_ONLY_HOURS, *MARKET_DATA_HOURS}))


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

    Runs 8 times daily (Europe/Berlin):
    - 08:00, 11:00, 13:00, 15:00, 20:00, 22:00: Market data only (7 days)
    - 18:00: Full sync (IBKR + 730 days market data) — fills historical gaps gradually
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

    async def sync_ibkr_data(self, force: bool = False) -> dict:
        """
        Sync securities and tax lots from IBKR Flex Query.

        Returns `status="skipped"` without touching the network when IBKR has already
        generated today's statement — see `app/services/flex_generation.py` for the
        evidence. Asking anyway cannot succeed and is not free: the refusal arrives as
        `Code=1001` at the SendRequest step, which is a *failed generation*, and failed
        generations are exactly what the `Code=1025` token lockout counts.

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
                        f"IBKR already generated today's statement (last successful sync "
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

    async def sync_market_data(self, days_back: int = 730) -> dict:
        """
        Sync market prices from Yahoo Finance for all securities.
        Only fetches missing dates (incremental sync).

        Args:
            days_back: Number of days to look back (default 730 = 2 years)

        Returns:
            Summary of synced data
        """
        logger.info("Starting scheduled market data sync...")

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

                # The loop itself lives on the service, shared with
                # POST /api/market-data/sync — it fetches the dates we are missing plus
                # the trailing few whose cached close may still be a mid-session price,
                # and stops early if Yahoo rate-limits.
                swept = await market_data_service.sync_securities(
                    securities, days_back=days_back
                )
                total_prices = swept["total_prices"]
                processed = swept["processed"]
                errors = swept["errors"]
                rate_limited_after = swept["rate_limited_after"]

                # Commit all price data
                await db.commit()

                result = {
                    "status": "success" if not (errors or rate_limited_after) else "partial_success",
                    "message": f"Synced market data for {processed} of {len(securities)} securities",
                    "securities_processed": processed,
                    "prices_fetched": total_prices,
                    "timestamp": utc_iso(utcnow())
                }

                if errors:
                    result["errors"] = errors
                    result["errors_count"] = len(errors)

                if rate_limited_after:
                    result["rate_limited"] = True
                    result["warnings"] = [
                        f"Yahoo rate-limited this run at {rate_limited_after}; "
                        f"{len(securities) - processed} securities were skipped and "
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
                # is the backstop for a broken token; the 2-day one is what actually
                # protects the data, because a `Last 3 Calendar Days` statement loses
                # trades permanently once two consecutive ET days go by with no
                # successful generation — four days before the 7-day one says anything.
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
                try:
                    diagnostics += await self.find_stale_etf_baskets(db)
                except Exception as e:
                    logger.warning(f"Could not check ETF basket freshness: {e}")
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
        as_of = as_of or date.today()
        cutoff = as_of - timedelta(days=STALE_PRICE_DAYS)

        # Deliberately two queries rather than one join. Joining taxlots to
        # market_prices multiplies them — a security with 100 open lots and 730 cached
        # closes is 73,000 rows to aggregate, and the portfolio holds 972 lots against
        # 21,000 prices. Two indexed scans and a dict lookup cost nothing by comparison.
        held = await db.execute(
            select(Security.id, Security.symbol, Security.exchange)
            .join(TaxLot, TaxLot.security_id == Security.id)
            .where(TaxLot.is_open == True)  # noqa: E712 — SQLAlchemy needs the operator
            .group_by(Security.id)
        )
        held_rows = held.all()
        if not held_rows:
            return []

        newest = await db.execute(
            select(MarketPrice.security_id, func.max(MarketPrice.date))
            .where(MarketPrice.security_id.in_([row[0] for row in held_rows]))
            .group_by(MarketPrice.security_id)
        )
        newest_by_security = dict(newest.all())

        warnings = []
        for security_id, symbol, exchange in held_rows:
            name = f"{symbol}@{exchange}" if exchange else str(symbol)
            latest = newest_by_security.get(security_id)
            if latest is None:
                warnings.append(
                    f"{name}: no cached price at all — the position is being "
                    f"valued at 0.00. Check the ticker_mappings row and the Yahoo symbol"
                )
            elif latest < cutoff:
                warnings.append(
                    f"{name}: newest price is {latest} "
                    f"({(as_of - latest).days} days old) — the price feed looks broken"
                )

        if warnings:
            logger.warning(f"{len(warnings)} security(ies) with missing or stale prices")
        return warnings

    async def find_flex_generation_gap(
        self, db: AsyncSession, as_of: Optional[datetime] = None
    ) -> list:
        """
        Warn after `FLEX_GENERATION_GAP_WARN_DAYS` ET days with no successful IBKR sync.

        **This is the alarm that protects the data; `find_stale_ibkr_sync` is only the
        backstop.** The Flex Query period is `Last 3 Calendar Days`, so a statement
        generated on day D covers D-3..D-1 and a trade on day T is unreachable from T+4
        onward. With one generation available per ET day, two consecutive missed days
        is the entire margin — and the 7-day detector fires four days after the trades
        have already gone from every future statement. Warning at 7 about a 3-day
        window is warning after the loss.

        Counted in **ET days**, not in elapsed hours, because that is the unit IBKR
        issues statements in: a failure at 23:00 ET and one at 01:00 ET the next day are
        two missed generations two hours apart, and an hours-based age would call that
        one.

        Uses `IBKR_SYNC_TYPES`, so an offline `ibkr_manual_xml` ingest **closes** the
        gap. That is the opposite choice from the guard in `flex_generation.py`, which
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

        latest = await db.execute(
            select(func.max(SyncRun.finished_at)).where(
                and_(
                    SyncRun.sync_type.in_(IBKR_SYNC_TYPES),
                    SyncRun.status == 'success',
                )
            )
        )
        newest = latest.scalar_one_or_none()
        if newest is None:
            return []

        missed = (et_date(as_of) - et_date(newest)).days
        if missed < FLEX_GENERATION_GAP_WARN_DAYS:
            return []

        return [
            f"IBKR: no statement has been generated for {missed} US-Eastern days (last "
            f"success {et_date(newest):%Y-%m-%d} ET). The Flex Query window only reaches "
            f"back a few days, so trades are about to become unreachable from every "
            f"future statement — download the statement from Client Portal with a wider "
            f"period and ingest it via app/cli/ingest_flex_xml.py"
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
        the data, so it should reset the clock exactly like an API sync.

        Returns `warnings[]`-ready strings, like the sibling detectors.
        """
        as_of = as_of or utcnow()

        latest = await db.execute(
            select(func.max(SyncRun.finished_at)).where(
                and_(
                    SyncRun.sync_type.in_(IBKR_SYNC_TYPES),
                    SyncRun.status == 'success',
                )
            )
        )
        newest = latest.scalar_one_or_none()

        if newest is None:
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

        age = (as_of - newest).days
        if age < IBKR_SYNC_STALE_DAYS:
            return []

        return [
            f"IBKR: last successful sync was {newest:%Y-%m-%d} ({age} days ago). "
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
        held = await db.execute(
            select(Security.id, Security.symbol, Security.exchange)
            .join(TaxLot, TaxLot.security_id == Security.id)
            .where(TaxLot.is_open == True)  # noqa: E712 — SQLAlchemy needs the operator
            .group_by(Security.id)
        )
        held_rows = held.all()
        if not held_rows:
            return []

        # Same two-queries-not-a-join reasoning as above: joining lots to dividend
        # rows multiplies them for no benefit.
        newest = await db.execute(
            select(DividendPayment.security_id, func.max(DividendPayment.last_computed))
            .where(
                and_(
                    DividendPayment.security_id.in_([row[0] for row in held_rows]),
                    DividendPayment.source == "yfinance_estimate",
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
        none. They are populated by a deliberate CLI run and then decay in place while
        the numbers stay confidently on screen: nine of the ten feeds republish daily, so
        a week after a fetch the whole tab describes an older index. The tab's own `†`
        badge was the only signal, and it requires someone to look.

        Runs from the **market-data** job, like its siblings and for the same reason:
        those slots succeed while Flex is refusing.

        Four rules, each of which would be wrong the other way:

        - **Held funds only.** Mirrors `find_stale_priced_securities` restricting itself
          to open lots — a basket for a fund nobody holds moves no figure.
        - **The basket a fund actually reads comes from
          `LookthroughService._alias_proxied_baskets`, never from a second copy of the
          proxy rule.** Two implementations of "which basket does this fund use" is this
          codebase's dominant failure mode; the age reported here has to be the age of
          the file the numbers came from, which for a proxied fund is the *source's*.
        - **The threshold is `lookthrough_service.stale_after_days`**, the existing
          per-adapter table. A single global constant meant two different things once and
          badged Vanguard permanently for publishing month-end as documented.
        - **Nothing unclearable warns.** A fund excluded from look-through by design, and
          one whose only route is a hand-downloaded file, can never be fixed by running
          anything — and a warning that can never clear is the always-present-Flex-banner
          pathology, which teaches the reader to skip the banner that also carries a
          skipped tax lot. A held fund with no basket that *does* have a route warns,
          because that is what a newly bought fund looks like and one CLI run clears it.
          **"Has a route" follows the proxy**: VWCE's own adapter is `manual`, but it
          reads VT's basket and VT is fetchable, so a missing VT is actionable and must
          not be silently skipped just because the *held* fund has no route of its own.
        """
        from app.cli.fetch_etf_baskets import AUTOMATED_ADAPTERS
        from app.etf_mappings import is_known_etf_isin, symbol_for_fund_isin
        from app.etf_sources import basket_proxy_for, source_for_fund_isin
        from app.repositories.etf_basket_repository import EtfBasketRepository
        from app.services.lookthrough_service import (
            LookthroughService,
            stale_after_days,
        )

        as_of = as_of or date.today()

        held = await db.execute(
            select(distinct(Security.isin))
            .join(TaxLot, TaxLot.security_id == Security.id)
            .where(TaxLot.is_open == True)  # noqa: E712 — SQLAlchemy needs the operator
        )
        fund_isins = sorted({
            isin.strip().upper() for (isin,) in held.all()
            if isin and is_known_etf_isin(isin)
        })
        if not fund_isins:
            return []

        # Load each held fund's own basket plus any it borrows, then let the read path's
        # own resolver decide which one each fund actually uses.
        proxy_sources = sorted({
            src for i in fund_isins if (src := basket_proxy_for(i)) and src != i
        })
        repo = EtfBasketRepository(db)
        baskets = await repo.get_baskets(fund_isins + proxy_sources)
        LookthroughService._alias_proxied_baskets(fund_isins, baskets, {})

        warnings = []
        for isin in fund_isins:
            source = source_for_fund_isin(isin)
            if source is not None and not source.look_through_eligible:
                continue  # excluded by design: no run of anything would clear it
            name = symbol_for_fund_isin(isin) or isin
            basket = baskets.get(isin)

            if basket is None:
                # `AUTOMATED_ADAPTERS` is imported from the fetcher CLI rather than
                # re-listed here, and that direction is deliberate: the set has to name
                # the fetchers that actually exist, which is a property of that module —
                # its own comment says so, and a copy here would go stale the first time
                # an adapter is added. Follow the proxy, so a fund whose basket is
                # borrowed is judged by whether the *source* can be fetched.
                route = source.adapter if source else None
                if route not in AUTOMATED_ADAPTERS:
                    proxy = basket_proxy_for(isin)
                    proxy_source = source_for_fund_isin(proxy) if proxy else None
                    route = proxy_source.adapter if proxy_source else None
                if route not in AUTOMATED_ADAPTERS:
                    # Nothing the reader could run fixes this — a hand download, or no
                    # published file at all. The look-through tab names it in the fund
                    # table, where it does not train anyone to ignore a banner.
                    continue
                warnings.append(
                    f"{name}: no constituent basket has ever been fetched, so its whole "
                    f"value is missing from the look-through's company rows. Run "
                    f"`python -m app.cli.fetch_etf_baskets --all` and the import line it "
                    f"prints, then `python -m app.cli.resolve_identities --constituents`"
                )
                continue

            limit = stale_after_days(basket.adapter)
            age = (as_of - basket.as_of_date).days
            if age <= limit:
                continue
            warnings.append(
                f"{name}: its basket is dated {basket.as_of_date:%Y-%m-%d} ({age} days "
                f"old against a {limit}-day expectation for {basket.adapter}), so every "
                f"company figure it contributes describes an older index. Re-run "
                f"`python -m app.cli.fetch_etf_baskets --all` plus the import lines, "
                f"then `python -m app.cli.resolve_identities --constituents` — a "
                f"re-import clears the CINS/SEDOL resolutions on purpose"
            )

        if warnings:
            logger.warning(f"{len(warnings)} held fund(s) with a missing or stale basket")
        return warnings

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
                currencies = held | currency_service.WARM_CURRENCIES

                logger.info(f"Syncing exchange rates for currencies: {sorted(currencies)}")

                today = date.today()

                warmed = await currency_service.warm_rates(
                    currencies, target_date=today, days_back=days_back
                )
                total_rates = warmed["frankfurter"] + warmed["fallback"]

                # Also keep EUR->base rates fresh for the selectable base currencies
                # (CHF/USD) so switching the display currency is instant.
                for base in ("CHF", "USD"):
                    try:
                        await currency_service._batch_fetch_rates(
                            from_currency="EUR",
                            target_date=today,
                            to_currency=base,
                            days_back=days_back
                        )
                        logger.info(f"Fetched EUR->{base} exchange rates")
                        total_rates += 1
                    except Exception as e:
                        logger.error(f"Failed to fetch EUR->{base} rates: {e}")

                await db.commit()

                result = {
                    "status": "success",
                    "currencies_synced": total_rates,
                    "currencies": sorted(currencies),
                    "held_currencies": sorted(held),
                    "timestamp": utc_iso(utcnow())
                }
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
        Sync last 7 days of benchmark prices for benchmarks already in the DB.
        Only syncs benchmarks the user has previously selected (i.e., have cached prices).
        """
        logger.info("Starting benchmark price sync...")

        async with AsyncSessionLocal() as db:
            try:
                # Find which benchmark tickers already have data in the DB
                result = await db.execute(
                    select(distinct(BenchmarkPrice.ticker))
                )
                existing_tickers = {row[0] for row in result.all()}

                if not existing_tickers:
                    logger.info("No benchmarks in DB to refresh")
                    return {"status": "success", "benchmarks_synced": 0}

                # Map tickers back to benchmark info for currency
                ticker_to_benchmark = {
                    info["ticker"]: info for info in BENCHMARKS.values()
                }

                bench_service = BenchmarkService(db)
                today = date.today()
                start = today - timedelta(days=7)
                synced = 0

                for ticker in existing_tickers:
                    bench_info = ticker_to_benchmark.get(ticker)
                    currency = bench_info["currency"] if bench_info else "USD"
                    try:
                        # No provisional refresh here: this loops every warm benchmark
                        # back to back with a 1-2s gap, and doing it at all seven
                        # market-data slots would multiply that burst by seven for a
                        # value nobody is reading. The chart's own lazy fetch refreshes
                        # the benchmark being viewed. See _ensure_prices_available.
                        count = await bench_service._ensure_prices_available(
                            ticker, start, today, currency=currency,
                            refresh_provisional=False,
                        )
                        if count > 0:
                            logger.info(f"Synced {count} benchmark prices for {ticker}")
                        synced += 1
                    except Exception as e:
                        # Rule 1: stop on a rate limit rather than working through the
                        # remaining benchmarks against an IP that has already refused
                        # us. This loop runs them back to back with only a 1-2s gap, so
                        # it is the fastest of the Yahoo loops at burning through a
                        # limit. A benchmark this pass skipped keeps its cached prices
                        # and the next slot refreshes it.
                        if is_rate_limit(e):
                            logger.warning(
                                f"Yahoo rate limit on benchmark {ticker}; "
                                f"abandoning the rest of the warm-up"
                            )
                            break
                        logger.error(f"Failed to sync benchmark {ticker}: {e}")

                await db.commit()
                logger.info(f"Benchmark price sync completed: {synced} benchmarks refreshed")
                return {"status": "success", "benchmarks_synced": synced}

            except Exception as e:
                await db.rollback()
                logger.error(f"Failed to sync benchmark prices: {e}", exc_info=True)
                return {"status": "error", "message": str(e)}

    async def sync_dividends(self) -> dict:
        """
        Sync dividend ex-dates from Yahoo Finance and compute EUR income.

        Independent of IBKR (yfinance-based); computes against existing tax lots.
        Cheap to run daily: DividendService applies a 7-day per-security staleness
        guard that skips already-fetched securities before any network call.

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
                return {
                    "status": "success",
                    "sync": sync_res,
                    "compute": compute_res,
                    "timestamp": utc_iso(utcnow())
                }

            except Exception as e:
                logger.error(f"Failed to sync dividends: {str(e)}", exc_info=True)
                return {
                    "status": "error",
                    "message": f"Failed to sync dividends: {str(e)}",
                    "timestamp": utc_iso(utcnow())
                }

    async def _gated_job(self, job_type: str, run) -> Optional[dict]:
        """
        Run one pipeline job under the shared sync-pipeline gate.

        A collision (e.g. a manual trigger already in flight at a scheduled slot)
        records itself as 'skipped' instead of running concurrently — two
        pipelines would race the Flex budget toward a Code=1025 lockout and
        double-hit Yahoo. The next scheduled slot recovers the freshness.

        `reason` distinguishes this skip from the other one a job can now report.
        `sync_ibkr_data` also returns `status="skipped"` when IBKR has already
        generated today's statement, and the two mean opposite things: this one is
        "come back shortly", that one is "there is nothing more to fetch today".
        `trigger_sync_now` keys on the reason, because answering 429 *busy* to a day
        that simply had nothing left to sync is a misleading error.
        """
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

    async def full_sync_job(self) -> Optional[dict]:
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
           must exist before this prices it.
        3. Dividend sync (Yahoo Finance ex-dates + EUR income)
        """
        return await self._gated_job("full_sync", self._full_sync_job_locked)

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
        logger.info("Syncing market data (730 days)...")
        market_result = await self.sync_market_data(days_back=730)
        logger.info(f"Market Data Sync Result: {market_result}")

        # Step 4: Sync dividends (always — yfinance-based, computes against existing tax lots)
        logger.info("Syncing dividends...")
        div_result = await self.sync_dividends()
        logger.info(f"Dividend Sync Result: {div_result}")

        # Track result
        self.last_sync_result = {
            "type": "full_sync",
            "timestamp": utc_iso(utcnow()),
            "ibkr_result": ibkr_result,
            "fx_result": fx_result,
            "market_result": market_result,
            "dividend_result": div_result,
            "status": ibkr_result.get("status", "error"),
        }
        # A skip is only useful in the history if it says which kind it was; `status`
        # alone reads the same as a pipeline collision.
        if ibkr_result.get("reason"):
            self.last_sync_result["reason"] = ibkr_result["reason"]
        _collect_warnings(self.last_sync_result, ibkr_result, market_result)
        await self._record_run(self.last_sync_result, started_at)

        logger.info("=" * 80)
        logger.info("FULL SYNC JOB COMPLETED")
        logger.info("=" * 80)
        return self.last_sync_result

    async def market_data_only_sync_job(self) -> Optional[dict]:
        """
        Market-data-only sync job, at each of MARKET_DATA_HOURS (Europe/Berlin).

        Only checks the last 7 days — lightweight, one Yahoo request per security. Most
        of those slots land *inside* a session, so the trailing rows it writes are
        mid-session prices that a later slot restates; that is by design and rests on
        MarketPriceRepository.PROVISIONAL_PRICE_DAYS. Also syncs exchange rates.
        """
        return await self._gated_job("market_data_only", self._market_data_only_locked)

    async def _market_data_only_locked(self) -> dict:
        logger.info("=" * 80)
        logger.info("STARTING MARKET DATA + EXCHANGE RATE SYNC (7 days)")
        logger.info("=" * 80)

        started_at = utcnow()

        # Sync exchange rates first (needed for portfolio value calculation)
        fx_result = await self.sync_exchange_rates(days_back=7)
        logger.info(f"Exchange Rate Sync Result: {fx_result}")

        market_result = await self.sync_market_data(days_back=7)
        logger.info(f"Market Data Sync Result: {market_result}")

        # Sync benchmark prices (only previously used benchmarks)
        bench_result = await self.sync_benchmark_prices()
        logger.info(f"Benchmark Price Sync Result: {bench_result}")

        # Invalidate recent benchmark timeline cache (prices updated for last 7 days)
        async with AsyncSessionLocal() as db:
            try:
                bench_service = BenchmarkService(db)
                cleared = await bench_service.clear_cache_recent_days(days=7)
                await db.commit()
                logger.info(f"Cleared {cleared} recent benchmark timeline cache entries")
            except Exception as e:
                logger.error(f"Failed to clear benchmark timeline cache: {e}")

        # Track result
        self.last_sync_result = {
            "type": "market_data_only",
            "timestamp": utc_iso(utcnow()),
            "fx_result": fx_result,
            "market_result": market_result,
            "benchmark_result": bench_result,
            "status": market_result.get("status", "error"),
        }
        _collect_warnings(self.last_sync_result, market_result)
        await self._record_run(self.last_sync_result, started_at)

        logger.info("=" * 80)
        logger.info("MARKET DATA ONLY SYNC COMPLETED")
        logger.info("=" * 80)
        return self.last_sync_result

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

    async def ibkr_only_sync_job(self) -> Optional[dict]:
        return await self._gated_job("ibkr_sync", self._ibkr_only_sync_locked)

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
        _collect_warnings(self.last_sync_result, ibkr_result)
        await self._record_run(self.last_sync_result, started_at)

        logger.info("=" * 80)
        logger.info("IBKR-ONLY SYNC COMPLETED")
        logger.info("=" * 80)
        return self.last_sync_result

    def _add_or_keep(self, job_id: str, func, trigger: CronTrigger, name: str) -> None:
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
        Start the scheduler with 8 daily syncs (Europe/Berlin):
        - 18:00: Full sync (IBKR + 730 days market data) — the day's one generation
        - 00:00: IBKR only — the recovery attempt, a no-op unless 18:00 failed
        - MARKET_DATA_HOURS: market data only (7 days), through both sessions

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
        # Market data runs seven times a day, and five of those are *inside* a
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
        #     08:00  full sync; settles yesterday's US close and Asia's
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
        for index, hour in enumerate(MARKET_DATA_HOURS, start=1):
            self._add_or_keep(
                f'market_sync_{index}', market_data_only_sync_job_entry,
                CronTrigger(hour=hour, minute=0, timezone='Europe/Berlin'),
                f'Market Data Sync ({hour:02d}:00 Europe/Berlin)',
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
    return await get_scheduler().full_sync_job()


async def ibkr_only_sync_job_entry() -> dict:
    return await get_scheduler().ibkr_only_sync_job()


async def market_data_only_sync_job_entry() -> dict:
    return await get_scheduler().market_data_only_sync_job()
