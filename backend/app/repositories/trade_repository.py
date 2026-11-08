from typing import Dict, List, Optional, Tuple
from datetime import date, datetime
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import IBKR
from app.models.trade import Trade, buy_sign


class TradeRepository:
    """Repository for Trade model operations (idempotent on ib_key)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, data: dict) -> Trade:
        result = await self.session.execute(
            select(Trade).where(Trade.ib_key == data["ib_key"])
        )
        existing = result.scalar_one_or_none()

        if existing:
            for key, value in data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            await self.session.flush()
            return existing

        trade = Trade(**data)
        self.session.add(trade)
        await self.session.flush()
        return trade

    async def get_by_conid_in_range(
        self, conid: str, start: Optional[date], end: date
    ) -> List[Trade]:
        """Trades for a conid with trade_date in (start, end]. If start is None, all up to end."""
        stmt = select(Trade).where(
            and_(Trade.conid == str(conid), Trade.trade_date <= end)
        )
        if start is not None:
            stmt = stmt.where(Trade.trade_date > start)
        stmt = stmt.order_by(Trade.trade_date.asc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def buy_dates_by_instant(self) -> Dict[Tuple[str, datetime], date]:
        """
        ``{(conid, executed_at): trade_date}`` for every IBKR purchase whose instant is
        known: what dates the lot each one opened (`date_lots_by_their_buys`).
        """
        result = await self.session.execute(
            select(Trade).where(Trade.executed_at.is_not(None), Trade.account == IBKR)
        )
        return {
            (str(t.conid), t.executed_at): t.trade_date
            for t in result.scalars().all()
            if buy_sign(t) == 1
        }

    async def get_all(self) -> List[Trade]:
        """Every trade, oldest first — the cash balance runs from the account's start."""
        result = await self.session.execute(
            select(Trade).order_by(Trade.trade_date.asc())
        )
        return list(result.scalars().all())

    async def get_between(self, start: date, end: date) -> List[Trade]:
        """All trades with trade_date in [start, end] (used by the tax report)."""
        result = await self.session.execute(
            select(Trade)
            .where(and_(Trade.trade_date >= start, Trade.trade_date <= end))
            .order_by(Trade.trade_date.asc())
        )
        return list(result.scalars().all())

    async def count(self) -> int:
        from sqlalchemy import func
        result = await self.session.execute(select(func.count(Trade.id)))
        return int(result.scalar() or 0)
