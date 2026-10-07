from typing import List

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.models.dividend_forecast_snapshot import DividendForecastSnapshot


class DividendForecastSnapshotRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, data: dict) -> None:
        """
        Store one day's figures, replacing whatever was there.

        Idempotent on `snapshot_date`, because several scheduled jobs write the same
        day. Last write wins deliberately: the evening's figure has seen the day's
        statement and prices, the morning's has not.
        """
        stmt = sqlite_insert(DividendForecastSnapshot).values(
            recorded_at=utcnow(), **data
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=[DividendForecastSnapshot.snapshot_date],
                set_={
                    "next_12m_eur": stmt.excluded.next_12m_eur,
                    "ttm_net_eur": stmt.excluded.ttm_net_eur,
                    "recorded_at": stmt.excluded.recorded_at,
                },
            )
        )

    async def get_all(self) -> List[DividendForecastSnapshot]:
        """Every snapshot, oldest first."""
        result = await self.session.execute(
            select(DividendForecastSnapshot).order_by(
                DividendForecastSnapshot.snapshot_date.asc()
            )
        )
        return list(result.scalars().all())
