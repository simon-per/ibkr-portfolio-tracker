"""
Fetch the other regulators' company figures now, instead of waiting for Sunday's pass
(docs/company-financials.md, *Markets beyond the SEC*): `--source dart` reads OpenDART's
Korean reports, `--source esef` the ESEF annual reports on filings.xbrl.org. The same pass the
weekly job runs (`dart_sync.KoreaFinancialsSync.refresh`, `esef_sync.EuropeFinancialsSync
.refresh`): its scope, its pacing, its once-per-report stamps. `--force` asks again what held
nothing (OpenDART's empty reports and profiles, an LEI's filings list) — never a report
already read. `--dry-run` lists the scope and asks nothing. Touches no Yahoo, no IBKR and no
EDGAR. Needs `OPENDART_API_KEY` for `dart`; `esef` needs no key.

Usage, inside the container:

    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_markets --source dart --dry-run
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_markets --source dart
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_markets --source esef
"""

import argparse
import asyncio
import logging
import sys

from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.logging_setup import configure_logging
from app.repositories.sync_run_repository import SyncRunRepository
from app.single_flight import single_flight

logger = logging.getLogger(__name__)


def _source(name: str):
    """(gate, manual sync type, the pass's class, its not-configured message) of a source."""
    if name == "dart":
        from app.services import dart_sync

        return (
            dart_sync.DART_GATE,
            dart_sync.MANUAL_SYNC_TYPE,
            dart_sync.KoreaFinancialsSync,
            dart_sync.NOT_CONFIGURED,
        )
    if name == "esef":
        from app.services import esef_sync

        return (
            esef_sync.ESEF_GATE,
            esef_sync.MANUAL_SYNC_TYPE,
            esef_sync.EuropeFinancialsSync,
            None,
        )
    if name == "twse":
        from app.services import twse_sync

        return (
            twse_sync.TWSE_GATE,
            twse_sync.MANUAL_SYNC_TYPE,
            twse_sync.TaiwanFinancialsSync,
            None,
        )
    if name == "edinet":
        from app.services import edinet_sync

        return (
            edinet_sync.EDINET_GATE,
            edinet_sync.MANUAL_SYNC_TYPE,
            edinet_sync.JapanFinancialsSync,
            edinet_sync.NOT_CONFIGURED,
        )
    if name == "vendor":
        from app.services import vendor_sync

        return (
            vendor_sync.VENDOR_GATE,
            vendor_sync.MANUAL_SYNC_TYPE,
            vendor_sync.VendorFinancialsSync,
            vendor_sync.NOT_CONFIGURED,
        )
    raise ValueError(name)


async def fetch(source: str, dry_run: bool = False, force: bool = False) -> int:
    gate, sync_type, sync_class, not_configured = _source(source)
    started_at = utcnow()
    async with AsyncSessionLocal() as db:
        try:
            with single_flight(gate):
                summary = await sync_class(db).refresh(dry_run=dry_run, force=force)
        except Exception as e:
            await db.rollback()
            await SyncRunRepository(db).record(
                sync_type=sync_type,
                status="error",
                message=str(e),
                started_at=started_at,
            )
            print(f"FAILED: {e}", file=sys.stderr)
            return 1
        if summary is None:
            print(not_configured, file=sys.stderr)
            return 1
        for row in summary.get("outcomes", []):
            print(f"{row['key']:<34} {row['outcome']}")
        for warning in summary.get("warnings", []):
            print(f"WARNING: {warning}")
        print(summary["message"])
        if dry_run:
            print("DRY RUN - nothing written, no requests made.")
            return 0
        await SyncRunRepository(db).record(
            sync_type=sync_type,
            status=summary["status"],
            message=summary["message"],
            details={k: v for k, v in summary.items() if k != "warnings"},
            warnings=summary.get("warnings"),
            started_at=started_at,
        )
        return 0


def main() -> int:
    configure_logging(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO; OpenDART's carries the key (masked anyway).
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", choices=("dart", "esef", "twse", "edinet", "vendor"), required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return asyncio.run(fetch(args.source, dry_run=args.dry_run, force=args.force))


if __name__ == "__main__":
    sys.exit(main())
