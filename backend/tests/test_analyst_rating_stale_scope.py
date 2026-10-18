"""
`/api/analyst-ratings/status` must not count as stale a row the sync never refreshes
(GitHub issue #21).

The status route ran its own 3-day query over every `analyst_ratings` row, while
`sync_stale_ratings` walks only `sync_scope()` (held and Yahoo-eligible). A sold
security's row (or a `price_source` opt-out's) therefore stayed "stale" forever.
Fixed as fundamentals was: one `STALE_AFTER` and one scoped `_stale` in the repository,
`count_stale_ratings` for `/status`. The family form, both tables and every category,
is `tests/test_staleness_predicate_family.py`.

Offline: `fetch_rating_for_security` is faked.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.clock import utcnow
import app.models  # noqa: F401
from app.models.analyst_rating import AnalystRating
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.routers.analyst_ratings import get_analyst_ratings_status
from app.services.analyst_rating_service import AnalystRatingService


@pytest.fixture
def fetched(monkeypatch):
    seen = []

    async def _fake_fetch(_self, security):
        seen.append(security.symbol)
        return {"security_id": security.id, "strong_buy": 1, "buy": 0,
                "hold": 0, "sell": 0, "strong_sell": 0}

    monkeypatch.setattr(AnalystRatingService, "fetch_rating_for_security", _fake_fetch)
    return seen


@pytest.mark.asyncio
async def test_status_stale_count_matches_what_a_sync_would_refresh(db, fetched):
    db.add(Security(id=1, isin="US0000000001", symbol="SOLD", description="SOLD",
                    currency="USD", conid=1, asset_category="STK", exchange="NASDAQ"))
    db.add(TaxLot(security_id=1, open_date=date(2025, 1, 2), close_date=date(2026, 1, 2),
                  is_open=False, quantity=Decimal("10"), cost_basis=Decimal("1000"),
                  price_per_unit=Decimal("100"), currency="USD",
                  cost_basis_eur=Decimal("900")))
    db.add(AnalystRating(security_id=1, strong_buy=1, buy=0, hold=0, sell=0,
                         strong_sell=0, last_updated=utcnow() - timedelta(days=10)))
    await db.commit()

    result = await AnalystRatingService(db).sync_stale_ratings()
    assert fetched == [] and result["securities_processed"] == 0  # the sync skips it

    status = await get_analyst_ratings_status(db=db)
    # Was 1: a row no sync will ever refresh. Out of scope, it is in no figure at all.
    assert status["stale_ratings"] == 0
    assert (status["total_ratings"], status["fresh_ratings"]) == (0, 0)
