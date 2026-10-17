"""
Fetch holding logos from logo.dev now, instead of waiting for the 05:00 pass (docs/logos.md).

The same `LogoService.refresh` the scheduled step runs, with the same bounds: only ISINs with
no row or a row older than 90 days, at most 30 lookups, stop on 401/402/403/429. A manual
logo (`app/cli/import_logo.py`) is never asked about, `--force` included.

`--dry-run` lists what is due and makes **no request** — every found logo is billed, so a
dry run that asked would pay twice for the same answer. The real run prints each ISIN's
outcome, which is the measurement (did the ETFs resolve to their issuer's mark?).

Touches no Yahoo and no IBKR. Needs `LOGO_DEV_SECRET_KEY` for anything but `--dry-run`.

Usage, inside the container:

    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos --dry-run
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos --isin US02079K3059 --force
"""
import argparse
import asyncio
import logging
import sys
from typing import Optional

from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.logo_service import LogoService, is_configured

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_logo_fetch"


async def fetch_logos(dry_run: bool = False, force: bool = False,
                      isin: Optional[str] = None) -> int:
    started_at = utcnow()
    if not dry_run and not is_configured():
        print("LOGO_DEV_SECRET_KEY is not set in backend/.env - nothing to do.", file=sys.stderr)
        return 1

    async with AsyncSessionLocal() as db:
        try:
            summary = await LogoService(db).refresh(dry_run=dry_run, force=force, isin=isin)
        except Exception as e:
            await db.rollback()
            # Best-effort bookkeeping, as ingest_flex_xml does: it must never mask the error.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED: {e}", file=sys.stderr)
            return 1

        if isin and not summary.get("held"):
            print(f"No security carries ISIN {isin}.", file=sys.stderr)
            return 1
        for row in summary.get("outcomes", []):
            print(f"{row['symbol']:<10} {row['isin']}  {row['outcome']}")
        for warning in summary.get("warnings", []):
            print(f"WARNING: {warning}")
        print(summary["message"])
        if dry_run:
            # A dry run is not an event: no sync_run, exactly as the other CLIs.
            if not summary.get("configured"):
                print("LOGO_DEV_SECRET_KEY is not set: a real run would do nothing.")
            print("DRY RUN - nothing written, no requests made.")
            return 0

        await SyncRunRepository(db).record(
            sync_type=SYNC_TYPE, status=summary["status"], message=summary["message"],
            details={k: v for k, v in summary.items() if k != "warnings"},
            warnings=summary.get("warnings"), started_at=started_at,
        )
        return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="List what is due without a request or a write")
    parser.add_argument("--force", action="store_true",
                        help="Ask again even when the ISIN was checked within 90 days "
                             "(never a manual logo)")
    parser.add_argument("--isin", default=None,
                        help="Only this ISIN (any security carrying it, held or not)")
    args = parser.parse_args()
    return asyncio.run(fetch_logos(dry_run=args.dry_run, force=args.force, isin=args.isin))


if __name__ == "__main__":
    raise SystemExit(main())
