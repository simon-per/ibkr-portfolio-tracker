"""
Native -> EUR rates preloaded for a window, and a bounded forward-fill over them: the FX
source for a read path that converts many dated amounts.

**One query, then pure lookups — never a fetch, never a write.** That is the property a
GET needs and `CurrencyService.get_exchange_rate` does not have: on a cache miss it asks
Frankfurter, and its carry-forward *adds a row*, so a read path built on it makes network
calls and takes SQLite's write lock inside a request. The ECB publishes no weekend rates,
so for a daily series every Saturday and Sunday would be such a miss.

Extracted on 2026-09-28 from the two copies that already existed —
`PortfolioService._preload_exchange_rates` / `_get_exchange_rate_with_fallback` and
`BenchmarkService._preload_fx_rates` — which now delegate here. The crypto view's history
would have been the third copy (see CLAUDE.md on why the third is the one to stop).
`tests/test_exchange_rate_readers.py` pins which modules may select `ExchangeRate` rows.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Iterable, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exchange_rate import ExchangeRate

# How far back a missing rate may be filled from. Covers a weekend, a holiday cluster
# and a provider outage of a few days; past it, a rate is missing rather than stale.
FX_LOOKBACK_DAYS = 14

RateCache = Dict[Tuple[str, date], Decimal]


async def preload_eur_rates(
    db: AsyncSession,
    currencies: Iterable[str],
    start: date,
    end: date,
    lookback_days: int = FX_LOOKBACK_DAYS,
) -> RateCache:
    """
    Every cached `currency -> EUR` rate from `start - lookback_days` to `end`, as
    `{(currency, date): rate}`. The window reaches back so the first days of the range
    can forward-fill from before it. EUR itself needs no rate and is skipped.
    """
    wanted = {c for c in currencies if c and c != "EUR"}
    if not wanted:
        return {}

    extended_start = start - timedelta(days=lookback_days)
    result = await db.execute(
        select(ExchangeRate).where(
            and_(
                ExchangeRate.from_currency.in_(wanted),
                ExchangeRate.to_currency == "EUR",
                ExchangeRate.date >= extended_start,
                ExchangeRate.date <= end,
            )
        )
    )
    return {(r.from_currency, r.date): r.rate for r in result.scalars().all()}


def eur_rate_on(
    cache: RateCache,
    currency: str,
    target_date: date,
    max_lookback_days: int = FX_LOOKBACK_DAYS,
) -> Optional[Decimal]:
    """The rate on `target_date`, else the most recent one up to `max_lookback_days`
    earlier, else None — never a guess from further away."""
    for days_back in range(0, max_lookback_days + 1):
        rate = cache.get((currency, target_date - timedelta(days=days_back)))
        if rate:
            return rate
    return None


class PreloadedRates:
    """
    Stands in for the one `CurrencyService` method `NativeToBase` calls, answering from
    a preloaded cache only.

    So a read path gets `NativeToBase`'s whole two-step conversion (native -> EUR at the
    row's date, then EUR -> base) — the implementation every other reader uses — without
    its network and write side effects. A miss raises `ValueError`, which `NativeToBase`
    turns into `None`: the amount is reported missing, never zeroed.
    """

    def __init__(self, cache: RateCache, max_lookback_days: int = FX_LOOKBACK_DAYS):
        self._cache = cache
        self._max_lookback_days = max_lookback_days

    async def get_exchange_rate(
        self, from_currency: str, target_date: date, to_currency: str = "EUR"
    ) -> Decimal:
        if from_currency == to_currency:
            return Decimal("1.0")
        if to_currency != "EUR":
            raise ValueError(f"PreloadedRates only holds rates into EUR, not {to_currency}")
        rate = eur_rate_on(self._cache, from_currency, target_date, self._max_lookback_days)
        if rate is None:
            raise ValueError(
                f"No preloaded {from_currency}->EUR rate within "
                f"{self._max_lookback_days} days of {target_date}"
            )
        return rate
