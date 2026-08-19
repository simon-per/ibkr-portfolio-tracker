from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.peg_ratio import peg_from_growth
from app.services.safe_numbers import safe_float
from app.services.watchlist_service import WatchlistService
from app.single_flight import SYNC_PIPELINE, SyncBusy, single_flight
from app.repositories.watchlist_repository import WatchlistRepository
from app.schemas.portfolio import (
    WatchlistItemResponse,
    AddWatchlistItemRequest,
    UpdateWatchlistItemRequest,
)

router = APIRouter()


@router.get("", response_model=list[WatchlistItemResponse])
async def get_watchlist(db: AsyncSession = Depends(get_db)):
    """Get all watchlist items."""
    service = WatchlistService(db)
    items = await service.get_all_items()
    return [_item_to_response(item) for item in items]


@router.post("", response_model=WatchlistItemResponse, status_code=201)
async def add_to_watchlist(
    request: AddWatchlistItemRequest,
    db: AsyncSession = Depends(get_db),
):
    """Add a stock to the watchlist by Yahoo Finance ticker."""
    repo = WatchlistRepository(db)

    existing = await repo.get_by_ticker(request.yahoo_ticker.upper())
    if existing:
        raise HTTPException(status_code=409, detail=f"{request.yahoo_ticker} is already on the watchlist")

    item = await repo.add(
        yahoo_ticker=request.yahoo_ticker.upper(),
        notes=request.notes,
        target_price=request.target_price,
    )
    await db.commit()

    # Immediately sync data — through the shared gate: this is a publicly reachable
    # Yahoo trigger, and without it rapid-fire adds of distinct tickers are exactly
    # the unthrottled fetch storm the gate exists to stop. Busy or cooling: the add
    # itself still succeeds, last_synced stays null, and the next sync fills it in.
    try:
        with single_flight(SYNC_PIPELINE, cooldown_seconds=60):
            service = WatchlistService(db)
            await service.sync_item(item.yahoo_ticker, force=True)
    except SyncBusy:
        pass

    # Re-fetch with cached data
    item = await repo.get_by_id(item.id)
    return _item_to_response(item)


@router.patch("/{item_id}", response_model=WatchlistItemResponse)
async def update_watchlist_item(
    item_id: int,
    request: UpdateWatchlistItemRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update notes and/or target price for a watchlist item."""
    repo = WatchlistRepository(db)
    item = await repo.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")

    update_kwargs = {}
    if request.notes is not None:
        update_kwargs["notes"] = request.notes
    if request.target_price is not None:
        update_kwargs["target_price"] = request.target_price

    if update_kwargs:
        # Direct attribute update for simplicity
        if "notes" in update_kwargs:
            item.notes = update_kwargs["notes"]
        if "target_price" in update_kwargs:
            item.target_price = update_kwargs["target_price"]
        await db.flush()
        await db.commit()
        await db.refresh(item)

    return _item_to_response(item)


@router.delete("/{item_id}")
async def remove_from_watchlist(
    item_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Remove a stock from the watchlist."""
    repo = WatchlistRepository(db)
    removed = await repo.remove(item_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    await db.commit()
    return {"message": "Removed from watchlist"}


@router.post("/sync")
async def sync_watchlist(
    force: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Force refresh all watchlist items."""
    try:
        with single_flight(SYNC_PIPELINE, cooldown_seconds=300):
            service = WatchlistService(db)
            result = await service.sync_all(force=force)
            return result
    except SyncBusy as e:
        raise HTTPException(status_code=429, detail=str(e),
                            headers={"Retry-After": str(e.retry_after_seconds)})


def _compute_peg(item):
    """
    Derive PEG at read time when the column is NULL, through the shared helper.

    This used to be a fourth hand-written PEG — after the watchlist's and the
    fundamentals service's copies were extracted into `peg_ratio.py` precisely to stop
    that, with the router left behind. The arithmetic was a faithful copy of the
    forward-EPS tier (x100, round to 4) and the *guard* was not: `if item.trailing_pe`
    is a truthiness test, so a **negative** P/E passed it where `peg_from_growth`
    refuses `trailing_pe <= 0` on the stated grounds that a loss-making company has no
    meaningful PEG.

    A loss-making growth name with `trailing_pe = -15.0` and 50% forward EPS growth
    therefore printed **-0.30** in the PEG column — a number that, on a PEG scale, reads
    as spectacularly cheap. And the NULL column that makes this function fire at all is
    exactly the state such a company is in, because both fallbacks in the sync path
    correctly refused it. `/api/fundamentals/portfolio` showed blank for the same
    company at the same moment.

    `is_fraction=True` because `fwd_eps_growth` is stored as a decimal, matching the
    forward-EPS tier the copy came from — the tier whose inference bug turned 222%
    growth into 2.2245% and a PEG of 0.25 into 24.56.
    """
    if item.peg_ratio is not None:
        return item.peg_ratio
    # Through `safe_float` for the same reason the sync path is: it rounds to 4dp, and
    # a derived PEG that rounds differently from a stored one is the `_safe_float`
    # divergence CLAUDE.md records — the same ratio reading differently on two screens.
    return safe_float(
        peg_from_growth(item.trailing_pe, item.fwd_eps_growth, is_fraction=True)
    )


def _item_to_response(item) -> WatchlistItemResponse:
    return WatchlistItemResponse(
        id=item.id,
        yahoo_ticker=item.yahoo_ticker,
        symbol=item.symbol,
        company_name=item.company_name,
        notes=item.notes,
        target_price=item.target_price,
        current_price=item.current_price,
        currency=item.currency,
        trailing_pe=item.trailing_pe,
        revenue_growth=item.revenue_growth,
        earnings_growth=item.earnings_growth,
        fwd_revenue_growth=item.fwd_revenue_growth,
        fwd_eps_growth=item.fwd_eps_growth,
        profit_margins=item.profit_margins,
        market_cap=item.market_cap,
        week52_high=item.week52_high,
        week52_low=item.week52_low,
        pct_from_52w_high=item.pct_from_52w_high,
        ma200=item.ma200,
        ma50=item.ma50,
        pct_from_ma200=item.pct_from_ma200,
        rsi14=item.rsi14,
        data_currency=item.data_currency,
        forward_pe=item.forward_pe,
        peg_ratio=_compute_peg(item),
        ev_to_ebitda=item.ev_to_ebitda,
        analyst_target=item.analyst_target,
        analyst_rating=item.analyst_rating,
        analyst_count=item.analyst_count,
        buy_score=item.buy_score,
        last_synced=item.last_synced.isoformat() if item.last_synced else None,
        created_at=item.created_at.isoformat() if item.created_at else None,
    )
