import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.repositories.fundamentals_repository import FundamentalsRepository
from app.repositories.security_repository import held
from app.repositories.sync_run_repository import utc_iso
from app.services.fundamentals_service import FundamentalsService
from app.single_flight import (
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
# back to back indefinitely — is_running() only fences *overlapping* runs. Same
# 300s as /sync-stale and the other bulk Yahoo routes.
SYNC_COOLDOWN_SECONDS = 300


async def _run_sync_background(force_refresh: bool) -> None:
    """Run fundamentals sync in background with its own DB session."""
    try:
        # The handler already answered, so the gate lives here, spanning the
        # actual Yahoo work rather than the enqueue.
        with single_flight(SYNC_PIPELINE, cooldown_seconds=SYNC_COOLDOWN_SECONDS):
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
    retry_after = cooldown_remaining(SYNC_PIPELINE, SYNC_COOLDOWN_SECONDS)
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
        with single_flight(SYNC_PIPELINE, cooldown_seconds=300):
            service = FundamentalsService(db)
            result = await service.sync_stale_fundamentals()
            return {"status": "success", **result}

    except SyncBusy as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": str(e.retry_after_seconds)})
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to sync stale fundamentals: {str(e)}")


@router.get("/portfolio")
async def get_portfolio_fundamentals(db: AsyncSession = Depends(get_db)):
    """Get fundamental metrics for all portfolio securities."""
    service = FundamentalsService(db)
    return await service.get_fundamentals_for_portfolio()


@router.get("/earnings/upcoming")
async def get_upcoming_earnings(
    days_ahead: int = Query(90, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Get upcoming earnings dates for portfolio securities."""
    service = FundamentalsService(db)
    return await service.get_earnings_calendar(days_ahead)


@router.get("/earnings/history")
async def get_earnings_history(
    days_back: int = Query(365, ge=1, le=1825),
    db: AsyncSession = Depends(get_db),
):
    """Get past earnings with surprise data for portfolio securities."""
    service = FundamentalsService(db)
    return await service.get_earnings_history(days_back)


@router.get("/status")
async def get_fundamentals_status(db: AsyncSession = Depends(get_db)):
    """Get status of fundamentals data cache."""
    from sqlalchemy import select, func
    from app.models.fundamental_metrics import FundamentalMetrics
    from app.models.earnings_event import EarningsEvent
    from app.models.security import Security

    # Holdings, not every security ever traded: the tab says "N holdings have no data
    # yet" off these two counts, and a position sold long ago is not a holding. Same
    # predicate as the table the tab draws (`security_repository.held`).
    total_result = await db.execute(
        select(func.count(Security.id)).where(held())
    )
    total_securities = total_result.scalar() or 0

    metrics_result = await db.execute(
        select(func.count(FundamentalMetrics.id)).where(
            FundamentalMetrics.security_id.in_(select(Security.id).where(held()))
        )
    )
    total_metrics = metrics_result.scalar() or 0

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
        "securities_without_data": total_securities - total_metrics,
        "stale_metrics": stale_metrics,
        "total_earnings_events": total_earnings,
        # Through `utc_iso`: these are naive UTC, and the tab compares `newest_update`
        # against the browser's clock to see a background sync finish. `str()` was read
        # as local time — two hours early in Berlin — so a finished sync went unseen.
        "oldest_update": utc_iso(oldest_update),
        "newest_update": utc_iso(newest_update),
    }
