"""
The Company tab's Yahoo chunk for the companies nobody holds (`company_profile_sync`).

Rule 1, owner-approved on 2026-11-07 for exactly this budget: each such company once per
`ROTATION_DAYS`, a chunk a run, oldest first, never a held company, abandoned on a 429 with
nothing stamped. The same readers as the held pass (`extract_metrics`, `extract_earnings`,
`fetch_counts`), and refuse-whole per company.

Offline: the two fetchers are replaced on their classes; no request leaves the machine.
"""

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

import app.models  # noqa: F401  register all mappers
from app.clock import utc_today, utcnow
from app.models.company_financials import (
    CompanyFinancial,
    CompanyYahooProfile,
    SecCompany,
    SecurityCik,
)
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services import company_profile_sync as chunk_module
from app.services.analyst_rating_service import AnalystRatingService
from app.services.company_profile_sync import (
    REQUESTS_PER_COMPANY,
    ROTATION_DAYS,
    RUNS_PER_ROTATION,
    RUNS_PER_WEEK,
    CompanyProfileSync,
    chunk_size,
)
from app.services.fundamentals_service import FundamentalsService
from app.services.scheduler_service import FUNDAMENTALS_REFRESH_DAYS

HELD_CIK = 1045810
COUNTS = {"strong_buy": 5, "buy": 10, "hold": 3, "sell": 0, "strong_sell": 0}


async def _no_sleep(*_a, **_kw):
    return None


def _company(db, cik, ticker, stamp):
    db.add(
        SecCompany(
            cik=cik, ticker=ticker, name=f"{ticker} Corp", universe_seen_at=stamp
        )
    )
    db.add(
        CompanyFinancial(
            cik=cik,
            period_end=date(2026, 6, 30),
            fiscal_year=2026,
            fiscal_period="Q2",
            metric="revenue",
            value=100.0,
            unit="USD",
            source_concept="X",
            filed=date(2026, 7, 30),
            fetched_at=utcnow(),
        )
    )


async def _seed(db, n):
    """`n` index-only companies T00…, and one held company the chunk must never ask."""
    stamp = utcnow()
    for i in range(n):
        _company(db, 100 + i, f"T{i:02d}", stamp)
    _company(db, HELD_CIK, "NVDA", stamp)
    db.add(
        Security(
            id=1,
            isin="US67066G1040",
            symbol="NVDA",
            description="NVIDIA",
            currency="USD",
            conid=1,
            asset_category="STK",
            exchange="NASDAQ",
        )
    )
    db.add(
        TaxLot(
            security_id=1,
            open_date=utc_today() - timedelta(days=100),
            is_open=True,
            quantity=Decimal("1"),
            cost_basis=Decimal("100"),
            price_per_unit=Decimal("100"),
            currency="USD",
            cost_basis_eur=Decimal("100"),
        )
    )
    db.add(
        SecurityCik(
            security_id=1, cik=HELD_CIK, sec_ticker="NVDA", cik_checked_at=utcnow()
        )
    )
    await db.commit()


@pytest.fixture
def yahoo(monkeypatch):
    """The two fetchers, faked on their classes; `behaviour[ticker]` picks the answer."""
    state = {"calls": [], "behaviour": {}}

    async def fetch(self, ticker):
        state["calls"].append(("yahoo", ticker))
        how = state["behaviour"].get(ticker)
        if how == "429":
            raise RuntimeError("429 Too Many Requests")
        if how == "empty":
            return ({}, None, None, None, None)
        info = {
            "forwardPE": 25.0,
            "targetMeanPrice": 200.0,
            "numberOfAnalystOpinions": 40,
            "currency": "USD",
            "quoteType": "EQUITY",
            "longBusinessSummary": f"  {ticker} makes things.  ",
        }
        earnings = pd.DataFrame(
            {
                "EPS Estimate": [1.5, 1.2],
                "Reported EPS": [None, 1.3],
                "Surprise(%)": [None, 8.3],
            },
            index=pd.DatetimeIndex(
                [
                    pd.Timestamp(utcnow() + timedelta(days=20)),
                    pd.Timestamp(utcnow() - timedelta(days=70)),
                ]
            ),
        )
        return (info, earnings, None, None, None)

    async def counts(self, ticker, label):
        state["calls"].append(("ratings", ticker))
        return (
            None
            if state["behaviour"].get(ticker) == "no-rating-answer"
            else dict(COUNTS)
        )

    monkeypatch.setattr(FundamentalsService, "_fetch_yahoo_data", fetch)
    monkeypatch.setattr(AnalystRatingService, "fetch_counts", counts)
    monkeypatch.setattr(chunk_module.random, "uniform", lambda *_a, **_kw: 0)
    return state


def _sync(db):
    return CompanyProfileSync(db, sleep=_no_sleep)


def _asked(state):
    return [t for kind, t in state["calls"] if kind == "yahoo"]


def test_the_rotation_is_derived_and_pinned_to_the_schedule():
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    first, last = FUNDAMENTALS_REFRESH_DAYS.split("-")
    assert RUNS_PER_WEEK == days.index(last) - days.index(first) + 1, (
        "the chunk's runs per week must be the 05:00 job's"
    )
    assert RUNS_PER_ROTATION == ROTATION_DAYS * RUNS_PER_WEEK // 7 == 10
    assert chunk_size(84) == 9
    assert chunk_size(0) == 0
    # The owner's budget: about 60 Yahoo requests a run, for a Nasdaq-100's worth.
    assert chunk_size(100) * REQUESTS_PER_COMPANY <= 70


@pytest.mark.asyncio
async def test_a_run_asks_a_chunk_oldest_first_and_never_a_held_company(db, yahoo):
    await _seed(db, 25)  # chunk of 3
    # Twenty-two were written over two weeks ago, oldest T21; three never.
    for i in range(22):
        db.add(
            CompanyYahooProfile(
                cik=100 + i, fetched_at=utcnow() - timedelta(days=15, hours=i)
            )
        )
    await db.commit()

    summary = await _sync(db).refresh()

    assert summary["in_scope"] == 25 and summary["due"] == 25
    assert _asked(yahoo) == ["T22", "T23", "T24"]
    assert "NVDA" not in _asked(yahoo)
    assert summary["refreshed"] == 3 and summary["rate_limited"] is False

    row = await db.get(CompanyYahooProfile, 122)
    assert (row.forward_pe, row.target_mean_price, row.number_of_analysts) == (
        25.0,
        200.0,
        40,
    )
    assert row.business_summary == "T22 makes things."
    assert (row.strong_buy, row.buy, row.hold) == (5, 10, 3)
    assert row.next_eps_estimate == 1.5
    assert row.next_earnings_date > utcnow()
    assert row.fetched_at is not None and row.miss_count is None
    assert await db.get(CompanyYahooProfile, HELD_CIK) is None

    # The next run takes the next oldest: T21, T20, T19.
    yahoo["calls"].clear()
    await _sync(db).refresh()
    assert _asked(yahoo) == ["T21", "T20", "T19"]


@pytest.mark.asyncio
async def test_a_company_written_within_the_rotation_is_not_asked(db, yahoo):
    await _seed(db, 5)
    for i in range(5):
        db.add(
            CompanyYahooProfile(
                cik=100 + i, fetched_at=utcnow() - timedelta(days=ROTATION_DAYS - 1)
            )
        )
    await db.commit()

    summary = await _sync(db).refresh()

    assert yahoo["calls"] == []
    assert summary["due"] == 0 and summary["asked"] == 0


@pytest.mark.asyncio
async def test_a_rate_limit_abandons_the_chunk_and_stamps_nothing(db, yahoo):
    await _seed(db, 25)
    yahoo["behaviour"]["T00"] = "429"

    summary = await _sync(db).refresh()

    assert _asked(yahoo) == ["T00"], "asked Yahoo again after its 429"
    assert summary["rate_limited"] is True
    assert summary["warnings"][0].startswith("Yahoo Finance rate limit reached")
    assert (await db.execute(select(CompanyYahooProfile))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_failed_rating_refuses_the_whole_company_and_backs_it_off(db, yahoo):
    await _seed(db, 25)
    yahoo["behaviour"]["T00"] = "no-rating-answer"
    yahoo["behaviour"]["T01"] = "empty"

    summary = await _sync(db).refresh()

    assert (
        summary["errors"] == 1 and summary["no_data"] == 1 and summary["refreshed"] == 1
    )
    for cik in (100, 101):
        row = await db.get(CompanyYahooProfile, cik)
        assert row.fetched_at is None and row.forward_pe is None, (
            "half an answer was written"
        )
        assert row.miss_count == 1 and row.checked_at is not None

    yahoo["calls"].clear()
    summary = await _sync(db).refresh()
    assert "T00" not in _asked(yahoo) and "T01" not in _asked(yahoo)
    assert summary["backing_off"] == 2


def test_the_chunk_reads_no_yahoo_answer_of_its_own():
    """One `.info` reader (`extract_metrics`), one recommendations reader (`fetch_counts`)."""
    source = Path(chunk_module.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"info\.get\(|info\[|\.recommendations|import yfinance", source
    )
