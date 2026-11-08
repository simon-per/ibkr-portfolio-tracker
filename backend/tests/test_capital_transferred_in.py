"""
Capital transferred in once the deposit ledger is complete is neither profit nor savings
(review item C2, 2026-11-08).

A cash TRANSFER_IN after `coverage_from` — finpension's vested benefits, an IBKR transfer's
cash leg — entered Total Value and nothing beside it: the chart read it as profit and the
benchmark never invested it. It now rides in `capital_legs` (the chart's Money In line, the
benchmark, the decomposition's flows) and stays out of `money_in_legs` (the monthly bars,
the windows, the 3a deduction). Pure DB, EUR base.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.cash_flow import DEPOSIT_WITHDRAW, TRANSFER_IN, CashFlow
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.base_fx import BaseFx
from app.services.portfolio_service import PortfolioService
from tests.memory_db import memory_engine


async def _book(with_twin=False):
    engine = await memory_engine()
    s = AsyncSession(engine, expire_on_commit=False)
    s.add(
        Security(
            id=1,
            isin="US0000000001",
            symbol="AAA",
            description="Test Co",
            currency="EUR",
            conid=100,
            asset_category="STK",
            exchange="XETRA",
        )
    )
    s.add(
        CashFlow(
            ib_key="D1",
            flow_date=date(2026, 1, 9),
            flow_type=DEPOSIT_WITHDRAW,
            amount=Decimal("1000"),
            currency="EUR",
            amount_eur=Decimal("1000"),
        )
    )
    s.add(
        CashFlow(
            ib_key="X1",
            flow_date=date(2026, 2, 2),
            flow_type=TRANSFER_IN,
            amount=Decimal("5000"),
            currency="EUR",
            amount_eur=Decimal("5000"),
        )
    )
    if with_twin:
        # IBKR's <Transfers> row for the same cash the reclassified deposit above records.
        s.add(
            CashFlow(
                ib_key="TR1",
                flow_date=date(2026, 2, 2),
                flow_type=TRANSFER_IN,
                amount=Decimal("5000"),
                currency="EUR",
                amount_eur=Decimal("5000"),
            )
        )
    s.add(
        Trade(
            ib_key="T1",
            conid="100",
            security_id=1,
            symbol="AAA",
            trade_date=date(2026, 2, 3),
            buy_sell="BUY",
            quantity=Decimal("50"),
            price=Decimal("100"),
            proceeds=Decimal("-5000"),
            commission=Decimal("0"),
            currency="EUR",
        )
    )
    s.add(
        TaxLot(
            security_id=1,
            open_date=date(2026, 2, 3),
            quantity=Decimal("50"),
            cost_basis=Decimal("5000"),
            price_per_unit=Decimal("100"),
            currency="EUR",
            cost_basis_eur=Decimal("5000"),
            is_open=True,
        )
    )
    for day in (2, 3, 4):
        s.add(
            MarketPrice(
                security_id=1,
                date=date(2026, 2, day),
                close_price=Decimal("100"),
                currency="EUR",
                source="test",
            )
        )
    await s.flush()
    await AppSettingsRepository(s).widen_cash_flows_covered_from(date(2026, 1, 9))
    await s.commit()
    return engine, s


@pytest.mark.asyncio
@pytest.mark.parametrize("with_twin", [False, True])
async def test_a_transfer_in_is_not_profit_and_not_a_contribution(with_twin):
    engine, s = await _book(with_twin)
    try:
        ps = PortfolioService(s)
        timeline = {
            r["date"]: r
            for r in await ps.get_portfolio_value_over_time(
                date(2026, 1, 30), date(2026, 2, 4)
            )
        }
        profit = {
            d: r["total_value_eur"] - r["money_in_eur"] for d, r in timeline.items()
        }
        # Nothing moved in price: the profit is flat across the transfer and the purchase.
        assert profit["2026-02-04"] == pytest.approx(profit["2026-01-30"])
        assert timeline["2026-02-04"]["money_in_eur"] == pytest.approx(6000)

        # The savings figures do not see it: the monthly bars and the windows are deposits.
        contributions = await ps.get_contributions(as_of=date(2026, 2, 4))
        everything = next(w for w in contributions["windows"] if w["label"] == "all")
        assert everything["money_in_eur"] == pytest.approx(1000)
        assert sum(
            m["money_in_eur"] for m in contributions["monthly"]
        ) == pytest.approx(1000)

        # One leg for the benchmark and the decomposition, the chart's own line.
        inputs = await ps._contribution_inputs(BaseFx("EUR", {}))
        assert [(d, float(a)) for d, a in inputs["capital_legs"]] == [
            (date(2026, 1, 9), 1000.0),
            (date(2026, 2, 2), 5000.0),
        ]
        assert [(d, float(a)) for d, a in inputs["money_in_legs"]] == [
            (date(2026, 1, 9), 1000.0)
        ]
    finally:
        await s.close()
        await engine.dispose()
