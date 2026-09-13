"""
`/api/performance/*` — where the return came from.

Pure database reads: none of these can reach Yahoo or IBKR. The window guards mirror
`/api/portfolio/value-over-time`, including the 5-year ceiling the frontend's
`MAX_RANGE_DAYS` is pinned to.
"""
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.performance import (
    ClosedPositionsResponse,
    ReturnDecompositionResponse,
    SegmentAttributionResponse,
)
from app.services.performance_analytics_service import PerformanceAnalyticsService

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_DAYS = 365 * 5


def _window(start_date: date, end_date: date) -> None:
    if start_date >= end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date")
    if (end_date - start_date).days > MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large. Maximum allowed is {MAX_DAYS} days (5 years)",
        )


@router.get("/decomposition", response_model=ReturnDecompositionResponse)
async def get_return_decomposition(
    start_date: date = Query(..., description="Window start (the value on this day is the base)"),
    end_date: date = Query(..., description="Window end"),
    db: AsyncSession = Depends(get_db),
):
    """
    The change in Total Value over the window split into flows, price, FX, dividends,
    fees/interest and an explicit remainder — for the window and per calendar year.
    """
    _window(start_date, end_date)
    return await PerformanceAnalyticsService(db).decomposition(start_date, end_date)


@router.get("/segments", response_model=SegmentAttributionResponse)
async def get_segment_attribution(
    start_date: date = Query(...),
    end_date: date = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Per-security gains folded onto sectors and countries through the look-through."""
    _window(start_date, end_date)
    return await PerformanceAnalyticsService(db).segments(start_date, end_date)


@router.get("/closed-positions", response_model=ClosedPositionsResponse)
async def get_closed_positions(db: AsyncSession = Depends(get_db)):
    """Realized outcome, holding period and post-sale move of every sold position."""
    return await PerformanceAnalyticsService(db).closed_positions()
