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
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

    today = date.today()

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
    return earliest or (date.today() - timedelta(days=365))
