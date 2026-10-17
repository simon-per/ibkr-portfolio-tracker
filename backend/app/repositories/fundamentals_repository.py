from typing import List, Optional
from datetime import timedelta
from app.clock import utcnow
from sqlalchemy import select, and_, func
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fundamental_metrics import FundamentalMetrics
from app.models.earnings_event import EarningsEvent
from app.models.security import Security
from app.repositories.security_repository import held
from app.services.yahoo_eligibility import yahoo_eligible

#: How old a metrics row has to be before a sync will refresh it.
#:
#: One number, because there used to be three and they disagreed: this default was 7,
#: `sync_fundamentals_data` passed `days_old=1`, and `/api/fundamentals/status` ran its own
#: hardcoded 7-day query. So the status endpoint could report `stale_metrics: 0` while a
#: sync would in fact refresh nearly every row — a status figure that did not describe the
#: behaviour it was reporting on. The `/sync-stale` docstring claimed 7 days too.
#:
#: Twenty hours, not a day, because the scheduled refresh runs daily at a fixed hour
#: (`FUNDAMENTALS_REFRESH_HOUR`): a row written at 05:02 is 23 h 58 m old at the next
#: 05:00, so a one-day rule would refresh each security only every other pass. Twenty
#: hours still leaves a manual Refresh pressed the same day spending nothing on rows the
#: morning pass already wrote — each costs ~6 Yahoo requests.
STALE_AFTER = timedelta(hours=20)


class FundamentalsRepository:
    """Repository for FundamentalMetrics and EarningsEvent model operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # --- FundamentalMetrics ---

    async def get_metrics_by_security_id(self, security_id: int) -> Optional[FundamentalMetrics]:
        result = await self.session.execute(
            select(FundamentalMetrics).where(FundamentalMetrics.security_id == security_id)
        )
        return result.scalar_one_or_none()

    async def get_all_metrics(self) -> List[FundamentalMetrics]:
        result = await self.session.execute(select(FundamentalMetrics))
        return list(result.scalars().all())

    async def get_stale_metrics(
        self, older_than: timedelta = STALE_AFTER
    ) -> List[FundamentalMetrics]:
        result = await self.session.execute(
            select(FundamentalMetrics).where(self._stale(older_than))
        )
        return list(result.scalars().all())

    async def count_stale_metrics(self, older_than: timedelta = STALE_AFTER) -> int:
        """
        How many rows a sync would refresh. Exists so `/status` cannot answer that
        question with a different query than the sync asks — which is exactly how the
        two came to use different thresholds.
        """
        result = await self.session.execute(
            select(func.count(FundamentalMetrics.id)).where(self._stale(older_than))
        )
        return int(result.scalar() or 0)

    @staticmethod
    def _stale(older_than: timedelta):
        """
        A row a sync would refresh: older than `older_than`, of a security the sync walks
        (held and Yahoo-eligible). Without the scope, a sold position's row — never
        refreshed since 2026-10-17 — stayed in the count forever.
        """
        return and_(
            FundamentalMetrics.last_updated < utcnow() - older_than,
            FundamentalMetrics.security_id.in_(
                select(Security.id).where(yahoo_eligible(), held())
            ),
        )

    async def upsert_metrics(self, data: dict) -> FundamentalMetrics:
        existing = await self.get_metrics_by_security_id(data['security_id'])

        if existing:
            for key, value in data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            existing.last_updated = utcnow()
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        else:
            data['last_updated'] = utcnow()
            metrics = FundamentalMetrics(**data)
            self.session.add(metrics)
            await self.session.flush()
            await self.session.refresh(metrics)
            return metrics

    # --- EarningsEvent ---

    async def get_earnings_by_security(self, security_id: int) -> List[EarningsEvent]:
        result = await self.session.execute(
            select(EarningsEvent)
            .where(EarningsEvent.security_id == security_id)
            .order_by(EarningsEvent.earnings_date.desc())
        )
        return list(result.scalars().all())

    async def upsert_earnings_event(self, data: dict) -> EarningsEvent:
        result = await self.session.execute(
            select(EarningsEvent).where(
                and_(
                    EarningsEvent.security_id == data['security_id'],
                    EarningsEvent.earnings_date == data['earnings_date'],
                )
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            for key, value in data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        else:
            event = EarningsEvent(**data)
            self.session.add(event)
            await self.session.flush()
            await self.session.refresh(event)
            return event

    async def get_upcoming_earnings(self, days_ahead: int = 90) -> List[EarningsEvent]:
        now = utcnow()
        cutoff = now + timedelta(days=days_ahead)
        result = await self.session.execute(
            select(EarningsEvent)
            .options(joinedload(EarningsEvent.security))
            .where(
                and_(
                    EarningsEvent.earnings_date >= now,
                    EarningsEvent.earnings_date <= cutoff,
                )
            )
            .order_by(EarningsEvent.earnings_date.asc())
        )
        return list(result.unique().scalars().all())

    async def get_recent_earnings(self, days_back: int = 365) -> List[EarningsEvent]:
        now = utcnow()
        cutoff = now - timedelta(days=days_back)
        result = await self.session.execute(
            select(EarningsEvent)
            .options(joinedload(EarningsEvent.security))
            .where(
                and_(
                    EarningsEvent.earnings_date < now,
                    EarningsEvent.earnings_date >= cutoff,
                    EarningsEvent.reported_eps.isnot(None),
                )
            )
            .order_by(EarningsEvent.earnings_date.desc())
        )
        return list(result.unique().scalars().all())
