"""
The price-currency map used by every valuation (timeline, snapshot, realized,
attribution) must be deterministic. It was "whatever row the unordered query
returned last": a mixed-currency price history — the shape left behind by a
wrong ticker mapping — got a random member applied to the security's entire
series, silently mis-scaling market value by a whole FX rate.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.services.portfolio_service import PortfolioService
from tests.memory_db import memory_engine


async def _session_with_prices(rows):
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="CA0000000001", symbol="SBI", description="Test",
                         currency="CAD", conid=100, asset_category="STK", exchange="TSE"))
    for d, close, cur in rows:
        session.add(MarketPrice(security_id=1, date=d, close_price=Decimal(close),
                                currency=cur, source="test"))
    await session.flush()
    return engine, session


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    # Same data, both insert orders — the choice must not depend on row order.
    [(date(2026, 6, 1), "4.7", "USD"), (date(2026, 7, 1), "4.8", "CAD")],
    [(date(2026, 7, 1), "4.8", "CAD"), (date(2026, 6, 1), "4.7", "USD")],
])
async def test_the_newest_rows_currency_wins_regardless_of_order(rows):
    engine, session = await _session_with_prices(rows)
    try:
        svc = PortfolioService(session)
        security = (await session.get(Security, 1))
        _, currency_map = await svc._preload_market_prices(
            {security}, date(2026, 5, 1), date(2026, 7, 28)
        )
        assert currency_map[1] == "CAD"
    finally:
        await session.close()
        await engine.dispose()
