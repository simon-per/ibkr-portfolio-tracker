"""
get_realized_totals picks its source: authoritative SELL trades when they exist,
else the market-price approximation over closed lots. The picker must key on
SELLs specifically — "some trades exist" is not "realized figures exist", and a
BUY-only trades table used to return hard zeros while the tax report (which
picks per-year) showed real gains.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.services.portfolio_service import PortfolioService
from tests.memory_db import memory_engine

CLOSE = date(2026, 3, 10)


async def _make_session():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="Alpha",
                         currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    # One closed lot: bought for 100, sold when the price implied 120.
    session.add(TaxLot(
        security_id=1, open_date=date(2026, 1, 5), close_date=CLOSE, is_open=False,
        quantity=Decimal("10"), cost_basis=Decimal("100"), cost_basis_eur=Decimal("100"),
        price_per_unit=Decimal("10"), currency="EUR",
    ))
    session.add(MarketPrice(security_id=1, date=CLOSE, close_price=Decimal("12"),
                            currency="EUR", source="test"))
    await session.flush()
    return engine, session


def _trade(buy_sell: str, ib_key: str, proceeds=None, pnl=None):
    return Trade(
        ib_key=ib_key, conid="100", security_id=1, symbol="AAA", trade_date=CLOSE,
        buy_sell=buy_sell, quantity=Decimal("-10") if buy_sell == "SELL" else Decimal("10"),
        price=Decimal("12"), proceeds=proceeds, commission=Decimal("-1"),
        currency="EUR", realized_pnl=pnl,
    )


@pytest.mark.asyncio
async def test_buy_only_trades_do_not_mask_the_closed_lot_fallback():
    engine, session = await _make_session()
    try:
        session.add(_trade("BUY", "T1"))
        await session.flush()
        totals = await PortfolioService(session).get_realized_totals()
        # Approximation over the closed lot: 10 sh × 12.00 = 120 proceeds, 100 cost.
        assert totals["total_realized_proceeds_eur"] == pytest.approx(120.0)
        assert totals["total_realized_gain_loss_eur"] == pytest.approx(20.0)
        assert totals["num_closed_positions"] == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_sell_trade_switches_to_the_authoritative_path():
    engine, session = await _make_session()
    try:
        # Deliberately different from the approximation (130/25 vs 120/20) so the
        # assertion proves which source answered.
        session.add(_trade("SELL", "T2", proceeds=Decimal("130"), pnl=Decimal("25")))
        await session.flush()
        totals = await PortfolioService(session).get_realized_totals()
        assert totals["total_realized_proceeds_eur"] == pytest.approx(130.0)
        assert totals["total_realized_gain_loss_eur"] == pytest.approx(25.0)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_trades_at_all_uses_the_fallback():
    engine, session = await _make_session()
    try:
        totals = await PortfolioService(session).get_realized_totals()
        assert totals["total_realized_gain_loss_eur"] == pytest.approx(20.0)
    finally:
        await session.close()
        await engine.dispose()
