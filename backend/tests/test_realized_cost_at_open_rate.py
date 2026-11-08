"""
A realized gain in the base currency prices its cost at the rate it was bought at
(review item V4, 2026-11-08).

IBKR states the gain in the trade's currency; converting it at the sale date's rate priced
the cost at the sale date too, while every unrealized figure prices cost at the open date
(`cost_basis_eur`). A USD holding bought at 0.90 and sold at 0.80 then read its FX loss on
the cost as part of the gain. The summary and the closed positions now take the sales' net
proceeds at their date less IBKR's FIFO cost at the closed lots' open-date rate
(`realized_trades.gain_in_base`). Offline: the currency service is stubbed.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.services.performance_analytics_service import PerformanceAnalyticsService
from app.services.portfolio_service import PortfolioService
from tests.memory_db import memory_engine

OPENED, SOLD = date(2026, 1, 5), date(2026, 3, 10)
SALE_RATE = Decimal("0.8")  # USD -> EUR on the sale date; 0.9 when bought


async def _session(lot_cost="1000"):
    engine = await memory_engine()
    s = AsyncSession(engine, expire_on_commit=False)
    s.add(
        Security(
            id=1,
            isin="US0000000031",
            symbol="USDCO",
            description="x",
            currency="USD",
            conid=31,
            asset_category="STK",
            exchange="X",
        )
    )
    await s.flush()
    # Bought for 1,000 USD at 0.90: 900 EUR.
    s.add(
        TaxLot(
            security_id=1,
            open_date=OPENED,
            close_date=SOLD,
            is_open=False,
            quantity=Decimal("10"),
            cost_basis=Decimal(lot_cost),
            cost_basis_eur=Decimal(lot_cost) * Decimal("0.9"),
            price_per_unit=Decimal("100"),
            currency="USD",
        )
    )
    s.add(
        MarketPrice(
            security_id=1,
            date=SOLD,
            close_price=Decimal("130"),
            currency="USD",
            source="test",
        )
    )
    s.add(
        ExchangeRate(
            date=SOLD,
            from_currency="USD",
            to_currency="EUR",
            rate=SALE_RATE,
            source="test",
        )
    )
    # Sold for 1,300 USD less 1 commission: IBKR's FIFO gain 299 USD.
    s.add(
        Trade(
            ib_key="S",
            conid="31",
            security_id=1,
            symbol="USDCO",
            trade_date=SOLD,
            buy_sell="SELL",
            quantity=Decimal("-10"),
            price=Decimal("130"),
            proceeds=Decimal("1300"),
            commission=Decimal("-1"),
            currency="USD",
            realized_pnl=Decimal("299"),
        )
    )
    await s.commit()
    return engine, s


def _stub(portfolio: PortfolioService):
    async def rate(currency, on_date):
        return SALE_RATE

    portfolio.currency_service.get_exchange_rate = rate


@pytest.mark.asyncio
async def test_the_summary_prices_the_cost_at_the_rate_it_was_paid_in():
    engine, s = await _session()
    try:
        portfolio = PortfolioService(s)
        _stub(portfolio)
        totals = await portfolio.get_realized_totals()
        # 1,299 x 0.80 - 1,000 x 0.90; IBKR's 299 at 0.80 would be 239.20.
        assert totals["total_realized_gain_loss_eur"] == pytest.approx(139.2)
        assert totals["total_realized_proceeds_eur"] == pytest.approx(1040.0)
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_closed_positions_agree_with_the_summary():
    engine, s = await _session()
    try:
        service = PerformanceAnalyticsService(s)
        _stub(service.portfolio)
        (position,) = (await service.closed_positions())["positions"]
        assert position["realized_source"] == "trade"
        assert position["realized_pnl_eur"] == pytest.approx(139.2)
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_lots_that_do_not_add_up_to_the_sale_leave_ibkrs_figure():
    """Closed lots that cost 800 cannot vouch for sales IBKR costed at 1,000: IBKR's figure,
    converted."""
    engine, s = await _session(lot_cost="800")
    try:
        portfolio = PortfolioService(s)
        _stub(portfolio)
        totals = await portfolio.get_realized_totals()
        assert totals["total_realized_gain_loss_eur"] == pytest.approx(239.2)
    finally:
        await s.close()
        await engine.dispose()
