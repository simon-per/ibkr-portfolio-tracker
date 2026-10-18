"""
Every timestamp in this application is naive UTC, and nothing enforced it.

`datetime.now()` returns naive *local* time. It was used at ~49 sites and was
only correct because `python:3.11-slim` sets no `TZ`, so the container's local
clock happens to be UTC — an undeclared dependency on the base image that
nothing would have noticed breaking, and that was already wrong on every
developer machine.

The offset lands in three places at once: cache cutoffs expire early, ages read
too old, and `utc_iso()` labels local time as UTC so the browser converts it a
second time. That last one is the "two clocks on one line" failure `utc_iso`'s
own docstring describes, reintroduced through its argument.

`app.clock.utcnow()` is the fix; these tests are what stop the old call from
coming back.
"""

import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.clock import utc_today, utcnow

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Argument-less `now()` only. `datetime.now(timezone.utc)` is correct and is what
# `utcnow()` itself calls, so the pattern must not match it.
NAIVE_LOCAL_NOW = re.compile(r"\bdatetime\.now\(\s*\)")
# Deprecated since 3.12 and returns a naive value whose UTC-ness is invisible at
# the call site; `utcnow()` says it in the name.
DEPRECATED_UTCNOW = re.compile(r"\bdatetime\.utcnow\(")
# The local *date*, with the same missing-`TZ` dependency as `now()`: a day ahead of
# UTC from 00:00 to 02:00 on a Berlin machine in summer (issue #45). A site that means
# an exchange-local or Berlin day converts `utcnow()` into that zone explicitly.
LOCAL_TODAY = re.compile(r"\bdate\.today\(")


def _python_sources() -> list[Path]:
    return [p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_utcnow_is_naive():
    """
    Naive on purpose: it is compared against columns holding naive datetimes, and
    an aware value there raises TypeError rather than degrading.
    """
    assert utcnow().tzinfo is None


def test_utcnow_actually_returns_utc():
    """
    The whole point. On a machine whose local time is not UTC — every developer
    machine here — this is the assertion `datetime.now()` failed.
    """
    reference = datetime.now(timezone.utc).replace(tzinfo=None)
    drift = abs((utcnow() - reference).total_seconds())
    assert drift < 5, f"utcnow() is {drift}s from UTC"


def test_utcnow_is_not_local_time_unless_the_host_is_utc():
    """
    Guards the replacement itself rather than the helper: if `utcnow()` were
    quietly reverted to local time, this fails anywhere with a non-zero offset
    and stays silent on the UTC container, which is exactly the blind spot.
    """
    local_offset = datetime.now().astimezone().utcoffset()
    if local_offset is None or local_offset.total_seconds() == 0:
        pytest.skip("host is on UTC, so local and UTC are indistinguishable here")
    naive_local = datetime.now()
    assert abs((utcnow() - naive_local).total_seconds()) > 60


def test_utc_today_is_the_utc_calendar_day():
    """The UTC date, not the host's: brackets the call so a midnight tick cannot flake."""
    before = datetime.now(timezone.utc).date()
    today = utc_today()
    after = datetime.now(timezone.utc).date()
    assert type(today) is date
    assert before <= today <= after


@pytest.mark.parametrize(
    "pattern,replacement",
    [
        (NAIVE_LOCAL_NOW, "app.clock.utcnow()"),
        (DEPRECATED_UTCNOW, "app.clock.utcnow()"),
        (LOCAL_TODAY, "app.clock.utc_today()"),
    ],
    ids=["datetime.now()", "datetime.utcnow()", "date.today()"],
)
def test_no_module_reaches_for_a_local_or_deprecated_clock(pattern, replacement):
    offenders = []
    for path in _python_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # The ban is described in clock.py's own docstring, which has to be
            # able to name what it replaces.
            if path.name == "clock.py":
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(APP_DIR)}:{lineno}: {line.strip()}")

    assert not offenders, (
        f"Use {replacement} instead — every stored timestamp here is naive UTC.\n"
        + "\n".join(offenders)
    )
