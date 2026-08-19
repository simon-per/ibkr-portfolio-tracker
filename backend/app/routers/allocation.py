"""
API endpoints for portfolio allocation data (sector, geography, asset type).
"""
from fastapi import APIRouter, Depends, HTTPException
from app.clock import utcnow
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.allocation_service import (
    ALLOCATION_STALE_DAYS,
    AllocationService,
    needs_allocation_refresh,
)
from app.single_flight import SYNC_PIPELINE, SyncBusy, single_flight


router = APIRouter()


@router.post("/sync")
async def sync_allocation_data(
    force_refresh: bool = False,
    db: AsyncSession = Depends(get_db)
):
    """
    Sync allocation data for all securities.
    Fetches sector and country information from yfinance with rate limiting.
    Uses cached data unless force_refresh=True or data is >7 days old.
    """
    try:
        # Public route, one Yahoo request per security: one at a time, cooled down.
        with single_flight(SYNC_PIPELINE, cooldown_seconds=300):
            service = AllocationService(db)
            result = await service.sync_allocation_data(force_refresh=force_refresh)
            return result
    except SyncBusy as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": str(e.retry_after_seconds)})


@router.get("/portfolio")
async def get_portfolio_allocation(db: AsyncSession = Depends(get_db)):
    """
    Get portfolio allocation breakdown by sector, geography, and asset type.
    Returns weighted percentages based on current market values.
    """
    service = AllocationService(db)
    allocation = await service.get_portfolio_allocation()
    return allocation


@router.get("/status")
async def get_allocation_status(db: AsyncSession = Depends(get_db)):
    """
    Get status of allocation data (how many securities have data, staleness, etc.)
    """
    from sqlalchemy import select, func, or_
    from app.models.security import Security
    from datetime import timedelta

    # Count securities with/without allocation data
    result = await db.execute(select(func.count(Security.id)))
    total_securities = result.scalar()

    # Counted by whether allocation data is actually present, not by whether the
    # timestamp is set. The timestamp records the last *attempt* — it has to, or a
    # security Yahoo has no data for is re-fetched on every sync forever — so keying
    # the banner on it would report a security as covered the moment we gave up on
    # it. An ETF legitimately has no sector (it has many), so it counts as covered
    # once its asset type is known.
    has_allocation = or_(
        Security.sector.isnot(None),
        Security.country.isnot(None),
        Security.asset_type == 'ETF',
    )

    result = await db.execute(select(func.count(Security.id)).where(has_allocation))
    securities_with_data = result.scalar()

    # Counted through the sync's own predicate, not a re-derived one.
    #
    # This used to be `allocation_last_updated < cutoff` — which never matches NULL, so
    # every security that has *never* been attempted was invisible to it. Nothing
    # schedules `sync_allocation_data`, so on a fresh install that is all of them: the
    # endpoint answered `stale_securities: 0` while `POST /api/allocation/sync` was
    # about to issue a Yahoo request per security. The comment here claimed it used "the
    # same threshold the sync selects on", and it did — the threshold was shared and the
    # predicate was not, which is the same sentence CLAUDE.md already records for
    # `stale_metrics: 0` and the same tell: a comment asserting alignment.
    cutoff_date = utcnow() - timedelta(days=ALLOCATION_STALE_DAYS)
    result = await db.execute(
        select(func.count(Security.id)).where(needs_allocation_refresh(cutoff_date))
    )
    stale_securities = result.scalar()

    # Get oldest and newest update times
    result = await db.execute(
        select(func.min(Security.allocation_last_updated))
        .where(Security.allocation_last_updated.isnot(None))
    )
    oldest_update = result.scalar()

    result = await db.execute(
        select(func.max(Security.allocation_last_updated))
        .where(Security.allocation_last_updated.isnot(None))
    )
    newest_update = result.scalar()

    return {
        'total_securities': total_securities or 0,
        'securities_with_data': securities_with_data or 0,
        'securities_without_data': (total_securities or 0) - (securities_with_data or 0),
        'stale_securities': stale_securities or 0,
        'oldest_update': oldest_update.isoformat() if oldest_update else None,
        'newest_update': newest_update.isoformat() if newest_update else None,
    }
