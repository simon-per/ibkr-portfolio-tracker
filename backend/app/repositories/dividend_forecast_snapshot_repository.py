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
            recorded_at=utcnow(), reconstructed=False, **data
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=[DividendForecastSnapshot.snapshot_date],
                set_={
                    "next_12m_eur": stmt.excluded.next_12m_eur,
                    "ttm_net_eur": stmt.excluded.ttm_net_eur,
                    "recorded_at": stmt.excluded.recorded_at,
                    # A figure recorded on its own day outranks a replay of it.
                    "reconstructed": stmt.excluded.reconstructed,
                },
            )
        )

    async def upsert_reconstructed(self, data: dict) -> None:
        """
        Store one replayed day (`app.cli.backfill_dividend_forecast`).

        Replaces an earlier replay of the same day, so the backfill can be re-run after
        the forecast's rules change — and **never a recorded row**: what the application
        said on the day is the one thing a replay cannot know better.
        """
        stmt = sqlite_insert(DividendForecastSnapshot).values(
            recorded_at=utcnow(), reconstructed=True, **data
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=[DividendForecastSnapshot.snapshot_date],
                set_={
                    "next_12m_eur": stmt.excluded.next_12m_eur,
                    "ttm_net_eur": stmt.excluded.ttm_net_eur,
                    "recorded_at": stmt.excluded.recorded_at,
                },
                where=DividendForecastSnapshot.reconstructed.is_(True),
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
