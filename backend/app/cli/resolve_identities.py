"""
Resolve company identity for the ISINs this account holds — OpenFIGI + GLEIF, cached.

This is what makes the look-through view fold one company's several rows into one. Without
it, grouping falls back to the ISIN alone, which already merges a listing held on two
exchanges (GOOGL@NASDAQ + ABEA@IBIS share ISIN US02079K3059) but cannot merge:

    GOOG  US02079K1079  + GOOGL US02079K3059    Alphabet, two share classes  -> needs the LEI
    ASML  USN070592100  + ASML  NL0010273215    ASML, two ISINs              -> needs the LEI
    2330  TW0002330008                          TSMC — GLEIF has no record   -> needs the FIGI

**A CLI rather than a route**, following the precedent that there is no upload endpoint for
Flex XML and no route for price import: two third-party services, `/api/` is proxied
publicly, and nothing here needs to be reachable from a browser. Being a separate process
it also needs no `single_flight` gate — and it must never be put on `SYNC_PIPELINE` if it
ever does become a route, because entering that gate bumps the shared last-start clock
every other route's cooldown reads, so a look-through refresh would 429 a real IBKR sync.

Touches **no** Yahoo and **no** IBKR, so neither rule at the top of CLAUDE.md applies. It
does reach OpenFIGI and GLEIF: both keyless, both paced well under their limits, both
answering about public identifiers.

With `--constituents` it also resolves the fund constituents whose issuer published a **CINS or
a SEDOL instead of an ISIN** — 77 of GRID's 128 rows, 20 of QTUM's 89. Those get a
shareClassFIGI rather than an ISIN, because OpenFIGI's mapping endpoint has no ISIN to give,
and a FIGI folds the row just as well without inventing an identifier. Note the idType matters:
asked as `ID_CUSIP` a CINS returns zero rows, silently.

Re-running is cheap and safe. A positive answer is cached forever (LEIs do not change) and a
definitive "no record" is cached too, so a second run asks about nothing. A *failed*
request is not cached, so it is retried. The one exception is a constituent identifier that
resolves to nothing, which is re-asked — a hundred-odd identifiers behind two small funds, two
requests with a key.

**Set `OPENFIGI_API_KEY` before asking about constituents.** It is free and self-service, and
takes the batch from 10 identifiers per request to 100.

Usage, inside the container:

    docker exec backend-portfolio-backend-1 \
        python -m app.cli.resolve_identities --dry-run
    docker exec backend-portfolio-backend-1 \
        python -m app.cli.resolve_identities
"""
import argparse
import asyncio
import logging
import sys
from typing import List, Optional

from sqlalchemy import select

from app.clock import utcnow
from app.config import settings
from app.database import AsyncSessionLocal
from app.etf_mappings import is_known_etf_isin
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.identity_service import GLEIF_MIN_INTERVAL_S, IdentityService
from app.services.lookthrough_service import LookthroughService

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_identity_resolve"


async def _held_isins(db, include_funds: bool) -> List[str]:
    """
    Distinct ISINs of securities with at least one open tax lot.

    Funds are excluded by default: GLEIF returns no record for a fund ISIN (measured on
    IE00B4L5Y983), a fund is never a row in the company table, and the twelve requests buy
    nothing. `--include-funds` exists because a fund missing from `ETF_ALLOCATIONS` is
    treated as an ordinary holding, and resolving it then gives it a proper name.
    """
    rows = (
        await db.execute(
            select(Security.isin)
            .join(TaxLot, TaxLot.security_id == Security.id)
            .where(TaxLot.is_open == True)  # noqa: E712 - SQLAlchemy needs the comparison
            .distinct()
        )
    ).scalars().all()
    isins = sorted({r.strip().upper() for r in rows if r and r.strip()})
    if include_funds:
        return isins
    return [i for i in isins if not is_known_etf_isin(i)]


async def resolve_identities(
    dry_run: bool = False,
    limit: Optional[int] = None,
    include_funds: bool = False,
    constituents: bool = False,
) -> int:
    started_at = utcnow()

    async with AsyncSessionLocal() as db:
        try:
            service = IdentityService(db)
            targets = await _held_isins(db, include_funds)
            direct_count = len(targets)
            constituent_count = 0
            if constituents:
                # Tier B: the head of the constituent ranking. Additive rather than a separate
                # mode, because the directly-held ISINs are where the user-visible folding
                # lives and must never be traded away for tail coverage.
                extra = await LookthroughService(db).material_constituent_isins()
                known = set(targets)
                added = [i for i in extra if i not in known]
                constituent_count = len(added)
                targets = targets + added
            if not targets:
                print("No held securities with an ISIN — nothing to resolve.")
                return 0
            print(f"Targets: {len(targets)} ISIN(s) — {direct_count} held directly"
                  + (f" + {constituent_count} material fund constituent(s)"
                     if constituents else ""))

            pending_figi = await service.repo.unchecked(targets, "openfigi")
            pending_lei = await service.repo.unchecked(targets, "gleif")
            # Fund constituents whose issuer published a CINS or a SEDOL instead of an ISIN.
            # A separate question with a separate answer, so it is counted separately.
            pending_ids = (
                await EtfBasketRepository(db).unresolved_identifiers() if constituents else []
            )
            asking_lei = min(len(pending_lei), limit) if limit else len(pending_lei)
            print(
                f"Never asked: OpenFIGI {len(pending_figi)}, GLEIF {len(pending_lei)}"
                + (f", non-ISIN constituent identifiers {len(pending_ids)}" if constituents
                   else "")
                + (f" (limited to {limit} each)" if limit else "")
            )
            if asking_lei:
                # GLEIF has no batch form, so it decides how long this takes. Said up front
                # rather than discovered forty minutes in.
                print(
                    f"Estimated runtime ~{asking_lei * GLEIF_MIN_INTERVAL_S / 60:.0f} min, "
                    f"almost all of it GLEIF (one request per ISIN). Answers are cached, so "
                    f"--limit and several runs come to the same thing."
                )
            if not settings.openfigi_api_key.strip():
                print(
                    "OPENFIGI_API_KEY is not set: 10 identifiers per request instead of 100. "
                    "Free from https://www.openfigi.com/api and worth it above a few hundred."
                )

            if dry_run:
                # Returns before any write and records no sync_run, exactly as
                # import_prices and manage_mappings do — a dry run is not an event.
                print("DRY RUN - nothing written, no requests made.")
                return 0

            if not pending_figi and not pending_lei and not pending_ids:
                print("Every held ISIN has already been asked about. Nothing to do.")
                return 0

            summary = await service.resolve(targets, limit=limit)
            if constituents:
                summary.update(
                    await service.resolve_constituent_identifiers(limit=limit)
                )
            await db.commit()

            print(
                f"OpenFIGI: {summary['openfigi_resolved']} resolved, "
                f"{summary['openfigi_not_found']} no match, "
                f"{summary['openfigi_errors']} failed"
            )
            print(
                f"GLEIF: {summary['gleif_resolved']} resolved, "
                f"{summary['gleif_not_found']} no record, "
                f"{summary['gleif_ambiguous']} ambiguous (recorded as none), "
                f"{summary['gleif_errors']} failed"
            )
            print(f"Rows written: {summary['rows_written']}")
            if constituents:
                print(
                    f"Constituent identifiers: {summary['identifiers_resolved']} resolved to a "
                    f"shareClassFIGI, {summary['identifiers_not_found']} no match, "
                    f"{summary['identifiers_errors']} failed; "
                    f"{summary['holdings_updated']} holding row(s) updated"
                )

        except Exception as e:
            await db.rollback()
            # Best-effort bookkeeping, exactly as ingest_flex_xml does: it must never mask
            # the real error.
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED: {e}", file=sys.stderr)
            return 1

        await SyncRunRepository(db).record(
            sync_type=SYNC_TYPE,
            status="success",
            message=(
                f"Resolved identity for {summary['rows_written']} ISIN(s): "
                f"{summary['gleif_resolved']} LEI, "
                f"{summary['openfigi_resolved']} shareClassFIGI"
                + (f"; {summary.get('holdings_updated', 0)} fund constituent row(s) folded "
                   f"by CINS/SEDOL" if constituents else "")
            ),
            details=summary,
            started_at=started_at,
        )
        return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be asked without making a request or a write",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Ask about at most this many ISINs per provider (bounds one run's duration)",
    )
    parser.add_argument(
        "--include-funds", action="store_true",
        help="Also resolve fund ISINs, which are normally skipped as never being companies",
    )
    parser.add_argument(
        "--constituents", action="store_true",
        help=(
            "Also resolve the largest fund constituents, down to "
            "IDENTITY_COVERAGE_TARGET_PCT of cumulative look-through value and capped at "
            "IDENTITY_MAX_ISINS. Without this, a company held only inside funds groups on "
            "its ISIN alone, so two of its ISINs would not fold — and the CINS/SEDOL rows "
            "GRID and QTUM publish have no identity at all until this runs"
        ),
    )
    args = parser.parse_args()

    return asyncio.run(
        resolve_identities(
            dry_run=args.dry_run, limit=args.limit, include_funds=args.include_funds,
            constituents=args.constituents,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
