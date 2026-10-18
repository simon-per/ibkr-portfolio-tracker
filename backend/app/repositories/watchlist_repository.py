from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.watchlist_item import WatchlistItem


class WatchlistRepository:
    """Repository for WatchlistItem model operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(self) -> List[WatchlistItem]:
        result = await self.session.execute(
            select(WatchlistItem).order_by(WatchlistItem.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_id(self, item_id: int) -> Optional[WatchlistItem]:
        result = await self.session.execute(
            select(WatchlistItem).where(WatchlistItem.id == item_id)
        )
        return result.scalar_one_or_none()

    async def get_by_ticker(self, yahoo_ticker: str) -> Optional[WatchlistItem]:
        result = await self.session.execute(
            select(WatchlistItem).where(WatchlistItem.yahoo_ticker == yahoo_ticker)
        )
        return result.scalar_one_or_none()

    async def add(
        self,
        yahoo_ticker: str,
        notes: Optional[str] = None,
        target_price: Optional[float] = None,
    ) -> WatchlistItem:
        item = WatchlistItem(
            yahoo_ticker=yahoo_ticker,
            notes=notes,
            target_price=target_price,
        )
        self.session.add(item)
        await self.session.flush()
        await self.session.refresh(item)
        return item

    async def update_cached_data(self, item_id: int, data: dict) -> Optional[WatchlistItem]:
        item = await self.get_by_id(item_id)
        if not item:
            return None
        for key, value in data.items():
            if hasattr(item, key):
                setattr(item, key, value)
        await self.session.flush()
        await self.session.refresh(item)
        return item

    async def remove(self, item_id: int) -> bool:
        item = await self.get_by_id(item_id)
        if not item:
            return False
        await self.session.delete(item)
        await self.session.flush()
        return True
