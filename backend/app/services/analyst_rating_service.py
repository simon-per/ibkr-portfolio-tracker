"""
Analyst Rating Service
Fetches analyst recommendations from Yahoo Finance using yfinance library.
Updates ratings twice weekly to avoid excessive API calls.
"""
from typing import List, Dict, Optional
import logging
import yfinance as yf
import random
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.repositories.analyst_rating_repository import AnalystRatingRepository
from app.models.security import Security
from app.models.analyst_rating import AnalystRating
from app.services.yahoo_rate_limit import is_rate_limit

logger = logging.getLogger(__name__)

#: yfinance labels the current month's recommendation snapshot ``0m``, with
#: ``-1m``/``-2m``/``-3m`` behind it.
CURRENT_PERIOD = '0m'


def select_current_period(recommendations):
    """
    The current-month row, chosen by its ``period`` label rather than by position.

    This used to be ``recommendations.iloc[0]`` under a comment asserting row 0 *is*
    ``0m``. That is provider ordering taken on trust, and it is the same shape as
    the `openDateTime` bug CLAUDE.md tells you not to reintroduce: data matched back
    by index position instead of by the field that identifies it. If yfinance ever
    reorders the frame, positional access silently reports a three-month-old
    consensus as current — a plausible number, so nothing looks wrong.

    Falls back to the first row when there is no ``period`` column at all, since a
    frame shaped differently is still better read than discarded; but a frame that
    *has* the column and lacks ``0m`` yields None rather than a stale row.
    """
    if recommendations is None or getattr(recommendations, 'empty', True):
        return None

    if 'period' not in getattr(recommendations, 'columns', []):
        logger.debug("recommendations frame has no 'period' column; using the first row")
        return recommendations.iloc[0]

    current = recommendations[recommendations['period'] == CURRENT_PERIOD]
    if current.empty:
        logger.info(
            f"recommendations frame has no '{CURRENT_PERIOD}' row "
            f"(periods: {list(recommendations['period'])}); reporting nothing rather "
            f"than an older period as current"
        )
        return None
    return current.iloc[0]


def _count(row, column: str) -> int:
    """
    One analyst-count cell as an int, treating anything unusable as zero.

    `int()` on a NaN raises, and the caller wraps the whole extraction in a broad
    ``except`` — so a single unparseable column used to discard all five counts and
    the security's rating with them. Zero is the right reading here specifically
    because these are *counts within a distribution*: no analysts in a bucket is a
    genuine zero, and `AnalystRating.consensus` already answers "No Rating" when all
    five sum to zero, so an empty frame cannot masquerade as a real consensus.
    """
    try:
        value = row.get(column, 0)
    except Exception:
        return 0
    if value is None:
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number != number or number in (float('inf'), float('-inf')):  # NaN / Inf
        return 0
    return int(number)


class AnalystRatingService:
    """
    Service for fetching and caching analyst ratings from Yahoo Finance.

    Uses yfinance library to fetch aggregated analyst recommendations
    showing counts of: strong buy, buy, hold, sell, strong sell.

    Caches ratings in database and refreshes twice weekly (every 3-4 days).
    """


    # Latched the first time Yahoo answers with a rate limit, mirroring
    # `MarketDataService.rate_limited`. On the class as well as the instance so a
    # service built through `__new__` in a test can still read it.
    rate_limited = False

    def __init__(self, db: AsyncSession):
        self.db = db
        self.rating_repo = AnalystRatingRepository(db)
        self.rate_limited = False

    async def _get_yahoo_ticker(self, security: Security) -> str:
        """
        Convert IBKR security info to Yahoo Finance ticker format.
        Reuses the same logic from market_data_service.
        """
        from app.services.market_data_service import MarketDataService
        market_service = MarketDataService(self.db)
        return await market_service._get_yahoo_ticker(security)

    async def fetch_rating_for_security(self, security: Security) -> Optional[Dict]:
        """
        Fetch analyst rating for a single security from Yahoo Finance.

        Args:
            security: Security object

        Returns:
            Dict with rating counts or None if not available
        """
        try:
            yahoo_ticker = await self._get_yahoo_ticker(security)
            logger.info(f"Fetching analyst rating for {security.symbol} ({yahoo_ticker})...")

            # Add random delay to avoid rate limiting (1-3 seconds)
            await asyncio.sleep(random.uniform(1.0, 3.0))

            # Fetch recommendations using yfinance (in thread to avoid blocking event loop)
            def _fetch():
                t = yf.Ticker(yahoo_ticker)
                return t.recommendations

            recommendations = await asyncio.to_thread(_fetch)

            if recommendations is None or recommendations.empty:
                logger.info(f"No analyst ratings available for {security.symbol}")
                return None

            latest = select_current_period(recommendations)
            if latest is None:
                logger.info(f"No current-period analyst ratings for {security.symbol}")
                return None

            rating_data = {
                'security_id': security.id,
                'strong_buy': _count(latest, 'strongBuy'),
                'buy': _count(latest, 'buy'),
                'hold': _count(latest, 'hold'),
                'sell': _count(latest, 'sell'),
                'strong_sell': _count(latest, 'strongSell'),
            }

            total = sum([rating_data['strong_buy'], rating_data['buy'],
                        rating_data['hold'], rating_data['sell'],
                        rating_data['strong_sell']])

            logger.info(f"Found {total} analyst ratings for {security.symbol}")
            return rating_data

        except Exception as e:
            # A rate limit is not this security's problem and must not be absorbed as
            # if it were. This `except` wrapped the whole yfinance call, so the 429 was
            # converted to `None`, the caller read it as "no ratings available for this
            # one", and the pass asked the same IP about the remaining ~37 securities
            # two to four seconds apart — the exact behaviour rule 1 in CLAUDE.md
            # forbids, and the exact shape that was fixed across five other services on
            # 2026-08-04. The loop's `is_rate_limit(e)` break was unreachable the whole
            # time: nothing it could catch was ever raised.
            if is_rate_limit(e):
                raise
            logger.error(f"Error fetching rating for {security.symbol}: {e}")
            return None

    async def sync_ratings_for_securities(self, security_ids: Optional[List[int]] = None) -> Dict:
        """
        Sync analyst ratings for specified securities or all securities.

        Args:
            security_ids: List of security IDs to sync. If None, syncs all.

        Returns:
            Dict with sync statistics
        """
        # Get securities
        if security_ids:
            result = await self.db.execute(
                select(Security).where(Security.id.in_(security_ids))
            )
        else:
            result = await self.db.execute(select(Security))

        securities = list(result.scalars().all())

        if not securities:
            return {
                'securities_processed': 0,
                'ratings_updated': 0,
                'errors': 0,
                'message': 'No securities found'
            }

        logger.info(f"Syncing analyst ratings for {len(securities)} securities")

        ratings_updated = 0
        errors = 0

        pending_ids = [s.id for s in securities]

        for i, security_id in enumerate(pending_ids, 1):
            # Reloaded by id each iteration rather than held across the loop.
            # `db.rollback()` in the handler below expires EVERY object in the session,
            # so the next iteration's `security.symbol` was a lazy refresh — and in async
            # SQLAlchemy an expired-attribute load outside an await raises
            # MissingGreenlet. That line sits outside the try, so one failed security
            # did not cost one security: it crashed the whole pass, uncaught. `db.get`
            # is awaited and serves the identity map on the happy path, so this costs
            # nothing when nothing has failed.
            security = await self.db.get(Security, security_id)
            if security is None:
                continue
            logger.info(
                f"[{i}/{len(pending_ids)}] Processing {security.symbol} "
                f"({(security.description or '')[:50]}...)"
            )

            try:
                rating_data = await self.fetch_rating_for_security(security)

                if rating_data:
                    # Save to database
                    await self.rating_repo.upsert(rating_data)
                    ratings_updated += 1
                    await self.db.commit()
                else:
                    logger.warning(f"Skipping {security.symbol} - no ratings available")

                # Add delay between securities to avoid rate limiting (2-4 seconds)
                if i < len(securities):
                    delay = random.uniform(2.0, 4.0)
                    logger.debug(f"Waiting {delay:.1f}s before next security...")
                    await asyncio.sleep(delay)

            except Exception as e:
                # Before the rollback -- it expires the ORM attributes, and a lazy
                # refresh inside an except handler raises MissingGreenlet.
                symbol = security.symbol
                errors += 1
                await self.db.rollback()
                # Rule 1: stop on a rate limit rather than working through the rest of
                # the list against an IP that has already refused us. `sync_stale_ratings`
                # selects on staleness, so whatever this pass never reached is still
                # stale and the next run picks it up.
                if is_rate_limit(e):
                    self.rate_limited = True
                    logger.warning(
                        f"Yahoo rate limit at {symbol} "
                        f"({i}/{len(pending_ids)}); abandoning the rest of this pass"
                    )
                    break
                logger.error(f"Error processing {symbol}: {e}")
                continue

        logger.info(
            f"Analyst Rating Sync Complete: "
            f"processed={len(securities)}, updated={ratings_updated}, errors={errors}"
        )

        result = {
            'securities_processed': len(securities),
            'ratings_updated': ratings_updated,
            'errors': errors,
            'rate_limited': self.rate_limited,
            'message': f'Successfully synced {ratings_updated}/{len(securities)} analyst ratings'
        }
        if self.rate_limited:
            result['warnings'] = [
                'Yahoo Finance rate limit reached; the rest of this pass was abandoned. '
                'Do not retry manually — the next scheduled run resumes where it stopped.'
            ]
        return result

    async def get_stale_ratings(self, days_old: int = 3) -> List[AnalystRating]:
        """
        Get ratings that need to be refreshed (older than specified days).
        Default is 3 days for twice-weekly updates.
        """
        return await self.rating_repo.get_stale_ratings(days_old)

    async def sync_stale_ratings(self) -> Dict:
        """
        Sync ratings that are stale (3+ days old) **or missing entirely**.

        The work list used to come only from `get_stale_ratings`, which selects
        rows that already exist — so a security that had never been rated was
        invisible to this path forever, and on an empty table it reported "No
        stale ratings to update" and fetched nothing. A newly-bought security
        therefore never acquired a rating unless someone happened to run the
        full sync instead.

        That is the shape CLAUDE.md describes for benchmarks: refreshing only
        what already has rows means nothing ever bootstraps.

        This docstring used to say `FundamentalsService.sync_stale_fundamentals`
        "already unions the two sets; this now matches it". It did not: the union
        lived in the function it delegated to, one call below a pre-filter that
        bailed on an empty stale list, so it had this same bug. Fixed 2026-08-05.
        The lesson is about the citation rather than the code — a fix justified by
        a sibling's supposed correctness should read the sibling's entry point.
        """
        result = await self.db.execute(select(Security))
        all_securities = list(result.scalars().all())

        rated_ids = {r.security_id for r in await self.rating_repo.get_all()}
        stale_ids = {r.security_id for r in await self.get_stale_ratings(days_old=3)}

        security_ids = [
            s.id for s in all_securities
            if s.id not in rated_ids or s.id in stale_ids
        ]

        if not security_ids:
            return {
                'securities_processed': 0,
                'ratings_updated': 0,
                'errors': 0,
                'message': 'No stale or missing ratings to update'
            }

        never_rated = sum(1 for s in all_securities if s.id not in rated_ids)
        logger.info(
            f"Refreshing {len(security_ids)} ratings "
            f"({never_rated} never rated, {len(security_ids) - never_rated} stale)"
        )

        return await self.sync_ratings_for_securities(security_ids)

    async def get_rating_for_security(self, security_id: int) -> Optional[AnalystRating]:
        """Get cached analyst rating for a security"""
        return await self.rating_repo.get_by_security_id(security_id)

    async def get_all_ratings(self) -> List[AnalystRating]:
        """Get all cached analyst ratings"""
        return await self.rating_repo.get_all()
