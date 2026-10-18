"""
The dividend counterpart of find_stale_priced_securities().

A missing *price* has warned since that detector landed. Nothing watched the
dividend side, which is why SBI's two estimate rows survived the mapping's
correction to SBI.TO, the price purge, and a month of syncs — and went on
defining a gold miner's payout schedule while its real IBKR payment was skipped.

The signal has to be specific, not a staleness guess: a row computed before its
mapping's updated_at came from a *different* ticker. A genuine non-payer has no
estimate rows and must never trip it.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.ticker_mapping import TickerMapping
from app.services.scheduler_service import SchedulerService
from tests.memory_db import memory_engine


async def _db():
    engine = await memory_engine()
    return engine, AsyncSession(engine, expire_on_commit=False)


def _held(session, security_id, symbol, exchange, currency="CAD"):
    session.add(Security(id=security_id, isin=f"X{security_id:011d}", symbol=symbol,
                         description=symbol, currency=currency, conid=security_id,
                         asset_category="STK", exchange=exchange))
    session.add(TaxLot(security_id=security_id, open_date=date(2026, 6, 12),
                       quantity=Decimal("50"), cost_basis=Decimal("100"),
                       price_per_unit=Decimal("2"), cost_basis_eur=Decimal("100"),
                       currency=currency, is_open=True))


def _estimate(session, security_id, computed):
    session.add(DividendPayment(
        security_id=security_id, ex_date=date(2026, 6, 23), source="yfinance_estimate",
        amount_per_share=Decimal("0.042"), currency="CAD", shares_held=Decimal("50"),
        gross_amount_eur=Decimal("1.30"), last_computed=computed,
    ))


def _mapping(session, symbol, exchange, ticker, updated_at):
    session.add(TickerMapping(ibkr_symbol=symbol, ibkr_exchange=exchange,
                              yahoo_ticker=ticker, source="manual", is_active=True,
                              created_at=updated_at, updated_at=updated_at))


@pytest.mark.asyncio
async def test_estimates_older_than_their_mapping_are_reported():
    engine, session = await _db()
    try:
        _held(session, 33, "SBI", "TSE")
        _estimate(session, 33, datetime(2026, 7, 25))       # fetched under the old ticker
        _mapping(session, "SBI", "TSE", "SBI.TO", datetime(2026, 7, 27))  # corrected after
        await session.flush()

        warnings = await SchedulerService().find_dividends_predating_their_mapping(session)

        assert len(warnings) == 1
        assert "SBI@TSE" in warnings[0]
        assert "came from a different ticker" in warnings[0]
        assert "purge_dividend_estimates SBI TSE" in warnings[0]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_estimates_newer_than_the_mapping_are_fine():
    engine, session = await _db()
    try:
        _held(session, 33, "SBI", "TSE")
        _mapping(session, "SBI", "TSE", "SBI.TO", datetime(2026, 7, 27))
        _estimate(session, 33, datetime(2026, 7, 30))       # refetched after the fix
        await session.flush()

        assert await SchedulerService().find_dividends_predating_their_mapping(session) == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_mapping_with_unknown_stamps_is_not_reported():
    """
    NULL timestamps mean "we don't know when this was set", not "just changed".

    `o8d5f2a9b3c4` adds the columns and leaves pre-existing rows NULL for exactly
    this reason. It backfilled CURRENT_TIMESTAMP once, which dated every mapping to
    the migration's own run time and made every estimate computed before it look
    like it came from another ticker — three held payers warned on every
    market-data sync while their rows were correct.

    This pins the half a runtime assertion can reach: that NULL is the quiet value
    the migration is relying on. What the migration itself writes is a property of
    a file that has already run everywhere, so it is guarded by the reasoning in
    its own docstring rather than by a test.
    """
    engine, session = await _db()
    try:
        _held(session, 33, "SBI", "TSE")
        _estimate(session, 33, datetime(2026, 7, 25))
        _mapping(session, "SBI", "TSE", "SBI.TO", datetime(2026, 7, 27))
        await session.flush()

        # The migration adds the columns without a server_default, so rows that
        # predate it hold NULL. The ORM cannot express that — the model's
        # server_default=func.now() fills in whenever the attribute is None — which
        # is also why a NULL can only ever mean "written before the columns
        # existed", never a row this application inserted.
        await session.execute(
            update(TickerMapping).values(created_at=None, updated_at=None)
        )

        assert await SchedulerService().find_dividends_predating_their_mapping(session) == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_genuine_non_payer_never_trips_it():
    """Accumulating ETFs correctly have no estimate rows — they must stay silent."""
    engine, session = await _db()
    try:
        _held(session, 5, "IWDA", "XETRA", currency="EUR")
        _mapping(session, "IWDA", "XETRA", "IWDA.DE", datetime(2026, 7, 27))
        await session.flush()

        assert await SchedulerService().find_dividends_predating_their_mapping(session) == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_ibkr_only_history_never_trips_it():
    engine, session = await _db()
    try:
        _held(session, 33, "SBI", "TSE")
        _mapping(session, "SBI", "TSE", "SBI.TO", datetime(2026, 7, 27))
        session.add(DividendPayment(
            security_id=33, ex_date=date(2026, 7, 10), pay_date=date(2026, 7, 10),
            source="ibkr", currency="CAD", shares_held=Decimal("0"),
            gross_amount_eur=Decimal("2.91"), net_amount_eur=Decimal("2.91"),
            last_computed=datetime(2026, 7, 20),   # older than the mapping, but authoritative
        ))
        await session.flush()

        assert await SchedulerService().find_dividends_predating_their_mapping(session) == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_closed_out_holding_is_not_reported():
    """Matches the price-side rule: only open lots move a number the user sees."""
    engine, session = await _db()
    try:
        _held(session, 33, "SBI", "TSE")
        lot = (await session.execute(
            __import__("sqlalchemy").select(TaxLot)
        )).scalars().first()
        lot.is_open = False
        lot.close_date = date(2026, 7, 1)
        _estimate(session, 33, datetime(2026, 7, 25))
        _mapping(session, "SBI", "TSE", "SBI.TO", datetime(2026, 7, 27))
        await session.flush()

        assert await SchedulerService().find_dividends_predating_their_mapping(session) == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_security_with_no_mapping_row_is_not_reported():
    """A bare/suffix-resolved ticker has no mapping row to compare against."""
    engine, session = await _db()
    try:
        _held(session, 40, "SOXQ", "NASDAQ", currency="USD")
        _estimate(session, 40, datetime(2026, 7, 25))
        await session.flush()

        assert await SchedulerService().find_dividends_predating_their_mapping(session) == []
    finally:
        await session.close()
        await engine.dispose()
