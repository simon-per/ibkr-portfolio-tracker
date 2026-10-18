"""
Analyst Rating Service
Fetches analyst recommendations from Yahoo Finance using yfinance library.
Refreshed for held securities by the scheduler's daily `fundamentals_refresh` job
(FUNDAMENTALS_REFRESH_DAYS in `scheduler_service.py`), and on demand.
"""
from typing import List, Dict, Optional
import logging
import yfinance as yf
import random
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.etf_mappings import allocation_for_fund_isin
from app.repositories.analyst_rating_repository import AnalystRatingRepository
from app.repositories.fundamentals_repository import sync_scope
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.services.yahoo_eligibility import yahoo_eligible
from app.models.analyst_rating import AnalystRating
from app.services.yahoo_estimates import is_fund_quote_type
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


RATING_COUNT_FIELDS = ('strong_buy', 'buy', 'hold', 'sell', 'strong_sell')


def no_rating(security_id: int) -> Dict:
    """
    Yahoo's answer "no analysts cover this", as a row: five zero counts.

    Stored rather than skipped, because a skipped security has no `last_updated` and
    `sync_stale_ratings` re-queues anything without a row — so every ETF was re-asked on
    every pass, a request against an IP-based limit for an answer already known. The
    shape `allocation_service` stamps `allocation_last_updated` on a failed lookup for.
    """
    return {'security_id': security_id, **dict.fromkeys(RATING_COUNT_FIELDS, 0)}


class AnalystRatingService:
    """
    Service for fetching and caching analyst ratings from Yahoo Finance.

    Uses yfinance library to fetch aggregated analyst recommendations
    showing counts of: strong buy, buy, hold, sell, strong sell.

    Caches ratings in the database; a rating older than the repository's `STALE_AFTER`
    is stale, so the scheduled
    refresh asks again for every security but a fund (`_fund_ids`), whose no-rating row
    is re-stamped without a request.
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

    async def _fund_ids(self, securities: List[Security]) -> set:
        """
        The securities known to be funds, which have no analyst coverage to ask about.

        A fund always came back `no_rating`, and the 3-day staleness is shorter than the
        pass interval, so every fund cost a `recommendations` request on every pass
        (GitHub issue #11, item e). Known without Yahoo: the stored
        `fundamental_metrics.quote_type` (written by the fundamentals pass, which runs
        just before this one) through `FUND_QUOTE_TYPES`, or an ISIN the ETF table maps
        (`allocation_for_fund_isin`, the allocation pass's test). A security neither
        knows yet — bought today, fundamentals not fetched — is asked once, as before.
        """
        ids = [s.id for s in securities]
        rows = await self.db.execute(
            select(FundamentalMetrics.security_id, FundamentalMetrics.quote_type)
            .where(FundamentalMetrics.security_id.in_(ids))
        )
        funds = {sid for sid, quote_type in rows.all() if is_fund_quote_type(quote_type)}
        return funds | {s.id for s in securities if allocation_for_fund_isin(s.isin)}

    async def fetch_rating_for_security(self, security: Security) -> Optional[Dict]:
        """
        Fetch analyst rating for a single security from Yahoo Finance.

        Args:
            security: Security object

        Returns:
            Dict with rating counts — **all zero when Yahoo answered that there is no
            current rating** — or None when the fetch itself failed.

        The zero-count answer is stored like any other (see `sync_ratings_for_securities`):
        `AnalystRating.consensus` reads it as "No Rating", and its `last_updated` is what
        keeps an ETF from being re-asked on every pass for an answer already known.
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
                return no_rating(security.id)

            latest = select_current_period(recommendations)
            if latest is None:
                logger.info(f"No current-period analyst ratings for {security.symbol}")
                return no_rating(security.id)

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
        Sync analyst ratings for specified securities or every held security.

        Args:
            security_ids: List of security IDs to sync. If None, syncs every held one.

        Returns:
            Dict with sync statistics
        """
        # Get securities
        # yahoo_eligible() on both branches, including the explicit-ids one: a
        # caller passing an id is not asserting Yahoo covers it. "All" means
        # `sync_scope()` (held and eligible), the scope of the scheduled pass.
        if security_ids:
            result = await self.db.execute(
                select(Security).where(
                    Security.id.in_(security_ids), yahoo_eligible()
                )
            )
        else:
            result = await self.db.execute(select(Security).where(sync_scope()))

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
        no_ratings = 0
        funds_not_asked = 0
        errors = 0

        fund_ids = await self._fund_ids(securities)
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
                if security.id in fund_ids:
                    # Stored as Yahoo's own answer for a fund would be, without asking
                    # (`_fund_ids`): it still reads "No Rating", and it is never paced.
                    await self.rating_repo.upsert(no_rating(security.id))
                    await self.db.commit()
                    no_ratings += 1
                    funds_not_asked += 1
                    continue

                rating_data = await self.fetch_rating_for_security(security)

                if rating_data is not None:
                    # Save to database — a "no rating" answer too, which stamps the
                    # attempt (see `no_rating`).
                    await self.rating_repo.upsert(rating_data)
                    await self.db.commit()
                    if any(rating_data[f] for f in RATING_COUNT_FIELDS):
                        ratings_updated += 1
                    else:
                        no_ratings += 1
                else:
                    # The fetch failed for this security (not a rate limit — that
                    # raised): left unstamped, so the next pass asks again.
                    errors += 1
                    logger.warning(f"Skipping {security.symbol} - rating fetch failed")

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
            f"processed={len(securities)}, updated={ratings_updated}, "
            f"no_rating={no_ratings}, errors={errors}"
        )

        result = {
            'securities_processed': len(securities),
            'ratings_updated': ratings_updated,
            'no_rating': no_ratings,
            'funds_not_asked': funds_not_asked,
            'errors': errors,
            'rate_limited': self.rate_limited,
            'message': (
                f'Synced {ratings_updated}/{len(securities)} analyst ratings'
                + (f', {no_ratings} with no analyst coverage' if no_ratings else '')
            ),
        }
        if self.rate_limited:
            result['warnings'] = [
                'Yahoo Finance rate limit reached; the rest of this pass was abandoned. '
                'Do not retry manually — the next scheduled run resumes where it stopped.'
            ]
        return result

    async def sync_stale_ratings(self) -> Dict:
        """
        Sync ratings that are stale (older than the repository's `STALE_AFTER`) **or
        missing entirely**.

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
        # `sync_scope()`, the fundamentals pass's own predicate (held and Yahoo-eligible):
        # this builds the candidate list for a Yahoo fetch, so an ineligible security
        # must never enter it -- otherwise it is "missing a rating" forever and re-queued
        # on every pass -- and a sold position is not re-rated daily. The stale query
        # and `/status`'s count are scoped by the same predicate, so they agree.
        result = await self.db.execute(select(Security).where(sync_scope()))
        all_securities = list(result.scalars().all())

        rated_ids = {r.security_id for r in await self.rating_repo.get_all()}
        stale_ids = {r.security_id for r in await self.rating_repo.get_stale_ratings()}
        # Funds stay candidates, so their row keeps the staleness every other row has
        # (`/api/analyst-ratings/status`), but `sync_ratings_for_securities` re-stamps
        # them without a request.
        fund_ids = await self._fund_ids(all_securities)

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

        funds = sum(1 for sid in security_ids if sid in fund_ids)
        never_rated = sum(
            1 for s in all_securities if s.id not in rated_ids and s.id not in fund_ids
        )
        logger.info(
            f"Refreshing {len(security_ids)} ratings ({never_rated} never rated, "
            f"{len(security_ids) - never_rated - funds} stale, "
            f"{funds} funds stored as no rating without asking)"
        )

        return await self.sync_ratings_for_securities(security_ids)

    async def get_rating_for_security(self, security_id: int) -> Optional[AnalystRating]:
        """Get cached analyst rating for a security"""
        return await self.rating_repo.get_by_security_id(security_id)

    async def get_all_ratings(self) -> List[AnalystRating]:
        """Get all cached analyst ratings"""
        return await self.rating_repo.get_all()
