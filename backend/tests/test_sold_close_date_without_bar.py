"""
A sold security whose last close date has no price bar must not stay in the 18:00 repair
set forever (GitHub issue #33).

`sync_securities` puts a sold security in repair mode while `get_missing_dates(first_open
.. last_close)` is non-empty. A close date the feed has no bar for (a holiday on the
listing's exchange — a heuristic close is stamped with the statement's to_date, a US
trading day; a delisting or merger effective date) is a TRAILING gap of that range, and
trailing gaps never age into holidays. The fetch returned the buffer days' bars, already
cached, so no miss was stamped, no back-off engaged, nothing was written, and the next long
pass asked the same question. The repair probe now forgives a short trailing run at a close
older than HOLIDAY_GRACE_DAYS (`REPAIR_TRAILING_GRACE_WEEKDAYS`); everything else still
repairs, and `get_missing_dates` without the argument still reports trailing gaps, so a
purge refills.

Offline: yf.Ticker / httpx are recording stubs (the harness from test_price_pass_scope).
"""
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import delete

from app.models.market_price import MarketPrice
from app.repositories.market_price_repository import MarketPriceRepository
from app.services import market_data_service as mds
from app.services.market_data_service import REPAIR_TRAILING_GRACE_WEEKDAYS
from app.services.portfolio_service import PRICE_LOOKBACK_DAYS
from tests.test_price_pass_scope import (  # noqa: F401  db is a fixture
    LONG, TODAY, _pass, _security, _weekdays, _written, db,
)

GRACE = MarketPriceRepository.HOLIDAY_GRACE_DAYS


def _weekday_at_least(days_ago: int) -> date:
    day = TODAY - timedelta(days=days_ago)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _feed(monkeypatch, holes=()):
    """Yahoo serving every weekday except `holes`; returns the (ticker, start, end) asked."""
    asked = []

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker
            self._price_history = SimpleNamespace(_history_metadata={"currency": "USD"})

        def history(self, start, end, **_kw):
            asked.append((self.ticker, start, end))
            days = [d for d in _weekdays(start, min(end - timedelta(days=1), TODAY))
                    if d not in holes]
            return pd.DataFrame({"Close": [100.0] * len(days)}, index=pd.to_datetime(days))

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(mds.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(mds.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(mds.random, "uniform", lambda *_: 0)
    return asked


def test_the_forgiven_run_stays_inside_the_valuation_lookback():
    """Valuation finds the close-date price by looking back PRICE_LOOKBACK_DAYS; the last
    bar before a forgiven trailing run must be within reach from any start day."""
    def calendar_span(start: date) -> int:
        day, counted = start, 0
        while counted < REPAIR_TRAILING_GRACE_WEEKDAYS:
            day += timedelta(days=1)
            counted += day.weekday() < 5
        return (day - start).days

    worst = max(calendar_span(date(2026, 1, 5) + timedelta(days=i)) for i in range(7))
    assert worst <= PRICE_LOOKBACK_DAYS


@pytest.mark.asyncio
async def test_a_close_date_without_a_bar_does_not_keep_the_security_in_repair(db, monkeypatch):  # noqa: F811
    close = _weekday_at_least(60)
    asked = _feed(monkeypatch, holes={close})
    # Sold 60 days ago; every weekday of its held span cached except the close date itself.
    await _security(db, 1, "SLD", opened_days_ago=200, closed_days_ago=(TODAY - close).days,
                    prices=(200, (TODAY - close).days + 1))

    for _ in range(3):                 # three daily 18:00 passes
        await _pass(db, LONG)

    ends = [end for _t, _s, end in asked]
    assert ends.count(close + timedelta(days=1)) <= 1, (
        f"asked {ends.count(close + timedelta(days=1))}x for a span that can never complete"
    )
    assert ends and ends[-1] == TODAY + timedelta(days=1), "never graduated to post-sale pricing"


@pytest.mark.asyncio
async def test_a_long_trailing_gap_is_still_repaired_to_the_last_close(db, monkeypatch):  # noqa: F811
    """Several weeks purged before the close: not a missing close bar, a real gap."""
    close = _weekday_at_least(60)
    close_days = (TODAY - close).days
    asked = _feed(monkeypatch)
    await _security(db, 1, "SLD", opened_days_ago=200, closed_days_ago=close_days,
                    prices=(200, close_days + 30))

    await _pass(db, LONG)

    assert [end for _t, _s, end in asked] == [close + timedelta(days=1)]
    assert close in await _written(db, 1)


@pytest.mark.asyncio
async def test_a_leading_gap_is_still_repaired_beside_a_missing_close_bar(db, monkeypatch):  # noqa: F811
    """The close date has no bar, but the start of the span was never fetched: repair it,
    then graduate."""
    close = _weekday_at_least(60)
    close_days = (TODAY - close).days
    asked = _feed(monkeypatch, holes={close})
    await _security(db, 1, "SLD", opened_days_ago=200, closed_days_ago=close_days,
                    prices=(150, close_days + 1))

    await _pass(db, LONG)
    assert [end for _t, _s, end in asked] == [close + timedelta(days=1)]
    assert min(await _written(db, 1)) <= TODAY - timedelta(days=195)

    await _pass(db, LONG)
    assert asked[-1][2] == TODAY + timedelta(days=1)


@pytest.mark.asyncio
async def test_a_young_close_with_an_interior_gap_is_still_repaired(db, monkeypatch):  # noqa: F811
    """A close inside HOLIDAY_GRACE_DAYS forgives nothing: a genuine interior hole (not yet
    old enough to be a holiday) is fetched, to the last close."""
    close = _weekday_at_least(20)
    close_days = (TODAY - close).days
    assert close_days < GRACE
    hole = _weekday_at_least(close_days + 5)
    asked = _feed(monkeypatch, holes={close})
    await _security(db, 1, "SLD", opened_days_ago=200, closed_days_ago=close_days,
                    prices=(200, close_days + 1))
    await db.execute(delete(MarketPrice).where(MarketPrice.date == hole))
    await db.commit()

    await _pass(db, LONG)

    assert [end for _t, _s, end in asked] == [close + timedelta(days=1)]
    assert hole in await _written(db, 1)


@pytest.mark.asyncio
async def test_get_missing_dates_forgives_a_trailing_run_only_when_asked(db):  # noqa: F811
    as_of = date(2026, 10, 16)
    end = date(2026, 8, 19)                      # a Wednesday, older than the grace
    await _security(db, 1, "SLD", opened_days_ago=0)
    run = [end]
    while len(run) < REPAIR_TRAILING_GRACE_WEEKDAYS + 1:
        prev = run[-1] - timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)
        run.append(prev)
    for day in _weekdays(date(2026, 6, 1), run[-1] - timedelta(days=1)):
        db.add(MarketPrice(security_id=1, date=day, close_price=100, currency="USD",
                           source="yahoo_finance"))
    await db.commit()
    repo = MarketPriceRepository(db)

    async def missing(start=date(2026, 6, 1), stop=end, **kw):
        return await repo.get_missing_dates(1, start, stop, as_of=as_of, **kw)

    # Default: a trailing gap stays missing at any age, so a purge refills.
    assert await missing() == sorted(run)
    # One weekday too many to forgive.
    assert await missing(trailing_grace_weekdays=REPAIR_TRAILING_GRACE_WEEKDAYS) == sorted(run)
    # Exactly the grace: forgiven.
    assert await missing(stop=run[1], trailing_grace_weekdays=REPAIR_TRAILING_GRACE_WEEKDAYS) == []
    # The same run at a close younger than the grace: still missing.
    young = await repo.get_missing_dates(
        1, date(2026, 6, 1), run[1], as_of=run[1] + timedelta(days=GRACE - 1),
        trailing_grace_weekdays=REPAIR_TRAILING_GRACE_WEEKDAYS,
    )
    assert young == sorted(run[1:])
    # Nothing cached in the range: nothing to look back to, nothing forgiven.
    assert await missing(start=run[-1], stop=run[1],
                         trailing_grace_weekdays=REPAIR_TRAILING_GRACE_WEEKDAYS) == sorted(run[1:])
