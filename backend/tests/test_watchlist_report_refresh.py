"""
The watchlist re-reads its growth columns the run after a company reports.

They were carried for `ESTIMATES_REFRESH_AFTER` (7 days), so after AMD's 2026-11-03 report
the watchlist still showed the old quarter's revenue and EPS growth while Fundamentals,
asked daily, showed the new one. `.info` — read every run anyway — carries the newest
reported quarter (`mostRecentQuarter`); a later one than the last run saw fetches the
statements and estimates that run. Offline: `yf.Ticker` is a stub that records every read.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import app.models  # noqa: F401
import app.services.watchlist_service as watchlist_module
from app.clock import utcnow
from app.models.watchlist_item import WatchlistItem
from app.services.watchlist_service import (
    WatchlistService,
    new_quarter,
    reported_quarter,
)

SLOW = {"quarterly_financials", "revenue_estimate"}
Q2, Q3 = date(2026, 6, 27), date(2026, 9, 26)


def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def yahoo(monkeypatch):
    state = {"quarter": Q3, "reads": []}

    class _Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        @property
        def info(self):
            state["reads"].append("info")
            info = {"quoteType": "EQUITY", "symbol": self.ticker, "currentPrice": 100.0}
            if state["quarter"] is not None:
                info["mostRecentQuarter"] = _epoch(state["quarter"])
            return info

        def history(self, period=None):
            return None

        @property
        def quarterly_financials(self):
            state["reads"].append("quarterly_financials")
            return None

        @property
        def revenue_estimate(self):
            state["reads"].append("revenue_estimate")
            return None

        @property
        def recommendations(self):
            return None

    monkeypatch.setattr(watchlist_module.yf, "Ticker", _Ticker)
    monkeypatch.setattr(watchlist_module.random, "uniform", lambda *_a, **_kw: 0)
    return state


async def _item(db, known):
    item = WatchlistItem(
        yahoo_ticker="AMD",
        estimates_synced_at=utcnow() - timedelta(days=2),
        ratings_synced_at=utcnow() - timedelta(days=2),
        reported_quarter=known,
        revenue_growth=0.50,
        earnings_growth=1.55,
    )
    db.add(item)
    await db.commit()
    return item


@pytest.mark.asyncio
async def test_a_new_quarter_fetches_the_slow_fields_that_run(db, yahoo):
    item = await _item(db, known=Q2)

    await WatchlistService(db).sync_item("AMD", force=True)
    await db.refresh(item)

    assert SLOW <= set(yahoo["reads"])
    assert item.reported_quarter == Q3
    assert utcnow() - item.estimates_synced_at < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_the_same_quarter_carries_the_slow_fields(db, yahoo):
    item = await _item(db, known=Q3)

    await WatchlistService(db).sync_item("AMD", force=True)
    await db.refresh(item)

    assert not SLOW & set(yahoo["reads"])
    assert (item.revenue_growth, item.earnings_growth) == (0.50, 1.55)


@pytest.mark.asyncio
async def test_the_first_sighting_records_the_quarter_and_fetches_nothing_extra(
    db, yahoo
):
    item = await _item(db, known=None)

    await WatchlistService(db).sync_item("AMD", force=True)
    await db.refresh(item)

    assert not SLOW & set(yahoo["reads"])
    assert item.reported_quarter == Q3


@pytest.mark.asyncio
async def test_a_missing_quarter_keeps_the_stored_one(db, yahoo):
    yahoo["quarter"] = None
    item = await _item(db, known=Q2)

    await WatchlistService(db).sync_item("AMD", force=True)
    await db.refresh(item)

    assert not SLOW & set(yahoo["reads"])
    assert item.reported_quarter == Q2


@pytest.mark.asyncio
async def test_an_older_answer_never_moves_the_stored_quarter_back(db, yahoo):
    """
    Y8: Yahoo alternating between two `mostRecentQuarter` values stored the older over the
    newer, so the next run saw a "new" quarter and paid the slow fetch every other run.
    """
    item = await _item(db, known=Q3)
    slow_runs = []
    for quarter in (Q2, Q3, Q2, Q3):
        yahoo["quarter"], yahoo["reads"] = quarter, []
        await WatchlistService(db).sync_item("AMD", force=True)
        await db.refresh(item)
        slow_runs.append(bool(SLOW & set(yahoo["reads"])))
        assert item.reported_quarter == Q3
    assert slow_runs == [False, False, False, False]


@pytest.mark.parametrize("value", [None, "1751241600", True, 0, -5, 10**20])
def test_anything_but_a_timestamp_is_no_quarter(value):
    assert reported_quarter({"mostRecentQuarter": value}) is None
    assert not new_quarter({"mostRecentQuarter": value}, Q2)


def test_an_older_quarter_is_not_a_new_one():
    assert not new_quarter({"mostRecentQuarter": _epoch(Q2)}, Q3)
