"""
Import daily closing prices for one security from a local JSON file — no Yahoo call.

This is the price-side twin of `ingest_flex_xml.py`, and exists for the same reason:
when the automatic feed is unavailable or wrong, there has to be a way to put correct
data in without waiting on it. Concretely it covers three situations:

- a poisoned cache was deleted and the position is reading 0.00 until the next
  scheduled job (SBI@TSE, 2026-07-27);
- Yahoo cannot resolve a listing at all, so the security would never get prices;
- the prices are better obtained from IBKR itself, whose daily bars are the same
  source as the statements and are quoted in the listing's own currency.

Touches **no** network — so CLAUDE.md rule 1 (never call Yahoo without permission)
simply does not apply to it. Re-running is safe: `MarketPriceRepository.bulk_create`
upserts on `(security_id, date)`, so an import can be repeated or later overwritten by
a scheduled Yahoo fetch.

There is deliberately no upload endpoint. `/api/` is proxied publicly and
unauthenticated, and a route that rewrites the prices every valuation depends on is a
far larger surface than a CLI run over ssh.

Input file:

    {
      "symbol": "SBI", "exchange": "TSE",     // or "security_id": 33
      "currency": "CAD",
      "source": "ibkr",                        // free text, lands in market_prices.source
      "prices": [{"date": "2026-07-27", "close": 4.79}, ...]
    }

Usage, inside the container:

    docker exec backend-portfolio-backend-1 \
        python -m app.cli.import_prices /tmp/sbi_prices.json --dry-run
    docker exec backend-portfolio-backend-1 \
        python -m app.cli.import_prices /tmp/sbi_prices.json
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from app.clock import utcnow
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.database import AsyncSessionLocal
from app.logging_setup import configure_logging
from app.models.security import Security
from app.cli.security_lookup import resolve_security
from app.repositories.market_price_repository import MarketPriceRepository
from app.repositories.sync_run_repository import SyncRunRepository

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_prices"


class PriceImportError(Exception):
    """A problem with the file or its target — reported, never partially applied."""


def parse_payload(raw: dict) -> Tuple[Dict, List[Dict]]:
    """
    Validate the file and normalise its rows.

    Rejects the whole file rather than skipping bad rows: a partial price import is
    indistinguishable, afterwards, from a complete one, and every consumer of
    `market_prices` treats a present row as authoritative.
    """
    if not isinstance(raw, dict):
        raise PriceImportError("Top level must be a JSON object")

    currency = (raw.get("currency") or "").strip().upper()
    if len(currency) != 3:
        raise PriceImportError(f"'currency' must be a 3-letter code, got {raw.get('currency')!r}")

    prices = raw.get("prices")
    if not isinstance(prices, list) or not prices:
        raise PriceImportError("'prices' must be a non-empty list")

    # Later rows win, so a file that repeats a date is applied deterministically
    # instead of depending on insertion order inside the upsert loop.
    by_date: Dict[date, Decimal] = {}
    for i, row in enumerate(prices):
        if not isinstance(row, dict):
            raise PriceImportError(f"prices[{i}] is not an object")
        try:
            price_date = date.fromisoformat(str(row["date"]))
        except (KeyError, ValueError) as e:
            raise PriceImportError(f"prices[{i}] has no usable 'date' (YYYY-MM-DD): {e}")
        try:
            close = Decimal(str(row["close"]))
        except (KeyError, InvalidOperation) as e:
            raise PriceImportError(f"prices[{i}] has no usable 'close': {e}")
        if close <= 0:
            raise PriceImportError(f"prices[{i}] close must be positive, got {close}")
        by_date[price_date] = close

    target = {
        "security_id": raw.get("security_id"),
        "symbol": raw.get("symbol"),
        "exchange": raw.get("exchange"),
        "currency": currency,
        "source": (raw.get("source") or "manual").strip()[:50],
    }
    rows = [{"date": d, "close_price": c} for d, c in sorted(by_date.items())]
    return target, rows


async def _resolve_security(db, target: Dict, security_id_override: Optional[int]) -> Security:
    security_id = security_id_override or target.get("security_id")
    symbol, exchange = target.get("symbol"), target.get("exchange")
    if not security_id and not symbol:
        raise PriceImportError("Provide 'security_id' (or --security-id), or 'symbol' + 'exchange'")
    return await resolve_security(db, symbol, exchange, security_id, PriceImportError)


async def import_prices(
    path: Path, dry_run: bool = False, security_id: Optional[int] = None
) -> int:
    started_at = utcnow()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        target, rows = parse_payload(raw)
    except (json.JSONDecodeError, PriceImportError) as e:
        print(f"FAILED - nothing was written: {e}", file=sys.stderr)
        return 1

    async with AsyncSessionLocal() as db:
        try:
            security = await _resolve_security(db, target, security_id)

            # The same check the Yahoo auto-discovery path now makes. A price series in
            # the wrong currency is the exact failure this CLI was written to repair:
            # SBI's USD closes were stamped CAD, and because nothing downstream compares
            # the two, the position was carried 61% high for months.
            if security.currency and target["currency"] != security.currency:
                raise PriceImportError(
                    f"File is quoted in {target['currency']} but {security.symbol}"
                    f"@{security.exchange} is a {security.currency} security — refusing "
                    f"the whole file (a mislabelled currency is invisible downstream)"
                )

            first, last = rows[0]["date"], rows[-1]["date"]
            print(
                f"Target: id={security.id} {security.symbol}@{security.exchange} "
                f"({security.currency})"
            )
            print(f"Prices: {len(rows)} rows, {first} .. {last}, source={target['source']!r}")

            if dry_run:
                print("DRY RUN - nothing written.")
                return 0

            price_repo = MarketPriceRepository(db)
            count = await price_repo.bulk_create([
                {
                    "security_id": security.id,
                    "date": row["date"],
                    "close_price": row["close_price"],
                    "currency": target["currency"],
                    "source": target["source"],
                }
                for row in rows
            ])
            await db.commit()
            print(f"Imported {count} price(s).")

        except Exception as e:
            await db.rollback()
            # Best-effort bookkeeping, exactly as ingest_flex_xml does: it must never
            # mask the real error.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED - nothing was written: {e}", file=sys.stderr)
            return 1

        details = {
            "security_id": security.id,
            "symbol": security.symbol,
            "exchange": security.exchange,
            "prices_imported": count,
            "currency": target["currency"],
            "source": target["source"],
            "date_from": str(first),
            "date_to": str(last),
        }
        await SyncRunRepository(db).record(
            sync_type=SYNC_TYPE,
            status="success",
            message=(
                f"Imported {count} price(s) for {security.symbol}@{security.exchange} "
                f"from {path.name}"
            ),
            details=details,
            started_at=started_at,
        )
        return 0


def main() -> int:
    configure_logging(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", type=Path, help="JSON file of daily closes")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Validate and report without writing to the database",
    )
    parser.add_argument(
        "--security-id", type=int, default=None,
        help="Resolve the target by id, overriding symbol/exchange in the file",
    )
    args = parser.parse_args()

    if not args.json_path.is_file():
        print(f"No such file: {args.json_path}", file=sys.stderr)
        return 2

    return asyncio.run(
        import_prices(args.json_path, dry_run=args.dry_run, security_id=args.security_id)
    )


if __name__ == "__main__":
    raise SystemExit(main())
