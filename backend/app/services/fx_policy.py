"""
How far an FX rate may be carried, and which rows count as a rate to carry from: the one
policy both FX paths apply.

There are two: `CurrencyService.get_exchange_rate` (activity, cash, tax, dividends, the
sync), which carries forward on a miss and **persists the copy**, and `fx_preload` (the
timeline, the benchmark, crypto history), which fills forward over a preloaded window.
Until 2026-10-18 (issue #20) the first carried 30 days and the second 14, and because the
first writes rows the second reads, a date 15-30 days past the newest rate was unpriced
on the value chart until someone opened the ledger, and priced after. So:

- **One bound.** A rate may stand in for at most `FX_MAX_CARRY_DAYS` days after the date
  it was published for. 14 is the bound the read path always had and the one the price
  path uses (`portfolio_service.PRICE_LOOKBACK_DAYS`): a holding drops out of the chart at
  the same age whether its price or its rate went missing. It still covers every gap the
  ECB itself leaves (a weekend, the Christmas closing days) and `find_stale_exchange_rates`
  warns after three missed publication days, so an outage is announced well before it.
- **Age from the published rate.** A carried copy (`CARRIED_SUFFIX`) is a cached verdict,
  never an anchor: carrying from a copy restarted the clock, so daily carries chained past
  any bound, and a reader accepting a copy as its anchor doubled it. `published_rows()`
  is the criterion every anchor query uses.
- **One cache-only reader.** A reader that must not reach a provider (the dividend
  forecast and accruals, market caps) asks `newest_published_rate`, which applies the
  bound. The unbounded `_get_most_recent_rate` is private to this module and
  `CurrencyService._carry_forward`: until issue #56 the dividend service called it bare
  and converted with a rate of any age that `get_exchange_rate` refused, and `market_cap`
  restated the bound beside it.

`tests/test_fx_carry_bound_family.py` fails a second definition of the bound, any caller
of the unbounded reader outside the two modules, and pins the read-order invariance.
"""
from datetime import date
from decimal import Decimal
from typing import NamedTuple, Optional

from sqlalchemy import select

from app.models.exchange_rate import ExchangeRate

FX_MAX_CARRY_DAYS = 14

# Appended to the origin's tag when a rate is copied onto a later date. A copy is a
# stand-in exactly like a fallback row — the day's real quote may simply not have been
# published yet — and `_store_published_rate` has to be able to tell it apart from a
# quote for that day, or the copy outlives the quote it was standing in for.
CARRIED_SUFFIX = "+carried"


def published_rows():
    """The criterion for a row that is a provider's quote (ECB or fallback), not a copy."""
    return ExchangeRate.source.notlike(f"%{CARRIED_SUFFIX}")


class PublishedRate(NamedTuple):
    rate: Decimal
    source: str
    date: date


def within_carry_bound(published_on: date, as_of: date) -> bool:
    """Whether a rate published for ``published_on`` may stand in for ``as_of``."""
    return (as_of - published_on).days <= FX_MAX_CARRY_DAYS


async def _get_most_recent_rate(
    session, from_currency: str, to_currency: str, on_or_before: date
) -> Optional[PublishedRate]:
    """
    The newest *published* rate on or before ``on_or_before``, of ANY age — so private:
    only `newest_published_rate` and `CurrencyService._carry_forward`, which both apply
    `within_carry_bound` to it, may ask.

    A carried copy is skipped (`published_rows`): its date is not the rate's, so an age
    measured from it restarts at every carry and daily copies chain past the bound.

    The `date <= on_or_before` bound is what stops a currency we only learned about
    recently from being projected backwards onto older lots: a rate first cached today is
    invisible to a request for last year, which still refuses and so keeps the caller's
    skip-with-warning path.
    """
    row = (
        await session.execute(
            select(ExchangeRate)
            .where(
                ExchangeRate.date <= on_or_before,
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency,
                published_rows(),
            )
            .order_by(ExchangeRate.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return PublishedRate(row.rate, row.source, row.date)


async def newest_published_rate(
    session, from_currency: str, to_currency: str, as_of: date
) -> Optional[Decimal]:
    """
    The rate for ``as_of`` from the cache alone: the newest published rate on or before
    it, None once that is past `FX_MAX_CARRY_DAYS` — the verdict `get_exchange_rate`
    reaches for the same date — or when there is none. Never reaches a provider and
    never writes a copy, so a GET may call it.
    """
    recent = await _get_most_recent_rate(session, from_currency, to_currency, as_of)
    if recent is None or not recent.rate or not within_carry_bound(recent.date, as_of):
        return None
    return recent.rate
