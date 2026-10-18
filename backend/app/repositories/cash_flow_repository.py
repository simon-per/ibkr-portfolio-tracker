from typing import List, Optional
from datetime import date
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW, TRANSFER_IN


class CashFlowLedger:
    """
    The questions the money-in splice asks of the ledger, answered off rows already
    loaded — `CashFlowRepository.get_all()`, oldest first.

    `CashService` hands its memoized rows here, so a request that has read the ledger
    once asks nothing of it again (GitHub #73: the decomposition read `cash_flows` eleven
    times). The repository's own methods answer through it too, so the deposit whitelist
    and the account scoping exist once rather than once in SQL and once in Python.
    """

    def __init__(self, rows: List[CashFlow]):
        self.rows = rows

    def deposits(self) -> List[CashFlow]:
        """
        Deposits and withdrawals only — real external money.

        TRANSFER rows are deliberately excluded: an incoming transfer moves capital
        that was already saved at another broker, and the transferred lots already
        carry their original open_date, so counting it here would both invent a
        contribution and double-count the original purchase.
        """
        return [f for f in self.rows if f.flow_type == DEPOSIT_WITHDRAW]

    def earliest_deposit_date(self) -> Optional[date]:
        """
        Date of the oldest deposit/withdrawal, or None if there are none.

        This is the coverage floor for the contributions report. The Flex Query is
        Year to Date and the earlier history arrived by broker transfer, so there is
        simply no deposit data before this date — a window reaching further back has
        to say so rather than average over months it cannot see. Transfers are
        excluded so an incoming one can't backdate the floor past real coverage.
        """
        return min((f.flow_date for f in self.deposits()), default=None)

    def earliest_transfer_in_date(self) -> Optional[date]:
        """Date of the oldest incoming transfer — informational, to explain the floor."""
        return min(
            (f.flow_date for f in self.rows if f.flow_type == TRANSFER_IN), default=None
        )

    def earliest_flow_date(self, account: Optional[str] = None) -> Optional[date]:
        """
        Date of the oldest row of ANY type — when the ledger first had anything to say.

        Deliberately not filtered the way deposits() is. A transfer is never money
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
        return min(
            (f.flow_date for f in self.rows if account is None or f.account == account),
            default=None,
        )


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
        """Deposits and withdrawals only — see `CashFlowLedger.deposits`."""
        return CashFlowLedger(await self.get_all()).deposits()

    async def get_between(self, start: date, end: date) -> List[CashFlow]:
        """All flows with flow_date in [start, end]."""
        result = await self.session.execute(
            select(CashFlow)
            .where(and_(CashFlow.flow_date >= start, CashFlow.flow_date <= end))
            .order_by(CashFlow.flow_date.asc())
        )
        return list(result.scalars().all())

    async def earliest_deposit_date(self) -> Optional[date]:
        """See `CashFlowLedger.earliest_deposit_date`."""
        return CashFlowLedger(await self.get_all()).earliest_deposit_date()

    async def earliest_transfer_in_date(self) -> Optional[date]:
        """See `CashFlowLedger.earliest_transfer_in_date`."""
        return CashFlowLedger(await self.get_all()).earliest_transfer_in_date()

    async def earliest_flow_date(self, account: Optional[str] = None) -> Optional[date]:
        """See `CashFlowLedger.earliest_flow_date` — `account` scopes the clamp."""
        return CashFlowLedger(await self.get_all()).earliest_flow_date(account=account)

    async def count(self) -> int:
        result = await self.session.execute(select(func.count(CashFlow.id)))
        return int(result.scalar() or 0)
