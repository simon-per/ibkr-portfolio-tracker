"""
Fetch company financials from SEC EDGAR now, instead of waiting for Sunday's pass
(docs/company-financials.md). The first fill, and a re-fetch after a normaliser change.

The same `CompanyFinancialsSync.refresh` the weekly job runs, with the same scope (held,
US-listed, not a known fund), pacing (5 requests a second), bounded retries and back-off.
`--force` fetches every company in scope regardless of staleness and back-off;
`--symbol NVDA` (repeatable) narrows the scope. `--dry-run` lists what is due and makes no
request and no write.

Touches no Yahoo and no IBKR. Needs `EDGAR_USER_AGENT` for anything but `--dry-run`.

Usage, inside the container:

    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_financials --dry-run
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_financials
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_financials --symbol NVDA --force
"""

import argparse
import asyncio
import logging
import sys
from typing import List, Optional

from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.logging_setup import configure_logging
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.company_financials_sync import (
    EDGAR_GATE,
    MANUAL_SYNC_TYPE,
    CompanyFinancialsSync,
)
from app.single_flight import single_flight
from app.services.edgar_client import is_configured

logger = logging.getLogger(__name__)


async def fetch_company_financials(
    dry_run: bool = False, force: bool = False, symbols: Optional[List[str]] = None
) -> int:
    started_at = utcnow()
    if not dry_run and not is_configured():
        print(
            "EDGAR_USER_AGENT is not set in backend/.env - nothing to do.",
            file=sys.stderr,
        )
        return 1

    async with AsyncSessionLocal() as db:
        try:
            # The scheduled job's gate. In-process, so it fences this run's own
            # process only; the container's job runs in another (docs/company-financials.md).
            with single_flight(EDGAR_GATE):
                summary = await CompanyFinancialsSync(db).refresh(
                    dry_run=dry_run,
                    force=force,
                    symbols=symbols,
                )
        except Exception as e:
            await db.rollback()
            # Best-effort bookkeeping, as the other CLIs: it must never mask the error.
            await SyncRunRepository(db).record(
                sync_type=MANUAL_SYNC_TYPE,
                status="error",
                message=str(e),
                started_at=started_at,
            )
            print(f"FAILED: {e}", file=sys.stderr)
            return 1

        if symbols and not summary.get("in_scope"):
            print(
                f"None of {', '.join(symbols)} is a held, US-listed security.",
                file=sys.stderr,
            )
            return 1
        for row in summary.get("outcomes", []):
            print(
                f"CIK {row['cik']:<10} {','.join(row['symbols']):<14} {row['outcome']}"
            )
        for warning in summary.get("warnings", []):
            print(f"WARNING: {warning}")
        print(summary["message"])
        if dry_run:
            # A dry run is not an event: no sync_run, exactly as the other CLIs.
            print("DRY RUN - nothing written, no requests made.")
            return 0

        await SyncRunRepository(db).record(
            sync_type=MANUAL_SYNC_TYPE,
            status=summary["status"],
            message=summary["message"],
            details={k: v for k, v in summary.items() if k != "warnings"},
            warnings=summary.get("warnings"),
            started_at=started_at,
        )
        return 0


def main() -> int:
    configure_logging(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO; the per-company lines below are the record.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what is due without a request or a write",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Fetch every company in scope, ignoring staleness and back-off",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        help="Only this held symbol; repeatable",
    )
    args = parser.parse_args()
    return asyncio.run(
        fetch_company_financials(
            dry_run=args.dry_run,
            force=args.force,
            symbols=args.symbol,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
