"""
Offline tests for linking Flex transactions to securities, and for adopting real sale
dates onto lots that were closed before <Trades> was ever ingested.

Both cover the same root cause: ``conid_to_security_id`` is built from the statement's
OpenPositions, so a security sold out completely drops off it. That left every SELL trade
with ``security_id = NULL`` even though the security row was still in the database, and
left the matching closed lots stamped with the date the sync *noticed* the drop rather
than the date they actually sold (CRM and NFLX both read 2026-04-17 for a 2026-03-13 sale).
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.models.corporate_action import CorporateAction
from app.repositories.trade_repository import TradeRepository
from app.repositories.corporate_action_repository import CorporateActionRepository
from app.repositories.taxlot_repository import TaxLotRepository
from app.services.sync_helper import persist_transactions, restamp_unsourced_closed_lots


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [Security.__table__, TaxLot.__table__, Trade.__table__,
              CorporateAction.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    session = AsyncSession(engine, expire_on_commit=False)
    # A security we no longer hold: still in the DB, absent from OpenPositions.
    session.add(Security(
        id=1, isin="US79466L3024", symbol="CRM", description="Salesforce",
        currency="USD", conid=29624264, asset_category="STK", exchange="NYSE",
    ))
    await session.flush()
    return engine, session


def _sell(ib_key: str, conid: str, qty: str, on: date) -> dict:
    return {
        "ib_key": ib_key, "conid": conid, "symbol": "CRM", "trade_date": on,
        "buy_sell": "SELL", "quantity": Decimal(qty), "price": Decimal("250"),
        "proceeds": Decimal("500"), "commission": Decimal("-1"), "currency": "USD",
        "realized_pnl": Decimal("-74.21"), "asset_category": "STK",
    }


@pytest.mark.asyncio
async def test_sold_out_security_is_resolved_from_the_database():
    """The statement map can't contain it, but the DB can — so security_id must be set."""
    engine, session = await _make_session()
    try:
        await persist_transactions(
            TradeRepository(session),
            CorporateActionRepository(session),
            {},  # empty: nothing is held any more, so OpenPositions has no row for it
            [_sell("TX1", "29624264", "-2", date(2026, 3, 13))],
            [],
        )
        await session.commit()

        trade = (await session.execute(select(Trade))).scalar_one()
        assert trade.security_id == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_conid_still_leaves_security_id_null():
    """A conid we have never held has nothing to resolve to; nullable is correct."""
    engine, session = await _make_session()
    try:
        await persist_transactions(
            TradeRepository(session),
            CorporateActionRepository(session),
            {},
            [_sell("TX9", "999999999", "-1", date(2026, 3, 13))],
            [],
        )
        await session.commit()

        trade = (await session.execute(select(Trade))).scalar_one()
        assert trade.security_id is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_heuristic_closed_lot_adopts_the_real_sale_date():
    engine, session = await _make_session()
    try:
        # Closed by the old heuristic: stamped with the sync date, no provenance.
        session.add(TaxLot(
            id=10, security_id=1, open_date=date(2025, 11, 6),
            close_date=date(2026, 4, 17), quantity=Decimal("2"),
            cost_basis=Decimal("400"), price_per_unit=Decimal("200"), currency="USD",
            cost_basis_eur=Decimal("409.92"), is_open=False, close_source=None,
        ))
        session.add(Trade(
            ib_key="TX1", conid="29624264", security_id=1, symbol="CRM",
            trade_date=date(2026, 3, 13), buy_sell="SELL", quantity=Decimal("-2"),
            price=Decimal("250"), proceeds=Decimal("500"), commission=Decimal("-1"),
            currency="USD", realized_pnl=Decimal("-74.21"), asset_category="STK",
        ))
        await session.commit()

        restamped = await restamp_unsourced_closed_lots(
            TaxLotRepository(session), TradeRepository(session)
        )
        await session.commit()

        assert restamped == 1
        lot = (await session.execute(select(TaxLot))).scalar_one()
        assert lot.close_date == date(2026, 3, 13)   # the real sale, ~5 weeks earlier
        assert lot.close_source == "trade"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_restamping_is_idempotent_and_leaves_stamped_lots_alone():
    engine, session = await _make_session()
    try:
        session.add(TaxLot(
            id=11, security_id=1, open_date=date(2025, 11, 6),
            close_date=date(2026, 3, 13), quantity=Decimal("2"),
            cost_basis=Decimal("400"), price_per_unit=Decimal("200"), currency="USD",
            cost_basis_eur=Decimal("409.92"), is_open=False, close_source="trade",
        ))
        session.add(Trade(
            ib_key="TX1", conid="29624264", security_id=1, symbol="CRM",
            trade_date=date(2026, 3, 13), buy_sell="SELL", quantity=Decimal("-2"),
            price=Decimal("250"), proceeds=Decimal("500"), commission=Decimal("-1"),
            currency="USD", realized_pnl=Decimal("-74.21"), asset_category="STK",
        ))
        await session.commit()

        assert await restamp_unsourced_closed_lots(
            TaxLotRepository(session), TradeRepository(session)
        ) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_quantity_match_is_left_alone():
    """Two same-size sells can't be told apart, so guessing would invent a date."""
    engine, session = await _make_session()
    try:
        session.add(TaxLot(
            id=12, security_id=1, open_date=date(2025, 11, 6),
            close_date=date(2026, 4, 17), quantity=Decimal("2"),
            cost_basis=Decimal("400"), price_per_unit=Decimal("200"), currency="USD",
            cost_basis_eur=Decimal("409.92"), is_open=False, close_source=None,
        ))
        for key, on in (("TX1", date(2026, 1, 5)), ("TX2", date(2026, 3, 13))):
            session.add(Trade(
                ib_key=key, conid="29624264", security_id=1, symbol="CRM",
                trade_date=on, buy_sell="SELL", quantity=Decimal("-2"),
                price=Decimal("250"), proceeds=Decimal("500"), commission=Decimal("-1"),
                currency="USD", realized_pnl=Decimal("-74.21"), asset_category="STK",
            ))
        await session.commit()

        assert await restamp_unsourced_closed_lots(
            TaxLotRepository(session), TradeRepository(session)
        ) == 0
        lot = (await session.execute(select(TaxLot))).scalar_one()
        assert lot.close_date == date(2026, 4, 17)
        assert lot.close_source is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_cancelled_sale_is_no_candidate_for_the_restamp():
    """
    Of the two same-size sells above, IBKR busted one (GitHub issue #58): its date is not
    when anything was sold, so the sale that stands is the unambiguous match.
    """
    engine, session = await _make_session()
    try:
        session.add(TaxLot(
            id=13, security_id=1, open_date=date(2025, 11, 6),
            close_date=date(2026, 4, 17), quantity=Decimal("2"),
            cost_basis=Decimal("400"), price_per_unit=Decimal("200"), currency="USD",
            cost_basis_eur=Decimal("409.92"), is_open=False, close_source=None,
        ))
        for key, side, qty, on in (
            ("TX1", "SELL", "-2", date(2026, 1, 5)),
            ("TX2", "SELL", "-2", date(2026, 3, 13)),
            ("TX2c", "CANCELSELL", "2", date(2026, 3, 13)),
        ):
            session.add(Trade(
                ib_key=key, conid="29624264", security_id=1, symbol="CRM",
                trade_date=on, buy_sell=side, quantity=Decimal(qty),
                price=Decimal("250"), proceeds=Decimal("-250") * Decimal(qty),
                commission=Decimal("0"), currency="USD",
                realized_pnl=Decimal("37.1") * Decimal(qty), asset_category="STK",
            ))
        await session.commit()

        assert await restamp_unsourced_closed_lots(
            TaxLotRepository(session), TradeRepository(session)
        ) == 1
        lot = (await session.execute(select(TaxLot))).scalar_one()
        assert lot.close_date == date(2026, 1, 5)
        assert lot.close_source == "trade"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_sell_after_the_recorded_close_date_is_not_adopted():
    """
    The heuristic date is when the drop was *detected*, so the true sale is never later.
    A newer sell is therefore a different event and must not be borrowed.
    """
    engine, session = await _make_session()
    try:
        session.add(TaxLot(
            id=13, security_id=1, open_date=date(2025, 11, 6),
            close_date=date(2026, 4, 17), quantity=Decimal("2"),
            cost_basis=Decimal("400"), price_per_unit=Decimal("200"), currency="USD",
            cost_basis_eur=Decimal("409.92"), is_open=False, close_source=None,
        ))
        session.add(Trade(
            ib_key="TX1", conid="29624264", security_id=1, symbol="CRM",
            trade_date=date(2026, 6, 1), buy_sell="SELL", quantity=Decimal("-2"),
            price=Decimal("250"), proceeds=Decimal("500"), commission=Decimal("-1"),
            currency="USD", realized_pnl=Decimal("-74.21"), asset_category="STK",
        ))
        await session.commit()

        assert await restamp_unsourced_closed_lots(
            TaxLotRepository(session), TradeRepository(session)
        ) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_restamp_is_a_no_op_without_trade_data():
    """Before <Trades> is enabled there is nothing to adopt; must not touch anything."""
    engine, session = await _make_session()
    try:
        session.add(TaxLot(
            id=14, security_id=1, open_date=date(2025, 11, 6),
            close_date=date(2026, 4, 17), quantity=Decimal("2"),
            cost_basis=Decimal("400"), price_per_unit=Decimal("200"), currency="USD",
            cost_basis_eur=Decimal("409.92"), is_open=False, close_source=None,
        ))
        await session.commit()

        assert await restamp_unsourced_closed_lots(TaxLotRepository(session), None) == 0
    finally:
        await session.close()
        await engine.dispose()
