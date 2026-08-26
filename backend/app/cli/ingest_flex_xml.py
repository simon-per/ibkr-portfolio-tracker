"""
Ingest a Flex statement from a local XML file — no token, no Flex API call.

IBKR's Flex Web Service and the "download" button in Client Portal serve the same
statement over two independent paths. Only the API path consumes the token's request
budget, so only it can trip `Code=1025` (the undocumented lockout that blocks all
syncing for hours). Feeding a hand-downloaded file therefore works *during* a lockout,
and — because the file's period is whatever the browser asked for — it is also the only
way to reach a prior tax year, since our Flex Query is Year-to-Date.

Runs the exact same pipeline as `POST /api/sync/ibkr`: it shares `parse_flex_xml` and
`ingest_flex_statement` with both the endpoint and the scheduled jobs, so reconciliation,
the empty-statement wipe guard and the idempotent upserts all behave identically. Safe to
re-run: everything is keyed on `ib_key` / `isin + exchange`.

Touches **no** network: no Flex request and no Yahoo (so rule 1 in CLAUDE.md does not
apply). FX conversion reads the cached `exchange_rates` table.

Usage, inside the container:

    docker exec backend-portfolio-backend-1 \
        python -m app.cli.ingest_flex_xml /tmp/App_OpenLots.xml

Add `--dry-run` to parse and report what *would* be ingested without writing anything.
"""
import argparse
import asyncio
import logging
import sys
from app.clock import utcnow
from pathlib import Path

from app.database import AsyncSessionLocal
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.ibkr_service import IBKRService
from app.services.sync_helper import ingest_flex_statement

logger = logging.getLogger(__name__)

SYNC_TYPE = "ibkr_manual_xml"


async def ingest(path: Path, dry_run: bool = False) -> int:
    started_at = utcnow()
    xml_bytes = path.read_bytes()
    print(f"Read {len(xml_bytes):,} bytes from {path}")

    service = IBKRService()
    flex_data = service.parse_flex_xml(xml_bytes)
    print(
        f"Statement: account={flex_data['account_id']} "
        f"from={flex_data['from_date']} to={flex_data['to_date']}"
    )

    if dry_run:
        # Report the same counts the real run would apply, without opening a transaction.
        counts = {
            "securities": len(await service.extract_securities(flex_data)),
            "trades": len(await service.extract_trades(flex_data)),
            "corporate_actions": len(await service.extract_corporate_actions(flex_data)),
            "cash_transactions": len(await service.extract_cash_transactions(flex_data)),
            "cash_flows": len(await service.extract_cash_flows(flex_data)),
            "transfers": len(await service.extract_transfers(flex_data)),
            "taxlots": len(await service.extract_taxlots(flex_data)),
            # Both cash sections, so a dry run says whether the portal edit took.
            # A silently-absent count reads exactly like a silently-empty section.
            "cash_balances": (
                len(await service.extract_equity_summary(flex_data))
                + len(await service.extract_cash_report(flex_data))
            ),
        }
        print("DRY RUN - nothing written. Would ingest:")
        for key, value in counts.items():
            print(f"  {key:20} {value}")
        _print_warnings(flex_data.get("flex_warnings") or [])
        return 0

    async with AsyncSessionLocal() as db:
        try:
            result = await ingest_flex_statement(db, flex_data)
            await db.commit()
        except Exception as e:
            await db.rollback()
            # Record the failure for the dashboard, then surface it. Recording is
            # best-effort inside the repository and never masks this error.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED - nothing was written: {e}", file=sys.stderr)
            return 1

        warnings = result.pop("warnings", [])
        print("Ingested successfully:")
        for key, value in result.items():
            print(f"  {key:24} {value}")
        _print_warnings(warnings)

        await SyncRunRepository(db).record(
            sync_type=SYNC_TYPE,
            status="success",
            message=f"Ingested Flex statement from {path.name}",
            details=result,
            warnings=warnings or None,
            started_at=started_at,
        )
        return 0


def _print_warnings(warnings) -> None:
    if not warnings:
        return
    print(f"\n{len(warnings)} warning(s):")
    for w in warnings:
        print(f"  - {w[:300]}{'...' if len(w) > 300 else ''}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml_path", type=Path, help="Flex statement XML downloaded from IBKR")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and report counts without writing to the database",
    )
    args = parser.parse_args()

    if not args.xml_path.is_file():
        print(f"No such file: {args.xml_path}", file=sys.stderr)
        return 2

    return asyncio.run(ingest(args.xml_path, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
