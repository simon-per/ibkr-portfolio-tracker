"""
Per-security attribution must credit capital coming back out. Without the
disposal term, a position sold mid-period contributed zero to the end value
while its full start value stayed on the books — a profitable sale read as a
total loss and corrupted every contribution_percent in the response.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.portfolio_service import PortfolioService
from tests.memory_db import memory_engine

START = date(2026, 1, 5)
MID = date(2026, 3, 10)
END = date(2026, 6, 5)


async def _make_session():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    for sid, sym in ((1, "SOLD"), (2, "HELD"), (3, "NEW")):
        session.add(Security(id=sid, isin=f"US000000000{sid}", symbol=sym, description=sym,
                             currency="EUR", conid=100 + sid, asset_category="STK",
                             exchange="XETRA"))
    await session.flush()
    return engine, session


def _lot(sid, open_date, qty, cost, close_date=None):
    return TaxLot(
        security_id=sid, open_date=open_date, close_date=close_date,
        is_open=close_date is None, quantity=Decimal(qty),
        cost_basis=Decimal(cost), cost_basis_eur=Decimal(cost),
        price_per_unit=Decimal(cost) / Decimal(qty), currency="EUR",
    )


def _price(sid, d, close):
    return MarketPrice(security_id=sid, date=d, close_price=Decimal(close),
                       currency="EUR", source="test")


@pytest.mark.asyncio
async def test_a_profitable_mid_period_sale_is_not_a_phantom_loss():
    engine, session = await _make_session()
    try:
        session.add_all([
            # SOLD: worth 100 at start, sold at 110 mid-period.
            _lot(1, date(2025, 6, 1), "10", "100", close_date=MID),
            _price(1, START, "10"), _price(1, MID, "11"), _price(1, END, "11"),
            # HELD: 100 -> 120 across the period.
            _lot(2, date(2025, 6, 1), "10", "100"),
            _price(2, START, "10"), _price(2, END, "12"),
            # NEW: bought mid-period for 110, worth 120 at the end.
            _lot(3, MID, "10", "110"),
            _price(3, MID, "11"), _price(3, END, "12"),
        ])
        await session.flush()

        out = await PortfolioService(session).get_performance_attribution(START, END)
        rows = {r["symbol"]: r for r in out["attributions"]}

        # SOLD: end 0 - start 100 + disposal 110 = +10, not -100.
        assert rows["SOLD"]["disposal_proceeds_eur"] == pytest.approx(110.0)
        assert rows["SOLD"]["pnl_contribution_eur"] == pytest.approx(10.0)
        # HELD: plain appreciation, no disposal term.
        assert rows["HELD"]["disposal_proceeds_eur"] == 0.0
        assert rows["HELD"]["pnl_contribution_eur"] == pytest.approx(20.0)
        # NEW: end 120 - new investment 110 = +10.
        assert rows["NEW"]["pnl_contribution_eur"] == pytest.approx(10.0)
        # The total is the sum of honest parts.
        assert out["total_pnl_eur"] == pytest.approx(40.0)
    finally:
        await session.close()
        await engine.dispose()
