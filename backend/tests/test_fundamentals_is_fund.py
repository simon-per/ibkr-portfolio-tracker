"""
`/api/fundamentals/portfolio` says which rows are funds (review items VI4/R7, 2026-11-08).

The tab split its tables by a client `quote_type !== 'ETF'`, so a MUTUALFUND and the two
NAV-priced 3a funds (no Yahoo quote type at all) were stock rows of dashes. `is_fund` is
the server's predicate: Yahoo's quote type where it answered, else the asset category.
"""
import pytest

import app.models  # noqa: F401
from app.clock import utcnow
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.services.fundamentals_service import FundamentalsService
from tests.test_company_yahoo_family import _held


@pytest.mark.asyncio
async def test_funds_are_told_apart_by_the_server(db):
    for sid, symbol, quote_type in ((1, "AAA", "EQUITY"), (2, "VT", "ETF"),
                                    (3, "MF", "MUTUALFUND")):
        await _held(db, sid, symbol)
        db.add(FundamentalMetrics(security_id=sid, quote_type=quote_type, last_updated=utcnow()))
    await _held(db, 4, "CH0117044948")
    (await db.get(Security, 4)).asset_category = "FUND"
    await db.commit()

    rows = {r["symbol"]: r["is_fund"]
            for r in await FundamentalsService(db).get_fundamentals_for_portfolio()}
    assert rows == {"AAA": False, "VT": True, "MF": True, "CH0117044948": True}
