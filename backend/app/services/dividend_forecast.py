"""
Project future dividend payments from a security's own payment history.

Nothing forward-looking is cached anywhere — no announced dividends, no calendar
(the fundamentals/earnings tables carry no dividend fields) — so a forecast is
necessarily inferred: the payout cadence comes from the spacing of past
ex-dates, the size from the recent dividend **per share** scaled to the holding
we have now. Pure functions, no DB and no network, so the inference rules are
pinned by fast unit tests and can never violate the no-Yahoo-at-request-time
rule.

Working per share rather than per payment received is what lets a security
bought last month be forecast at all: the payout schedule is a property of the
company, not of how long we have owned it. Keying on realized income instead
left TSMC, Samsung, SK Hynix, HPE and the SOXQ ETF — every one a real payer —
projecting nothing.

**How big each projected payment is** (since 2026-10-04; it was one flat median of
the last 8 payments before, which read low twice over — it averaged away a fund's
large December, and it trailed every raise by up to two years: NVDA had raised to
0.25 a quarter and was still projected at about 0.01):

- A payer whose recent payments are level, or level with a single step, is a
  **steady payer**: every projected payment is its latest regular payment, which
  carries a raise (or a cut) the company has already paid or IBKR has announced.
- Anything else is a **varying payer**: each projected payment repeats the same
  payment one year earlier, so a large December stays a large December.
- A **special** dividend is never the basis for anything — IBKR's own label when
  there is one, otherwise a payment more than twice its neighbours that does not
  recur a year apart.
- **No assumed growth.** Only a raise already paid or announced moves the amount;
  an unannounced one would be money no company has promised.

Every rule is decided from the payments themselves at read time — nothing is
classified by hand or stored.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import List, Optional, Sequence

# Cadence is inferred from the median gap between recent ex-dates. Anything
# slower than ~13 months is not a cadence, and anything faster than ~3 weeks is
# noise (monthly is the fastest real payout schedule), so both refuse to forecast.
MAX_GAP_DAYS = 400
MIN_GAP_DAYS = 20

# Only the most recent payments describe the current schedule; a decade of
# history would let long-dead behaviour outvote a recent dividend change.
CADENCE_SAMPLE = 8

# A payer that has skipped ~2.5 expected payouts before the horizon even starts
# has stopped, not paused — projecting a resumption would be invention.
STOPPED_AFTER_GAPS = 2.5

# Real schedules pay on a day of the month, not every N days. Stepping in days
# drifts against the calendar — 31-day steps give a monthly payer 11 payouts a
# year instead of 12 — so an inferred gap close to a calendar period is snapped
# to it. Anything that doesn't match one of these keeps day-stepping.
CALENDAR_PERIODS = {1: (26, 35), 3: (82, 100), 6: (170, 195), 12: (350, 380)}

# A payment more than 10% below the one before it is a DROP. 10% is wider than any
# rounding or ETF noise and narrower than a fund's seasonal swing (VT's March is
# roughly half its December).
STEADY_TOLERANCE = Decimal("1.10")
# How many recent payments decide steady vs varying, and how far back they may reach:
# two years of a quarterly payer — a seasonal pattern shows twice in that, a raise or
# a cut once.
STEADY_SAMPLE = 8
STEADY_WINDOW_DAYS = 760
# `mean4` (backtest only) averages this many.
MEAN_SAMPLE = 4

# A payment more than this multiple of its neighbours, with no comparable payment a
# year either side, is a special dividend. The year-apart test is what keeps a fund's
# recurring large December from being mistaken for one.
SPECIAL_MULTIPLE = Decimal("2")

# The sizing rules. Production uses `auto`; the others exist so the replay backtest
# (app/cli/backtest_dividend_forecast.py) can score every candidate through THIS
# implementation rather than a copy of it.
SIZING_AUTO = "auto"
SIZING_MEDIAN8 = "median8"                  # the rule until 2026-10-04
SIZING_MEAN4 = "mean4"
SIZING_LATEST = "latest"
SIZING_SAME_PAYMENT = "same_payment_last_year"
SIZING_METHODS = (SIZING_AUTO, SIZING_MEDIAN8, SIZING_MEAN4, SIZING_LATEST,
                  SIZING_SAME_PAYMENT)

# What a projected payment says about how it was sized (`ForecastPayment.method`).
METHOD_LATEST = "latest_payment"
METHOD_SAME_PAYMENT = "same_payment_last_year"


def _months_step(gap_days: int) -> Optional[int]:
    for months, (lo, hi) in CALENDAR_PERIODS.items():
        if lo <= gap_days <= hi:
            return months
    return None


def _add_months(d: date, months: int) -> date:
    """Same day-of-month N months on, clamped to the month's length."""
    total = d.month - 1 + months
    year, month = d.year + total // 12, total % 12 + 1
    if month == 12:
        last = 31
    else:
        last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last))


@dataclass(frozen=True)
class HistPayment:
    """
    One historical dividend: when it went ex, and how much per share in EUR.

    ``per_share_eur`` is None when the amount can't be established (an IBKR row
    with no per-share figure and no usable share count). Such a payment still
    counts towards the cadence — the date is evidence of the schedule — it just
    can't contribute to the size.

    ``special`` is IBKR's own word for it (the "(Special Dividend)" on the cash
    line): such a payment is never a basis. ``cadence=False`` marks amount evidence
    whose date is not part of the ex-date series the schedule is read from — an
    announced accrual, or an IBKR payment no Yahoo row records — so it can size a
    projection without bending the schedule.
    """
    on_date: date
    per_share_eur: Optional[Decimal] = None
    special: bool = False
    cadence: bool = True


@dataclass(frozen=True)
class ForecastPayment:
    on_date: date
    net_eur: Decimal
    # How the amount was chosen (METHOD_*). Not part of equality: two projections of
    # the same money on the same day are the same projection.
    method: Optional[str] = field(default=None, compare=False)


def infer_gap_days(dates: List[date]) -> Optional[int]:
    """Median gap between the most recent ex-dates, or None when no cadence exists."""
    uniq = sorted(set(dates))[-CADENCE_SAMPLE:]
    if len(uniq) < 2:
        return None
    gaps = [(b - a).days for a, b in zip(uniq, uniq[1:])]
    gap = int(median(gaps))
    if gap < MIN_GAP_DAYS or gap > MAX_GAP_DAYS:
        return None
    return gap


def _per_share(history: List[HistPayment]) -> Optional[Decimal]:
    """
    Median dividend per share over the recent payments — the sizing rule until
    2026-10-04, kept as `SIZING_MEDIAN8` so the backtest can score it.
    """
    recent = sorted(history, key=lambda p: p.on_date)[-CADENCE_SAMPLE:]
    amounts = [p.per_share_eur for p in recent
               if p.per_share_eur is not None and p.per_share_eur > 0]
    if not amounts:
        return None
    value = median(amounts)
    return value if value > 0 else None


def _slot_tolerance(gap: int) -> int:
    """How far from an exact year apart two payments may sit and still be the same
    payment: half a cycle, so a neighbouring payment is never mistaken for it."""
    return max(gap // 2, 10)


def _one_year_back(d: date, years: int = 1) -> date:
    return _add_months(d, -12 * years)


def _nearest(payments: Sequence[HistPayment], target: date, tol: int,
             exclude: Optional[HistPayment] = None) -> Optional[HistPayment]:
    best = None
    for p in payments:
        if p is exclude:
            continue
        dist = abs((p.on_date - target).days)
        if dist <= tol and (best is None or dist < abs((best.on_date - target).days)):
            best = p
    return best


def special_payments(history: Sequence[HistPayment], gap: int) -> List[HistPayment]:
    """
    The payments that are one-offs rather than part of the schedule.

    IBKR's own label wins. Without one, a payment is special when it is more than
    SPECIAL_MULTIPLE times the median of the other payments within a year of it, no
    payment at least half its size sits a year before or after it (a large December
    that comes back every December is the payer's shape, not a special), AND
    something shows it was a one-off:

    - it sat off the schedule, within half a cycle of another payment; or
    - the same payment a year apart EXISTS and was small, and the next payment fell
      back below half of it.

    Without a year-apart payment to compare there is no telling a special from the
    first December of a seasonal fund, and calling the December special would read
    low on every fund whose history starts in a winter — so it is left regular.

    That last condition is what separates a special from a raise. NVDA's step from
    0.01 to 0.25 passes the first two tests exactly as a special would; only what
    came after it can tell them apart, so the newest payment on the regular
    schedule is never called special on our inference — IBKR's label is the one
    source that can say so on the day.

    Needs two neighbours to judge; with fewer, nothing is flagged.
    """
    priced = [p for p in history if p.per_share_eur is not None and p.per_share_eur > 0]
    tol = _slot_tolerance(gap)
    out = [p for p in priced if p.special]
    for p in priced:
        if p.special:
            continue
        neighbours = [q.per_share_eur for q in priced
                      if q is not p and abs((q.on_date - p.on_date).days) <= 366]
        if len(neighbours) < 2:
            continue
        if p.per_share_eur <= SPECIAL_MULTIPLE * median(neighbours):
            continue
        year_apart = [
            q for q in priced for months in (-12, 12)
            if q is not p and abs((q.on_date - _add_months(p.on_date, months)).days) <= tol
        ]
        if any(q.per_share_eur * SPECIAL_MULTIPLE >= p.per_share_eur for q in year_apart):
            continue    # it recurs: the payer's shape
        off_cycle = any(q is not p and abs((q.on_date - p.on_date).days) < gap // 2
                        for q in priced)
        later = [q for q in priced if q.on_date > p.on_date]
        fell_back = bool(later) and min(later, key=lambda q: q.on_date).per_share_eur             * SPECIAL_MULTIPLE < p.per_share_eur
        if off_cycle or (year_apart and fell_back):
            out.append(p)
    return out


def is_steady(regular: Sequence[HistPayment]) -> bool:
    """
    True when the recent payments only rise, or drop once and stay down.

    That is the shape of a company: level payments, a raise now and then (NVDA 0.01
    -> 0.25, TSMC climbing every few quarters), occasionally a cut. A payer whose
    payments dip and come back — a fund's small March after its large December, SK
    Hynix's quarters around its year-end payment, ASML's interims around its final —
    is varying, and each of its payments is best forecast by the same one a year
    earlier.

    A DROP is a payment more than STEADY_TOLERANCE below the one before it. Steady
    means no drop at all, or exactly one drop after which nothing climbs back above
    the level it fell from (a cut, carried).

    ``regular`` is date-sorted, priced, and free of specials.
    """
    if not regular:
        return False
    latest = regular[-1]
    amounts = [p.per_share_eur for p in regular
               if (latest.on_date - p.on_date).days <= STEADY_WINDOW_DAYS][-STEADY_SAMPLE:]
    drops = [i for i in range(1, len(amounts))
             if amounts[i] * STEADY_TOLERANCE < amounts[i - 1]]
    if not drops:
        return True
    if len(drops) > 1:
        return False
    fell_from = amounts[drops[0] - 1]
    return all(a <= fell_from for a in amounts[drops[0]:])


def _size_for(d: date, regular: Sequence[HistPayment], gap: int, sizing: str,
              steady: bool, last_seen: date) -> tuple:
    """``(per_share, method)`` for the payment projected on ``d``."""
    latest = regular[-1].per_share_eur
    if sizing == SIZING_MEDIAN8:
        # Over every priced payment, specials included — exactly what it always did.
        return None, SIZING_MEDIAN8
    if sizing == SIZING_MEAN4:
        recent = [p.per_share_eur for p in regular[-MEAN_SAMPLE:]]
        return sum(recent, Decimal("0")) / len(recent), SIZING_MEAN4
    if sizing == SIZING_LATEST or (sizing == SIZING_AUTO and steady):
        return latest, METHOD_LATEST
    # The same payment in the most recent year it was paid. A projection more than a
    # year out (the horizon reaches the end of next year) looks back two years, so it
    # lands on a real payment rather than on another projection.
    tol = _slot_tolerance(gap)
    for years in (1, 2, 3):
        target = _one_year_back(d, years)
        if target > last_seen + timedelta(days=tol):
            continue
        slot = _nearest(regular, target, tol)
        if slot is not None:
            return slot.per_share_eur, METHOD_SAME_PAYMENT
        break
    # No year-ago payment to repeat — under a year of history, or the slot held a
    # special. The regular level is the honest fallback.
    return latest, METHOD_LATEST


def project_dividends(
    history: List[HistPayment],
    current_shares: Decimal,
    horizon_start: date,
    horizon_end: date,
    as_of: Optional[date] = None,
    sizing: str = SIZING_AUTO,
) -> List[ForecastPayment]:
    """
    Future payments for one security inside [horizon_start, horizon_end].

    ``as_of`` is *now* — the point from which staleness is judged. It is separate
    from ``horizon_start`` because asking about next year must not make every
    payer look stopped: the distance to a future horizon is a property of the
    question, not of the company.

    ``sizing`` picks the amount rule (SIZING_*); everything but the backtest uses
    the default.

    Returns [] whenever an honest projection isn't possible: nothing is held,
    fewer than two past ex-dates, no inferable cadence, no usable per-share
    amount, the payer looks stopped, or the horizon is empty.
    """
    if sizing not in SIZING_METHODS:
        raise ValueError(f"unknown sizing {sizing!r}")
    if current_shares <= 0 or horizon_start > horizon_end or not history:
        return []

    scheduled = [p for p in history if p.cadence]
    gap = infer_gap_days([p.on_date for p in scheduled])
    if gap is None:
        return []

    last_seen = max(p.on_date for p in scheduled)
    if ((as_of or horizon_start) - last_seen).days > STOPPED_AFTER_GAPS * gap:
        return []

    if sizing == SIZING_MEDIAN8:
        flat = _per_share(history)
        if flat is None:
            return []
        regular: List[HistPayment] = []
        steady = True
    else:
        flat = None
        specials = {id(p) for p in special_payments(history, gap)}
        regular = sorted(
            (p for p in history
             if p.per_share_eur is not None and p.per_share_eur > 0
             and id(p) not in specials),
            key=lambda p: p.on_date,
        )
        if not regular:
            return []
        steady = is_steady(regular)

    months = _months_step(gap)

    out: List[ForecastPayment] = []
    step = 1
    nxt = _add_months(last_seen, months) if months else last_seen + timedelta(days=gap)
    while nxt <= horizon_end:
        if nxt >= horizon_start:
            if flat is not None:
                per_share, method = flat, SIZING_MEDIAN8
            else:
                per_share, method = _size_for(nxt, regular, gap, sizing, steady, last_seen)
            out.append(ForecastPayment(on_date=nxt, net_eur=per_share * current_shares,
                                       method=method))
        step += 1
        nxt = (_add_months(last_seen, months * step) if months
               else nxt + timedelta(days=gap))
    return out
