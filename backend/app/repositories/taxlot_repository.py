"""
TaxLot Repository
Handles database operations for Tax Lots.
"""
from typing import List, Optional
from sqlalchemy import select, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.security import Security
from app.models.taxlot import TaxLot


class TaxLotRepository:
    """Repository for TaxLot model operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, taxlot_id: int) -> Optional[TaxLot]:
        """Get tax lot by ID with related security"""
        result = await self.session.execute(
            select(TaxLot)
            .options(joinedload(TaxLot.security))
            .where(TaxLot.id == taxlot_id)
        )
        return result.scalar_one_or_none()

    async def get_by_security_id(
        self,
        security_id: int,
        is_open: Optional[bool] = None
    ) -> List[TaxLot]:
        """Get all tax lots for a specific security"""
        query = select(TaxLot).where(TaxLot.security_id == security_id)

        if is_open is not None:
            query = query.where(TaxLot.is_open == is_open)

        result = await self.session.execute(query.order_by(TaxLot.open_date))
        return list(result.scalars().all())

    async def get_open_taxlots(self, account: Optional[str] = None) -> List[TaxLot]:
        """
        All open (active) tax lots with their securities.

        ``account`` defaults to None, meaning **every** account, because the two
        callers want opposite things and the safe default is different for each.
        `/api/sync/status` counts what the database holds and should keep blending.
        `reconcile_taxlots` passes ``IBKR`` and must: it feeds this straight into
        `delete_open_by_security_ids`, so an unscoped read there deletes the open
        lots of every account an IBKR statement does not happen to mention -- which
        is all of them.
        """
        query = (
            select(TaxLot)
            .options(joinedload(TaxLot.security))
            .where(TaxLot.is_open == True)
        )
        if account is not None:
            query = query.join(Security, TaxLot.security_id == Security.id).where(
                Security.account == account
            )
        result = await self.session.execute(query.order_by(TaxLot.open_date))
        return list(result.scalars().all())

    async def get_all(
        self,
        skip: int = 0,
        limit: int = 100,
        is_open: Optional[bool] = None
    ) -> List[TaxLot]:
        """Get all tax lots with pagination and optional filtering"""
        query = select(TaxLot).options(joinedload(TaxLot.security))

        if is_open is not None:
            query = query.where(TaxLot.is_open == is_open)

        result = await self.session.execute(
            query.order_by(TaxLot.open_date.desc()).offset(skip).limit(limit)
        )
        return list(result.scalars().all())

    async def create(self, taxlot_data: dict) -> TaxLot:
        """Create a new tax lot"""
        taxlot = TaxLot(**taxlot_data)
        self.session.add(taxlot)
        await self.session.flush()
        await self.session.refresh(taxlot)
        return taxlot

    async def create_many(self, rows: List[dict]) -> int:
        """
        Insert many tax lots with one flush and no per-row refresh.

        ``create()`` flushes *and* refreshes each lot — two round trips per row,
        and reconciliation writes every open lot on every sync (~975 here, so
        ~1,950 statements) while discarding all of the returned objects. Callers
        that need the persisted object back should still use ``create()``.
        """
        if not rows:
            return 0
        self.session.add_all([TaxLot(**row) for row in rows])
        await self.session.flush()
        return len(rows)

    async def update(self, taxlot_id: int, taxlot_data: dict) -> Optional[TaxLot]:
        """Update an existing tax lot"""
        taxlot = await self.get_by_id(taxlot_id)
        if taxlot:
            for key, value in taxlot_data.items():
                if hasattr(taxlot, key):
                    setattr(taxlot, key, value)
            await self.session.flush()
            await self.session.refresh(taxlot)
        return taxlot

    async def delete(self, taxlot_id: int) -> bool:
        """Delete a tax lot by ID"""
        taxlot = await self.get_by_id(taxlot_id)
        if taxlot:
            await self.session.delete(taxlot)
            await self.session.flush()
            return True
        return False

    async def delete_open_by_security_ids(self, security_ids) -> int:
        """
        Delete the OPEN lots of many securities in one statement, preserving closed
        (historical) lots. Returns the count deleted.

        Reconciliation calls this once per sync for every security it touched. It
        used to be a loop of per-security calls, each of which ran a SELECT and then
        an ORM delete per row — an N+1 inside an N+1, ~40 extra round trips on this
        portfolio before a single lot was written.

        ``synchronize_session="fetch"`` matters: the caller is holding the very rows
        this removes (it snapshots them first), and a bulk DELETE goes behind the
        ORM's back. "fetch" costs one extra SELECT and leaves the identity map
        correct, where the alternative is stale objects that reappear on flush.
        """
        ids = list(security_ids)
        if not ids:
            return 0
        result = await self.session.execute(
            delete(TaxLot)
            .where(and_(TaxLot.security_id.in_(ids), TaxLot.is_open == True))  # noqa: E712
            .execution_options(synchronize_session="fetch")
        )
        await self.session.flush()
        return result.rowcount or 0
