"""
A split restates the lots sold before it too (review item V5, 2026-11-08).

The split purges the cached prices up to its date, and the refetch is split-adjusted; IBKR
restates the open lots. A lot sold before the split kept its old share count, so its days
before the sale were valued at the old count times the adjusted price (a tenth, for a
10-for-1) and its realized approximation with them. And the closed positions' post-sale
move compared a close before a split with one after it. Offline: no IBKR, no Yahoo.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.corporate_action_repository import CorporateActionRepository
from app.repositories.trade_repository import TradeRepository
from app.services.performance_analytics_service import PerformanceAnalyticsService
from app.services.sync_helper import (
    persist_transactions,
    rescale_lots_closed_before_splits,
    split_ratio,
)
from tests.memory_db import memory_engine

CONID = "322447809"
SPLIT_DAY = date(2026, 6, 15)


def test_the_ratio_is_read_off_ibkrs_description():
    assert (
        split_ratio("AAA(US0000000001) SPLIT 10 FOR 1 (AAA, TEST CO, US0000000001)")
        == 10
    )
    assert split_ratio("AAA(US0000000001) SPLIT 1 FOR 10 (AAA, TEST CO)") == Decimal(
        "0.1"
    )
    assert split_ratio("AAA(US0000000001) SPLIT 3 FOR 2") == Decimal("1.5")
    assert split_ratio("AAA SPINOFF 1 FOR 20") is None
    assert split_ratio(None) is None


async def _session():
    engine = await memory_engine()
    s = AsyncSession(engine, expire_on_commit=False)
    s.add(
        Security(
            id=1,
            isin="US0000000001",
            symbol="AAA",
            description="Test Co",
            currency="USD",
            conid=int(CONID),
            asset_category="STK",
            exchange="NASDAQ",
        )
    )
    await s.flush()
    return engine, s


def _lot(open_date, close_date=None, qty="10", ppu="300"):
    return TaxLot(
        security_id=1,
        open_date=open_date,
        close_date=close_date,
        is_open=close_date is None,
        quantity=Decimal(qty),
        cost_basis=Decimal(qty) * Decimal(ppu),
        cost_basis_eur=Decimal(qty) * Decimal(ppu),
        price_per_unit=Decimal(ppu),
        currency="USD",
    )


SPLIT = {
    "ib_key": "CA-1",
    "conid": CONID,
    "symbol": "AAA",
    "action_date": SPLIT_DAY,
    "action_type": "FORWARDSPLIT",
    "quantity": Decimal("90"),
    "value": None,
    "proceeds": None,
    "currency": "USD",
    "description": f"AAA({CONID}) SPLIT 10 FOR 1 (AAA, TEST CO, US0000000001)",
}


async def _ingest(session):
    persisted = await persist_transactions(
        TradeRepository(session),
        CorporateActionRepository(session),
        {CONID: 1},
        [],
        [SPLIT],
    )
    result = await rescale_lots_closed_before_splits(
        session, persisted["newly_restating_actions"]
    )
    await session.commit()
    return result


@pytest.mark.asyncio
async def test_a_split_restates_the_lots_sold_before_it_once():
    engine, s = await _session()
    try:
        s.add(_lot(date(2026, 1, 5), close_date=date(2026, 3, 2)))  # sold before
        s.add(
            _lot(date(2026, 1, 5), close_date=date(2026, 7, 1), qty="100", ppu="30")
        )  # after
        s.add(_lot(date(2026, 1, 5), qty="100", ppu="30"))  # still held
        await s.commit()

        assert (await _ingest(s))["closed_lots_rescaled"] == 1
        # The same statement again: the split is not new, nothing moves twice.
        assert (await _ingest(s))["closed_lots_rescaled"] == 0

        lots = (
            (await s.execute(select(TaxLot).order_by(TaxLot.close_date.is_(None), TaxLot.close_date)))
            .scalars()
            .all()
        )
        shape = [
            (lot.close_date, lot.quantity, lot.price_per_unit, lot.cost_basis)
            for lot in lots
        ]
        assert shape == [
            (date(2026, 3, 2), Decimal("100"), Decimal("30"), Decimal("3000")),
            (date(2026, 7, 1), Decimal("100"), Decimal("30"), Decimal("3000")),
            (None, Decimal("100"), Decimal("30"), Decimal("3000")),
        ]
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_post_sale_move_is_computed_across_a_split():
    engine, s = await _session()
    try:
        sold = date(2026, 3, 2)
        s.add(_lot(date(2026, 1, 5), close_date=sold, qty="100", ppu="30"))
        s.add(
            MarketPrice(
                security_id=1,
                date=sold,
                close_price=Decimal("320"),
                currency="USD",
                source="test",
            )
        )
        s.add(
            ExchangeRate(
                date=sold, from_currency="USD", to_currency="EUR", rate=Decimal("1"),
                source="test",
            )
        )
        s.add(
            MarketPrice(
                security_id=1,
                date=date(2026, 7, 1),
                close_price=Decimal("33"),
                currency="USD",
                source="test",
            )
        )
        s.add(
            CorporateAction(
                ib_key="CA-1",
                conid=CONID,
                security_id=1,
                symbol="AAA",
                action_date=SPLIT_DAY,
                action_type="FORWARDSPLIT",
                description=SPLIT["description"],
            )
        )
        await s.commit()
        (position,) = (await PerformanceAnalyticsService(s).closed_positions())[
            "positions"
        ]
        # 33 against 320 would read -90%; it is two share bases, so no figure.
        assert position["post_sale_pct"] is None
    finally:
        await s.close()
        await engine.dispose()
