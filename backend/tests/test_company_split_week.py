"""
The week after a held company splits (review item CO3, 2026-11-08).

The Sunday price pass stores the weekly closes, the daily held pass the listing's newest
close. A split mid-week put that daily close on the new share basis while the weekly
closes stayed on the old one until the next Sunday, and appended to them it read as a -90%
week and a tenth of every multiple. With IBKR's record of the split after the newest
weekly close, the daily close waits for the refetch. Pure DB.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

import app.models  # noqa: F401
from app.clock import utcnow
from app.models.company_financials import CompanyFinancial, CompanyPrice, SecCompany
from app.models.corporate_action import CorporateAction
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.services.company_financials_service import CompanyFinancialsService

CIK = 1234


async def _book(db, split: bool):
    db.add(Security(id=1, isin="US0000000077", symbol="SPLT", description="Splitter",
                    currency="USD", conid=77, asset_category="STK", exchange="NASDAQ"))
    db.add(SecCompany(cik=CIK, ticker="SPLT", fetched_at=utcnow()))
    db.add(CompanyFinancial(cik=CIK, period_end=date(2026, 9, 30), fiscal_year=2026,
                            fiscal_period="Q3", metric="revenue", value=1.0, unit="USD",
                            source_concept="X", filed=date(2026, 10, 28), fetched_at=utcnow()))
    d = date(2026, 8, 7)
    while d <= date(2026, 10, 30):
        db.add(CompanyPrice(cik=CIK, date=d, close=1000.0, currency="USD"))
        d += timedelta(days=7)
    db.add(MarketPrice(security_id=1, date=date(2026, 11, 6), close_price=Decimal("100"),
                       currency="USD", source="test"))
    if split:
        db.add(CorporateAction(ib_key="CA1", conid="77", security_id=1, symbol="SPLT",
                               action_date=date(2026, 11, 4), action_type="FORWARDSPLIT",
                               description="SPLT SPLIT 10 FOR 1"))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("split, newest", [(True, 1000.0), (False, 100.0)])
async def test_a_daily_close_after_a_split_waits_for_the_weekly_refetch(db, split, newest):
    await _book(db, split)
    service = CompanyFinancialsService(db)
    rows = await service.repo.rows_for(CIK)
    closes = await service._closes(CIK, rows, "USD", await db.get(Security, 1))
    assert closes[-1][1] == newest
