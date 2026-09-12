from typing import List, Optional
from datetime import date
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW, TRANSFER_IN


class CashFlowRepository:
    """Repository for CashFlow model operations (idempotent on ib_key)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, data: dict) -> CashFlow:
        result = await self.session.execute(
            select(CashFlow).where(CashFlow.ib_key == data["ib_key"])
        )
        existing = result.scalar_one_or_none()

        if existing:
            for key, value in data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            await self.session.flush()
            return existing

        flow = CashFlow(**data)
        self.session.add(flow)
        await self.session.flush()
        return flow

    async def get_all(self) -> List[CashFlow]:
        result = await self.session.execute(
            select(CashFlow).order_by(CashFlow.flow_date.asc())
        )
        return list(result.scalars().all())

    async def get_deposits(self) -> List[CashFlow]:
        """
        Deposits and withdrawals only — real external money.

        TRANSFER rows are deliberately excluded: an incoming transfer moves capital
        that was already saved at another broker, and the transferred lots already
        carry their original open_date, so counting it here would both invent a
        contribution and double-count the original purchase.
        """
        result = await self.session.execute(
            select(CashFlow)
            .where(CashFlow.flow_type == DEPOSIT_WITHDRAW)
            .order_by(CashFlow.flow_date.asc())
        )
        return list(result.scalars().all())

    async def get_between(self, start: date, end: date) -> List[CashFlow]:
        """All flows with flow_date in [start, end]."""
        result = await self.session.execute(
            select(CashFlow)
            .where(and_(CashFlow.flow_date >= start, CashFlow.flow_date <= end))
            .order_by(CashFlow.flow_date.asc())
        )
        return list(result.scalars().all())

    async def earliest_deposit_date(self) -> Optional[date]:
        """
        Date of the oldest deposit/withdrawal, or None if there are none.

        This is the coverage floor for the contributions report. The Flex Query is
        Year to Date and the earlier history arrived by broker transfer, so there is
        simply no deposit data before this date — a window reaching further back has
        to say so rather than average over months it cannot see. Transfers are
        excluded so an incoming one can't backdate the floor past real coverage.
        """
        result = await self.session.execute(
            select(func.min(CashFlow.flow_date)).where(CashFlow.flow_type == DEPOSIT_WITHDRAW)
        )
        return result.scalar()

    async def earliest_transfer_in_date(self) -> Optional[date]:
        """Date of the oldest incoming transfer — informational, to explain the floor."""
        result = await self.session.execute(
            select(func.min(CashFlow.flow_date)).where(CashFlow.flow_type == TRANSFER_IN)
        )
        return result.scalar()

    async def earliest_flow_date(self, account: Optional[str] = None) -> Optional[date]:
        """
        Date of the oldest row of ANY type — when the ledger first had anything to say.

        Deliberately not filtered the way get_deposits() is. A transfer is never money
        in, but it is still evidence that the account existed and was reporting, which
        is the only question being asked here: the ledger cannot claim to cover dates
        on which it holds no rows at all. A statement period routinely starts before
        the account did (a Year-to-Date query in the account's first year), and in that
        slice an empty deposit list means the money went to another broker, not that no
        money was added — so the contributions splice clamps to this date.

        `account` scopes the question to one ledger. The clamp's caller asks about the
        IBKR statement's claim, and a pillar-3a row older than that claim would make the
        `>` test false and silently disable the clamp — the IBKR purchases in the
        unevidenced gap would then vanish from Money In with nothing reporting it.
        """
        query = select(func.min(CashFlow.flow_date))
        if account is not None:
            query = query.where(CashFlow.account == account)
        result = await self.session.execute(query)
        return result.scalar()

    async def count(self) -> int:
        result = await self.session.execute(select(func.count(CashFlow.id)))
        return int(result.scalar() or 0)
