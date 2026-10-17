from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Set

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.models.dividend_forecast_snapshot import (
    DividendForecastSnapshot,
    DividendForecastSnapshotLine,
)


class DividendForecastSnapshotRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, data: dict, lines: Optional[List[Dict]] = None) -> None:
        """
        Store one day's figures, replacing whatever was there.

        Idempotent on `snapshot_date`, because several scheduled jobs write the same
        day. Last write wins deliberately: the evening's figure has seen the day's
        statement and prices, the morning's has not.

        ``lines`` are the per-security figures behind the total and replace the day's
        lines with it — the two are one write, or the lines would describe an earlier
        total than the one stored beside them.
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
        if lines is not None:
            await self._replace_lines(data["snapshot_date"], lines, reconstructed=False)

    async def upsert_reconstructed(
        self, data: dict, lines: Optional[List[Dict]] = None
    ) -> None:
        """
        Store one replayed day (`app.cli.backfill_dividend_forecast`).

        Replaces an earlier replay of the same day, so the backfill can be re-run after
        the forecast's rules change — and **never a recorded row**: what the application
        said on the day is the one thing a replay cannot know better. ``lines`` follow
        the same rule on their own (`fill_lines`).
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
        if lines is not None:
            await self.fill_lines(data["snapshot_date"], lines)

    async def fill_lines(self, snapshot_date: date, lines: List[Dict]) -> bool:
        """
        Store replayed lines for a day, flagged, unless the day has recorded ones.

        Separate from the parent's rule because the two can differ: a day recorded
        before the lines table existed has a recorded total and no lines at all, and a
        replay is the only way it will ever get any. Its total is left alone. Returns
        whether anything was written.
        """
        recorded = await self.session.execute(
            select(DividendForecastSnapshotLine.id).where(
                DividendForecastSnapshotLine.snapshot_date == snapshot_date,
                DividendForecastSnapshotLine.reconstructed.is_(False),
            ).limit(1)
        )
        if recorded.first() is not None:
            return False
        await self._replace_lines(snapshot_date, lines, reconstructed=True)
        return True

    async def fill_levels(self, snapshot_date: date, levels: Dict[int, Decimal]) -> int:
        """
        Give a day's lines the payer levels they were stored without, from a replay.

        Only where `level_eur` is NULL, and on recorded lines too: those were written
        before the column existed, and a missing value is not a recorded one, so filling
        it replaces nothing the application said. A level already there is never
        touched. Returns how many lines were filled.
        """
        filled = 0
        for security_id, level in levels.items():
            if level is None:
                continue
            result = await self.session.execute(
                update(DividendForecastSnapshotLine)
                .where(
                    DividendForecastSnapshotLine.snapshot_date == snapshot_date,
                    DividendForecastSnapshotLine.security_id == security_id,
                    DividendForecastSnapshotLine.level_eur.is_(None),
                )
                .values(level_eur=level)
            )
            filled += result.rowcount or 0
        await self.session.flush()
        return filled

    async def dates_missing_levels(self) -> Set[date]:
        """Days with a recorded line that has no level — what `fill_levels` is for."""
        result = await self.session.execute(
            select(DividendForecastSnapshotLine.snapshot_date).where(
                DividendForecastSnapshotLine.reconstructed.is_(False),
                DividendForecastSnapshotLine.level_eur.is_(None),
            ).distinct()
        )
        return set(result.scalars().all())

    async def dates_with_recorded_lines(self) -> Set[date]:
        result = await self.session.execute(
            select(DividendForecastSnapshotLine.snapshot_date).where(
                DividendForecastSnapshotLine.reconstructed.is_(False)
            ).distinct()
        )
        return set(result.scalars().all())

    async def _replace_lines(
        self, snapshot_date: date, lines: List[Dict], reconstructed: bool
    ) -> None:
        await self.session.execute(
            delete(DividendForecastSnapshotLine).where(
                DividendForecastSnapshotLine.snapshot_date == snapshot_date
            )
        )
        self.session.add_all(
            DividendForecastSnapshotLine(
                snapshot_date=snapshot_date, reconstructed=reconstructed, **line
            )
            for line in lines
        )
        await self.session.flush()

    async def get_lines(
        self, since: Optional[date] = None
    ) -> Dict[date, List[DividendForecastSnapshotLine]]:
        """Every day's lines from ``since`` on, keyed by day. A day without any is absent."""
        query = select(DividendForecastSnapshotLine).order_by(
            DividendForecastSnapshotLine.snapshot_date.asc(),
            DividendForecastSnapshotLine.security_id.asc(),
        )
        if since is not None:
            query = query.where(DividendForecastSnapshotLine.snapshot_date >= since)
        out: Dict[date, List[DividendForecastSnapshotLine]] = {}
        for line in (await self.session.execute(query)).scalars().all():
            out.setdefault(line.snapshot_date, []).append(line)
        return out

    async def get_all(self) -> List[DividendForecastSnapshot]:
        """Every snapshot, oldest first."""
        result = await self.session.execute(
            select(DividendForecastSnapshot).order_by(
                DividendForecastSnapshot.snapshot_date.asc()
            )
        )
        return list(result.scalars().all())
