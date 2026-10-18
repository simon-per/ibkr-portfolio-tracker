import logging
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.repositories.fundamentals_repository import FundamentalsRepository, sync_scope
from app.repositories.sync_run_repository import utc_iso
from app.schemas.portfolio import (
    EarningsCalendarItem,
    EarningsHistoryItem,
    FundamentalMetricsResponse,
    FundamentalsStatus,
)
from app.services.fundamentals_service import FundamentalsService
from app.single_flight import (
    PUBLIC_SYNC_COOLDOWN_SECONDS,
    SYNC_PIPELINE,
    SyncBusy,
    cooldown_remaining,
    is_running,
    single_flight,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# A full pass is ~5 Yahoo calls per security across the whole portfolio, so a
# poller that waits for each pass to finish before starting the next runs them
# back to back indefinitely — is_running() only fences *overlapping* runs. Hence
# PUBLIC_SYNC_COOLDOWN_SECONDS, the same fence as /sync-stale and the other bulk
# Yahoo routes.


async def _run_sync_background(force_refresh: bool) -> None:
    """Run fundamentals sync in background with its own DB session."""
    try:
        # The handler already answered, so the gate lives here, spanning the
        # actual Yahoo work rather than the enqueue.
        with single_flight(SYNC_PIPELINE, cooldown_seconds=PUBLIC_SYNC_COOLDOWN_SECONDS):
            async with AsyncSessionLocal() as db:
                try:
                    service = FundamentalsService(db)
                    await service.sync_fundamentals_data(force_refresh=force_refresh)
                except Exception as e:
                    logger.error(f"Background fundamentals sync failed: {e}")
    except SyncBusy as e:
        logger.info(f"Fundamentals sync not started: {e}")


@router.post("/sync")
async def sync_fundamentals(
    background_tasks: BackgroundTasks,
    force_refresh: bool = Query(False, description="Force refresh all, even fresh data"),
):
    """Kick off a fundamentals sync in the background and return immediately."""
    if is_running(SYNC_PIPELINE):
        return {"status": "already_running", "message": "Fundamentals sync is already in progress"}
    # The gate below is what actually enforces this; checking it here too is so
    # the answer is honest rather than "started" for a run that gets dropped.
    retry_after = cooldown_remaining(SYNC_PIPELINE, PUBLIC_SYNC_COOLDOWN_SECONDS)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"A sync started moments ago; retry in ~{retry_after}s",
            headers={"Retry-After": str(retry_after)},
        )
    background_tasks.add_task(_run_sync_background, force_refresh)
    return {
        "status": "started",
        "message": "Sync running in background. Data will appear within a few minutes.",
        "securities_processed": 0,
        "metrics_updated": 0,
        "earnings_updated": 0,
        "errors": 0,
    }


@router.post("/sync-stale")
async def sync_stale_fundamentals(db: AsyncSession = Depends(get_db)):
    """Sync fundamentals that are stale or missing entirely — see the service."""
    try:
        with single_flight(SYNC_PIPELINE, cooldown_seconds=PUBLIC_SYNC_COOLDOWN_SECONDS):
            service = FundamentalsService(db)
            result = await service.sync_stale_fundamentals()
            return {"status": "success", **result}

    except SyncBusy as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": str(e.retry_after_seconds)})
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to sync stale fundamentals: {str(e)}")


@router.get("/portfolio", response_model=List[FundamentalMetricsResponse])
async def get_portfolio_fundamentals(db: AsyncSession = Depends(get_db)):
    """Get fundamental metrics for all portfolio securities."""
    service = FundamentalsService(db)
    return await service.get_fundamentals_for_portfolio()


@router.get("/earnings/upcoming", response_model=List[EarningsCalendarItem])
async def get_upcoming_earnings(
    days_ahead: int = Query(90, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Get upcoming earnings dates for portfolio securities."""
    service = FundamentalsService(db)
    return await service.get_earnings_calendar(days_ahead)


@router.get("/earnings/history", response_model=List[EarningsHistoryItem])
async def get_earnings_history(
    days_back: int = Query(365, ge=1, le=1825),
    db: AsyncSession = Depends(get_db),
):
    """Get past earnings with surprise data for portfolio securities."""
    service = FundamentalsService(db)
    return await service.get_earnings_history(days_back)


@router.get("/status", response_model=FundamentalsStatus)
async def get_fundamentals_status(db: AsyncSession = Depends(get_db)):
    """Get status of fundamentals data cache."""
    from sqlalchemy import select, func
    from app.models.fundamental_metrics import FundamentalMetrics
    from app.models.earnings_event import EarningsEvent
    from app.models.security import Security

    # The securities a sync walks (`sync_scope`: held and Yahoo-eligible), not every
    # security ever traded and not a holding Yahoo is never asked about: the tab says
    # "N holdings have no data yet" off these two counts and offers Update for them, and
    # a sold position or a `price_source` opt-out is something no sync will fill.
    in_scope = select(Security.id).where(sync_scope())
    total_result = await db.execute(select(func.count(Security.id)).where(sync_scope()))
    total_securities = total_result.scalar() or 0

    metrics_result = await db.execute(
        select(func.count(FundamentalMetrics.id)).where(
            FundamentalMetrics.security_id.in_(in_scope)
        )
    )
    total_metrics = metrics_result.scalar() or 0

    # Asked, and Yahoo had nothing usable (`fundamentals_checked_at`, issue #66): no row,
    # so not data — and not "no data yet" either, which offers an Update that the
    # back-off would answer with no request, an amber line that never went away.
    unavailable_result = await db.execute(
        select(func.count(Security.id)).where(
            sync_scope(),
            Security.fundamentals_checked_at.isnot(None),
            Security.id.not_in(select(FundamentalMetrics.security_id)),
        )
    )
    unavailable = unavailable_result.scalar() or 0

    # Through the repository, so this reports the same threshold a sync acts on. It
    # used to run its own 7-day query while the sync refreshed anything over a day old,
    # so `stale_metrics: 0` could sit beside a sync that would rewrite every row.
    stale_metrics = await FundamentalsRepository(db).count_stale_metrics()

    # Count earnings events
    earnings_result = await db.execute(select(func.count(EarningsEvent.id)))
    total_earnings = earnings_result.scalar() or 0

    # Update timestamps
    update_result = await db.execute(
        select(
            func.min(FundamentalMetrics.last_updated),
            func.max(FundamentalMetrics.last_updated),
        )
    )
    oldest_update, newest_update = update_result.one()

    return {
        "total_securities": total_securities,
        "securities_with_data": total_metrics,
        "securities_without_data": total_securities - total_metrics - unavailable,
        "securities_unavailable": unavailable,
        "stale_metrics": stale_metrics,
        "total_earnings_events": total_earnings,
        # Through `utc_iso`: these are naive UTC, and the tab compares `newest_update`
        # against the browser's clock to see a background sync finish. `str()` was read
        # as local time — two hours early in Berlin — so a finished sync went unseen.
        "oldest_update": utc_iso(oldest_update),
        "newest_update": utc_iso(newest_update),
        # The tab's other way to see a background sync end: a pass that writes no row
        # (all fresh, a 429 at the first security, refused as busy) never moves
        # `newest_update`. The gate is shared by every sync, so this reads "a pipeline
        # is running" — which is the honest answer, since a fundamentals pass queued
        # behind another sync is refused rather than waiting (`_run_sync_background`).
        "sync_running": is_running(SYNC_PIPELINE),
    }
