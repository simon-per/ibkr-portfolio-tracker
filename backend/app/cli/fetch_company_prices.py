"""
Fetch the Company tab's weekly closes from Yahoo now, instead of waiting for Sunday's pass
(docs/company-financials.md, *Prices*). The first fill, and a re-fetch after a change.

The same `CompanyPriceSync.refresh` the weekly job runs: the companies the tab lists, one
history request each with 2-5 s between them, the back-off, and a 429 abandoning the pass.
`--force` fetches every company regardless of staleness and back-off; `--symbol NVDA`
(repeatable) narrows the scope. `--dry-run` lists what is due and asks nothing.

**Yahoo** (CLAUDE.md rule 1): about one request per company on a first fill, one more for
a company whose basis moved. Never run it in a loop, and not within the hold after a 429.

Usage, inside the container:

    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_prices --dry-run
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_company_prices
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
from app.services.company_price_sync import MANUAL_SYNC_TYPE, CompanyPriceSync
from app.single_flight import SYNC_PIPELINE, single_flight


async def fetch_company_prices(
    dry_run: bool = False, force: bool = False, symbols: Optional[List[str]] = None
) -> int:
    started_at = utcnow()
    async with AsyncSessionLocal() as db:
        try:
            # Every Yahoo job's gate. In-process, so it fences this run's own process
            # only; the container's jobs run in another.
            with single_flight(SYNC_PIPELINE):
                summary = await CompanyPriceSync(db).refresh(
                    dry_run=dry_run, force=force, symbols=symbols
                )
        except Exception as e:
            await db.rollback()
            await SyncRunRepository(db).record(
                sync_type=MANUAL_SYNC_TYPE,
                status="error",
                message=str(e),
                started_at=started_at,
            )
            print(f"FAILED: {e}", file=sys.stderr)
            return 1

        if symbols and not summary["in_scope"]:
            print(
                f"None of {', '.join(symbols)} is a company the Company tab lists.",
                file=sys.stderr,
            )
            return 1
        for warning in summary.get("warnings", []):
            print(f"WARNING: {warning}")
        print(summary["message"])
        if dry_run:
            print("Due:", ", ".join(summary.get("due", [])) or "none")
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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List what is due without a request"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Fetch every company, ignoring staleness and back-off",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        help="Only this ticker; repeatable",
    )
    args = parser.parse_args()
    return asyncio.run(
        fetch_company_prices(
            dry_run=args.dry_run, force=args.force, symbols=args.symbol
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
