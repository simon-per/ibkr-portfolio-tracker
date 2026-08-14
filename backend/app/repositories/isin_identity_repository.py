"""Repository for IsinIdentity — the cached ISIN -> company identity map."""
import logging
from typing import Dict, Iterable, List

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.isin_identity import IsinIdentity

logger = logging.getLogger(__name__)


class IsinIdentityRepository:
    """Repository for IsinIdentity model operations"""

    # SQLITE_MAX_VARIABLE_NUMBER at ~10 params per row, same reasoning as
    # MarketPriceRepository.UPSERT_CHUNK.
    UPSERT_CHUNK = 80

    # A `select ... where isin in (...)` is one bound parameter per ISIN, so a 12,000-ISIN
    # basket has to be read in pieces or sqlite refuses the statement.
    SELECT_CHUNK = 400

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_map(self, isins: Iterable[str]) -> Dict[str, IsinIdentity]:
        """
        ISIN -> row, for the ISINs that have one. Absence means *never asked*.

        Upper-cases on the way in and keys the result the same way, because issuer files
        disagree about ISIN casing and a lower-cased lookup missing its row would silently
        unfold a company.
        """
        wanted = sorted({i.strip().upper() for i in isins if i and i.strip()})
        out: Dict[str, IsinIdentity] = {}
        for start in range(0, len(wanted), self.SELECT_CHUNK):
            chunk = wanted[start:start + self.SELECT_CHUNK]
            rows = (
                await self.session.execute(
                    select(IsinIdentity).where(IsinIdentity.isin.in_(chunk))
                )
            ).scalars().all()
            for row in rows:
                out[row.isin] = row
        return out

    async def unchecked(
        self, isins: Iterable[str], provider: str
    ) -> List[str]:
        """
        Which of `isins` this provider has never been asked about.

        Keys on the provider's own `*_checked_at` rather than on the identifier being
        NULL, which is the whole point of storing the two separately: four of this
        account's direct ISINs are permanently absent from GLEIF, and asking again every
        run is the loop the holiday rule and `UPSTREAM_RETRY_COOLDOWN_SECONDS` both exist
        to prevent.
        """
        attr = {"gleif": "lei_checked_at", "openfigi": "figi_checked_at"}[provider]
        known = await self.get_map(isins)
        wanted = sorted({i.strip().upper() for i in isins if i and i.strip()})
        return [i for i in wanted if getattr(known.get(i), attr, None) is None]

    async def upsert_many(self, rows: List[dict]) -> int:
        """
        Merge identity rows, updating only the columns each dict actually supplies.

        Only-what-is-supplied matters here more than usual: the two providers are resolved
        in separate passes, so an OpenFIGI result must not blank the LEI columns a GLEIF
        pass already wrote. `ON CONFLICT DO UPDATE` with a `set_` built from the incoming
        keys is what gives that, and it is why the two passes can run in either order.
        """
        if not rows:
            return 0
        count = 0
        for start in range(0, len(rows), self.UPSERT_CHUNK):
            chunk = rows[start:start + self.UPSERT_CHUNK]
            stmt = sqlite_insert(IsinIdentity).values(chunk)
            updatable = {
                c.name: getattr(stmt.excluded, c.name)
                for c in IsinIdentity.__table__.columns
                if c.name not in ("isin", "created_at") and c.name in chunk[0]
            }
            if updatable:
                stmt = stmt.on_conflict_do_update(
                    index_elements=["isin"], set_=updatable
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=["isin"])
            await self.session.execute(stmt)
            count += len(chunk)
        return count
