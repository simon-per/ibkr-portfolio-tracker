"""
Store a logo you downloaded by hand for a holding logo.dev misses (docs/logos.md).

The file passes the same refuse-whole check as a fetched logo (`validate_logo`): PNG, JPEG
or WebP by its magic bytes, never SVG, at most 100 KB. It is stored under the ISIN with
`source='manual'`, replacing whatever was there, and **the automatic pass never overwrites
or re-asks about a manual row**. Every listing of that ISIN shows it.

There is deliberately no upload endpoint (`/api/` is public); this CLI over the console is
the write path. Records a `sync_runs` row (`manual_logo_import`); a dry run does not.

Usage — copy the file to the VPS (`scp`, or the Hostinger file manager), into the
container, then import it:

    docker cp /root/logo.png backend-portfolio-backend-1:/tmp/logo.png
    docker exec backend-portfolio-backend-1 python -m app.cli.import_logo TW0002330008 /tmp/logo.png --dry-run
    docker exec backend-portfolio-backend-1 python -m app.cli.import_logo TW0002330008 /tmp/logo.png
"""
import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

from sqlalchemy import select

from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.logging_setup import configure_logging
from app.models.security import Security
from app.models.security_logo import LOGO_SOURCE_MANUAL
from app.repositories.security_logo_repository import SecurityLogoRepository
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.logo_service import MAX_LOGO_BYTES, LogoRefused, validate_logo

logger = logging.getLogger(__name__)

SYNC_TYPE = "manual_logo_import"

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


async def import_logo(isin: str, path: Path, dry_run: bool = False) -> int:
    started_at = utcnow()
    isin = isin.strip().upper()
    if not _ISIN.match(isin):
        print(f"REFUSED: {isin!r} is not an ISIN.", file=sys.stderr)
        return 1
    try:
        with open(path, "rb") as fh:
            # One byte past the limit is enough to refuse an oversized file.
            data = fh.read(MAX_LOGO_BYTES + 1)
    except OSError as e:
        print(f"REFUSED: cannot read {path}: {e}", file=sys.stderr)
        return 1
    try:
        content_type = validate_logo(data, None)
    except LogoRefused as e:
        print(f"REFUSED: {path.name}: {e}. Nothing stored.", file=sys.stderr)
        return 1

    async with AsyncSessionLocal() as db:
        # By ISIN, the logo's key: every listing of it shares the logo, so no symbol lookup
        # (app/cli/security_lookup.py) and no ambiguity to refuse.
        securities = (
            await db.execute(select(Security).where(Security.isin == isin))
        ).scalars().all()
        if not securities:
            print(f"REFUSED: no security carries ISIN {isin}. Nothing stored.", file=sys.stderr)
            return 1
        listing = ", ".join(sorted({s.symbol for s in securities}))
        print(f"{isin} ({listing}): {content_type}, {len(data)} bytes")
        if dry_run:
            print("DRY RUN - nothing written.")
            return 0

        try:
            await SecurityLogoRepository(db).store(
                isin, data, content_type, LOGO_SOURCE_MANUAL, utcnow()
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE, status="error", message=str(e), started_at=started_at,
            )
            print(f"FAILED: {e}", file=sys.stderr)
            return 1

        message = f"Stored a manual logo for {isin} ({listing}): {content_type}, {len(data)} bytes"
        await SyncRunRepository(db).record(
            sync_type=SYNC_TYPE, status="success", message=message,
            details={"isin": isin, "content_type": content_type, "bytes": len(data)},
            started_at=started_at,
        )
        print(message)
        return 0


def main() -> int:
    configure_logging(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("isin", help="The ISIN the logo belongs to")
    parser.add_argument("file", type=Path, help="A PNG, JPEG or WebP file of at most 100 KB")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate the file and name the holding without storing it")
    args = parser.parse_args()
    return asyncio.run(import_logo(args.isin, args.file, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
