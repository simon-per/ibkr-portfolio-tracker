"""
AnalystRating Repository
Handles database operations for analyst ratings.
"""
from typing import List, Optional
from datetime import datetime, timedelta
from app.clock import utcnow
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyst_rating import AnalystRating
from app.models.security import Security
from app.repositories.fundamentals_repository import sync_scope
from app.services.miss_backoff import miss_backoff_until

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


def ratings_backoff_until(security: Security) -> Optional[datetime]:
    """
    When the ratings pass may next ask about `security`, or None when it is not backing
    off: the price loop's rule (`miss_backoff`), on its own pair of columns.

    A fetch that failed with something not a rate limit used to leave the security
    unstamped, "so the next pass asks again" — every pass, for good, about a ticker Yahoo
    cannot serve. No held exemption: every security this pass walks is held, and a rating
    a few days old costs nothing.
    """
    return miss_backoff_until(security.ratings_checked_at, security.ratings_miss_count)


def ratings_backing_off(security: Security, now: datetime) -> bool:
    """True while `security` waits out a failed ratings fetch. The one predicate the
    sync's work list and `/status`'s stale count both apply."""
    until = ratings_backoff_until(security)
    return until is not None and now < until


def record_ratings_miss(security: Security, now: datetime) -> None:
    """The fetch failed with something not a rate limit."""
    security.ratings_miss_count = (security.ratings_miss_count or 0) + 1
    security.ratings_checked_at = now


def clear_ratings_backoff(security: Security) -> None:
    """An answer was stored (ratings, or none) or the mapping changed."""
    security.ratings_checked_at = None
    security.ratings_miss_count = None


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
        """The rows a sync would refresh (`_due`)."""
        return await self._due(older_than)

    async def count_stale_ratings(self, older_than: timedelta = STALE_AFTER) -> int:
        """
        How many rows a sync would refresh. `/status` counts through this so it cannot
        answer with a different query than the sync asks — off the same `_due` rows, so
        the two cannot part over a security backing off.
        """
        return len(await self._due(older_than))

    async def _due(self, older_than: timedelta) -> List[AnalystRating]:
        """
        `_stale` rows, less a security waiting out a failed fetch (`ratings_backing_off`).
        The back-off is computed per security, so it is applied after the query, as
        `fundamentals_repository` does, rather than restated in SQL.
        """
        now = utcnow()
        result = await self.session.execute(
            select(AnalystRating, Security)
            .join(Security, Security.id == AnalystRating.security_id)
            .where(self._stale(older_than))
        )
        return [
            rating for rating, security in result.all()
            if not ratings_backing_off(security, now)
        ]

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
