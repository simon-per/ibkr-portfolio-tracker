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

`tests/test_fx_carry_bound_family.py` fails a second definition of the bound and pins the
read-order invariance it exists for.
"""
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
