"""
Market Data Router
API endpoints for syncing and managing market price data.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redact import redact_secrets
from app.services.scheduler_service import get_scheduler
from app.single_flight import PUBLIC_SYNC_COOLDOWN_SECONDS, SYNC_PIPELINE, SyncBusy, single_flight

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/sync")
async def sync_market_data(
    # Bounded: this drives a no-await per-day Python loop per security, so an
    # unbounded value from the public internet would block the event loop.
    days_back: int = Query(730, ge=1, le=3650),
):
    """
    Sync historical market prices for all securities.

    Args:
        days_back: Number of days to look back (default 730 = 2 years)

    The body is `SchedulerService.sync_market_data`, the scheduled jobs' own price
    step: which securities a pass asks about (`MarketDataService.sync_securities`),
    the calendar's provisional rule (missing dates only on a Berlin non-trading day,
    GitHub issue #9), the status rule, the rate-limit warning and the five diagnostics.
    This route adds the shared pipeline gate and the public cooldown, and nothing else
    (GitHub issue #65). It used to carry its own copy of that body, which had drifted:
    it never passed `refresh_provisional`, so a weekend manual sync re-downloaded every
    cached close that could no longer change, and its rate-limit warning said something
    else.

    Returns:
        Summary of synced data including counts, errors and any warnings
    """
    try:
        # Shared pipeline gate: a full market sync is 50-150+ Yahoo requests, and
        # racing the scheduled jobs doubles that against an IP-based rate limit.
        with single_flight(SYNC_PIPELINE, cooldown_seconds=PUBLIC_SYNC_COOLDOWN_SECONDS):
            result = await get_scheduler().sync_market_data(days_back=days_back)
    except SyncBusy as e:
        raise HTTPException(
            status_code=429, detail=str(e),
            headers={"Retry-After": str(e.retry_after_seconds)},
        )
    # The step catches its own failure (rolled back) and reports it as a status; this
    # route has always answered that with a 500.
    if result.get("status") == "error":
        raise HTTPException(
            status_code=500,
            detail=redact_secrets(result.get("message") or "Failed to sync market data"),
        )
    return redact_secrets(result)


@router.get("/status")
async def get_market_data_status(db: AsyncSession = Depends(get_db)):
    """
    Get status of market data cache.

    Returns:
        Statistics about cached market prices
    """
    from app.repositories.market_price_repository import MarketPriceRepository

    price_repo = MarketPriceRepository(db)

    # Get total count and date range
    from sqlalchemy import select, func
    from app.models.market_price import MarketPrice

    result = await db.execute(
        select(
            func.count(MarketPrice.id),
            func.min(MarketPrice.date),
            func.max(MarketPrice.date)
        )
    )
    count, min_date, max_date = result.one()

    return {
        "total_prices": count,
        "earliest_date": str(min_date) if min_date else None,
        "latest_date": str(max_date) if max_date else None,
    }
