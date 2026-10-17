"""
MarketPrice Repository
Handles database operations for historical market prices.
"""
from typing import Collection, List, Optional
from datetime import date, timedelta
from sqlalchemy import select, and_, delete, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from decimal import Decimal

from app.models.market_price import MarketPrice


class MarketPriceRepository:
    """Repository for MarketPrice model operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_security_and_date(
        self, security_id: int, target_date: date
    ) -> Optional[MarketPrice]:
        """Get market price for a specific security and date"""
        result = await self.session.execute(
            select(MarketPrice).where(
                and_(
                    MarketPrice.security_id == security_id,
                    MarketPrice.date == target_date
                )
            )
        )
        return result.scalar_one_or_none()

    async def get_latest_price(self, security_id: int) -> Optional[MarketPrice]:
        """Get the most recent market price for a security"""
        result = await self.session.execute(
            select(MarketPrice)
            .where(MarketPrice.security_id == security_id)
            .order_by(MarketPrice.date.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_price_range(
        self, security_id: int, start_date: date, end_date: date
    ) -> List[MarketPrice]:
        """Get all prices for a security within a date range"""
        result = await self.session.execute(
            select(MarketPrice)
            .where(
                and_(
                    MarketPrice.security_id == security_id,
                    MarketPrice.date >= start_date,
                    MarketPrice.date <= end_date
                )
            )
            .order_by(MarketPrice.date.asc())
        )
        return list(result.scalars().all())

    # An interior weekday hole that stayed empty this long is a market holiday
    # (the exchange never produced a close) — every daily sync since then has
    # already failed to fill it. Younger holes stay "missing" so late data can
    # still arrive.
    HOLIDAY_GRACE_DAYS = 30

    # How far back a *cached* close is still treated as provisional and re-fetched.
    #
    # Yahoo answers a daily-interval request with a bar for the session in progress,
    # whose `Close` is just the last trade so far. Because this method only ever
    # returned dates with no row at all, **the first sync of the day won that date
    # forever**: production stored every European close as the 15:00 Berlin
    # mid-session price, 2.5 hours before Xetra and Euronext close at 17:30, and
    # Korea's alternated between mid-session and final depending on whether the
    # 08:00 or the 15:00 job happened to land the row first.
    #
    # That makes the freeze a *precondition* for intraday syncing rather than a
    # separate cleanup: adding an earlier slot without this would have frozen an
    # even earlier price. So a recent date is re-fetched even when cached, and
    # `bulk_create`'s ON CONFLICT DO UPDATE lets the settled close overwrite the
    # provisional one.
    #
    # Three days is what it takes to cover the two cases where "today" is not
    # enough: the 22:00 Berlin slot writes a US close within seconds of the bell
    # and only the next morning settles it, and Friday's close must still be
    # refreshable on Monday. It costs no extra Yahoo *requests* — one per security
    # either way — only a slightly wider range on a request already being made.
    PROVISIONAL_PRICE_DAYS = 3

    async def get_missing_dates(
        self, security_id: int, start_date: date, end_date: date,
        as_of: Optional[date] = None,
        sources: Optional[Collection[str]] = None,
        refresh_provisional: bool = True,
    ) -> List[date]:
        """
        Dates in range that need fetching: no cached price, or a provisional one.

        Weekends are never missing, and neither is an old INTERIOR weekday hole —
        cached data on both sides plus HOLIDAY_GRACE_DAYS of failed refills means
        a market holiday (July 4th, Good Friday …), and treating those as missing
        made every sync re-request the security's whole range from Yahoo forever.
        Leading and trailing gaps stay missing on purpose: that is what a new
        security, a split purge, or a growing range look like.

        A date within PROVISIONAL_PRICE_DAYS of ``as_of`` is returned **even when
        cached**, because a row written during a live session holds a mid-session
        price rather than a close. The two rules cannot collide: the holiday rule
        needs a date older than 30 days and this one needs it newer than 3.
        ``refresh_provisional=False`` drops this rule — the weekend full sync, when no
        cached close can still change (GitHub issue #9).

        ``sources`` narrows which rows count as cached, for a security whose rows
        do not all come from the feed being asked. A sibling-priced fund's statement
        NAVs sit weeks apart, and counted as "cached data on both sides" they turned
        every day between two of them into a holiday after 30 days — days no fetch
        had ever been tried for, never asked for again (found 2026-10-12).

        ``as_of`` is injectable purely for tests.
        """
        as_of = as_of or date.today()

        # Get all existing dates in the range
        result = await self.session.execute(
            select(MarketPrice.date)
            .where(
                and_(
                    MarketPrice.security_id == security_id,
                    MarketPrice.date >= start_date,
                    MarketPrice.date <= end_date,
                    *([MarketPrice.source.in_(sources)] if sources is not None else []),
                )
            )
        )
        existing_dates = set(row[0] for row in result.all())
        first_cached = min(existing_dates) if existing_dates else None
        last_cached = max(existing_dates) if existing_dates else None
        holiday_cutoff = as_of - timedelta(days=self.HOLIDAY_GRACE_DAYS)
        provisional_from = as_of - timedelta(days=self.PROVISIONAL_PRICE_DAYS)

        # Generate all dates in range (excluding weekends for market data)
        missing_dates = []
        current_date = start_date
        while current_date <= end_date:
            # Skip weekends (Saturday=5, Sunday=6)
            if current_date.weekday() < 5:
                if current_date not in existing_dates:
                    interior = (
                        first_cached is not None
                        and first_cached < current_date < last_cached
                    )
                    if not (interior and current_date < holiday_cutoff):
                        missing_dates.append(current_date)
                elif refresh_provisional and current_date >= provisional_from:
                    # Cached, but recent enough that the row may be a mid-session
                    # price. Re-fetch so the settled close replaces it.
                    missing_dates.append(current_date)
            current_date += timedelta(days=1)

        return missing_dates

    async def create(self, price_data: dict) -> MarketPrice:
        """Create a new market price record"""
        market_price = MarketPrice(**price_data)
        self.session.add(market_price)
        await self.session.flush()
        await self.session.refresh(market_price)
        return market_price

    async def upsert(self, price_data: dict) -> MarketPrice:
        """
        Insert or update market price.
        Uses security_id + date as unique key.
        """
        existing = await self.get_by_security_and_date(
            price_data['security_id'],
            price_data['date']
        )

        if existing:
            # Update existing
            for key, value in price_data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        else:
            # Create new
            return await self.create(price_data)

    # Chunked so the statement stays well inside SQLite's variable limit
    # (SQLITE_MAX_VARIABLE_NUMBER, 999 on older builds) — a price row binds ~6
    # parameters, so 100 rows is ~600.
    UPSERT_CHUNK = 100

    async def bulk_create(self, prices_data: List[dict]) -> int:
        """
        Insert or update many prices in one statement per chunk.

        Was a loop over ``upsert()``, which costs a SELECT + flush + refresh *per
        row* — three round trips each, despite the (security_id, date) unique
        constraint making SQLite's ON CONFLICT DO UPDATE available. A split purge
        refill is ~500 rows (~1,500 statements) while holding the sync gate, and a
        cold 40-security sync is ~20,000 rows (~60,000 statements).

        ``upsert()`` stays for single-row callers that want the ORM object back.
        Returns the number of rows submitted, matching the previous contract (it
        counted rows processed, not rows newly inserted).
        """
        if not prices_data:
            return 0

        count = 0
        for start in range(0, len(prices_data), self.UPSERT_CHUNK):
            chunk = prices_data[start:start + self.UPSERT_CHUNK]
            stmt = sqlite_insert(MarketPrice).values(chunk)
            # Update every supplied column except the identity itself, so a
            # re-fetch corrects a restated close (a split) or a wrong currency.
            updatable = {
                c.name: getattr(stmt.excluded, c.name)
                for c in MarketPrice.__table__.columns
                if c.name not in ("id", "security_id", "date")
                and c.name in chunk[0]
            }
            if updatable:
                stmt = stmt.on_conflict_do_update(
                    index_elements=["security_id", "date"], set_=updatable
                )
            else:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["security_id", "date"]
                )
            await self.session.execute(stmt)
            count += len(chunk)

        # Deliberately no expire_all() here. This writes behind the ORM's back, so
        # the identity map can hold stale MarketPrice rows — but both callers commit
        # immediately, and the production session is expire_on_commit=True, which
        # refreshes them. Expiring here instead breaks a session that opted out of
        # that (expire_on_commit=False): the next attribute access on *any* loaded
        # object becomes a lazy refresh, which in async raises MissingGreenlet.
        return count

    async def delete_up_to(self, security_id: int, cutoff_date: date) -> int:
        """
        Drop cached prices for a security on or before ``cutoff_date``.

        Used to invalidate a price history a corporate action has restated. The rows
        simply become "missing dates" again, and because fetching is range-based the
        whole span is rebuilt by a single request on the next market-data sync.

        Returns the number of rows removed.
        """
        result = await self.session.execute(
            delete(MarketPrice).where(
                and_(
                    MarketPrice.security_id == security_id,
                    MarketPrice.date <= cutoff_date
                )
            )
        )
        await self.session.flush()
        return int(result.rowcount or 0)

    async def delete_all_for_security(self, security_id: int) -> int:
        """
        Drop every cached price for a security.

        For when the prices themselves are wrong rather than merely stale — a mapping
        that pointed at the wrong instrument, which is only repaired by removing what it
        produced. The dates become "missing" again and a scheduled job refills them, so
        this costs no ad-hoc Yahoo call.

        Returns the number of rows removed.
        """
        result = await self.session.execute(
            delete(MarketPrice).where(MarketPrice.security_id == security_id)
        )
        await self.session.flush()
        return int(result.rowcount or 0)

    async def get_date_range_for_security(
        self, security_id: int
    ) -> Optional[tuple[date, date]]:
        """Get the earliest and latest dates we have data for a security"""
        result = await self.session.execute(
            select(
                func.min(MarketPrice.date),
                func.max(MarketPrice.date)
            ).where(MarketPrice.security_id == security_id)
        )
        row = result.one()
        if row[0] and row[1]:
            return (row[0], row[1])
        return None

    async def count_by_security(self, security_id: int) -> int:
        """Count how many price records exist for a security"""
        result = await self.session.execute(
            select(func.count(MarketPrice.id))
            .where(MarketPrice.security_id == security_id)
        )
        return result.scalar() or 0
