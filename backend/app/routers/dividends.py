import logging
from datetime import datetime, timedelta
from app.clock import utcnow
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.portfolio import (
    DividendBreakdownResponse,
    DividendForecastHistoryResponse,
    DividendSummaryResponse,
)
from app.services.dividend_service import DividendService
from app.services.scheduler_service import get_scheduler
from app.single_flight import (
    PUBLIC_SYNC_COOLDOWN_SECONDS,
    SYNC_PIPELINE,
    SyncBusy,
    cooldown_remaining,
    single_flight,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_sync_in_progress = False

# A full pass is one Yahoo call per non-fresh security across the whole portfolio, so
# a poller that waits for `_sync_in_progress` to clear and POSTs again runs passes
# back to back indefinitely against an IP-based limit — the flag fences only
# *overlapping* runs. Hence PUBLIC_SYNC_COOLDOWN_SECONDS, the same fence as the
# fundamentals, ratings and watchlist routes; this was the one bulk Yahoo route
# without it.

# The summary enqueues a pass only while `needs_dividend_check` selects a security —
# the loop's own predicate — and no more often than this. The interval is a backstop
# (in-process, so a restart resets it): a pass stamps what it asked, so after one
# nothing is due until a holding is new, remapped, split, a month old
# (`DIVIDENDS_RECHECK_AFTER`), or was cut off by a rate limit.
_AUTO_SYNC_MIN_INTERVAL = timedelta(hours=6)
_last_auto_sync: Optional[datetime] = None


async def _run_dividend_sync_background() -> None:
    """
    Run the dividend pass in the background: `SchedulerService.dividend_sync_locked`.

    Holds the shared pipeline gate, not just the module flag: the 18:00 Berlin
    full_sync_job runs sync_dividends() too, and a dashboard load that finds a
    security due would otherwise start a second yfinance pass straight through it.

    The gate and the cooldown are all this adds (GitHub issue #65). It used to run
    sync-then-compute itself, dropping the `rate_limited` flag and the warnings
    `sync_dividends` hoists and recording no `sync_runs` row, so a pass Yahoo cut short
    left no trace; the scheduler's body records one (`dividend_sync`).
    """
    global _sync_in_progress
    try:
        with single_flight(SYNC_PIPELINE, cooldown_seconds=PUBLIC_SYNC_COOLDOWN_SECONDS):
            _sync_in_progress = True
            try:
                await get_scheduler().dividend_sync_locked()
            except Exception as e:
                logger.error(f"Background dividend sync failed: {e}")
            finally:
                _sync_in_progress = False
    except SyncBusy as e:
        # A scheduled sync owns the pipeline; the card refreshes after it lands.
        logger.info(f"Dividend sync skipped: {e}")


@router.get("/summary", response_model=DividendSummaryResponse)
async def get_dividend_summary(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Return dividend summary. Triggers a background sync while a held security is due a
    dividend check (`needs_dividend_check`), throttled.

    It used to trigger on the card's `last_updated`, the newest `last_computed` among
    the era-spliced rows — moved by a Flex re-delivery, never by the Yahoo pass — so on
    every day without a dividend in the Flex window each load past the throttle started
    another near-full pass, up to four a day (GitHub issue #6).
    """
    global _last_auto_sync

    service = DividendService(db)
    summary = await service.get_dividend_summary()
    summary["sync_in_progress"] = _sync_in_progress

    # Auto-refresh while something is due, throttled so we don't enqueue on every load.
    if not _sync_in_progress:
        now = utcnow()
        throttled = _last_auto_sync is not None and (now - _last_auto_sync) < _AUTO_SYNC_MIN_INTERVAL
        if not throttled and await service.pending_dividend_checks() > 0:
            _last_auto_sync = now
            background_tasks.add_task(_run_dividend_sync_background)
            summary["sync_in_progress"] = True

    return summary


@router.get("/breakdown", response_model=DividendBreakdownResponse)
async def get_dividend_breakdown(
    year: Optional[int] = Query(None, ge=2000, le=2100),
    forecast: bool = Query(True),
    period: Optional[Literal["24m"]] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Dividends grouped by month × symbol with per-security totals and optional
    cadence-based forecast projections. Reads only cached data — unlike /summary
    it never enqueues a sync, so it can never touch Yahoo.
    """
    if year is not None and period is not None:
        raise HTTPException(status_code=422, detail="Choose either year or period, not both")
    return await DividendService(db).get_dividend_breakdown(
        year=year, include_forecast=forecast, period=period
    )


@router.get("/forecast-history", response_model=DividendForecastHistoryResponse)
async def get_dividend_forecast_history(db: AsyncSession = Depends(get_db)):
    """
    The recorded daily history of the next-twelve-months forecast and the trailing
    twelve months beside it, oldest first. Reads one table and the cached exchange
    rates — like /breakdown it never enqueues a sync, so it can never touch Yahoo.
    """
    return await DividendService(db).get_forecast_history()


@router.post("/sync")
async def sync_dividends(background_tasks: BackgroundTasks):
    """Manual trigger for dividend sync."""
    if _sync_in_progress:
        return {"status": "already_running", "message": "Dividend sync is already in progress"}
    # The gate in the background task is what enforces this; checking here too is so
    # the answer is honest rather than "started" for a run the gate then drops.
    retry_after = cooldown_remaining(SYNC_PIPELINE, PUBLIC_SYNC_COOLDOWN_SECONDS)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"A sync started moments ago; retry in ~{retry_after}s",
            headers={"Retry-After": str(retry_after)},
        )
    background_tasks.add_task(_run_dividend_sync_background)
    return {"status": "started", "message": "Dividend sync started in background"}
