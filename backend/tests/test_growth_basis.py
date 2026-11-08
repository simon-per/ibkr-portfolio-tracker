"""
Revenue and EPS growth from the filings where they are stored, and saying what it compares
(review item Y1, 2026-11-08).

yfinance returns at most five quarters, so the "TTM" growth Yahoo's rows carry is the
latest quarter against the same quarter a year before (NVDA's revenue +105.85% where its
TTM is +83.4%). Wherever a company's SEC figures are stored, Fundamentals and the watchlist
serve their TTM-over-TTM growth (`growth_basis: "ttm"`); elsewhere Yahoo's stays, marked
`"quarter"`. Pure DB; `yf.Ticker` stubbed for the watchlist sync.
"""
from datetime import date

import pytest

import app.models  # noqa: F401
from app.clock import utcnow
from app.models.company_financials import CompanyFinancial, SecCompany
from app.models.fundamental_metrics import FundamentalMetrics
from app.routers.watchlist import get_watchlist
from app.services.fundamentals_service import FundamentalsService
from app.services.watchlist_service import WatchlistService
from tests.test_company_yahoo_family import _held
from tests.test_watchlist_ratings import _item, reads  # noqa: F401 - the fixture

QUARTERS = [date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31),
            date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)]


async def _filed(db, cik):
    db.add(SecCompany(cik=cik, ticker="FILED", fetched_at=utcnow()))
    for i, end in enumerate(QUARTERS):
        for metric, value in (("revenue", 100.0 if i < 4 else 125.0),
                              ("eps_diluted", 1.0 if i < 4 else 1.5)):
            db.add(CompanyFinancial(
                cik=cik, period_end=end, fiscal_year=end.year, fiscal_period=f"Q{i % 4 + 1}",
                metric=metric, value=value, unit="USD", source_concept="X",
                filed=end, fetched_at=utcnow(),
            ))


@pytest.mark.asyncio
async def test_fundamentals_serve_the_filings_ttm_and_mark_yahoos_as_a_quarter(db):
    await _held(db, 1, "FILED", cik=777)
    await _held(db, 2, "YAHOO")
    await _filed(db, 777)
    for sid in (1, 2):
        db.add(FundamentalMetrics(security_id=sid, last_updated=utcnow(),
                                  revenue_growth=1.0585, earnings_growth=0.9))
    await db.commit()

    rows = {r["symbol"]: r for r in await FundamentalsService(db).get_fundamentals_for_portfolio()}
    assert (rows["FILED"]["revenue_growth"], rows["FILED"]["earnings_growth"],
            rows["FILED"]["growth_basis"]) == (pytest.approx(0.25), pytest.approx(0.5), "ttm")
    assert (rows["YAHOO"]["revenue_growth"], rows["YAHOO"]["growth_basis"]) == (1.0585, "quarter")


@pytest.mark.asyncio
async def test_the_watchlist_serves_the_same_for_a_held_ticker(db, reads):  # noqa: F811
    await _held(db, 1, "FILED", cik=777)
    await _filed(db, 777)
    db.add(FundamentalMetrics(security_id=1, last_updated=utcnow(), revenue_growth=1.0585))
    await db.commit()
    await _item(db, "FILED")

    await WatchlistService(db).sync_item("FILED", force=True)
    (row,) = [r for r in await get_watchlist(db=db) if r.yahoo_ticker == "FILED"]
    assert (row.revenue_growth, row.earnings_growth, row.growth_basis) == (
        pytest.approx(0.25), pytest.approx(0.5), "ttm")
