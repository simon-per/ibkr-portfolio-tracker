"""
Tests for the cash-flow CLI (`app/cli/manage_cash_flows.py`).

This was the one CLI in the family with no tests at all — `manage_mappings`,
`import_prices`, `prune_empty_dividends` and `purge_dividend_estimates` each have
their own file — and it guards what CLAUDE.md calls the highest-risk number in
the contributions feature: one row on the wrong side of the deposit/transfer line
moves "money added" by the size of a whole portfolio, inventing savings in a
month that had none and double-counting purchases already recorded under the
transferred lots' own open_dates.

`persist_cash_flows` catches IBKR booking a transfer's cash leg as an ordinary
"Deposits & Withdrawals" row automatically, but only when the Transfers section
is enabled and the amounts line up exactly — so the manual override has to work,
and it has to be honest about what it changed.

The fixture mirrors the real account's shape: deposits including one genuine
withdrawal (negative, sign preserved) and one non-EUR row, alongside in-kind
transfers carrying no cash.

Offline: no Yahoo, no IBKR, no network at all.
"""
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401  register all mappers
from app.models.cash_flow import (
    CashFlow, DEPOSIT_WITHDRAW, TRANSFER, TRANSFER_IN, TRANSFER_OUT,
)
from app.models.sync_run import SyncRun
from app.cli import manage_cash_flows as cli
from app.cli.manage_cash_flows import CashFlowError, build_parser, cmd_list, cmd_reclassify
from app.repositories.cash_flow_repository import CashFlowRepository
from tests.memory_db import memory_engine


def flow(ib_key, day, flow_type, amount, currency="EUR", amount_eur=None, description=None):
    return CashFlow(
        ib_key=ib_key,
        flow_date=day,
        flow_type=flow_type,
        amount=Decimal(str(amount)),
        currency=currency,
        amount_eur=Decimal(str(amount if amount_eur is None else amount_eur)),
        description=description,
    )


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = await memory_engine()

    session = AsyncSession(engine, expire_on_commit=False)
    session.add_all([
        flow("D1", date(2026, 1, 9), DEPOSIT_WITHDRAW, 5000, description="Cash Receipt"),
        flow("D2", date(2026, 2, 3), DEPOSIT_WITHDRAW, 2500),
        # A real withdrawal: negative, and the sign must survive into the total.
        flow("D3", date(2026, 3, 11), DEPOSIT_WITHDRAW, -800),
        # The CHF row that exercises the FX path against an otherwise-EUR ledger.
        flow("D4", date(2026, 4, 2), DEPOSIT_WITHDRAW, 1000, "CHF", amount_eur=1040),
        # In-kind transfers: no cash at all, which is why nothing needed
        # reclassifying on the real account.
        flow("T1", date(2026, 1, 20), TRANSFER_IN, 0, description="FOP"),
        flow("T2", date(2026, 1, 20), TRANSFER_IN, 0),
        flow("T3", date(2026, 5, 6), TRANSFER_OUT, 0),
    ])
    await session.commit()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: _Ctx())
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


def args(*argv):
    return build_parser().parse_args(list(argv))


async def _money_added(db) -> Decimal:
    deposits = await CashFlowRepository(db).get_deposits()
    return sum((d.amount_eur for d in deposits), Decimal("0"))


async def _runs(db):
    return list((await db.execute(select(SyncRun))).scalars().all())


async def _row(db, ib_key) -> CashFlow:
    return (await db.execute(
        select(CashFlow).where(CashFlow.ib_key == ib_key)
    )).scalars().first()


# --- list ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_marks_which_rows_count_as_money_added(db, capsys):
    assert await cmd_list(db, None) == 0
    out = capsys.readouterr().out

    # Every deposit says yes, every transfer says no. This column is the audit
    # CLAUDE.md prescribes before trusting any money-added figure. Keyed on the
    # ib_key column rather than the line prefix, so the "Deposit ledger starts"
    # summary is not mistaken for a row.
    rows = {
        line.split()[0]: line
        for line in out.splitlines()
        if line.split() and line.split()[0] in {"D1", "D2", "D3", "D4", "T1", "T2", "T3"}
    }
    assert len(rows) == 7, rows

    for key, line in rows.items():
        tokens = line.split()
        expected = "yes" if key.startswith("D") else "no"
        unexpected = "no" if expected == "yes" else "yes"
        assert expected in tokens, line
        assert unexpected not in tokens, line


@pytest.mark.asyncio
async def test_list_totals_only_deposits_and_keeps_a_withdrawal_negative(db, capsys):
    await cmd_list(db, None)
    out = capsys.readouterr().out
    # 5000 + 2500 - 800 + 1040 = 7740. A withdrawal that lost its sign would
    # report 9340 and overstate savings by 1600.
    assert "4 row(s) count as money added, totalling 7740.00 EUR" in out


@pytest.mark.asyncio
async def test_list_says_transfers_are_excluded_and_why(db, capsys):
    await cmd_list(db, None)
    out = capsys.readouterr().out
    assert "3 transfer row(s) excluded from money added" in out


@pytest.mark.asyncio
async def test_list_reports_both_ledger_start_dates(db, capsys):
    await cmd_list(db, None)
    out = capsys.readouterr().out
    # The deposit ledger and the first incoming transfer are different dates on
    # this account, and the contributions splice depends on which is which.
    assert "Deposit ledger starts 2026-01-09" in out
    assert "earliest incoming transfer 2026-01-20" in out


@pytest.mark.asyncio
async def test_list_filters_by_type(db, capsys):
    await cmd_list(db, TRANSFER_IN)
    out = capsys.readouterr().out
    assert "T1" in out and "T2" in out
    assert "D1" not in out
    assert "T3" not in out


@pytest.mark.asyncio
async def test_list_on_an_empty_ledger_names_the_flex_sections_to_enable(db, capsys):
    for key in ("D1", "D2", "D3", "D4", "T1", "T2", "T3"):
        await db.delete(await _row(db, key))
    await db.commit()

    assert await cmd_list(db, None) == 0
    out = capsys.readouterr().out
    # An empty ledger is a configuration problem, not an empty account, and the
    # fix is in the IBKR portal rather than in the code.
    assert "No cash flows recorded" in out
    assert "Deposits & Withdrawals" in out
    assert "Transfers" in out


# --- reclassify ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reclassifying_a_deposit_as_a_transfer_removes_it_from_money_added(db):
    before = await _money_added(db)
    code, details = await cmd_reclassify(db, "D2", TRANSFER_IN, dry_run=False)

    assert code == 0
    assert (await _row(db, "D2")).flow_type == TRANSFER_IN
    # The whole point: the figure moves by exactly that row.
    assert await _money_added(db) == before - Decimal("2500")
    assert details["from"] == DEPOSIT_WITHDRAW
    assert details["to"] == TRANSFER_IN


@pytest.mark.asyncio
async def test_reclassifying_a_transfer_as_a_deposit_adds_it(db):
    # The reverse direction has to work too — the automatic guard can also
    # misfire and pull a genuine deposit onto the transfer side.
    before = await _money_added(db)
    await cmd_reclassify(db, "T1", DEPOSIT_WITHDRAW, dry_run=False)
    assert (await _row(db, "T1")).flow_type == DEPOSIT_WITHDRAW
    # An in-kind transfer carries no cash, so it adds nothing but does now count.
    assert await _money_added(db) == before
    assert len(await CashFlowRepository(db).get_deposits()) == 5


@pytest.mark.asyncio
async def test_reclassify_reports_the_effect_in_the_direction_it_applies(db, capsys):
    await cmd_reclassify(db, "D2", TRANSFER_OUT, dry_run=False)
    assert "will stop counting as money added" in capsys.readouterr().out

    await cmd_reclassify(db, "T3", DEPOSIT_WITHDRAW, dry_run=False)
    assert "will start counting as money added" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_dry_run_changes_nothing_and_records_nothing(db, capsys):
    before = await _money_added(db)
    code, details = await cmd_reclassify(db, "D1", TRANSFER_IN, dry_run=True)

    assert code == 0
    assert details == {}
    assert (await _row(db, "D1")).flow_type == DEPOSIT_WITHDRAW
    assert await _money_added(db) == before
    assert "DRY RUN" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_reclassifying_to_the_same_type_is_a_no_op(db, capsys):
    code, details = await cmd_reclassify(db, "D1", DEPOSIT_WITHDRAW, dry_run=False)
    assert code == 0
    # No details means no sync_run, which is right: nothing changed.
    assert details == {}
    assert "already" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_an_unknown_key_is_refused_and_points_at_list(db):
    with pytest.raises(CashFlowError, match="No cash flow with ib_key"):
        await cmd_reclassify(db, "NOPE", TRANSFER_IN, dry_run=False)


@pytest.mark.asyncio
async def test_an_unknown_type_is_refused_before_anything_is_touched(db):
    # argparse's `choices` guards the CLI, but the function is also called
    # directly and must not write a flow_type nothing else understands.
    with pytest.raises(CashFlowError, match="Unknown flow type"):
        await cmd_reclassify(db, "D1", "DEPOSIT", dry_run=False)
    assert (await _row(db, "D1")).flow_type == DEPOSIT_WITHDRAW


# --- run(): exit codes and the sync_runs trail ---------------------------------------


@pytest.mark.asyncio
async def test_a_successful_edit_lands_in_the_sync_history(db):
    # An edit to the savings ledger being invisible is the failure that let a bad
    # ticker mapping run for months.
    assert await cli.run(args("reclassify", "D2", "--as", TRANSFER_IN)) == 0

    runs = await _runs(db)
    assert len(runs) == 1
    assert runs[0].sync_type == "manual_cash_flow"
    assert runs[0].status == "success"
    assert "D2" in runs[0].message
    assert DEPOSIT_WITHDRAW in runs[0].message and TRANSFER_IN in runs[0].message


@pytest.mark.asyncio
async def test_a_dry_run_leaves_no_sync_run(db):
    assert await cli.run(args("reclassify", "D2", "--as", TRANSFER_IN, "--dry-run")) == 0
    assert await _runs(db) == []


@pytest.mark.asyncio
async def test_a_refused_edit_exits_nonzero_and_records_the_error(db, capsys):
    before = await _money_added(db)
    assert await cli.run(args("reclassify", "MISSING", "--as", TRANSFER_IN)) == 1

    assert "FAILED - nothing was changed" in capsys.readouterr().err
    assert await _money_added(db) == before

    runs = await _runs(db)
    assert len(runs) == 1
    assert runs[0].status == "error"


@pytest.mark.asyncio
async def test_list_through_run_records_nothing(db):
    # Read-only, so it must not leave a trail in sync_runs.
    assert await cli.run(args("list")) == 0
    assert await _runs(db) == []


@pytest.mark.asyncio
async def test_every_valid_type_round_trips(db):
    # Each of the four is a value the rest of the app reads, so all four must be
    # settable — TRANSFER exists for a row whose direction IBKR did not give.
    for flow_type in (TRANSFER, TRANSFER_IN, TRANSFER_OUT, DEPOSIT_WITHDRAW):
        await cmd_reclassify(db, "D1", flow_type, dry_run=False)
        assert (await _row(db, "D1")).flow_type == flow_type
