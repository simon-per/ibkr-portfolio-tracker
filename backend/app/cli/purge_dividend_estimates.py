"""
Drop one security's yfinance-derived dividend rows, so a corrected ticker mapping
can rebuild them.

`dividend_payments` rows tagged `yfinance_estimate` are keyed to whatever Yahoo
ticker was resolved when they were written. When that mapping turns out to have
been wrong, they are poison — and unlike prices, nothing invalidated them:
`manage_mappings disable --purge-prices` clears `market_prices` only, and
`prune_empty_dividends` deletes only rows carrying no income, so a poisoned row
with a plausible positive amount was unreachable by any tool.

That is the SBI case. `SBI@TSE` is SERABI GOLD PLC (CAD, Toronto), but for months
it resolved to an unrelated US listing via the bare-symbol fallback. The mapping
was corrected to `SBI.TO` and the prices were purged and refetched — the two
dividend rows written under the wrong ticker were not. They survived, and because
`_forecast_inputs()` derives cadence from whichever series carries
`amount_per_share` (and skips the IBKR rows when one exists), those two rows alone
defined the schedule: a 31-day gap read as monthly, projecting five payouts a
small gold miner does not pay, while its one real IBKR payment was discarded.

Income was unaffected throughout — the era splice drops estimates after the first
IBKR payment — which is exactly why it went unnoticed. Expect this purge to change
no income figure and to remove a forecast.

IBKR rows are **never** deleted: they come from the Flex cash-transaction ledger,
carry real withholding, and no ticker mapping can invalidate them.

Sixth CLI in the same pattern as `ingest_flex_xml`, `import_prices`,
`manage_mappings`, `manage_cash_flows` and `prune_empty_dividends`, for the same
reason: `/api/` is proxied publicly and unauthenticated, so a route that deletes
financial history is a far larger surface than a command over ssh. Touches no
network — a later scheduled sync refetches through the corrected mapping.

    python -m app.cli.purge_dividend_estimates SBI TSE --dry-run
    python -m app.cli.purge_dividend_estimates SBI TSE
    python -m app.cli.purge_dividend_estimates --security-id 33
"""
import argparse
import asyncio
import logging
import sys
from app.clock import utcnow
from typing import Optional

from app.database import AsyncSessionLocal
from app.logging_setup import configure_logging
from app.models.security import Security
from app.cli.security_lookup import resolve_security
from app.repositories.dividend_repository import YFINANCE_SOURCE, DividendRepository
from app.repositories.sync_run_repository import SyncRunRepository

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_dividend_purge"


class PurgeError(Exception):
    """Bad arguments or an unresolvable security — nothing was written."""


async def _resolve_security(
    db, symbol: Optional[str], exchange: Optional[str], security_id: Optional[int]
) -> Security:
    """The shared CLI rules (`security_lookup`): explicit id wins, ambiguity refuses."""
    if not security_id and not symbol:
        raise PurgeError("Provide SYMBOL [EXCHANGE], or --security-id")
    return await resolve_security(db, symbol, exchange, security_id, PurgeError)


async def purge(
    symbol: Optional[str] = None,
    exchange: Optional[str] = None,
    security_id: Optional[int] = None,
    dry_run: bool = False,
) -> int:
    started_at = utcnow()

    async with AsyncSessionLocal() as db:
        try:
            security = await _resolve_security(db, symbol, exchange, security_id)
            repo = DividendRepository(db)

            estimates = await repo.get_estimates_for_security(security.id)
            all_rows = await repo.get_by_security(security.id)
            kept = [p for p in all_rows if p.source != YFINANCE_SOURCE]

            label = f"{security.symbol}@{security.exchange} (id={security.id})"
            if not estimates:
                print(f"Nothing to purge for {label}: no yfinance_estimate rows "
                      f"({len(kept)} IBKR row(s) present, untouched).")
                return 0

            print(f"{label}: {len(estimates)} yfinance_estimate row(s) to delete, "
                  f"{len(kept)} IBKR row(s) kept.")
            print(f"  {'ex_date':<12} {'per share':>12} {'shares':>10} {'gross_eur':>12}")
            for p in estimates:
                per_share = "—" if p.amount_per_share is None else f"{p.amount_per_share:.6f}"
                shares = "—" if p.shares_held is None else f"{p.shares_held:g}"
                gross = "—" if p.gross_amount_eur is None else f"{p.gross_amount_eur:.6f}"
                print(f"  {str(p.ex_date):<12} {per_share:>12} {shares:>10} {gross:>12}")
            if kept:
                print("  keeping (authoritative, real withholding):")
                for p in kept:
                    print(f"    {p.ex_date}  source={p.source}")

            if dry_run:
                print("\nDRY RUN - nothing deleted.")
                return 0

            deleted = await repo.delete_estimates_for_security(security.id)
            await db.commit()

            print(f"\nDeleted {deleted} estimate row(s) for {label}. A scheduled "
                  f"dividend sync will refetch through the current mapping; until it "
                  f"does, a forecast needing two per-share samples correctly projects "
                  f"nothing.")

        except PurgeError as e:
            print(f"REFUSED - nothing was deleted: {e}", file=sys.stderr)
            return 2
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
            message=(f"Purged {deleted} yfinance dividend estimate(s) for "
                     f"{security.symbol}@{security.exchange}"),
            details={"security_id": security.id, "symbol": security.symbol,
                     "exchange": security.exchange, "deleted": deleted,
                     "ibkr_rows_kept": len(kept)},
            started_at=started_at,
        )
        return 0


def main() -> int:
    configure_logging(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("symbol", nargs="?", help="IBKR symbol, e.g. SBI")
    parser.add_argument("exchange", nargs="?", help="IBKR exchange, e.g. TSE")
    parser.add_argument(
        "--security-id", type=int, default=None,
        help="Resolve by id instead — required when a symbol is dual-listed",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be deleted, delete nothing"
    )
    args = parser.parse_args()
    return asyncio.run(purge(
        symbol=args.symbol, exchange=args.exchange,
        security_id=args.security_id, dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
