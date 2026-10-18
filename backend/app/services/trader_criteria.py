"""
The ESTV's securities-dealer safe harbour (Kreisschreiben Nr. 36), as figures -- GitHub #78.

A private investor whose capital gains are tax-free in Switzerland can be reclassified as a
professional securities dealer, whose gains are taxable income. KS 36 names criteria under
which the ESTV does not do that; the report computes, per calendar year, the three the data
here can answer. They are figures for the reader to hold against the circular, not a
verdict: the report never says "safe" or "dealer". Definitions (docs/tax.md, *Securities-dealer
indicators*):

- **turnover**: the circular's own test -- the year's transaction volume (purchases plus
  sales) as a multiple of the securities and IBKR cash held at the prior 31 December
  (`turnover_multiple`).
- **sales held under six months**: of the year's sales, those that disposed of a lot opened
  less than six calendar months before the sale (`short_held_counts`).
- **margin used**: whether the IBKR cash balance closed any day of the year below zero
  (`lowest_balance` over `CashService.balance_events`).

Derivatives, the circular's fourth trading criterion, are not reported: the Flex ingest keeps
stock executions only, so the ledger can never show one.

Pure functions over rows the caller has read: no DB, no network.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# The one calendar-month step (same day N months on, clamped to the month's length).
from app.services.dividend_forecast import _add_months

#: KS 36's minimum holding period.
HOLDING_MONTHS = 6


def held_under_six_months(open_date: date, sale_date: date) -> bool:
    """True when the lot opened on `open_date` was sold before six calendar months had passed."""
    return sale_date < _add_months(open_date, HOLDING_MONTHS)


def sales_by_day(sales: Iterable) -> Dict[Tuple[str, date], List]:
    """
    The standing SELL rows grouped into sales: one per contract and trade date.

    IBKR sends each execution of an order as its own row, so a sale filled in three
    parts is three rows and one sale. The caller passes `uncancelled_sales(...)`, so a
    busted sale is no sale here either.
    """
    groups: Dict[Tuple[str, date], List] = defaultdict(list)
    for t in sales:
        groups[(t.conid, t.trade_date)].append(t)
    return dict(groups)


def short_held_counts(
    sales: Dict[Tuple[str, date], List], lots: Sequence
) -> Tuple[int, int]:
    """
    ``(held under six months, holding period unknown)`` over `sales` (`sales_by_day`).

    A sale's lots are the closed lots of its security that reconcile closed **on the
    sale's date** with `close_source='trade'` -- reconcile stamps a lot a SELL explained
    with that SELL's date. A sale is short-held when any of its lots opened less than
    six months before it. One with no such lot -- an unlinked trade, a window holding a
    second sale of the security (its lots carry the later date), a closure lost before
    issue #54 -- has no holding period on record and is counted as unknown rather than
    guessed either way.
    """
    lots_by_key: Dict[Tuple[int, date], List] = defaultdict(list)
    for lot in lots:
        lots_by_key[(lot.security_id, lot.close_date)].append(lot)
    short = unknown = 0
    for (_, sale_date), rows in sales.items():
        security_ids = {t.security_id for t in rows if t.security_id is not None}
        matched = [
            lot for sid in security_ids for lot in lots_by_key.get((sid, sale_date), [])
        ]
        if not matched:
            unknown += 1
        elif any(held_under_six_months(lot.open_date, sale_date) for lot in matched):
            short += 1
    return short, unknown


def lowest_balance(
    events: Sequence[Tuple[date, Decimal]], start: date, end: date
) -> Optional[Decimal]:
    """
    The lowest end-of-day balance over ``[start, end]``, the balance carried into
    `start` included; None when no event is dated on or before `end`.

    `events` are `CashService.balance_events`, sorted. End of day, because that is the
    granularity of every ledger here: an order that buys before the sale funding it
    settles the same day is not seen. None means the cash record does not reach the
    year at all, which is a different answer from a balance that never fell.
    """
    balance = Decimal("0")
    lowest: Optional[Decimal] = None
    i, n = 0, len(events)
    while i < n and events[i][0] < start:
        balance += events[i][1]
        i += 1
    if i:
        lowest = balance
    while i < n and events[i][0] <= end:
        day = events[i][0]
        while i < n and events[i][0] == day:
            balance += events[i][1]
            i += 1
        lowest = balance if lowest is None else min(lowest, balance)
    return lowest


def turnover_multiple(
    volume: Optional[Decimal], securities: Optional[Decimal], cash: Optional[Decimal]
) -> Optional[float]:
    """
    KS 36's turnover: the year's transaction volume over the opening securities **and**
    cash, as a multiple to two decimals (the circular's threshold is five).

    The cash is signed -- a margin debit at the start shrinks the base it is set against.
    None when any part is unknown or the base is not positive: a multiple of nothing, or
    of a debt, is no figure.
    """
    if volume is None or securities is None or cash is None:
        return None
    base = securities + cash
    if base <= 0:
        return None
    return float(round(volume / base, 2))
