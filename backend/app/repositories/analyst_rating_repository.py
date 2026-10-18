"""
AnalystRating Repository
Handles database operations for analyst ratings.
"""
from typing import List, Optional
from datetime import timedelta
from app.clock import utcnow
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyst_rating import AnalystRating
from app.models.security import Security
from app.repositories.fundamentals_repository import sync_scope

#: How old a rating row has to be before a sync will refresh it.
#:
#: One number, because there were four copies of "3 days": this method's default, the
#: service wrapper's default, the literal `sync_stale_ratings` passed, and
#: `/api/analyst-ratings/status`'s own query — which also counted every row while the
#: sync walks only `sync_scope()`, so a sold position's rating stayed "stale" forever.
#: Three days is the value all four copies agreed on, kept as is. It is not
#: `fundamentals_repository.STALE_AFTER` although both passes run in the same daily
#: `fundamentals_refresh` job, so a row written by one pass is re-asked only at the
#: pass three or more days later. Whether it should follow fundamentals is the
#: owner's call (issue #21); either way it changes here and nowhere else.
STALE_AFTER = timedelta(days=3)


class AnalystRatingRepository:
    """Repository for AnalystRating model operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_security_id(self, security_id: int) -> Optional[AnalystRating]:
        """Get analyst rating for a specific security"""
        result = await self.session.execute(
            select(AnalystRating).where(AnalystRating.security_id == security_id)
        )
        return result.scalar_one_or_none()

    async def get_all(self) -> List[AnalystRating]:
        """Get all analyst ratings"""
        result = await self.session.execute(select(AnalystRating))
        return list(result.scalars().all())

    async def create(self, rating_data: dict) -> AnalystRating:
        """Create a new analyst rating record"""
        rating = AnalystRating(**rating_data)
        self.session.add(rating)
        await self.session.flush()
        await self.session.refresh(rating)
        return rating

    async def upsert(self, rating_data: dict) -> AnalystRating:
        """
        Insert or update analyst rating.
        Uses security_id as unique key (one rating per security).
        """
        existing = await self.get_by_security_id(rating_data['security_id'])

        if existing:
            # Update existing
            for key, value in rating_data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            # Always update last_updated timestamp
            existing.last_updated = utcnow()
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        else:
            # Create new
            rating_data['last_updated'] = utcnow()
            return await self.create(rating_data)

    async def bulk_upsert(self, ratings_data: List[dict]) -> int:
        """
        Bulk insert/update analyst ratings.
        Returns count of records created or updated.
        """
        count = 0
        for rating_data in ratings_data:
            await self.upsert(rating_data)
            count += 1
        return count

    async def get_stale_ratings(
        self, older_than: timedelta = STALE_AFTER
    ) -> List[AnalystRating]:
        """The rows a sync would refresh (`_stale`)."""
        result = await self.session.execute(
            select(AnalystRating).where(self._stale(older_than))
        )
        return list(result.scalars().all())

    async def count_stale_ratings(self, older_than: timedelta = STALE_AFTER) -> int:
        """
        How many rows a sync would refresh. `/status` counts through this so it cannot
        answer with a different query than the sync asks.
        """
        result = await self.session.execute(
            select(func.count(AnalystRating.id)).where(self._stale(older_than))
        )
        return int(result.scalar() or 0)

    @staticmethod
    def in_scope():
        """Rows of a security the ratings sync walks (`sync_scope`)."""
        return AnalystRating.security_id.in_(select(Security.id).where(sync_scope()))

    @staticmethod
    def _stale(older_than: timedelta):
        """
        A row a sync would refresh: older than `older_than`, of a security the sync walks.
        Without the scope a sold position's or a `price_source` opt-out's row was counted
        stale forever, since nothing refreshes it — the defect fundamentals had first.
        """
        return and_(
            AnalystRating.last_updated < utcnow() - older_than,
            AnalystRatingRepository.in_scope(),
        )
