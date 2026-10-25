"""
The Company tab's price pass (`company_price_sync`, docs/company-financials.md *Prices*):
one Yahoo history per company kept as weekly closes, on one split basis, bounded.

Offline: the request (`CompanyPriceSync._history`) is replaced by a fake that answers
from a made-up daily history and records what it was asked.
"""

from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from app.clock import utcnow
from app.models.company_financials import (
    CompanyFinancial,
    CompanyPrice,
    CompanySplit,
    SecCompany,
)
from app.services.company_price_sync import (
    HISTORY_LEAD_DAYS,
    OVERLAP_DAYS,
    RATE_LIMITED_WARNING,
    CompanyPriceSync,
    weekly_closes,
)

LAST = date(2026, 10, 23)  # a Friday
FIRST_PERIOD = date(2024, 3, 31)


def _weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _frame(days, close, splits=None):
    days = list(days)
    return pd.DataFrame(
        {
            "Close": [close(d) for d in days],
            "Stock Splits": [(splits or {}).get(d, 0.0) for d in days],
        },
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]),
    )


class FakeYahoo:
    """Answers `_history(ticker, start)` from `close(day)`; `start=None` is the whole history."""

    def __init__(self, close=lambda d: 100.0, splits=None, currency="USD", last=LAST):
        self.close, self.splits, self.currency, self.last = (
            close,
            splits,
            currency,
            last,
        )
        self.calls = []
        self.fail = {}

    async def __call__(self, ticker, start):
        self.calls.append((ticker, start))
        if ticker in self.fail:
            raise self.fail[ticker]
        days = _weekdays(start or date(2020, 1, 1), self.last)
        return _frame(days, self.close, self.splits), self.currency


async def _seed(db, *tickers):
    for i, ticker in enumerate(tickers):
        cik = 1000 + i
        db.add(SecCompany(cik=cik, ticker=ticker, universe_seen_at=utcnow()))
        db.add(
            CompanyFinancial(
                cik=cik,
                period_end=FIRST_PERIOD,
                fiscal_year=2024,
                fiscal_period="Q1",
                metric="revenue",
                value=1.0,
                unit="USD",
                source_concept="X",
                filed=FIRST_PERIOD + timedelta(days=30),
                fetched_at=utcnow(),
            )
        )
    await db.commit()


def _sync(db, yahoo):
    sync = CompanyPriceSync(db)
    sync._history = yahoo
    return sync


async def _closes(db, cik=1000):
    rows = await db.execute(
        select(CompanyPrice).where(CompanyPrice.cik == cik).order_by(CompanyPrice.date)
    )
    return [(p.date, p.close, p.currency) for p in rows.scalars()]


def test_a_daily_history_keeps_each_weeks_last_close():
    days = list(
        _weekdays(date(2024, 6, 3), date(2024, 6, 19))
    )  # Mon 3 Jun - Wed 19 Jun
    hist = _frame(days, lambda d: float(d.day), splits={date(2024, 6, 10): 10.0})
    hist.loc[pd.Timestamp(date(2024, 6, 14)), "Close"] = float("nan")
    daily, closes, splits = weekly_closes(hist, 1)
    # Friday 14 June has no close: Thursday is that week's last.
    assert closes == [
        (date(2024, 6, 7), 7.0),
        (date(2024, 6, 13), 13.0),
        (date(2024, 6, 19), 19.0),
    ]
    assert splits == [(date(2024, 6, 10), 10.0)]
    assert len(daily) == len(days) - 1
    _, pence, _ = weekly_closes(hist, 100)
    assert pence[0] == (date(2024, 6, 7), 0.07)


@pytest.mark.asyncio
async def test_the_first_fill_asks_for_the_whole_history_and_trims_it(db):
    await _seed(db, "AAPL")
    yahoo = FakeYahoo(splits={date(2020, 8, 31): 4.0})
    summary = await _sync(db, yahoo).refresh()

    assert yahoo.calls == [("AAPL", None)]
    closes = await _closes(db)
    assert (summary["status"], summary["fetched"]) == ("success", 1)
    assert summary["rows_written"] == len(closes)
    assert closes[0][0] >= FIRST_PERIOD - timedelta(days=HISTORY_LEAD_DAYS)
    assert closes[-1] == (LAST, 100.0, "USD")
    assert all(d.weekday() == 4 for d, _, _ in closes)  # no holiday in the fake
    splits = (await db.execute(select(CompanySplit))).scalars().all()
    assert [(s.date, s.ratio) for s in splits] == [(date(2020, 8, 31), 4.0)]
    company = await db.get(SecCompany, 1000)
    assert company.prices_fetched_at is not None and company.prices_miss_count is None


@pytest.mark.asyncio
async def test_a_later_pass_waits_a_week_then_fetches_only_the_tail(db):
    await _seed(db, "AAPL")
    await _sync(db, FakeYahoo(last=LAST - timedelta(days=7))).refresh()
    before = await _closes(db)

    yahoo = FakeYahoo()
    assert (await _sync(db, yahoo).refresh())["companies_due"] == 0
    assert yahoo.calls == []

    summary = await _sync(db, yahoo).refresh(force=True)
    assert yahoo.calls == [("AAPL", before[-1][0] - timedelta(days=OVERLAP_DAYS))]
    assert summary["refetched"] == 0
    after = await _closes(db)
    assert after[: len(before)] == before and after[-1][0] == LAST


@pytest.mark.parametrize(
    "change",
    ["split", "moved", "currency"],
)
@pytest.mark.asyncio
async def test_a_moved_basis_fetches_the_whole_history_again(db, change):
    await _seed(db, "NVDA")
    await _sync(db, FakeYahoo(last=LAST - timedelta(days=7))).refresh()

    split_day = LAST - timedelta(days=3)
    yahoo, close, currency = {
        # Yahoo now adjusts the whole history for a 10:1 split it reports in the window.
        "split": (
            FakeYahoo(close=lambda d: 10.0, splits={split_day: 10.0}),
            10.0,
            "USD",
        ),
        "moved": (FakeYahoo(close=lambda d: 90.0), 90.0, "USD"),
        "currency": (FakeYahoo(currency="EUR"), 100.0, "EUR"),
    }[change]
    summary = await _sync(db, yahoo).refresh(force=True)

    assert [start is None for _, start in yahoo.calls] == [False, True]
    assert summary["refetched"] == 1
    # Replaced as one set: no close of the old basis is left beside the new.
    assert {(c, cur) for _, c, cur in await _closes(db)} == {(close, currency)}


@pytest.mark.asyncio
async def test_an_empty_answer_backs_off(db):
    await _seed(db, "GONE")
    yahoo = FakeYahoo(last=date(2019, 1, 1))  # no weekday from 2020 on
    summary = await _sync(db, yahoo).refresh()
    assert summary["no_data"] == 1 and await _closes(db) == []
    company = await db.get(SecCompany, 1000)
    assert company.prices_miss_count == 1
    assert (await _sync(db, yahoo).refresh())["companies_due"] == 0


@pytest.mark.asyncio
async def test_a_rate_limit_abandons_the_pass(db):
    await _seed(db, "AAPL", "MSFT")
    yahoo = FakeYahoo()
    yahoo.fail["AAPL"] = Exception("429 Client Error: Too Many Requests")
    summary = await _sync(db, yahoo).refresh()
    assert [t for t, _ in yahoo.calls] == ["AAPL"]
    assert (
        summary["status"] == "rate_limited"
        and RATE_LIMITED_WARNING in summary["warnings"]
    )
    # Nothing stamped: the next pass asks again.
    assert (await db.get(SecCompany, 1000)).prices_miss_count is None


@pytest.mark.asyncio
async def test_an_ordinary_error_skips_one_company(db):
    await _seed(db, "AAPL", "MSFT")
    yahoo = FakeYahoo()
    yahoo.fail["AAPL"] = ValueError("bad frame")
    summary = await _sync(db, yahoo).refresh()
    assert [t for t, _ in yahoo.calls] == ["AAPL", "MSFT"]
    assert (summary["errors"], summary["fetched"]) == (1, 1)
    assert summary["warnings"]


@pytest.mark.asyncio
async def test_a_minor_unit_quote_is_stored_in_the_major_currency(db):
    await _seed(db, "SHEL")
    await _sync(db, FakeYahoo(close=lambda d: 2500.0, currency="GBp")).refresh()
    assert (await _closes(db))[-1] == (LAST, 25.0, "GBP")


@pytest.mark.asyncio
async def test_a_dry_run_asks_nothing(db):
    await _seed(db, "AAPL", "MSFT")
    yahoo = FakeYahoo()
    summary = await _sync(db, yahoo).refresh(dry_run=True, symbols=["msft"])
    assert yahoo.calls == [] and summary["due"] == ["MSFT"]


@pytest.mark.asyncio
async def test_the_scheduled_job_records_its_run_and_warnings(db, monkeypatch):
    from app.models.sync_run import SyncRun
    from app.services import scheduler_service
    from app.services.company_price_sync import SCHEDULED_SYNC_TYPE

    await _seed(db, "AAPL")

    class _Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    yahoo = FakeYahoo()
    yahoo.fail["AAPL"] = Exception("429 Client Error: Too Many Requests")
    monkeypatch.setattr(scheduler_service, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(CompanyPriceSync, "_history", lambda self, t, s: yahoo(t, s))
    result = await scheduler_service.SchedulerService().company_prices_refresh_job()

    assert (result["type"], result["status"]) == (SCHEDULED_SYNC_TYPE, "success")
    assert result["warnings"] == [RATE_LIMITED_WARNING]
    run = (
        await db.execute(select(SyncRun).where(SyncRun.sync_type == SCHEDULED_SYNC_TYPE))
    ).scalar_one()
    assert run.warnings == [RATE_LIMITED_WARNING]
