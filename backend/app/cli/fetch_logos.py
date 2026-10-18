"""
Fetch holding logos from logo.dev now, instead of waiting for the 05:00 pass (docs/logos.md).

The same `LogoService.refresh` the scheduled step runs, with the same bounds: only ISINs with
no row or a row older than 90 days, at most 30 lookups (`--limit`), stop on 401/402/403/429.
A manual logo (`app/cli/import_logo.py`) is never asked about, `--force` included.

`--basket QQQM --basket VT --top 100` fills the constituents of stored fund baskets instead
(the `--top` heaviest of each), so a stock bought later already has its logo. Each found
logo is billed: dry-run first and size `--limit` to the free plan's credits.

`--dry-run` lists what is due and makes **no request** — every found logo is billed, so a
dry run that asked would pay twice for the same answer. The real run prints each ISIN's
outcome, which is the measurement (did the ETFs resolve to their issuer's mark?).

Touches no Yahoo and no IBKR. Needs `LOGO_DEV_SECRET_KEY` for anything but `--dry-run`.

Usage, inside the container:

    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos --dry-run
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos --isin US02079K3059 --force
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos --isin US74762E1029 --isin CH0012221716
    docker exec backend-portfolio-backend-1 python -m app.cli.fetch_logos --basket QQQM --basket VT --top 100 --limit 150
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
from app.services.logo_service import MAX_LOOKUPS_PER_PASS, LogoService, is_configured

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_logo_fetch"


async def fetch_logos(dry_run: bool = False, force: bool = False,
                      isins: Optional[List[str]] = None, baskets: Optional[List[str]] = None,
                      top: Optional[int] = None, limit: int = MAX_LOOKUPS_PER_PASS) -> int:
    started_at = utcnow()
    if not dry_run and not is_configured():
        print("LOGO_DEV_SECRET_KEY is not set in backend/.env - nothing to do.", file=sys.stderr)
        return 1

    async with AsyncSessionLocal() as db:
        try:
            summary = await LogoService(db).refresh(
                dry_run=dry_run, force=force, isins=isins, baskets=baskets, top=top, limit=limit,
            )
        except Exception as e:
            await db.rollback()
            # Best-effort bookkeeping, as ingest_flex_xml does: it must never mask the error.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED: {e}", file=sys.stderr)
            return 1

        if isins and not summary.get("targets"):
            print(f"No security or basket constituent carries {', '.join(isins)}.",
                  file=sys.stderr)
            return 1
        if summary.get("unknown_baskets"):
            print(f"No stored basket for: {', '.join(summary['unknown_baskets'])}",
                  file=sys.stderr)
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
    configure_logging(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and a download URL carries logo.dev's signature.
    # The app quiets the same loggers (`app.main._NOISY_AT_INFO`); the per-ISIN lines below
    # are the run's record.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="List what is due without a request or a write")
    parser.add_argument("--force", action="store_true",
                        help="Ask again even when the ISIN was checked within 90 days "
                             "(never a manual logo)")
    parser.add_argument("--isin", action="append", default=None,
                        help="Only this ISIN — a security carrying it, held or not, or a basket "
                             "constituent; repeatable")
    parser.add_argument("--basket", action="append", default=None, metavar="FUND",
                        help="The constituents of this stored fund basket (fund ISIN or "
                             "symbol); repeatable")
    parser.add_argument("--top", type=int, default=None,
                        help="With --basket: only the N heaviest constituents of each")
    parser.add_argument("--limit", type=int, default=MAX_LOOKUPS_PER_PASS,
                        help=f"At most this many lookups (default {MAX_LOOKUPS_PER_PASS})")
    args = parser.parse_args()
    if args.basket and args.isin:
        parser.error("--basket and --isin are exclusive")
    if args.top is not None and not args.basket:
        parser.error("--top needs --basket")
    return asyncio.run(fetch_logos(
        dry_run=args.dry_run, force=args.force, isins=args.isin, baskets=args.basket,
        top=args.top, limit=args.limit,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
