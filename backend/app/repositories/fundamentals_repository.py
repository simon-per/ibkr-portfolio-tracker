from typing import List, Optional
from datetime import datetime, timedelta
from app.clock import utcnow
from sqlalchemy import select, and_
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.fundamental_metrics import FundamentalMetrics
from app.models.earnings_event import EarningsEvent
from app.models.security import Security
from app.repositories.security_repository import held
from app.services.miss_backoff import miss_backoff_until
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


def sync_scope() -> ColumnElement[bool]:
    """
    SQLAlchemy criterion: the securities a fundamentals sync walks — held and
    Yahoo-eligible (`price_source`).

    One predicate for the sync's selection, the stale count and `/status`'s holding
    counts. `/status` used to count `held()` alone, so a held opt-out (the pillar 3a
    funds) sat in `securities_without_data` for good: an amber line and an Update button
    that never went away, and a pass that could not write the row it promised.
    """
    return and_(yahoo_eligible(), held())


def fundamentals_backoff_until(security: Security) -> Optional[datetime]:
    """
    When the fundamentals pass may next ask about `security`, or None when it is not
    backing off: the price loop's rule (`miss_backoff`), on its own pair of columns.

    No held exemption, unlike prices: every security this pass walks is held, and a
    holding's ratios a few days old cost nothing, while an unservable one re-asked every
    pass cost three to six Yahoo requests a day for good (issue #66).
    """
    return miss_backoff_until(security.fundamentals_checked_at, security.fundamentals_miss_count)


def backing_off(security: Security, now: datetime) -> bool:
    """True while `security` waits out a fundamentals miss. The one predicate the sync's
    work list and `/status`'s stale count both apply."""
    until = fundamentals_backoff_until(security)
    return until is not None and now < until


def record_fundamentals_miss(security: Security, now: datetime) -> None:
    """Yahoo answered nothing usable, or failed with something not a rate limit."""
    security.fundamentals_miss_count = (security.fundamentals_miss_count or 0) + 1
    security.fundamentals_checked_at = now


def clear_fundamentals_backoff(security: Security) -> None:
    """A row was written or the mapping changed: the next pass asks again."""
    security.fundamentals_checked_at = None
    security.fundamentals_miss_count = None


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
        """The rows a sync would refresh (`_due`)."""
        return await self._due(older_than)

    async def count_stale_metrics(self, older_than: timedelta = STALE_AFTER) -> int:
        """
        How many rows a sync would refresh. Exists so `/status` cannot answer that
        question with a different query than the sync asks — which is exactly how the
        two came to use different thresholds. Counted off the same `_due` rows since the
        back-off (issue #66), so the two cannot part over a row backing off.
        """
        return len(await self._due(older_than))

    async def _due(self, older_than: timedelta) -> List[FundamentalMetrics]:
        """
        `_stale` rows, less a security waiting out a miss (`backing_off`). The back-off is
        computed per row, so it is applied here, after the query, rather than restated in
        SQL.
        """
        now = utcnow()
        result = await self.session.execute(
            select(FundamentalMetrics, Security)
            .join(Security, Security.id == FundamentalMetrics.security_id)
            .where(self._stale(older_than))
        )
        return [metrics for metrics, security in result.all() if not backing_off(security, now)]

    @staticmethod
    def _stale(older_than: timedelta):
        """
        A row a sync would refresh: older than `older_than`, of a security the sync walks
        (held and Yahoo-eligible). Without the scope, a sold position's row — never
        refreshed since 2026-10-17 — stayed in the count forever.
        """
        return and_(
            FundamentalMetrics.last_updated < utcnow() - older_than,
            FundamentalMetrics.security_id.in_(select(Security.id).where(sync_scope())),
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
