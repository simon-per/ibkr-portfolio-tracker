"""
`GET /api/analyst-ratings/{id}` serves the rating every other surface serves (review item
DR8, 2026-11-08): the company's counts through `rating_of`, and no rating — a 404 — where
Yahoo says no analyst covers it. It served "No Rating" over five zeros there, where the
positions table, the watchlist and the overview serve null.
"""
import pytest
from fastapi import HTTPException

import app.models  # noqa: F401
from app.routers.analyst_ratings import get_analyst_rating
from tests.test_company_yahoo_family import _held


@pytest.mark.asyncio
async def test_no_coverage_is_no_rating(db):
    await _held(db, 1, "AAA", strong_buy=0, buy=0, hold=0, sell=0, strong_sell=0)
    with pytest.raises(HTTPException) as refused:
        await get_analyst_rating(1, db=db)
    assert refused.value.status_code == 404


@pytest.mark.asyncio
async def test_a_listing_serves_its_companys_rating(db):
    await _held(db, 1, "GOOG", cik=1652044, strong_buy=11, buy=43, hold=6, sell=0,
                strong_sell=0)
    await _held(db, 2, "GOOGL", cik=1652044, strong_buy=10, buy=43, hold=5, sell=0,
                strong_sell=0)
    body = await get_analyst_rating(2, db=db)
    assert (body["security_id"], body["strong_buy"], body["hold"], body["consensus"]) == (
        2, 11, 6, "Buy")
