"""
A market cap in the base currency — one converter for every surface that shows one.

Yahoo states `marketCap` in the listing's own quote currency, so a TWSE cap is in TWD and
a KRX one in KRW. Printed bare beside a USD cap, or sorted against it by the raw number,
that is a ranking of nothing (3 trillion USD against 400 billion TWD). The Fundamentals
tab and the Watchlist both show caps; this is the one conversion they share, so the two
cannot come to disagree about a rate or a refusal (`tests/test_market_cap_family.py`).

Two steps through EUR like every other read path (`native_amounts`), on the newest
**cached** rate only — a GET must never reach a provider — and refused past
`CurrencyService.CARRY_FORWARD_MAX_AGE_DAYS`. `sync_exchange_rates` keeps the rates of
every quoted currency warm for exactly this (`quoted_currencies`).

None, not a guess, for a minor-unit quote (`GBp`, `ZAc`, `ILA`): whether Yahoo states such
a listing's `marketCap` in the minor or the major unit is unverified here, and the two
readings are a factor of 100 apart.
"""
from datetime import date
from decimal import Decimal
from typing import Awaitable, Callable, Dict, Iterable, Optional, Set

from sqlalchemy import select

from app.clock import utcnow

MarketCapToBase = Callable[[Optional[int], Optional[str]], Awaitable[Optional[int]]]

# Kept warm whether or not a fundamentals row or a watchlist item is quoted in them yet
# (the owner, 2026-10-17): the large Asian markets and London, so a name added to the
# watchlist has a rate the day it lands rather than after the next FX pass notices it.
MARKET_CAP_CURRENCIES = frozenset({"JPY", "CNY", "HKD", "KRW", "TWD", "GBP"})

# A market cap moves far more in a few days than its exchange rate does, and Frankfurter
# costs one request per currency per call, so a quoted currency nothing is held in is
# refreshed once its newest cached rate is this old — not on each of the day's slots.
QUOTED_REFRESH_DAYS = 3


async def market_cap_to_base(db) -> MarketCapToBase:
    """A converter from ``(market_cap, quote_currency)`` to the base currency."""
    from app.repositories.app_settings_repository import AppSettingsRepository
    from app.services.currency_service import CurrencyService
    from app.services.market_data_service import MarketDataService

    base = await AppSettingsRepository(db).get_base_currency()
    currency_service = CurrencyService(db)
    today = utcnow().date()
    memo: Dict[tuple, Optional[Decimal]] = {}

    async def cached_rate(from_currency: str, to_currency: str) -> Optional[Decimal]:
        if from_currency == to_currency:
            return Decimal(1)
        key = (from_currency, to_currency)
        if key not in memo:
            recent = await currency_service._get_most_recent_rate(
                from_currency, today, to_currency
            )
            fresh = recent is not None and (
                (today - recent[2]).days <= CurrencyService.CARRY_FORWARD_MAX_AGE_DAYS
            )
            memo[key] = recent[0] if fresh and recent[0] else None
        return memo[key]

    async def to_base(market_cap: Optional[int], currency: Optional[str]) -> Optional[int]:
        if market_cap is None or not currency or MarketDataService._minor_unit(currency):
            return None
        if currency == base:
            return market_cap
        to_eur = await cached_rate(currency, "EUR")
        eur_to_base = await cached_rate("EUR", base)
        if to_eur is None or eur_to_base is None:
            return None
        return int(Decimal(market_cap) * to_eur * eur_to_base)

    return to_base


async def quoted_currencies(db) -> Set[str]:
    """
    Every currency a served market cap is quoted in — the fundamentals rows' and the
    watchlist's `data_currency` — plus `MARKET_CAP_CURRENCIES`. A minor unit is left
    out: `market_cap_to_base` refuses it whatever the rate.
    """
    from app.models.fundamental_metrics import FundamentalMetrics
    from app.models.watchlist_item import WatchlistItem
    from app.services.market_data_service import MarketDataService

    out = set(MARKET_CAP_CURRENCIES)
    for column in (FundamentalMetrics.data_currency, WatchlistItem.data_currency):
        for raw in (await db.execute(select(column).distinct())).scalars().all():
            code = (raw or "").strip()
            if len(code) == 3 and not MarketDataService._minor_unit(code):
                out.add(code.upper())
    out.discard("EUR")
    return out


async def quoted_currencies_to_warm(db, today: date, warmed_anyway: Iterable[str]) -> Set[str]:
    """
    The quoted currencies `sync_exchange_rates` should add to its pass: those not in
    ``warmed_anyway`` (held, or warmed on spec) whose newest cached rate to EUR is
    missing or `QUOTED_REFRESH_DAYS` old.
    """
    from app.services.currency_service import CurrencyService

    currency_service = CurrencyService(db)
    skip = {c.upper() for c in warmed_anyway}
    out = set()
    for code in sorted(await quoted_currencies(db) - skip):
        recent = await currency_service._get_most_recent_rate(code, today, "EUR")
        if recent is None or (today - recent[2]).days >= QUOTED_REFRESH_DAYS:
            out.add(code)
    return out
