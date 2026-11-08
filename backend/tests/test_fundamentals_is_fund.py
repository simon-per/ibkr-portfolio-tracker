"""
`/api/fundamentals/portfolio` says which rows are funds (review items VI4/R7, 2026-11-08).

The tab split its tables by a client `quote_type !== 'ETF'`, so a MUTUALFUND and the two
NAV-priced 3a funds (no Yahoo quote type at all) were stock rows of dashes. `is_fund` is
the server's predicate: Yahoo's quote type where it answered, else the ETF table's ISIN
predicate, else IBKR's FUND category.
"""
import pytest

import app.models  # noqa: F401
from app.clock import utcnow
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.services.finpension_ingest import FUND_EXCHANGE
from app.services.fundamentals_service import FundamentalsService
from tests.test_company_yahoo_family import _held


@pytest.mark.asyncio
async def test_funds_are_told_apart_by_the_server(db):
    for sid, symbol, quote_type in ((1, "AAA", "EQUITY"), (2, "VT", "ETF"),
                                    (3, "MF", "MUTUALFUND")):
        await _held(db, sid, symbol)
        db.add(FundamentalMetrics(security_id=sid, quote_type=quote_type, last_updated=utcnow()))
    # A 3a fund as finpension_ingest writes it: its ISIN as symbol, exchange FUND, asset_type
    # ETF and no asset_category. The first fixture set asset_category = "FUND", a shape
    # production never has, and the EM fund (no Yahoo quote type) stayed a stock row.
    await _held(db, 4, "CH1529078078")
    fund = await db.get(Security, 4)
    fund.isin, fund.exchange, fund.asset_type, fund.asset_category = (
        "CH1529078078", FUND_EXCHANGE, "ETF", None)
    # An IBKR mutual fund neither Yahoo nor the ETF table knows.
    await _held(db, 5, "MFX")
    (await db.get(Security, 5)).asset_category = "FUND"
    await db.commit()

    rows = {r["symbol"]: r["is_fund"]
            for r in await FundamentalsService(db).get_fundamentals_for_portfolio()}
    assert rows == {"AAA": False, "VT": True, "MF": True, "CH1529078078": True, "MFX": True}
