"""
A share of a zero whole is None on the valuation paths, never 0% (GitHub issue #29).

Inline `x / whole * 100 if whole > 0 else 0` copies in portfolio_service published 0%
where `percentages.share_pct` answers None:

- contribution_percent: two contributions that cancel (total P&L exactly 0) both read
  0.0%, although one gained and one lost.
- weight_percent: a window where everything attributed was sold read 0.0% weights.
- gain_loss_percent: a zero cost basis (a stock distribution) read +0.00% whatever its
  value, on the summary and on the position.

`test_percentages_family.py` fails the inline form coming back.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.portfolio_service import PortfolioService

START = date(2026, 3, 2)
END = date(2026, 3, 31)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    s = AsyncSession(engine, expire_on_commit=False)
    try:
        yield s
    finally:
        await s.close()
        await engine.dispose()


async def _sec(db, sid, cost="1000", closed=None):
    db.add(Security(id=sid, isin=f"US00000000{sid:02d}", symbol=f"S{sid}", description="x",
                    currency="EUR", conid=40 + sid, asset_category="STK", exchange="X"))
    await db.flush()
    db.add(TaxLot(security_id=sid, open_date=START - timedelta(days=30), close_date=closed,
                  is_open=closed is None, quantity=Decimal("10"), cost_basis=Decimal(cost),
                  cost_basis_eur=Decimal(cost), price_per_unit=Decimal("1"), currency="EUR"))


def _px(db, sid, d, close):
    db.add(MarketPrice(security_id=sid, date=d, close_price=Decimal(close),
                       currency="EUR", source="test"))


@pytest.mark.asyncio
async def test_cancelling_contributions_are_not_both_zero_percent(db):
    await _sec(db, 1)
    await _sec(db, 2)
    for sid, end_px in ((1, "110"), (2, "90")):
        _px(db, sid, START, "100")
        _px(db, sid, END, end_px)
    await db.commit()
    out = await PortfolioService(db).get_performance_attribution(START, END)
    assert out["total_pnl_eur"] == 0
    assert sorted(a["pnl_contribution_eur"] for a in out["attributions"]) == [-100.0, 100.0]
    assert all(a["contribution_percent"] is None for a in out["attributions"]), (
        "+100 and -100 both published as 0.0% of total P&L"
    )
    # A weight is still a share of a non-zero end value.
    assert sorted(a["weight_percent"] for a in out["attributions"]) == [45.0, 55.0]


@pytest.mark.asyncio
async def test_contributions_of_a_non_zero_total_are_shares_of_it(db):
    await _sec(db, 1)
    await _sec(db, 2)
    for sid, end_px in ((1, "130"), (2, "90")):
        _px(db, sid, START, "100")
        _px(db, sid, END, end_px)
    await db.commit()
    out = await PortfolioService(db).get_performance_attribution(START, END)
    shares = {a["symbol"]: a["contribution_percent"] for a in out["attributions"]}
    assert out["total_pnl_eur"] == 200.0
    assert shares == {"S1": 150.0, "S2": -50.0}


@pytest.mark.asyncio
async def test_a_fully_sold_window_has_no_weights_not_zero_weights(db):
    await _sec(db, 3, closed=START + timedelta(days=5))
    _px(db, 3, START, "100")
    _px(db, 3, START + timedelta(days=5), "105")
    await _sec(db, 4, closed=START + timedelta(days=6))
    _px(db, 4, START, "100")
    _px(db, 4, START + timedelta(days=6), "95")
    _px(db, 4, END, "95")  # something priced at the end, so the window is not empty
    await db.commit()
    out = await PortfolioService(db).get_performance_attribution(START, END)
    assert out["attributions"]
    assert all(a["weight_percent"] is None for a in out["attributions"]), (
        "a share of a zero end value published as 0.0%"
    )


@pytest.mark.asyncio
async def test_a_zero_cost_position_has_no_gain_percent(db):
    await _sec(db, 5, cost="0")
    _px(db, 5, date.today(), "10")
    await db.commit()
    service = PortfolioService(db)
    out = await service.get_current_portfolio_summary()
    assert out["total_cost_basis_eur"] == 0 and out["total_gain_loss_eur"] == 100.0
    assert out["total_gain_loss_percent"] is None, "a gain on a zero cost basis published as +0.00%"

    [position] = await service.get_positions_breakdown()
    assert position["gain_loss_eur"] == 100.0
    assert position["gain_loss_percent"] is None


@pytest.mark.asyncio
async def test_an_empty_book_has_no_gain_percent(db):
    out = await PortfolioService(db).get_current_portfolio_summary()
    assert out["num_positions"] == 0
    assert out["total_gain_loss_percent"] is None
