"""
Replay the dividend forecast for the days before it was recorded.

`dividend_forecast_snapshots` has one row a day from 2026-10-07, when the recorder was
added; before that the *Next 12 months* figure was recomputed on every read and kept
nowhere. This fills the earlier days by asking the forecast what it would say on each
one (`DividendService.forecast_figures(day, replay=True)`): the share counts, exchange
rates and payments of that day, under today's rules.

- **A reconstruction, stored as one.** Every row written here carries
  `reconstructed = true` and the history endpoint passes the flag on. It is not what
  the application showed that day — the rules have changed since, and a dividend IBKR
  had announced by then is inferred from the cadence, because accruals were never kept
  per day.
- **Never over a recorded day.** It stops the day before the first recorded snapshot,
  and the repository refuses to replace a recorded row regardless. Re-running it
  replaces its own earlier rows, which is the point after a change to the forecast.
- **It measures itself.** The first recorded days are replayed too and printed beside
  what was recorded: that gap is how far a replay sits from the real thing.
- **Whole or nothing.** Every day is computed before the first is written, in one
  transaction.
- **Offline.** No Yahoo, no Flex: it reads what the forecast reads.

    python -m app.cli.backfill_dividend_forecast --from 2026-06-01 --dry-run
    python -m app.cli.backfill_dividend_forecast --from 2026-06-01
"""
import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List

from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.repositories.dividend_forecast_snapshot_repository import (
    DividendForecastSnapshotRepository,
)
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.dividend_service import DividendService

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_dividend_forecast_backfill"
# How many recorded days are replayed for the comparison. Enough to see whether the
# gap is steady; replaying every recorded day would grow with the table for ever.
OVERLAP_DAYS = 7


class BackfillError(Exception):
    """Nothing to replay for the range asked — nothing was written."""


async def backfill(db, start: date, dry_run: bool = False) -> Dict:
    """
    Replay ``start`` up to the day before the first recorded snapshot and store the
    result (unless ``dry_run``). The caller commits.

    Returns ``{"rows": [...], "skipped": n, "overlap": [...], "through": date}``,
    where ``overlap`` pairs a replay with the figure recorded on the same day.
    """
    repo = DividendForecastSnapshotRepository(db)
    recorded = [r for r in await repo.get_all() if not r.reconstructed]
    through = (
        recorded[0].snapshot_date if recorded else utcnow().date()
    ) - timedelta(days=1)
    if start > through:
        raise BackfillError(
            f"--from {start} is after {through}, the last day without a recorded snapshot"
        )

    service = DividendService(db)
    rows: List[Dict] = []
    skipped = 0
    day = start
    while day <= through:
        figures = await service.forecast_figures(day, replay=True)
        if figures is None:
            skipped += 1    # nothing to forecast from that day: no row, never a zero
        else:
            rows.append(figures)
        day += timedelta(days=1)

    overlap: List[Dict] = []
    for r in recorded[:OVERLAP_DAYS]:
        figures = await service.forecast_figures(r.snapshot_date, replay=True)
        overlap.append({
            "date": r.snapshot_date,
            "recorded": r.next_12m_eur,
            "replayed": figures["next_12m_eur"] if figures else None,
        })

    if not dry_run:
        for figures in rows:
            await repo.upsert_reconstructed(figures)
    return {"rows": rows, "skipped": skipped, "overlap": overlap, "through": through}


def _gap_pct(recorded: Decimal, replayed) -> str:
    if replayed is None or not recorded:
        return "—"
    return f"{float((replayed - recorded) / recorded * 100):+.1f}%"


def _print_report(result: Dict) -> None:
    rows = result["rows"]
    print(f"Replayed {len(rows)} day(s), {rows[0]['snapshot_date']} .. "
          f"{rows[-1]['snapshot_date']}" if rows else "Replayed 0 days")
    if result["skipped"]:
        print(f"  {result['skipped']} day(s) had nothing to forecast from and get no row")
    print(f"  {'date':<12} {'next 12m EUR':>13} {'TTM net EUR':>12}")
    for i, r in enumerate(rows):
        # The first of each month and the two ends: enough to read the shape.
        if i in (0, len(rows) - 1) or r["snapshot_date"].day in (1, 15):
            print(f"  {str(r['snapshot_date']):<12} {r['next_12m_eur']:>13.2f} "
                  f"{r['ttm_net_eur']:>12.2f}")
    if result["overlap"]:
        print("\nReplay against what was recorded on the same day (next 12m, EUR):")
        print(f"  {'date':<12} {'recorded':>10} {'replayed':>10} {'gap':>7}")
        for o in result["overlap"]:
            replayed = "—" if o["replayed"] is None else f"{o['replayed']:.2f}"
            print(f"  {str(o['date']):<12} {o['recorded']:>10.2f} {replayed:>10} "
                  f"{_gap_pct(o['recorded'], o['replayed']):>7}")


async def run(start: date, dry_run: bool) -> int:
    started_at = utcnow()
    async with AsyncSessionLocal() as db:
        try:
            result = await backfill(db, start, dry_run=dry_run)
            _print_report(result)
            if dry_run:
                print("\nDRY RUN - nothing was written.")
                return 0
            await db.commit()
        except BackfillError as e:
            print(f"REFUSED - nothing was written: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            await db.rollback()
            # Best-effort bookkeeping, as in the other CLIs: never mask the error.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED - nothing was written: {e}", file=sys.stderr)
            return 1

        rows = result["rows"]
        await SyncRunRepository(db).record(
            sync_type=SYNC_TYPE,
            status="success",
            message=f"Replayed the dividend forecast for {len(rows)} day(s)",
            details={
                "from": start.isoformat(),
                "through": result["through"].isoformat(),
                "rows": len(rows),
                "skipped": result["skipped"],
            },
            started_at=started_at,
        )
        print(f"\nStored {len(rows)} reconstructed snapshot(s).")
        return 0


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True,
                        help="First day to replay, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and print, write nothing")
    args = parser.parse_args()
    return asyncio.run(run(args.start, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
