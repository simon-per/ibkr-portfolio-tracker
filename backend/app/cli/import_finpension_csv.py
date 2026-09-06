"""
Ingest a finpension transaction export (Swiss Pillar 3a) into the portfolio.

    docker cp report.csv backend-portfolio-backend-1:/tmp/report.csv
    docker exec backend-portfolio-backend-1 python -m app.cli.import_finpension_csv /tmp/report.csv --dry-run
    docker exec backend-portfolio-backend-1 python -m app.cli.import_finpension_csv /tmp/report.csv

Touches **neither** rule-bound provider: no Flex request, so it cannot spend the
token's budget or contribute to a `Code=1025` lockout, and no Yahoo call. It is
therefore safe at any hour, including *during* a lockout.

It may fetch a CHF/EUR rate from Frankfurter for a date not already cached, which
is neither rate-limited nor token-bound. A date it cannot convert has its row
skipped and named in `warnings[]` rather than being stored unconverted -- the
third site of the `_to_eur` rule, after the tax and dividend copies.

**A CLI rather than a route, deliberately.** `/api/` is proxied publicly, and this
rewrites tax lots — the same judgement that keeps `ingest_flex_xml` and
`import_prices` off the API surface. Write auth narrows that surface but does not
change the reasoning.

The file is applied whole or not at all, twice over: the parser refuses anything it
cannot book, and the ingest refuses a file that looks like a truncation of what is
already stored. `--dry-run` reports what would happen and records nothing, because a
dry run is not an event.

Log in `sync_runs` as ``pillar3a_csv``.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.accounts import PILLAR3A
from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.currency_service import CurrencyService
from app.services.finpension_ingest import ingest_finpension_report
from app.services.finpension_report import (
    DEPOSIT,
    FinpensionParseError,
    parse_transaction_report,
)

logger = logging.getLogger(__name__)

SYNC_TYPE = "pillar3a_csv"


def _print_warnings(warnings) -> None:
    if not warnings:
        return
    print("\nWarnings:")
    for warning in warnings:
        print(f"  ! {warning}")


async def ingest(path: Path, account: str, dry_run: bool, force: bool) -> int:
    started_at = utcnow()

    # Parse before opening a session: a malformed file is not a database event, and a
    # dry run must be able to report on it without one.
    try:
        report = parse_transaction_report(path.read_text(encoding="utf-8-sig"))
    except FinpensionParseError as e:
        print(f"REFUSED - nothing was read: {e}", file=sys.stderr)
        return 1

    deposits = sum(r.cash_flow for r in report.rows if r.kind == DEPOSIT)
    print(f"Parsed {len(report.rows)} rows for account {account!r}")
    print(f"  period                   {report.first_date} .. {report.last_date}")
    print(f"  securities               {len(report.assets)}")
    print(f"  deposits (money in)      {deposits} CHF")
    print(f"  closing balance          {report.final_balance} CHF")

    if dry_run:
        _print_warnings(report.warnings)
        print("\nDry run - nothing was written.")
        return 0

    async with AsyncSessionLocal() as db:
        try:
            result = await ingest_finpension_report(
                db, report, CurrencyService(db), account=account, force=force
            )
            # The money-in basis moved, and `benchmark_timeline_cache` is only sound
            # given a fixed basis: the comparison line buys the *same* contribution
            # legs the chart draws, so a cached point computed before this account
            # existed is now measuring a different portfolio. `ingest_flex_statement`
            # clears it on every Flex sync for exactly this reason.
            from app.services.benchmark_service import BenchmarkService
            await BenchmarkService(db).clear_cache()
            await db.commit()
        except Exception as e:
            await db.rollback()
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e),
                started_at=started_at,
            )
            print(f"FAILED - nothing was written: {e}", file=sys.stderr)
            return 1

        warnings = result.pop("warnings", [])
        print("\nIngested successfully:")
        for key, value in result.items():
            print(f"  {key:24} {value}")
        _print_warnings(warnings)

        await SyncRunRepository(db).record(
            sync_type=SYNC_TYPE, status="success",
            message=f"Ingested {path.name} into account {account!r}",
            details=result, warnings=warnings or None, started_at=started_at,
        )
        return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", type=Path,
                        help="finpension transaction report, exported as CSV")
    parser.add_argument("--account", default=PILLAR3A,
                        help=f"Account label for these rows (default: {PILLAR3A}). "
                             f"Give a second 3a portfolio its own label.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report without writing to the database")
    parser.add_argument("--force", action="store_true",
                        help="Apply even if the file is shorter than, or ends earlier "
                             "than, the stored ledger. Only for a deliberate rollback.")
    args = parser.parse_args()

    if not args.csv_path.is_file():
        print(f"No such file: {args.csv_path}", file=sys.stderr)
        return 2
    if not args.account or len(args.account) > 16:
        print("--account must be 1-16 characters", file=sys.stderr)
        return 2

    return asyncio.run(
        ingest(args.csv_path, args.account, args.dry_run, args.force)
    )


if __name__ == "__main__":
    raise SystemExit(main())
