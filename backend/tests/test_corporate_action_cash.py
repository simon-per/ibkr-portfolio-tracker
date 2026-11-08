"""
Corporate-action cash reaches the derived balance (review item C6, 2026-11-08).

Cash in lieu of a fractional share and a cash merger arrive as a corporate action's
`proceeds`. The Activity ledger showed them and the balance did not, so the cash line ran
short by them until the next measured level. Counted once when IBKR also booked the cash as
a trade. Pure DB, EUR base.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.cash_flow import DEPOSIT_WITHDRAW, CashFlow
from app.models.corporate_action import CorporateAction
from app.models.security import Security
from app.models.trade import Trade
from app.services.base_fx import BaseFx
from app.services.cash_service import CashService
from tests.memory_db import memory_engine

EUR = BaseFx("EUR", {})


async def _book(also_a_trade=False):
    engine = await memory_engine()
    s = AsyncSession(engine, expire_on_commit=False)
    s.add(Security(id=1, isin="US0000000001", symbol="AAA", description="Test Co",
                   currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    s.add(CashFlow(ib_key="D1", flow_date=date(2026, 1, 9), flow_type=DEPOSIT_WITHDRAW,
                   amount=Decimal("1000"), currency="EUR", amount_eur=Decimal("1000")))
    s.add(CorporateAction(ib_key="CA1", conid="100", security_id=1, symbol="AAA",
                          action_date=date(2026, 3, 2), action_type="FORWARDSPLIT",
                          quantity=Decimal("-0.5"), proceeds=Decimal("12.34"),
                          currency="EUR", description="AAA SPLIT 3 FOR 2 (cash in lieu)"))
    # A split that moved no cash.
    s.add(CorporateAction(ib_key="CA2", conid="100", security_id=1, symbol="AAA",
                          action_date=date(2026, 3, 2), action_type="FORWARDSPLIT",
                          quantity=Decimal("50"), proceeds=Decimal("0"), currency="EUR"))
    if also_a_trade:
        s.add(Trade(ib_key="FR1", conid="100", security_id=1, symbol="AAA",
                    trade_date=date(2026, 3, 2), buy_sell="SELL", quantity=Decimal("-0.5"),
                    price=Decimal("24.68"), proceeds=Decimal("12.34"),
                    commission=Decimal("0"), currency="EUR"))
    await s.commit()
    return engine, s


@pytest.mark.asyncio
@pytest.mark.parametrize("also_a_trade", [False, True])
async def test_cash_in_lieu_is_in_the_balance_once(also_a_trade):
    engine, s = await _book(also_a_trade)
    try:
        cash = CashService(s)
        events = await cash.balance_events(EUR)
        assert cash.balance_as_of(events, date(2026, 3, 1)) == Decimal("1000")
        assert cash.balance_as_of(events, date(2026, 3, 2)) == Decimal("1012.34")
    finally:
        await s.close()
        await engine.dispose()
