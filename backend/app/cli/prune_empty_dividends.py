"""
Delete dividend rows that can never represent income.

yfinance returns a security's ENTIRE dividend history — decades of it — so the
dividend sync stored an ex-date for every payout the company ever made, including
the years before this portfolio owned a share. `compute_dividend_income` then wrote
a zero row for each (no shares held at that ex-date) purely to mark it processed.
On this account that was 1355 of 1446 rows, reaching back to 1985-08.

They are invisible in the UI — both `get_dividend_summary` and
`get_dividend_breakdown` filter them through `DividendService._is_income()` — but
they are not harmless: they made the dividend card report 439 months, and when one
of those read-side filters was relaxed they broke `/api/dividends/breakdown`
outright. `sync_dividend_data` no longer ingests them, so pruning is permanent
rather than a treadmill.

A row is pruned only when it carries no income **and** falls outside the window
`sync_dividend_data` would still ingest (`PRE_OWNERSHIP_HISTORY_YEARS` before the
security's first lot). The income test alone is not sufficient and treating it as
such was a bug: the forecast infers cadence from the RAW history, so an
income-free pre-ownership row with an `amount_per_share` is exactly what lets a
recently-bought payer project at all. Deleting those silently reverts that fix.
Rows still awaiting computation (`shares_held IS NULL`) are always kept: they have
no amounts *yet*, and deleting them would silently drop real income.

Fifth CLI in the same pattern as `ingest_flex_xml`, `import_prices`,
`manage_mappings` and `manage_cash_flows`, for the same reason: `/api/` is proxied
publicly and unauthenticated, so a route that deletes financial history is a far
larger surface than a command over ssh. Touches no network.

    python -m app.cli.prune_empty_dividends --dry-run
    python -m app.cli.prune_empty_dividends
"""
import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta
from app.clock import utcnow
from typing import Optional

from sqlalchemy import delete, func, select

from app.database import AsyncSessionLocal
from app.logging_setup import configure_logging
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.dividend_service import PRE_OWNERSHIP_HISTORY_YEARS, DividendService

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_dividend_prune"


def _is_empty(p: DividendPayment, keep_from: Optional[date] = None) -> bool:
    """
    Prunable when the row carries no income **and** the current ingest window would no
    longer create it.

    The income half alone is not enough, and assuming it was is a bug this predicate
    shipped with. `_forecast_inputs()` is built from the RAW history precisely because a
    payout from before we owned the share still evidences the schedule and still sizes
    the next one — keying on realized income is what left TSMC, Samsung, SK Hynix, HPE
    and SOXQ forecasting nothing. So a pre-ownership yfinance row is *income-free and
    load-bearing*: the forecast is a reader, and it does not ignore these.

    `sync_dividend_data()` already draws that line, keeping `PRE_OWNERSHIP_HISTORY_YEARS`
    of history before the first lot. Reusing it here means prune deletes exactly what
    ingest would no longer write — one policy rather than two free to drift — so the
    decades of pre-portfolio bookkeeping it was built for still go, and the three years
    deliberately retained for cadence stay.

    `keep_from is None` means the security has no lots at all, which is the one case
    ingest itself refuses to guess a cutoff for; nothing is pruned there either.
    """
    if p.shares_held is None:
        return False                                  # awaiting computation, not empty
    if DividendService._is_income(p):
        return False                                  # real money
    if keep_from is None:
        return False                                  # no cutoff inferable, as at ingest
    return p.ex_date is not None and p.ex_date < keep_from


async def prune(dry_run: bool) -> int:
    started_at = utcnow()

    async with AsyncSessionLocal() as db:
        try:
            total = (await db.execute(
                select(func.count(DividendPayment.id))
            )).scalar() or 0
            rows = list((await db.execute(
                select(DividendPayment, Security)
                .join(Security, Security.id == DividendPayment.security_id)
                .order_by(DividendPayment.ex_date.asc())
            )).all())

            # Same cutoff the ingest applies, per security: first lot minus the
            # deliberately-retained pre-ownership history.
            first_lot = await DividendService(db)._first_lot_dates()
            lookback = timedelta(days=365 * PRE_OWNERSHIP_HISTORY_YEARS)
            keep_from = {sid: d - lookback for sid, d in first_lot.items()}

            empty = [(p, s) for p, s in rows if _is_empty(p, keep_from.get(p.security_id))]
            pending = sum(1 for p, _ in rows if p.shares_held is None)
            # Income-free rows that survive because the forecast infers cadence from them.
            cadence_kept = sum(
                1 for p, _ in rows
                if p.shares_held is not None
                and not DividendService._is_income(p)
                and not _is_empty(p, keep_from.get(p.security_id))
            )

            if not empty:
                print(f"Nothing to prune: {total} row(s), none prunable "
                      f"({pending} awaiting computation, "
                      f"{cadence_kept} income-free but inside the forecast window).")
                return 0

            per_symbol: dict = {}
            for p, s in empty:
                per_symbol.setdefault(s.symbol, []).append(p.ex_date)

            first = min(d for dates in per_symbol.values() for d in dates)
            last = max(d for dates in per_symbol.values() for d in dates)
            print(
                f"{len(empty)} of {total} row(s) predate the forecast window and carry no "
                f"income ({first} .. {last}); {pending} awaiting computation and "
                f"{cadence_kept} income-free cadence row(s) will be kept."
            )
            for symbol in sorted(per_symbol, key=lambda k: -len(per_symbol[k]))[:12]:
                dates = sorted(per_symbol[symbol])
                print(f"  {symbol:<10} {len(dates):>5}   {dates[0]} .. {dates[-1]}")
            if len(per_symbol) > 12:
                print(f"  ... and {len(per_symbol) - 12} more securities")

            if dry_run:
                print("\nDRY RUN - nothing deleted.")
                return 0

            ids = [p.id for p, _ in empty]
            await db.execute(
                delete(DividendPayment).where(DividendPayment.id.in_(ids))
            )
            await db.commit()

            remaining = (await db.execute(
                select(func.count(DividendPayment.id))
            )).scalar() or 0
            print(f"\nDeleted {len(ids)} row(s); {remaining} remain.")

        except Exception as e:
            await db.rollback()
            # Best-effort bookkeeping, as in the other CLIs: never mask the error.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED - nothing was deleted: {e}", file=sys.stderr)
            return 1

        await SyncRunRepository(db).record(
            sync_type=SYNC_TYPE,
            status="success",
            message=f"Pruned {len(ids)} empty dividend row(s)",
            details={"deleted": len(ids), "remaining": remaining,
                     "securities": len(per_symbol)},
            started_at=started_at,
        )
        return 0


def main() -> int:
    configure_logging(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be deleted, delete nothing"
    )
    args = parser.parse_args()
    return asyncio.run(prune(dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
