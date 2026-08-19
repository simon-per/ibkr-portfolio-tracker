"""
An earnings datetime must be converted to UTC, not stripped of its offset.

`app/clock.py` states the invariant this depends on: every stored timestamp is a
**naive UTC** datetime, because the columns default to `func.now()` (UTC in SQLite) and
`utc_iso()` serializes by stamping UTC onto a value it assumes already is.
`_extract_earnings` did `earnings_dt.replace(tzinfo=None)`, which discards the offset
and keeps the *wall clock* — so an exchange-local time was written into a UTC column.

Yahoo returns these localized to the listing's exchange, so the error is a different
size for every security and runs in both directions:

* Apple reports 16:30 America/New_York = 20:30 UTC. Stored as 16:30, it stops being
  "upcoming" at 16:30 UTC — 12:30 ET, four hours before the release, before the US
  market has even closed. It reappears under history with `reported_eps` still NULL.
* TSMC reports 14:00 Asia/Taipei = 06:00 UTC. Stored as 14:00, it stays "upcoming" for
  eight hours after it has already happened.

`get_upcoming_earnings` compares the column against `utcnow()`, and `is_upcoming` is
computed off the same comparison, so both surfaces are wrong together — which is why
neither disagrees with the other.

Offline: seeded frames, no Yahoo.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.security import Security
from app.services.fundamentals_service import FundamentalsService

ET = timezone(timedelta(hours=-4))      # America/New_York in October
TAIPEI = timezone(timedelta(hours=8))


class _Row(dict):
    """The subset of a yfinance earnings row `_extract_earnings` reads."""


class _Frame:
    """Minimal stand-in for the earnings_dates DataFrame: an index and `.iterrows()`."""

    empty = False

    def __init__(self, pairs):
        self._pairs = pairs

    def iterrows(self):
        return iter(self._pairs)


def _extract(when):
    service = FundamentalsService.__new__(FundamentalsService)
    security = Security(
        id=1, isin="X0000000001", symbol="TST", description="Test",
        exchange="NASDAQ", currency="USD", conid=1,
    )
    frame = _Frame([(when, _Row({"EPS Estimate": 1.0}))])
    events = service._extract_earnings(security, frame)
    assert len(events) == 1
    return events[0]


def test_an_eastern_release_is_stored_as_utc():
    """20:30 UTC, not the 16:30 wall clock."""
    event = _extract(datetime(2026, 10, 29, 16, 30, tzinfo=ET))
    assert event["earnings_date"] == datetime(2026, 10, 29, 20, 30)
    assert event["earnings_date"].tzinfo is None, "the column is naive by convention"


def test_an_asian_release_is_stored_as_utc():
    """The mirror direction — 06:00 UTC, not 14:00."""
    event = _extract(datetime(2026, 10, 15, 14, 0, tzinfo=TAIPEI))
    assert event["earnings_date"] == datetime(2026, 10, 15, 6, 0)


def test_upcoming_is_judged_against_the_converted_time():
    """
    The consequence, rather than the representation. An Eastern release four hours from
    now must still read as upcoming; under the old handling it read as past, because
    16:30 stored against a 17:00 UTC clock is in the past while 20:30 is not.
    """
    now = datetime.now(timezone.utc)
    soon_et = (now + timedelta(hours=4)).astimezone(ET)
    event = _extract(soon_et)
    assert event["is_upcoming"] is True


def test_a_naive_input_is_left_alone():
    """
    Yahoo does not always localize. A naive value carries no offset to convert, so it
    must pass through untouched rather than being assumed to be in some timezone.
    """
    event = _extract(datetime(2026, 10, 29, 16, 30))
    assert event["earnings_date"] == datetime(2026, 10, 29, 16, 30)
