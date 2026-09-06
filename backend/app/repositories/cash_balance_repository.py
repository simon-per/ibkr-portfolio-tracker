from datetime import date
from typing import List, Optional

from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import IBKR
from app.models.cash_balance import CashBalance


class CashBalanceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, data: dict) -> None:
        """
        Store one day's balance, replacing whatever was there.

        Idempotent on `(account, report_date)`, because the Flex window re-delivers the
        same days
        on every sync. Last write wins deliberately: a later statement's figure for a
        day supersedes an earlier one, which is how a same-day balance settles once the
        session closes — the same reasoning as `PROVISIONAL_PRICE_DAYS` on market prices.
        """
        # `account` must be materialised here rather than left to the column default:
        # ON CONFLICT keys on it, and a value absent from the INSERT is absent from
        # the conflict target too, so the upsert would silently become an insert.
        data = {'account': IBKR, **data}
        stmt = sqlite_insert(CashBalance).values(**data)
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=[CashBalance.account, CashBalance.report_date],
                set_={
                    "currency": stmt.excluded.currency,
                    "cash": stmt.excluded.cash,
                    "stock": stmt.excluded.stock,
                    "total": stmt.excluded.total,
                },
            )
        )

    async def get_all(self) -> List[CashBalance]:
        """Every measured balance, oldest first."""
        result = await self.session.execute(
            select(CashBalance).order_by(CashBalance.report_date.asc())
        )
        return list(result.scalars().all())

    async def earliest_date(self) -> Optional[date]:
        """
        First measured day, or None when the section has never been ingested.

        This is the splice point for `CashService`: measured balances win from here on,
        and the derived series covers everything before — the Flex window is bounded, so
        measured history can never reach back to the account's start.
        """
        result = await self.session.execute(select(func.min(CashBalance.report_date)))
        return result.scalar()

    async def count(self) -> int:
        result = await self.session.execute(select(func.count(CashBalance.id)))
        return int(result.scalar() or 0)
