"""
A cash flow reclassified by hand stays reclassified (review item I1, 2026-11-08).

`manage_cash_flows reclassify` is docs/troubleshooting.md's fix for a transfer IBKR booked
as a deposit (or the reverse). The Flex window re-delivers each row for about a month, and
the next ingest's upsert wrote the statement's type back over the owner's, so the fix lasted
a day. Both directions, and the one where the ingest's own <Transfers> match is what the
owner overruled. Pure DB.
"""

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.cli.manage_cash_flows import cmd_reclassify
from app.models.cash_flow import CashFlow
from app.repositories.cash_flow_repository import CashFlowRepository
from app.services.sync_helper import persist_cash_flows
from tests.memory_db import memory_engine


class EurOnly:
    async def convert_to_eur(self, amount, from_currency, target_date):
        return Decimal(str(amount))


DEPOSIT = {
    "ib_key": "D9",
    "flow_date": date(2026, 11, 2),
    "flow_type": "DEPOSITWITHDRAW",
    "amount": Decimal("25000"),
    "currency": "EUR",
    "description": "Cash Receipt",
}
# A position transfer whose cash leg matches the deposit: the ingest calls it a transfer.
TRANSFER = {
    "ib_key": "TR1",
    "transfer_date": date(2026, 11, 2),
    "direction": "IN",
    "cash_transfer": Decimal("25000"),
    "currency": "EUR",
    "company": "Other broker",
}


async def _ingest(session, transfers=()):
    await persist_cash_flows(
        CashFlowRepository(session),
        EurOnly(),
        [dict(DEPOSIT)],
        [dict(t) for t in transfers],
    )
    await session.commit()


async def _state(session):
    row = (
        await session.execute(select(CashFlow).where(CashFlow.ib_key == "D9"))
    ).scalar_one()
    added = sum(d.amount_eur for d in await CashFlowRepository(session).get_deposits())
    return row.flow_type, added


@pytest_asyncio.fixture
async def session():
    engine = await memory_engine()
    s = AsyncSession(engine, expire_on_commit=False)
    yield s
    await s.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_deposit_reclassified_as_a_transfer_stays_one(session):
    await _ingest(session)
    assert await _state(session) == ("DEPOSITWITHDRAW", Decimal("25000"))
    await cmd_reclassify(session, "D9", "TRANSFER_IN", dry_run=False)

    await _ingest(session)
    assert await _state(session) == ("TRANSFER_IN", 0)
    await _ingest(session)
    assert await _state(session) == ("TRANSFER_IN", 0)


@pytest.mark.asyncio
async def test_a_matched_transfer_reclassified_as_a_deposit_stays_one(session):
    await _ingest(session, [TRANSFER])
    assert (await _state(session))[0] == "TRANSFER_IN"
    await cmd_reclassify(session, "D9", "DEPOSITWITHDRAW", dry_run=False)

    # The same transfer re-delivered matches the deposit again; the owner's word holds.
    await _ingest(session, [TRANSFER])
    assert await _state(session) == ("DEPOSITWITHDRAW", Decimal("25000"))


@pytest.mark.asyncio
async def test_a_row_nobody_reclassified_still_takes_the_statements_type(session):
    await _ingest(session)
    await _ingest(session, [TRANSFER])
    assert (await _state(session))[0] == "TRANSFER_IN"
    row = (
        await session.execute(select(CashFlow).where(CashFlow.ib_key == "D9"))
    ).scalar_one()
    assert not row.flow_type_manual
