"""
An IBKR statement may only touch IBKR rows.

This is the load-bearing test of the account dimension, and the failure it pins is
not subtle once seen: `reconcile_taxlots` reads *every* open tax lot, unions those
security ids with the statement's own, and DELETEs. An IBKR Flex statement cannot
mention a Swiss pillar 3a fund -- there is no section it could appear in -- so before
this scoping existed, the first sync after a 3a lot was created would delete it, and
Phase D would then see the whole position as sold and book a fictitious disposal into
realized P&L, XIRR's flow terms and that day's `external_flow_eur`.

The wipe guard had the mirror problem, and its dangerous half is the quiet one: it
compared the incoming lots against every open lot in the database, so an IBKR book
that had genuinely emptied would be *masked* by a pillar 3a holding and the guard
would stay silent on exactly the statement it exists to catch.

Written as a family rule where it can be. Three specific cases are worth naming
individually because each is a different destructive path; the fourth test is the one
that catches the *next* path, which is the one nobody will remember to add a case for.

Offline: no network.
"""
import ast
import pathlib
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.accounts import IBKR, PILLAR3A
from app.database import Base
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.repositories.taxlot_repository import TaxLotRepository
from app.repositories.trade_repository import TradeRepository
from app.services.sync_helper import (
    EmptyStatementError,
    reconcile_taxlots,
    restamp_unsourced_closed_lots,
)


class FakeCurrencyService:
    """Identity conversion — FX is not what this file is about."""

    async def convert_to_eur(self, amount, from_currency, target_date):
        return amount


async def _make_repos():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [Security.__table__, TaxLot.__table__, Trade.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    session = AsyncSession(engine, expire_on_commit=False)
    return engine, session, TaxLotRepository(session), TradeRepository(session)


async def _security(session, security_id, account, conid=None, symbol=None):
    session.add(Security(
        id=security_id,
        isin=f"XX{security_id:010d}",
        symbol=symbol or f"SEC{security_id}",
        description=f"Security {security_id}",
        currency="CHF" if account != IBKR else "USD",
        # A pillar 3a fund genuinely has no IBKR contract id, and the point of the
        # column being nullable is that we refuse to invent one.
        conid=conid,
        exchange="FUND" if account != IBKR else "NASDAQ",
        account=account,
    ))
    await session.flush()


async def _open_lot(repo, security_id, quantity=Decimal("10"), price=Decimal("100")):
    return await repo.create({
        "security_id": security_id,
        "open_date": date(2026, 1, 5),
        "quantity": quantity,
        "cost_basis": quantity * price,
        "price_per_unit": price,
        "currency": "CHF",
        "cost_basis_eur": quantity * price,
        "is_open": True,
    })


def _incoming(conid, quantity=Decimal("10"), price=Decimal("100")):
    return {
        "conid": conid,
        "open_date": date(2026, 1, 5),
        "quantity": quantity,
        "cost_basis": quantity * price,
        "price_per_unit": price,
        "currency": "USD",
        "is_open": True,
    }


@pytest.mark.asyncio
async def test_a_flex_sync_cannot_delete_or_close_a_pillar3a_lot():
    """
    The headline failure. An ordinary, entirely successful IBKR sync, over a database
    that also holds a 3a position the statement has no way to mention.
    """
    engine, session, repo, _ = await _make_repos()
    try:
        await _security(session, 1, IBKR, conid=100)
        await _security(session, 2, PILLAR3A)
        ibkr_lot = await _open_lot(repo, 1)
        p3a_lot = await _open_lot(repo, 2, quantity=Decimal("3.615"),
                                  price=Decimal("121.201101"))

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=[_incoming("100")],
            report_to_date=date(2026, 9, 5),
        )

        # The 3a lot is untouched: still open, same id, same quantity.
        survivors = await repo.get_by_security_id(2, is_open=True)
        assert len(survivors) == 1
        assert survivors[0].id == p3a_lot.id
        assert survivors[0].quantity == Decimal("3.615000")

        # And crucially it was not booked as a disposal.
        assert await repo.get_by_security_id(2, is_open=False) == []
        assert result["lots_closed_full"] == 0
        assert result["lots_closed_partial"] == 0

        # The IBKR side still reconciles normally — scoping must not break the job.
        assert len(await repo.get_by_security_id(1, is_open=True)) == 1
        assert ibkr_lot is not None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_wipe_guard_counts_only_ibkr_lots():
    """
    Both directions, because the guard is wrong in both if it counts the wrong rows.

    It must still fire when IBKR lots exist (its whole purpose), and it must NOT be
    *suppressed* into silence by a foreign account's holdings — the quiet half, where
    a genuinely failed statement would be accepted and the IBKR book wiped.
    """
    engine, session, repo, _ = await _make_repos()
    try:
        await _security(session, 1, IBKR, conid=100)
        await _security(session, 2, PILLAR3A)
        await _open_lot(repo, 1)
        await _open_lot(repo, 2)

        # An empty statement while IBKR lots exist is a failed statement. It must
        # refuse, and the 3a holding beside it must not change that.
        with pytest.raises(EmptyStatementError):
            await reconcile_taxlots(
                repo, FakeCurrencyService(),
                conid_to_security_id={},
                taxlots_data=[],
                report_to_date=date(2026, 9, 5),
            )
        assert len(await repo.get_by_security_id(1, is_open=True)) == 1
        assert len(await repo.get_by_security_id(2, is_open=True)) == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_3a_only_database_does_not_trip_the_wipe_guard():
    """
    The mis-fire case. With no IBKR lots at all there is nothing for an empty
    statement to destroy, so refusing would block every sync on an account whose
    brokerage sleeve is fully liquidated while its 3a sleeve is not.
    """
    engine, session, repo, _ = await _make_repos()
    try:
        await _security(session, 2, PILLAR3A)
        await _open_lot(repo, 2)

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={},
            taxlots_data=[],
            report_to_date=date(2026, 9, 5),
        )
        assert result["lots_closed_full"] == 0
        assert len(await repo.get_by_security_id(2, is_open=True)) == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_restamp_never_touches_a_pillar3a_closed_lot():
    """
    The third destructive path, and the least obvious.

    `restamp_unsourced_closed_lots` matches a closed lot to a SELL trade **on quantity
    alone**. A 3a lot closed with the same quantity as some IBKR sale would otherwise
    be stamped with that sale's date and `close_source='trade'` — a date from a
    different account, on a different instrument, silently.
    """
    engine, session, repo, trade_repo = await _make_repos()
    try:
        await _security(session, 1, IBKR, conid=100)
        await _security(session, 2, PILLAR3A)

        # A 3a lot closed on the pre-fix shape: no close_source at all.
        p3a_closed = await repo.create({
            "security_id": 2, "open_date": date(2026, 1, 5),
            "quantity": Decimal("10"), "cost_basis": Decimal("1000"),
            "price_per_unit": Decimal("100"), "currency": "CHF",
            "cost_basis_eur": Decimal("1000"), "is_open": False,
            "close_date": date(2026, 8, 1), "close_source": None,
        })
        # An IBKR SELL of the same quantity, on a different security.
        await trade_repo.upsert({
            "ib_key": "T1", "conid": "100", "security_id": 2,
            "trade_date": date(2026, 6, 1), "buy_sell": "SELL",
            "quantity": Decimal("-10"), "account": IBKR,
        })

        restamped = await restamp_unsourced_closed_lots(repo, trade_repo)

        assert restamped == 0
        await session.refresh(p3a_closed)
        assert p3a_closed.close_source is None
        assert p3a_closed.close_date == date(2026, 8, 1)
    finally:
        await session.close()
        await engine.dispose()


# ── The family rule ─────────────────────────────────────────────────────────

def test_every_destructive_taxlot_read_in_sync_helper_is_account_scoped():
    """
    The check that survives a fourth destructive path being added.

    `sync_helper` is the only module allowed to delete tax lots, and every read whose
    result reaches a delete or a close must be account-scoped. Naming the three known
    ones would not catch the fourth, which is the one that costs a portfolio.

    The rule is stated as: no bare `get_open_taxlots()` may appear in this module. It
    is a narrow rule on purpose — the repository method defaults to *all accounts*
    because `/api/sync/status` legitimately wants that, so the danger is precisely a
    caller here taking the default.
    """
    path = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "sync_helper.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    unscoped = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "get_open_taxlots":
            continue
        if not any(kw.arg == "account" for kw in node.keywords) and not node.args:
            unscoped.append(node.lineno)

    assert not unscoped, (
        f"sync_helper.py calls get_open_taxlots() without an account at line(s) "
        f"{unscoped}. Its result feeds delete_open_by_security_ids, so an unscoped "
        "read deletes the open lots of every account this statement does not mention "
        "— which is every other account, always."
    )
