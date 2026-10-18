"""
Issue #30: /attribution and /annualized-return skipped the window guard their siblings
had, so `start_date=0001-01-01` reached `start_date - timedelta(days=14)` in
`_preload_market_prices` and answered 500. Their cost does not grow with the window (two
point valuations over preloaded rows); the defect was unvalidated input. Every window
route now goes through `app/routers/_windows.py` (the family is pinned in
`test_window_guard_family.py`). Offline: an in-memory DB through dependency override, no
lifespan (no scheduler, no network).
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.database import get_db
from app.main import app
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from tests.memory_db import create_schema, new_memory_engine


@pytest.fixture
def client():
    import asyncio

    engine = new_memory_engine()

    async def seed():
        await create_schema(engine)
        async with AsyncSession(engine) as s:
            s.add(Security(id=1, isin="US0000000031", symbol="A", description="A",
                           currency="EUR", conid=31, asset_category="STK", exchange="X"))
            await s.flush()
            s.add(TaxLot(security_id=1, open_date=date(2026, 1, 5), is_open=True,
                         quantity=Decimal("1"), cost_basis=Decimal("1"),
                         cost_basis_eur=Decimal("1"), price_per_unit=Decimal("1"),
                         currency="EUR"))
            s.add(MarketPrice(security_id=1, date=date(2026, 3, 2), close_price=Decimal("2"),
                              currency="EUR", source="test"))
            await s.commit()

    asyncio.run(seed())

    async def override():
        async with AsyncSession(engine, expire_on_commit=False) as s:
            yield s

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


@pytest.mark.parametrize("path", [
    "/api/portfolio/value-over-time",
    "/api/portfolio/attribution",
    "/api/portfolio/annualized-return",
    "/api/performance/decomposition",
])
def test_an_extreme_window_is_refused_not_a_server_error(client, path):
    r = client.get(f"{path}?start_date=0001-01-01&end_date=2026-03-02")
    assert r.status_code == 400, f"{path} answered {r.status_code}"


@pytest.mark.parametrize("path", [
    "/api/portfolio/attribution",
    "/api/portfolio/annualized-return",
])
@pytest.mark.parametrize("query", [
    # Short enough to pass the span cap: only the date range check stops these.
    "start_date=0001-01-01&end_date=0001-01-10",
    "start_date=9999-12-01&end_date=9999-12-31",
])
def test_a_short_window_at_the_edge_of_the_calendar_is_refused(client, path, query):
    r = client.get(f"{path}?{query}")
    assert r.status_code == 400, f"{path}?{query} answered {r.status_code}"


@pytest.mark.parametrize("path", [
    "/api/portfolio/attribution",
    "/api/portfolio/annualized-return",
])
def test_the_five_year_cap_holds_on_the_return_routes(client, path):
    end = date(2026, 3, 2)
    exactly = date.fromordinal(end.toordinal() - 365 * 5)
    one_more = date.fromordinal(exactly.toordinal() - 1)

    ok = client.get(f"{path}?start_date={exactly}&end_date={end}")
    assert ok.status_code == 200, ok.text
    too_long = client.get(f"{path}?start_date={one_more}&end_date={end}")
    assert too_long.status_code == 400
    assert "Date range too large" in too_long.json()["detail"]


@pytest.mark.parametrize("path", [
    "/api/portfolio/attribution",
    "/api/portfolio/annualized-return",
])
def test_the_return_routes_still_refuse_a_zero_length_window(client, path):
    r = client.get(f"{path}?start_date=2026-03-02&end_date=2026-03-02")
    assert r.status_code == 400
    assert r.json()["detail"] == "start_date must be before end_date"


def test_an_out_of_range_end_date_cannot_overflow_the_default_start(client):
    """value-over-time derives the default start from end_date; that subtraction must not run first."""
    r = client.get("/api/portfolio/value-over-time?end_date=0001-01-05")
    assert r.status_code == 400
