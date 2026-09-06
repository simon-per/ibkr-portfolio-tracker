"""
Security Repository
Handles database operations for Securities.
"""
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.sqlite import insert

from app.models.security import Security


class SecurityRepository:
    """Repository for Security model operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, security_id: int) -> Optional[Security]:
        """Get security by ID"""
        result = await self.session.execute(
            select(Security).where(Security.id == security_id)
        )
        return result.scalar_one_or_none()

    async def get_by_conid(self, conid: int) -> Optional[Security]:
        """
        Get security by IBKR conid (unique identifier).

        Refuses a NULL rather than answering, and the reason is not defensiveness.
        `conid` is nullable since accounts arrived (a Pillar 3a fund has no IBKR
        contract id), and SQLAlchemy renders ``conid == None`` as ``IS NULL`` -- so
        this would not merely fail to find anything, it would **match an arbitrary
        conid-less security**, or raise MultipleResultsFound once there are two.
        Resolve a non-IBKR security by ``get_by_isin_exchange`` instead.
        """
        if conid is None:
            raise ValueError(
                "get_by_conid(None): a conid-less security cannot be found by conid "
                "-- IS NULL would match an arbitrary one. Use get_by_isin_exchange."
            )
        result = await self.session.execute(
            select(Security).where(Security.conid == conid)
        )
        return result.scalar_one_or_none()

    async def get_by_isin_exchange(self, isin: str, exchange: Optional[str]) -> Optional[Security]:
        """Get security by ISIN and exchange (composite unique key)"""
        query = select(Security).where(Security.isin == isin)
        if exchange:
            query = query.where(Security.exchange == exchange)
        else:
            query = query.where(Security.exchange.is_(None))

        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_all(self, skip: int = 0, limit: int = 100) -> List[Security]:
        """Get all securities with pagination"""
        result = await self.session.execute(
            select(Security).offset(skip).limit(limit)
        )
        return list(result.scalars().all())

    async def create(self, security_data: dict) -> Security:
        """Create a new security"""
        security = Security(**security_data)
        self.session.add(security)
        await self.session.flush()
        await self.session.refresh(security)
        return security

    async def upsert(self, security_data: dict) -> Security:
        """
        Insert or update security, keyed on conid -- so this is **the IBKR path**.

        A security with no conid must go through ``upsert_by_isin_exchange``. Keying
        on conid here is not an implementation detail that could be relaxed: with a
        NULL it matches an arbitrary conid-less row (see ``get_by_conid``), so the
        second import of a second account would overwrite the first fund with the
        second one's name, ISIN and currency, having looked perfectly fine the first
        time. Refused loudly instead.
        """
        if security_data.get('conid') is None:
            raise ValueError(
                "SecurityRepository.upsert requires a conid: it is the IBKR upsert "
                "path. A security without one (Pillar 3a, or any non-IBKR ledger) "
                "must use upsert_by_isin_exchange, which keys on the constraint that "
                "actually identifies it."
            )
        # Try to find existing by conid
        existing = await self.get_by_conid(security_data['conid'])

        if existing:
            # Update existing
            for key, value in security_data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        else:
            # Create new
            return await self.create(security_data)

    async def upsert_by_isin_exchange(self, security_data: dict) -> Security:
        """
        Insert or update keyed on ``(isin, exchange)`` -- the composite constraint
        the table already carries, and the only identity a non-IBKR security has.

        Exists because the same ISIN legitimately appears twice (ASML on NASDAQ and
        on AEB), so the pair is the key and the ISIN alone is not. A caller that
        holds a conid should use ``upsert``; this one neither reads nor writes it.
        """
        existing = await self.get_by_isin_exchange(
            security_data['isin'], security_data.get('exchange')
        )
        if existing:
            for key, value in security_data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        return await self.create(security_data)

    async def bulk_upsert(self, securities_data: List[dict]) -> int:
        """
        Bulk upsert securities.
        Returns count of affected rows.

        Note: For SQLite, we use a simpler approach of individual upserts
        since SQLite's upsert syntax is different from PostgreSQL.
        """
        count = 0
        for security_data in securities_data:
            await self.upsert(security_data)
            count += 1
        return count

    async def delete(self, security_id: int) -> bool:
        """Delete a security by ID"""
        security = await self.get_by_id(security_id)
        if security:
            await self.session.delete(security)
            await self.session.flush()
            return True
        return False
