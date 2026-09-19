from typing import List
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

    async def get_open(self) -> List[DividendAccrual]:
        """
        Every open accrual, ordered by pay date.

        Unfiltered on purpose. The table holds exactly what the last statement listed as
        open, so any row here is a dividend IBKR still says it owes — including one
        overdue by months, which is the row a reader most needs to see.
        """
        result = await self.session.execute(
            select(DividendAccrual).order_by(DividendAccrual.pay_date.asc())
        )
        return list(result.scalars().all())
