"""
Parse one fund's downloaded holdings file and store it as that fund's basket. No network.

The offline half of the pair: `app/cli/fetch_etf_baskets.py` writes response bodies,
this parses and stores them. It works just as well on a file downloaded by hand from an
issuer's website, which is the only route for **VWCE** — Vanguard Europe publishes complete
holdings on request by email, not on the web — and the fallback for any adapter whose route
breaks.

Touches **no** network at all, so neither rule at the top of CLAUDE.md applies. Re-running is
safe and idempotent: a basket is replaced wholesale, and an incoming file with the same as-of
date simply rewrites the same rows.

**It refuses rather than half-applying.** A partially-parsed basket is indistinguishable
afterwards from a complete one — the rule `import_prices.py` follows — and on top of that
`EtfBasketRepository.replace_basket` refuses a basket that collapses in row count or moves its
as-of date backwards, because those replace a real basket with a *plausible* one and every
look-through figure then shrinks with nothing reporting it.

Usage, inside the container:

    docker exec backend-portfolio-backend-1 \\
        python -m app.cli.import_etf_basket IE00BMFKG444 /tmp/baskets/IE00BMFKG444.dws.csv --dry-run
    docker exec backend-portfolio-backend-1 \\
        python -m app.cli.import_etf_basket IE00BMFKG444 /tmp/baskets/IE00BMFKG444.dws.csv

Paginated sources take their pages in order, oldest argument first:

    python -m app.cli.import_etf_basket US9220427424 /tmp/baskets/US9220427424.vanguard_us.*.json
"""
import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.etf_mappings import is_known_etf_isin, symbol_for_fund_isin
from app.etf_sources import source_for_fund_isin
from app.repositories.etf_basket_repository import (
    BasketReplaceRefused,
    EtfBasketRepository,
)
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.etf_basket_parsers import (
    PARSERS,
    BasketParseError,
    parse_defiance,
    parse_dws,
    parse_first_trust,
    parse_invesco,
    parse_ishares,
    parse_vaneck,
    parse_vanguard_us,
)

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_etf_basket"


def _fetched_on(paths: List[Path]) -> date:
    """
    When the file was downloaded, used as the as-of only for issuers that publish none.

    The file's own mtime rather than today: importing a file saved three days ago must not
    claim the basket is current, since a stood-in date already errs toward looking fresher
    than the data is.
    """
    return min(date.fromtimestamp(p.stat().st_mtime) for p in paths)


def parse_file(fund_isin: str, paths: List[Path], adapter: str, symbol: str):
    """
    Dispatch to the adapter's parser. Pure apart from reading the files.

    `symbol` reaches the two HTML adapters because their routes are keyed by ticker in a query
    string or a path segment, so a redirect can serve another fund's holdings under a 200 —
    they check it against the page's `<title>`, which is their equivalent of the
    `ShareClass ISIN` echo `parse_dws` checks on every row.
    """
    bodies = [p.read_bytes() for p in paths]
    single = len(bodies) == 1

    if adapter == "vanguard_us":
        return parse_vanguard_us(bodies, fund_isin)
    if not single:
        raise BasketParseError(
            f"{adapter} responses are a single file; {len(bodies)} were given"
        )
    if adapter == "dws":
        return parse_dws(bodies[0], fund_isin, _fetched_on(paths))
    if adapter == "blackrock":
        return parse_ishares(bodies[0], fund_isin)
    if adapter == "invesco":
        return parse_invesco(bodies[0], fund_isin)
    if adapter == "first_trust":
        return parse_first_trust(bodies[0], fund_isin, symbol)
    if adapter == "defiance":
        return parse_defiance(bodies[0], fund_isin, symbol, _fetched_on(paths))
    if adapter == "vaneck":
        return parse_vaneck(bodies[0], fund_isin)
    raise BasketParseError(
        f"no parser for adapter {adapter!r}. Known: {', '.join(sorted(PARSERS))} — pass "
        f"--adapter to override what app/etf_sources.py declares"
    )


async def import_basket(
    fund_isin: str,
    paths: List[Path],
    adapter: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    started_at = utcnow()
    fund_isin = fund_isin.strip().upper()

    source = source_for_fund_isin(fund_isin)
    if not is_known_etf_isin(fund_isin):
        print(
            f"FAILED: {fund_isin} is not a fund in app/etf_mappings.py. Add it there first — "
            f"otherwise the holding is treated as an ordinary company and this basket would "
            f"never be read.",
            file=sys.stderr,
        )
        return 1
    if source is not None and not source.look_through_eligible and not force:
        print(
            f"FAILED: {source.symbol} is excluded from look-through and its basket would "
            f"never be read.\n  Reason: {source.exclude_reason}\n"
            f"Pass --force to store it anyway.",
            file=sys.stderr,
        )
        return 1

    adapter = adapter or (source.adapter if source else None) or "manual"
    symbol = symbol_for_fund_isin(fund_isin) or fund_isin

    try:
        basket = parse_file(fund_isin, paths, adapter, symbol)
    except (BasketParseError, OSError) as e:
        print(f"FAILED - nothing was written: {e}", file=sys.stderr)
        return 1

    print(f"Fund:    {symbol} ({fund_isin}) via {adapter}")
    # Two adapters reach this branch for different reasons — Xtrackers publishes no date at
    # all, Defiance publishes one in the future — so the note says the date is ours rather
    # than guessing which.
    print(f"As of:   {basket.as_of_date}"
          + ("" if basket.as_of_is_issuer_stated
             else "  (not the issuer's own; this is the file's date, so the basket may be "
                  "older)"))
    print(f"Rows:    {len(basket.rows)} parsed from {basket.source_rows} source row(s)")
    print(f"Weights: {basket.total_weight_pct:.4f}% total, "
          f"{basket.equity_weight_pct:.4f}% in securities"
          + ("" if basket.asset_class_available
             else "  (no asset-class column, so every row counts)"))
    print(f"ISINs:   {basket.identifier_coverage_pct:.2f}% of that weight carries one")
    top = sorted(basket.rows, key=lambda r: -r.weight_pct)[:5]
    for row in top:
        print(f"  {row.weight_pct:>8.4f}%  {row.isin or row.ticker or '(no id)':14} {row.name[:40]}")

    if dry_run:
        # Returns before any write and records no sync_run, as import_prices and
        # manage_mappings do — a dry run is not an event.
        print("DRY RUN - nothing written.")
        return 0

    async with AsyncSessionLocal() as db:
        try:
            result = await EtfBasketRepository(db).replace_basket(basket, force=force)
            await db.commit()
        except BasketReplaceRefused as e:
            await db.rollback()
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"REFUSED - the previous basket is untouched: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            await db.rollback()
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED - nothing was written: {e}", file=sys.stderr)
            return 1

        print(
            f"Stored {result.stored_rows} constituent(s)"
            + (f", replacing {result.previous_rows} from {result.previous_as_of}"
               if result.previous_rows else "")
            + "."
        )
        await SyncRunRepository(db).record(
            sync_type=SYNC_TYPE,
            status="success",
            message=(
                f"Imported {result.stored_rows} constituent(s) for {symbol} "
                f"({fund_isin}) as of {basket.as_of_date} via {adapter}"
            ),
            details={
                "fund_isin": fund_isin,
                "symbol": symbol,
                "adapter": adapter,
                "as_of_date": str(basket.as_of_date),
                "as_of_is_issuer_stated": basket.as_of_is_issuer_stated,
                "stored_rows": result.stored_rows,
                "previous_rows": result.previous_rows,
                "source_rows": basket.source_rows,
                "total_weight_pct": float(basket.total_weight_pct),
                "equity_weight_pct": float(basket.equity_weight_pct),
                "identifier_coverage_pct": float(basket.identifier_coverage_pct),
                "asset_class_available": basket.asset_class_available,
                "files": [p.name for p in paths],
            },
            started_at=started_at,
        )
        return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("fund_isin", help="ISIN of the fund the file describes")
    parser.add_argument("paths", nargs="+", type=Path,
                        help="Holdings file(s), in page order for paginated sources")
    parser.add_argument("--adapter", default=None,
                        help="Override the adapter app/etf_sources.py declares")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report without writing to the database")
    parser.add_argument("--force", action="store_true",
                        help="Override the row-collapse and backwards-as-of refusals")
    args = parser.parse_args()

    missing = [p for p in args.paths if not p.is_file()]
    if missing:
        print(f"No such file(s): {', '.join(str(p) for p in missing)}", file=sys.stderr)
        return 2

    return asyncio.run(import_basket(
        args.fund_isin, args.paths, adapter=args.adapter,
        dry_run=args.dry_run, force=args.force,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
