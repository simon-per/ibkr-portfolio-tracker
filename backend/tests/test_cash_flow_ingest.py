"""
Offline tests for the external-cash path: extract_cash_flows / extract_transfers,
and persist_cash_flows.

The money-critical behaviour here is that an incoming broker transfer never counts
as money added. Simon's pre-2026 history arrived at IBKR by transfer from Scalable
Capital and Trading 212, carrying every lot with its original open_date and cost
basis — so counting the transfer's cash leg as a contribution would both invent
savings in a month that had none and double-count purchases already recorded under
the transferred lots' own dates.

Parser tests use lightweight fake statement objects carrying the real ibflex enum
members, matching tests/test_ibkr_parsers.py.
"""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from ibflex import enums

from app.database import Base
import app.models  # noqa: F401
from app.models.cash_flow import (
    CashFlow, DEPOSIT_WITHDRAW, TRANSFER_IN, TRANSFER_OUT,
)
from app.models.exchange_rate import ExchangeRate
from app.models.app_settings import AppSetting
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.cash_flow_repository import CashFlowRepository
from app.services.ibkr_service import IBKRService
from app.services.currency_service import CurrencyService
from app.services.sync_helper import persist_cash_flows


def _svc():
    return IBKRService(token="t", query_id="q")


def _flex(statement):
    return {"statement": statement}


def _deposit(**kw):
    base = dict(
        type=enums.CashAction.DEPOSITWITHDRAW, conid=None, symbol=None,
        transactionID="D1", settleDate=date(2026, 2, 5),
        dateTime=datetime(2026, 2, 4, 0, 0), reportDate=date(2026, 2, 4),
        amount=Decimal("1000"), currency="EUR", description="ACH DEPOSIT",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _transfer(**kw):
    base = dict(
        type=enums.TransferType.ACATS, direction=enums.InOut.IN,
        transactionID="TR1", date=date(2026, 1, 20), reportDate=date(2026, 1, 20),
        cashTransfer=Decimal("0"), positionAmount=Decimal("18000"),
        quantity=Decimal("120"), symbol="VWCE", conid=456, currency="EUR",
        company="Scalable Capital",
    )
    base.update(kw)
    return SimpleNamespace(**base)


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [CashFlow.__table__, ExchangeRate.__table__, AppSetting.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    return engine, AsyncSession(engine, expire_on_commit=False)


# --------------------------------------------------------------------------- parsers

@pytest.mark.asyncio
async def test_extract_cash_flows_keeps_conid_less_deposits():
    """
    The dividend extractor drops rows without a conid, which is every deposit — the
    reason extract_cash_flows exists as a separate path rather than a widened filter.
    """
    statement = SimpleNamespace(CashTransactions=[
        _deposit(),
        SimpleNamespace(
            type=enums.CashAction.DIVIDEND, conid=100, symbol="AAA",
            transactionID="C1", settleDate=date(2026, 5, 2),
            dateTime=datetime(2026, 5, 1, 0, 0), reportDate=date(2026, 5, 1),
            amount=Decimal("42.50"), currency="USD", description="AAA DIVIDEND",
        ),
        SimpleNamespace(
            type=enums.CashAction.WHTAX, conid=100, symbol="AAA",
            transactionID="C2", settleDate=date(2026, 5, 2),
            dateTime=datetime(2026, 5, 1, 0, 0), reportDate=date(2026, 5, 1),
            amount=Decimal("-6.38"), currency="USD", description="AAA WHT",
        ),
    ])

    flows = await _svc().extract_cash_flows(_flex(statement))

    assert len(flows) == 1
    assert flows[0]["ib_key"] == "D1"
    assert flows[0]["flow_type"] == "DEPOSITWITHDRAW"
    assert flows[0]["amount"] == Decimal("1000")
    assert flows[0]["flow_date"] == date(2026, 2, 4)  # reportDate, not settleDate


@pytest.mark.asyncio
async def test_a_withdrawal_is_dated_when_ibkr_debits_it_not_when_it_settles():
    """
    A disbursement is debited from cash the day it is initiated. Read off production:
    one initiated 2026-09-28 and settling 09-30 was already inside the 09-28 Cash
    Report `endingCash`, so dating it by settleDate debited it a second time on 09-30
    (cash read -4,470 CHF) and left the benchmark two days of a deposit with no
    withdrawal beside it. The flow is dated on the cash ledger's basis: reportDate,
    then dateTime, and settleDate only when IBKR sent neither.
    """
    statement = SimpleNamespace(CashTransactions=[
        _deposit(transactionID="W", amount=Decimal("-4517.50"), currency="CHF",
                 reportDate=date(2026, 9, 28), dateTime=datetime(2026, 9, 28, 14, 0),
                 settleDate=date(2026, 9, 30), description="DISBURSEMENT INITIATED BY X"),
        _deposit(transactionID="NO_REPORT", reportDate=None,
                 dateTime=datetime(2026, 9, 28, 14, 0), settleDate=date(2026, 9, 30)),
        _deposit(transactionID="SETTLE_ONLY", reportDate=None, dateTime=None,
                 settleDate=date(2026, 9, 30)),
    ])

    flows = {f["ib_key"]: f for f in await _svc().extract_cash_flows(_flex(statement))}

    assert flows["W"]["flow_date"] == date(2026, 9, 28)
    assert flows["NO_REPORT"]["flow_date"] == date(2026, 9, 28)
    assert flows["SETTLE_ONLY"]["flow_date"] == date(2026, 9, 30)


@pytest.mark.asyncio
async def test_extract_cash_flows_keeps_the_sign_of_a_withdrawal():
    statement = SimpleNamespace(CashTransactions=[
        _deposit(transactionID="W1", amount=Decimal("-750"), description="WIRE OUT"),
    ])

    flows = await _svc().extract_cash_flows(_flex(statement))

    assert flows[0]["amount"] == Decimal("-750")


@pytest.mark.asyncio
async def test_extract_cash_flows_drops_zero_amounts():
    statement = SimpleNamespace(CashTransactions=[_deposit(amount=Decimal("0"))])
    assert await _svc().extract_cash_flows(_flex(statement)) == []


@pytest.mark.asyncio
async def test_extract_cash_flows_absent_section_returns_empty():
    assert await _svc().extract_cash_flows(_flex(SimpleNamespace())) == []


@pytest.mark.asyncio
async def test_extract_transfers_reads_type_direction_and_cash_leg():
    statement = SimpleNamespace(Transfers=[
        _transfer(cashTransfer=Decimal("2500")),
    ])

    transfers = await _svc().extract_transfers(_flex(statement))

    assert len(transfers) == 1
    t = transfers[0]
    assert t["ib_key"] == "TR1"
    assert t["transfer_type"] == "ACATS"
    assert t["direction"] == "IN"
    assert t["cash_transfer"] == Decimal("2500")
    assert t["position_amount"] == Decimal("18000")
    assert t["transfer_date"] == date(2026, 1, 20)
    assert t["company"] == "Scalable Capital"


@pytest.mark.asyncio
async def test_extract_transfers_absent_section_returns_empty():
    assert await _svc().extract_transfers(_flex(SimpleNamespace())) == []


# ------------------------------------------------------------------------ persistence

@pytest.mark.asyncio
async def test_transfer_direction_becomes_the_flow_type():
    engine, session = await _make_session()
    try:
        transfers = await _svc().extract_transfers(_flex(SimpleNamespace(Transfers=[
            _transfer(transactionID="IN1", direction=enums.InOut.IN),
            _transfer(transactionID="OUT1", direction=enums.InOut.OUT),
        ])))
        result = await persist_cash_flows(
            CashFlowRepository(session), CurrencyService(session), [], transfers
        )
        await session.commit()

        assert result["transfers_seen"] == 2
        rows = {r.ib_key: r for r in (await session.execute(select(CashFlow))).scalars()}
        assert rows["IN1"].flow_type == TRANSFER_IN
        assert rows["OUT1"].flow_type == TRANSFER_OUT
        # An in-kind transfer carries no cash but is still on the record.
        assert rows["IN1"].amount == Decimal("0")
        assert "ACATS" in rows["IN1"].description
        assert "Scalable Capital" in rows["IN1"].description
        assert "18000" in rows["IN1"].description
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unconvertible_transfer_type_is_left_out_of_the_description():
    """
    This account's transfers arrive as type='FOP', which ibflex's TransferType enum
    cannot convert, so the sanitizer drops the attribute and the parser yields
    'UNKNOWN'. That must not end up prefixing every row in the audit list.
    """
    engine, session = await _make_session()
    try:
        transfers = await _svc().extract_transfers(_flex(SimpleNamespace(Transfers=[
            _transfer(type=None),   # what the sanitizer leaves behind for 'FOP'
        ])))
        assert transfers[0]["transfer_type"] == "UNKNOWN"

        await persist_cash_flows(
            CashFlowRepository(session), CurrencyService(session), [], transfers
        )
        await session.commit()

        row = (await session.execute(select(CashFlow))).scalars().one()
        assert "UNKNOWN" not in row.description
        assert row.description.startswith("IN ")     # direction still leads
        assert row.flow_type == TRANSFER_IN          # and it is still excluded
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_deposit_matching_a_transfers_cash_leg_is_reclassified():
    """
    IBKR may book the cash leg of an incoming transfer as an ordinary
    'Deposits & Withdrawals' row. Left alone it would read as a large contribution in
    a month with no new savings, so an exact (date, amount, currency) match against a
    transfer reclassifies it. Never matched on description text.
    """
    engine, session = await _make_session()
    try:
        svc = _svc()
        deposits = await svc.extract_cash_flows(_flex(SimpleNamespace(CashTransactions=[
            # Same date/amount/currency as the transfer's cash leg below.
            _deposit(transactionID="D_TR", reportDate=date(2026, 1, 20),
                     amount=Decimal("2500"), description="INCOMING ACCT TRANSFER"),
            # A genuine, unrelated deposit.
            _deposit(transactionID="D_REAL", reportDate=date(2026, 2, 5),
                     amount=Decimal("1000")),
        ])))
        transfers = await svc.extract_transfers(_flex(SimpleNamespace(Transfers=[
            _transfer(cashTransfer=Decimal("2500")),
        ])))

        result = await persist_cash_flows(
            CashFlowRepository(session), CurrencyService(session), deposits, transfers
        )
        await session.commit()

        assert result["deposits_reclassified_as_transfer"] == 1

        rows = {r.ib_key: r for r in (await session.execute(select(CashFlow))).scalars()}
        assert rows["D_TR"].flow_type == TRANSFER_IN     # keeps the transfer's direction
        assert rows["D_REAL"].flow_type == DEPOSIT_WITHDRAW

        # Only the genuine deposit is money added, and the floor is its date.
        repo = CashFlowRepository(session)
        assert [f.ib_key for f in await repo.get_deposits()] == ["D_REAL"]
        assert await repo.earliest_deposit_date() == date(2026, 2, 5)
        assert await repo.earliest_transfer_in_date() == date(2026, 1, 20)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_in_kind_transfer_does_not_swallow_same_day_deposits():
    """
    A zero-cash transfer must not be used as a match key: every no-cash transfer
    would collide on (date, 0) and could reclassify a real deposit.
    """
    engine, session = await _make_session()
    try:
        svc = _svc()
        deposits = await svc.extract_cash_flows(_flex(SimpleNamespace(CashTransactions=[
            _deposit(transactionID="D_SAMEDAY", reportDate=date(2026, 1, 20),
                     amount=Decimal("900")),
        ])))
        transfers = await svc.extract_transfers(_flex(SimpleNamespace(Transfers=[
            _transfer(cashTransfer=Decimal("0")),   # in-kind, same day
        ])))

        result = await persist_cash_flows(
            CashFlowRepository(session), CurrencyService(session), deposits, transfers
        )
        await session.commit()

        assert result["deposits_reclassified_as_transfer"] == 0
        assert [f.ib_key for f in await CashFlowRepository(session).get_deposits()] \
            == ["D_SAMEDAY"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_reingesting_the_same_statement_is_idempotent():
    engine, session = await _make_session()
    try:
        svc = _svc()
        statement = SimpleNamespace(CashTransactions=[_deposit()])
        deposits = await svc.extract_cash_flows(_flex(statement))

        for _ in range(2):
            await persist_cash_flows(
                CashFlowRepository(session), CurrencyService(session), deposits, []
            )
        await session.commit()

        assert await CashFlowRepository(session).count() == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_zero_amount_needs_no_exchange_rate():
    """
    Zero is zero in every currency. Converting it would demand an FX rate we may not
    have, and skipping the row would cost us the in-kind transfer record — which is
    precisely the shape that carries no cash. 'ZZZ' has no rate anywhere.
    """
    engine, session = await _make_session()
    try:
        transfers = await _svc().extract_transfers(_flex(SimpleNamespace(Transfers=[
            _transfer(currency="ZZZ", cashTransfer=Decimal("0")),
        ])))

        result = await persist_cash_flows(
            CashFlowRepository(session), CurrencyService(session), [], transfers
        )
        await session.commit()

        assert result["cash_flows_skipped"] == 0
        row = (await session.execute(select(CashFlow))).scalars().one()
        assert row.amount_eur == Decimal("0")
        assert row.flow_type == TRANSFER_IN
        # And the transfer date survives, so the coverage explanation still works.
        assert await CashFlowRepository(session).earliest_transfer_in_date() \
            == date(2026, 1, 20)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unconvertible_currency_is_skipped_not_raised():
    """
    A deposit that cannot be converted must not fail the whole sync — the coverage
    floor already distinguishes "no data" from "no money".
    """
    engine, session = await _make_session()
    try:
        svc = _svc()
        deposits = await svc.extract_cash_flows(_flex(SimpleNamespace(CashTransactions=[
            _deposit(transactionID="D_BAD", currency="ZZZ", amount=Decimal("500")),
            _deposit(transactionID="D_OK", amount=Decimal("1000")),
        ])))

        result = await persist_cash_flows(
            CashFlowRepository(session), CurrencyService(session), deposits, []
        )
        await session.commit()

        assert result["cash_flows_skipped"] == 1
        assert result["skipped_currencies"] == {"ZZZ"}
        assert result["cash_flows_seen"] == 1
        assert [f.ib_key for f in await CashFlowRepository(session).get_deposits()] \
            == ["D_OK"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_coverage_only_ever_widens_backwards():
    """
    coverage_from is the splice point, so it must never move forward — a later YTD
    statement cannot shrink coverage a prior-year import established.
    """
    engine, session = await _make_session()
    try:
        settings = AppSettingsRepository(session)
        assert await settings.get_cash_flows_covered_from() is None

        assert await settings.widen_cash_flows_covered_from(date(2026, 1, 1)) \
            == date(2026, 1, 1)
        # A later statement leaves it alone...
        assert await settings.widen_cash_flows_covered_from(date(2027, 1, 1)) \
            == date(2026, 1, 1)
        # ...but a prior-year import extends it.
        assert await settings.widen_cash_flows_covered_from(date(2025, 1, 1)) \
            == date(2025, 1, 1)
        # A missing from_date is a no-op, not a crash.
        assert await settings.widen_cash_flows_covered_from(None) == date(2025, 1, 1)
        await session.commit()
    finally:
        await session.close()
        await engine.dispose()
