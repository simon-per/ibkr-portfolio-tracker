from typing import List, Optional
from datetime import date
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dividend_accrual import DividendAccrual


class DividendAccrualRepository:
    """Repository for the announced-but-unpaid dividends IBKR publishes."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def replace_all(self, rows: List[dict]) -> int:
        """
        Swap the whole set for ``rows``. Returns how many were written.

        **Wholesale, because IBKR publishes the currently-OPEN accruals rather than a
        log.** A paid dividend simply stops appearing in the section, so an upsert would
        leave it behind for ever and the calendar would keep promising money that had
        already arrived. Deleting first is what makes disappearance meaningful.

        Does NOT commit — the caller's sync transaction owns that, so a failure later in
        the sync rolls the old set back rather than leaving the table empty. Which is
        also why an empty ``rows`` still clears: a statement that carries the section and
        lists nothing is saying every accrual has been paid.
        """
        await self.session.execute(delete(DividendAccrual))
        for row in rows:
            self.session.add(DividendAccrual(**row))
        await self.session.flush()
        return len(rows)

    async def get_open(self, on_or_after: Optional[date] = None) -> List[DividendAccrual]:
        """Accruals ordered by pay date, optionally only those paying on/after a date."""
        stmt = select(DividendAccrual)
        if on_or_after is not None:
            stmt = stmt.where(DividendAccrual.pay_date >= on_or_after)
        stmt = stmt.order_by(DividendAccrual.pay_date.asc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
