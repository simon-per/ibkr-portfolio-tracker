"""
The EUR -> base-currency projection every read path applies, and its loader.

Moved out of `portfolio_service` on 2026-09-28, verbatim, when the crypto view needed the
same projection over a window the stock book does not define: `PortfolioService` loads it
from the first tax lot, the crypto view from its own first data point. One loader with a
`start` parameter instead of a second copy of it — the copy is how this codebase's
dominant failure mode begins (see CLAUDE.md). `BaseFx` is re-exported from
`portfolio_service`, where eight test modules and `benchmark_service` import it.
"""
import bisect
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.clock import utc_today

logger = logging.getLogger(__name__)


class BaseFx:
    """
    Converts EUR-denominated amounts into the selected base (display) currency
    at a given date. The whole portfolio pipeline computes values in EUR; this
    applies a single EUR->base factor as a read-time projection.

    - Cost basis is converted at each lot's open_date (so the cost-basis line
      only moves on buys/sells, never with day-to-day FX).
    - Market value is converted at the valuation date.

    When base_currency == 'EUR' this is a no-op (rate 1.0).
    """

    def __init__(self, base_currency: str, rate_cache: Dict[date, Decimal]):
        self.base_currency = base_currency
        self.rate_cache = rate_cache  # {date: EUR->base rate}
        self._sorted_dates = sorted(rate_cache.keys())

    def _rate_on(self, on_date: date) -> Optional[Decimal]:
        rate = self.rate_cache.get(on_date)
        if rate is not None:
            return rate
        if not self._sorted_dates:
            return None
        # Carry-forward: most recent rate on/before on_date
        idx = bisect.bisect_right(self._sorted_dates, on_date)
        if idx > 0:
            return self.rate_cache[self._sorted_dates[idx - 1]]
        # on_date precedes all cached rates: carry the earliest back
        return self.rate_cache[self._sorted_dates[0]]

    def convert(self, amount_eur: Decimal, on_date: date) -> Decimal:
        if self.base_currency == "EUR" or not amount_eur:
            return amount_eur
        rate = self._rate_on(on_date)
        if rate is None:
            # No rate available anywhere: fall back to EUR value rather than zero.
            return amount_eur
        return amount_eur * rate


async def load_base_fx(
    db: AsyncSession, currency_service, base_currency: str, start: date,
    backfill: bool = True,
) -> BaseFx:
    """
    Build a BaseFx for `base_currency`, loading EUR->base daily rates from `start`
    to today.

    Reads cached ExchangeRate rows; if none exist yet (base just switched and
    backfill was skipped), fetches the whole range once from Frankfurter — unless
    `backfill=False`, which the crypto view passes so a GET never reaches the network;
    it reports the figures as unconvertible instead.
    """
    if base_currency == "EUR":
        return BaseFx("EUR", {})

    from app.models.exchange_rate import ExchangeRate

    today = utc_today()

    async def load_cache() -> Dict[date, Decimal]:
        rows = (await db.execute(
            select(ExchangeRate).where(
                ExchangeRate.from_currency == "EUR",
                ExchangeRate.to_currency == base_currency,
                ExchangeRate.date >= start,
                ExchangeRate.date <= today,
            )
        )).scalars().all()
        return {r.date: r.rate for r in rows}

    cache = await load_cache()
    if not cache and backfill:
        # Safety net: populate EUR->base history once, then reload.
        try:
            await currency_service._batch_fetch_rates(
                from_currency="EUR",
                target_date=today,
                to_currency=base_currency,
                days_back=max((today - start).days, 30),
            )
            cache = await load_cache()
        except Exception as e:
            logger.warning(f"Could not backfill EUR->{base_currency} rates: {e}")

    return BaseFx(base_currency, cache)


def default_start(earliest: Optional[date]) -> date:
    """The loader's window start when a book has no dated history yet: one year back,
    which is what `PortfolioService` has always used for an empty lot table."""
    return earliest or (utc_today() - timedelta(days=365))


async def stock_book_start(db: AsyncSession) -> date:
    """The stock book's EUR->base window start: the earliest lot's `open_date` (IBKR and
    pillar 3a alike), or a year back for an empty lot table.

    One answer for the three places that need it — `PortfolioService._load_base_fx`
    (what is read), the base-currency switch (what it backfills) and the daily FX job's
    `ensure_base_history` (what it keeps filled). They were two copies of one query
    before the third was written; a backfill that stops short of what the reader loads
    is how a 2025 purchase ends up converted at a carried 2026 rate.
    """
    from sqlalchemy import func
    from app.models.taxlot import TaxLot

    earliest = (await db.execute(select(func.min(TaxLot.open_date)))).scalar()
    return default_start(earliest)


# How far after the window start the oldest stored rate may sit and still count as
# reaching it: the start can be a weekend or a TARGET holiday (Easter spans four days),
# and the ECB publishes nothing for those. `CurrencyService.WARM_REACH_SLACK_DAYS` is the
# same allowance for the warm-up's own window.
BASE_HISTORY_SLACK_DAYS = 7


async def ensure_base_history(
    db: AsyncSession, currency_service, bases: List[str], start: date,
) -> Dict[str, bool]:
    """
    Make each EUR->base history in `bases` reach back to `start`, with **one** Frankfurter
    range request for every base that does not. Returns `{base: stored anything}` for the
    bases that needed it — empty when every history already reaches. Never raises
    (`_fetch_ecb_range` does not).

    The switch fills a base's history when it is chosen, but only if Frankfurter answers
    at that moment — and the crypto book's warm-up and the daily warm-up each keep a
    shorter window of the same pair. So a base whose switch-time backfill failed could
    be accepted on 2026 rates alone, and `BaseFx` would carry the oldest one back over
    every earlier purchase: cost basis at the wrong day's rate, silently. The daily FX
    job calls this for every selectable base (`eur_to_base_pairs`), switched to or not,
    so each one's full history is in the cache before anyone picks it, and a failed fill
    is re-asked at the next slot. Once filled it is one indexed `min(date)` per base.
    """
    from sqlalchemy import func
    from app.models.exchange_rate import ExchangeRate

    short = []
    for base in bases:
        if base == "EUR":
            continue
        oldest = (await db.execute(
            select(func.min(ExchangeRate.date)).where(
                ExchangeRate.from_currency == "EUR",
                ExchangeRate.to_currency == base,
            )
        )).scalar()
        if oldest is None or oldest > start + timedelta(days=BASE_HISTORY_SLACK_DAYS):
            short.append(base)
    if not short:
        return {}
    pairs = [("EUR", base) for base in short]
    answered, _ = await currency_service._fetch_ecb_range(pairs, start, utc_today())
    for pair in pairs:
        if pair not in answered:
            logger.warning(f"Could not backfill EUR->{pair[1]} history from {start}")
    return {base: ("EUR", base) in answered for base in short}
