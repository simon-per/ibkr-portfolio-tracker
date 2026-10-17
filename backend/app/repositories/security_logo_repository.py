"""Repository for SecurityLogo — holding logos keyed by ISIN (docs/logos.md)."""
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.security import Security
from app.models.security_logo import LOGO_SOURCE_MANUAL, SecurityLogo

#: Characters of the sha256 that version a logo's URL. Twelve hex digits make a collision
#: between two images of one security out of the question.
VERSION_CHARS = 12


def _norm(isin: str) -> str:
    return isin.strip().upper()


class SecurityLogoRepository:
    """Repository for SecurityLogo model operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_map(self, isins: Iterable[str]) -> Dict[str, SecurityLogo]:
        """ISIN -> row, for the ISINs that have one. Absence means *never asked*."""
        wanted = sorted({_norm(i) for i in isins if i and i.strip()})
        if not wanted:
            return {}
        rows = (
            await self.session.execute(
                select(SecurityLogo).where(SecurityLogo.isin.in_(wanted))
            )
        ).scalars().all()
        return {row.isin: row for row in rows}

    async def due(
        self, isins: Iterable[str], now: datetime, recheck_after: Optional[timedelta]
    ) -> List[str]:
        """
        Which of `isins` the automatic pass may ask about: no row yet, or a row last
        checked longer than `recheck_after` ago (`None` = regardless of age, `--force`).

        **A manual row is never due**, whatever its age or `--force` says.
        """
        known = await self.get_map(isins)
        wanted = sorted({_norm(i) for i in isins if i and i.strip()})
        out = []
        for isin in wanted:
            row = known.get(isin)
            if row is None:
                out.append(isin)
            elif row.source == LOGO_SOURCE_MANUAL:
                continue
            elif recheck_after is None or row.checked_at <= now - recheck_after:
                out.append(isin)
        return out

    async def store(
        self, isin: str, image: bytes, content_type: str, source: str, now: datetime
    ) -> bool:
        """
        Store a validated image (the caller has run `validate_logo`). Returns whether a
        row was written.

        An automatic write is conditional on the stored row not being manual, in the same
        statement — so a manual import made while a pass was in flight still wins. A
        manual write replaces whatever is there.
        """
        values = dict(
            isin=_norm(isin), image=image, content_type=content_type,
            sha256=hashlib.sha256(image).hexdigest(), source=source,
            fetched_at=now, checked_at=now,
        )
        stmt = sqlite_insert(SecurityLogo).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["isin"],
            set_={k: getattr(stmt.excluded, k) for k in values if k != "isin"},
            where=(None if source == LOGO_SOURCE_MANUAL
                   else SecurityLogo.source != LOGO_SOURCE_MANUAL),
        )
        result = await self.session.execute(stmt)
        return result.rowcount > 0

    async def mark_checked(self, isin: str, source: str, now: datetime) -> None:
        """
        Record "asked, nothing usable" without touching an image already stored: a logo
        that later reads as not-found is kept rather than erased. Never touches a manual row.
        """
        stmt = sqlite_insert(SecurityLogo).values(
            isin=_norm(isin), image=None, content_type=None, sha256=None,
            source=source, fetched_at=None, checked_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["isin"],
            set_={"checked_at": stmt.excluded.checked_at},
            where=SecurityLogo.source != LOGO_SOURCE_MANUAL,
        )
        await self.session.execute(stmt)

    async def manifest(self) -> Dict[int, str]:
        """`security_id -> version` for every security whose ISIN has an image."""
        rows = await self.session.execute(
            select(Security.id, SecurityLogo.sha256)
            .join(SecurityLogo, SecurityLogo.isin == Security.isin)
            .where(SecurityLogo.image.is_not(None))
        )
        return {sid: sha[:VERSION_CHARS] for sid, sha in rows.all()}

    async def for_security(self, security_id: int) -> Optional[SecurityLogo]:
        """The stored logo row of a security, when it has an image."""
        return (
            await self.session.execute(
                select(SecurityLogo)
                .join(Security, Security.isin == SecurityLogo.isin)
                .where(Security.id == security_id, SecurityLogo.image.is_not(None))
            )
        ).scalars().first()
