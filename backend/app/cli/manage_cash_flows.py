"""
Inspect and reclassify the external cash ledger.

`cash_flows` decides the "money added" figure, and one row being on the wrong side of
the deposit/transfer line moves it by the size of a whole portfolio. The pre-2026
history arrived at IBKR by transfer from Scalable Capital and Trading 212, and IBKR
sometimes books such a transfer's cash leg as an ordinary "Deposits & Withdrawals"
row. `persist_cash_flows` catches that automatically by matching
(date, amount, currency) against the <Transfers> section — but only if the section is
enabled and the amounts line up exactly, so a manual override has to exist.

Counting a transfer as a deposit would report a large contribution in a month with no
new savings, and double-count purchases that are already recorded under the
transferred lots' own open_dates. That is the failure this command exists to fix.

Fourth CLI in the same pattern as `ingest_flex_xml`, `import_prices` and
`manage_mappings`, for the same reason: `/api/` is proxied publicly and
unauthenticated, so a route that rewrites the savings ledger is a far larger surface
than a command over ssh. Touches no network.

    python -m app.cli.manage_cash_flows list
    python -m app.cli.manage_cash_flows list --type DEPOSITWITHDRAW
    python -m app.cli.manage_cash_flows reclassify D1234 --as TRANSFER_IN --dry-run

`list` marks which rows count as money added, so a suspicious one is visible rather
than left to be noticed as a step in the chart.
"""
import argparse
import asyncio
import logging
import sys
from app.clock import utcnow
from decimal import Decimal
from typing import Dict, Optional, Tuple

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.logging_setup import configure_logging
from app.models.cash_flow import (
    CashFlow, DEPOSIT_WITHDRAW, TRANSFER, TRANSFER_IN, TRANSFER_OUT, TRANSFER_TYPES,
)
from app.repositories.cash_flow_repository import CashFlowRepository
from app.repositories.sync_run_repository import SyncRunRepository

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_cash_flow"
VALID_TYPES = (DEPOSIT_WITHDRAW, TRANSFER_IN, TRANSFER_OUT, TRANSFER)


class CashFlowError(Exception):
    """A refused edit — reported, never half-applied."""


async def cmd_list(db, flow_type: Optional[str]) -> int:
    stmt = select(CashFlow).order_by(CashFlow.flow_date.asc(), CashFlow.ib_key.asc())
    if flow_type:
        stmt = stmt.where(CashFlow.flow_type == flow_type)
    rows = list((await db.execute(stmt)).scalars().all())

    if not rows:
        print("No cash flows recorded.")
        print(
            "Enable 'Deposits & Withdrawals' under Cash Transactions (and the Transfers "
            "section) in the Flex Query; the next scheduled IBKR sync will populate this."
        )
        return 0

    print(f"{'ib_key':<20} {'date':<12} {'type':<16} {'amount':>14} {'cur':<4} "
          f"{'EUR':>12}  added?  description")
    for r in rows:
        counts = "yes" if r.flow_type == DEPOSIT_WITHDRAW else "no"
        print(
            f"{r.ib_key[:20]:<20} {str(r.flow_date):<12} {r.flow_type:<16} "
            f"{r.amount:>14} {(r.currency or '?'):<4} {r.amount_eur:>12.2f}  "
            f"{counts:<6}  {(r.description or '')[:44]}"
        )

    repo = CashFlowRepository(db)
    deposits = await repo.get_deposits()
    total = sum((d.amount_eur for d in deposits), Decimal("0"))
    print()
    print(f"{len(deposits)} row(s) count as money added, totalling {total:.2f} EUR.")
    print(f"Deposit ledger starts {await repo.earliest_deposit_date()}; "
          f"earliest incoming transfer {await repo.earliest_transfer_in_date()}.")
    excluded = [r for r in rows if r.flow_type in TRANSFER_TYPES]
    if excluded:
        print(
            f"{len(excluded)} transfer row(s) excluded from money added — capital that "
            f"was saved earlier elsewhere, whose purchases are already counted under "
            f"the transferred lots' own dates."
        )
    return 0


async def cmd_reclassify(
    db, ib_key: str, new_type: str, dry_run: bool
) -> Tuple[int, Dict]:
    if new_type not in VALID_TYPES:
        raise CashFlowError(
            f"Unknown flow type {new_type!r}; expected one of {', '.join(VALID_TYPES)}"
        )

    row = (await db.execute(
        select(CashFlow).where(CashFlow.ib_key == ib_key)
    )).scalars().first()
    if not row:
        raise CashFlowError(
            f"No cash flow with ib_key {ib_key!r} — run `list` to see what exists"
        )

    if row.flow_type == new_type:
        print(f"{ib_key} is already {new_type}; nothing to do.")
        return 0, {}

    was = row.flow_type
    effect = (
        "it will stop counting as money added"
        if new_type in TRANSFER_TYPES else "it will start counting as money added"
    )

    if dry_run:
        print(
            f"DRY RUN - would reclassify {ib_key} ({row.amount} {row.currency} on "
            f"{row.flow_date}) from {was} to {new_type}; {effect}."
        )
        return 0, {}

    row.flow_type = new_type
    await db.flush()
    await db.commit()

    print(f"Reclassified {ib_key} from {was} to {new_type}; {effect}.")
    return 0, {
        "action": "reclassify", "ib_key": ib_key, "from": was, "to": new_type,
        "amount": str(row.amount), "currency": row.currency,
        "flow_date": str(row.flow_date),
    }


async def run(args) -> int:
    started_at = utcnow()

    async with AsyncSessionLocal() as db:
        try:
            if args.command == "list":
                return await cmd_list(db, args.type)

            code, details = await cmd_reclassify(
                db, args.ib_key, getattr(args, "as"), args.dry_run
            )

        except Exception as e:
            await db.rollback()
            # Best-effort bookkeeping, as in the other CLIs: it must never mask the error.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED - nothing was changed: {e}", file=sys.stderr)
            return 1

        if details:
            # An edit to the savings ledger being invisible is the same failure mode that
            # let a bad ticker mapping run for months, so it lands in the sync history.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE,
                status="success",
                message=(
                    f"reclassify {details['ib_key']} "
                    f"{details['from']} -> {details['to']}"
                ),
                details=details,
                started_at=started_at,
            )
        return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show every recorded cash flow")
    p_list.add_argument(
        "--type", choices=VALID_TYPES, default=None, help="only this flow type"
    )

    p_re = sub.add_parser(
        "reclassify", help="move a flow between the deposit and transfer sides"
    )
    p_re.add_argument("ib_key", help="the flow's ib_key (IBKR transactionID)")
    p_re.add_argument(
        "--as", required=True, choices=VALID_TYPES, help="the flow type to set"
    )
    p_re.add_argument("--dry-run", action="store_true", help="report, change nothing")

    return parser


def main() -> int:
    configure_logging(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
