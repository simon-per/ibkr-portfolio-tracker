"""
The one date-window guard every `start_date`/`end_date` route goes through.

It was hand-written four times (value-over-time, benchmark, activity, performance) and
missing from two (attribution, annualized-return), where `start_date=0001-01-01` reached
`start_date - timedelta(days=14)` in the price preload and answered 500 instead of 400.
`tests/test_window_guard_family.py` fails for a route that takes the window without
calling one of these.

Three refusals, each a 400:
- a date outside `EARLIEST_DATE..LATEST_DATE`, so no service's look-back or look-ahead
  arithmetic can overflow `date` (and a 9-day window in year 1 passes the span cap);
- an inverted window — strict (`start < end`) for a return over the window, which a
  zero-length one has none of; inclusive for a series or a ledger, where one day is a
  valid answer;
- a span over `MAX_WINDOW_DAYS`. The route is anonymous, so the window has to be bounded
  somewhere, and `frontend/src/lib/dateRanges.ts` pins `MAX_RANGE_DAYS` **equal** to it
  (`tests/test_range_limit_agreement.py`): the ALL button sends exactly this span.
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import HTTPException

MAX_WINDOW_DAYS = 365 * 5
DEFAULT_WINDOW_DAYS = 365

EARLIEST_DATE = date(1900, 1, 1)
LATEST_DATE = date(2199, 12, 31)


def _check_in_range(name: str, value: date) -> None:
    if not EARLIEST_DATE <= value <= LATEST_DATE:
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be between {EARLIEST_DATE} and {LATEST_DATE}",
        )


def validate_window(start_date: date, end_date: date, *, allow_same_day: bool = False) -> None:
    """Refuse an out-of-range, inverted or too-long window with a 400."""
    _check_in_range("start_date", start_date)
    _check_in_range("end_date", end_date)
    if allow_same_day:
        if start_date > end_date:
            raise HTTPException(
                status_code=400, detail="start_date must be before or equal to end_date"
            )
    elif start_date >= end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date")
    if (end_date - start_date).days > MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large. Maximum allowed is {MAX_WINDOW_DAYS} days (5 years)",
        )


def default_window(
    start_date: Optional[date], end_date: Optional[date]
) -> tuple[date, date]:
    """
    Default an optional window to the year ending today, then validate it (one day allowed).

    A year rather than everything: the activity ledger is the landing view of a tab, and an
    unbounded default would make the first paint the most expensive query on the site.
    `end_date` is range-checked before the default start is derived from it, so the
    subtraction itself cannot overflow.
    """
    end_date = end_date or date.today()
    _check_in_range("end_date", end_date)
    start_date = start_date or (end_date - timedelta(days=DEFAULT_WINDOW_DAYS))
    validate_window(start_date, end_date, allow_same_day=True)
    return start_date, end_date
