"""
The broker's interest on the cash account (review item T4, 2026-11-08).

KS 36's margin criterion asks what margin cost, and the Flex ingest kept only deposits and
withdrawals from <CashTransactions>, so IBKR's interest bookings were dropped: no margin
interest anywhere, and a derived cash balance short by every booking. Now both interest
types are cash-flow rows — cash moves, never money in — and the tax report's
`trader_criteria.margin_interest` sums what was paid, absent while the interest is not on
record from the year's start. Offline: a real Flex document through the sanitizer and the
parser, and an in-memory ledger.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from ibflex import parser

from app.accounts import IBKR
from app.models.cash_flow import (
    BROKER_INTEREST_PAID,
    BROKER_INTEREST_RECEIVED,
    DEPOSIT_WITHDRAW,
    CashFlow,
)
from app.repositories.app_settings_repository import (
    BROKER_INTEREST_COVERED_FROM_KEY,
    AppSettingsRepository,
)
from app.repositories.cash_flow_repository import CashFlowLedger
from app.services.ibkr_service import IBKRService
from app.services.tax_service import TaxService

XML = b"""<FlexQueryResponse queryName="q" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1" fromDate="2026-01-01" toDate="2026-03-31" period="YTD"
 whenGenerated="2026-04-01;080000">
 <CashTransactions>
  <CashTransaction type="Deposits/Withdrawals" transactionID="CT-DEP" reportDate="2026-01-05"
   dateTime="2026-01-05" amount="5000" currency="EUR" description="CASH RECEIPTS" />
  <CashTransaction type="Broker Interest Paid" transactionID="CT-INT-PAID" reportDate="2026-02-04"
   dateTime="2026-02-04" amount="-12.34" currency="EUR" description="EUR DEBIT INT FOR JAN-2026" />
  <CashTransaction type="Broker Interest Received" transactionID="CT-INT-RCVD" reportDate="2026-03-04"
   dateTime="2026-03-04" amount="1.50" currency="EUR" description="EUR CREDIT INT FOR FEB-2026" />
 </CashTransactions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


@pytest.mark.asyncio
async def test_the_ingest_keeps_the_brokers_interest_beside_the_deposits():
    service = IBKRService(token="t", query_id="q")
    sanitized, _ = service._sanitize_flex_xml(XML)
    statement = parser.parse(sanitized).FlexStatements[0]
    flows = await service.extract_cash_flows({"statement": statement})
    assert sorted((f["flow_type"], f["amount"]) for f in flows) == [
        (BROKER_INTEREST_PAID, Decimal("-12.34")),
        (BROKER_INTEREST_RECEIVED, Decimal("1.50")),
        (DEPOSIT_WITHDRAW, Decimal("5000")),
    ]


def _flow(key, kind, amount, day):
    return CashFlow(
        ib_key=key,
        account=IBKR,
        flow_date=day,
        flow_type=kind,
        amount=Decimal(amount),
        currency="EUR",
        amount_eur=Decimal(amount),
    )


def test_interest_moves_cash_and_is_never_money_in():
    ledger = CashFlowLedger(
        [
            _flow("d", DEPOSIT_WITHDRAW, "5000", date(2026, 1, 5)),
            _flow("p", BROKER_INTEREST_PAID, "-12.34", date(2026, 2, 4)),
            _flow("r", BROKER_INTEREST_RECEIVED, "1.50", date(2026, 3, 4)),
        ]
    )
    assert [f.ib_key for f in ledger.deposits()] == ["d"]
    assert [f.ib_key for f in ledger.cash_moves()] == ["d", "p", "r"]


EUR = SimpleNamespace(base_currency="EUR", convert=lambda amount, on: amount)


@pytest.mark.asyncio
async def test_margin_interest_is_what_was_paid_from_the_years_start(db):
    db.add_all(
        [
            _flow("p1", BROKER_INTEREST_PAID, "-12.34", date(2026, 2, 4)),
            _flow("p2", BROKER_INTEREST_PAID, "-7.66", date(2026, 3, 4)),
            _flow("r", BROKER_INTEREST_RECEIVED, "1.50", date(2026, 3, 4)),
            _flow("old", BROKER_INTEREST_PAID, "-99", date(2025, 12, 4)),
        ]
    )
    await AppSettingsRepository(db).widen_date_setting(
        BROKER_INTEREST_COVERED_FROM_KEY, date(2025, 6, 1)
    )
    await db.commit()
    paid = await TaxService(db)._margin_interest(
        date(2026, 1, 1), date(2026, 12, 31), EUR
    )
    assert paid == 20.0


@pytest.mark.asyncio
async def test_margin_interest_is_absent_where_the_record_starts_late(db):
    db.add(_flow("p1", BROKER_INTEREST_PAID, "-12.34", date(2026, 11, 4)))
    service = TaxService(db)
    # Not on record at all, and on record only from November: neither is the year's.
    assert (
        await service._margin_interest(date(2026, 1, 1), date(2026, 12, 31), EUR)
        is None
    )
    await AppSettingsRepository(db).widen_date_setting(
        BROKER_INTEREST_COVERED_FROM_KEY, date(2026, 10, 15)
    )
    await db.commit()
    assert (
        await service._margin_interest(date(2026, 1, 1), date(2026, 12, 31), EUR)
        is None
    )
    # A year on record with no booking paid none.
    assert (
        await service._margin_interest(date(2027, 1, 1), date(2027, 12, 31), EUR) == 0.0
    )


@pytest.mark.asyncio
async def test_the_coverage_only_ever_widens(db):
    repo = AppSettingsRepository(db)
    assert await repo.widen_date_setting(
        BROKER_INTEREST_COVERED_FROM_KEY, date(2026, 10, 1)
    ) == date(2026, 10, 1)
    assert await repo.widen_date_setting(
        BROKER_INTEREST_COVERED_FROM_KEY, date(2026, 11, 1)
    ) == date(2026, 10, 1)
    assert await repo.widen_date_setting(
        BROKER_INTEREST_COVERED_FROM_KEY, date(2026, 1, 1)
    ) == date(2026, 1, 1)


@pytest.mark.asyncio
async def test_a_statement_with_cash_transactions_puts_the_interest_on_record(db):
    from app.services.sync_helper import record_cash_coverage

    service = IBKRService(token="t", query_id="q")
    sanitized, _ = service._sanitize_flex_xml(XML)
    statement = parser.parse(sanitized).FlexStatements[0]
    flex_data = {"statement": statement, "from_date": date(2026, 1, 1)}
    flows = await service.extract_cash_flows(flex_data)
    repo = AppSettingsRepository(db)

    assert await record_cash_coverage(repo, flex_data, flows) == date(2026, 1, 1)
    assert await repo.get_date_setting(BROKER_INTEREST_COVERED_FROM_KEY) == date(2026, 1, 1)

    # Interest without deposits puts the interest on record, never the deposits.
    only_interest = [f for f in flows if f["flow_type"] != DEPOSIT_WITHDRAW]
    earlier = {"statement": statement, "from_date": date(2025, 7, 1)}
    assert await record_cash_coverage(repo, earlier, only_interest) is None
    assert await repo.get_cash_flows_covered_from() == date(2026, 1, 1)
    assert await repo.get_date_setting(BROKER_INTEREST_COVERED_FROM_KEY) == date(2025, 7, 1)

