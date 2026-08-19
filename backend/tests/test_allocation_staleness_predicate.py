"""
`/api/allocation/status` must count the securities the sync would actually fetch.

It counted `allocation_last_updated < cutoff`. SQL comparison against NULL is not
true, so a security that has **never been attempted** was invisible to that query —
while `sync_allocation_data` explicitly included `allocation_last_updated is None` in
its own selection.

Nothing schedules the allocation sync (it reaches Yahoo, so it is a deliberate manual
step, and CLAUDE.md's troubleshooting table says so). On a fresh install, or for any
newly bought security, `allocation_last_updated` is therefore NULL — which is exactly
the population the status endpoint could not see. It answered `stale_securities: 0`
beside a sync about to issue one Yahoo request per security.

The comment above the query said it used "the same threshold the sync selects on".
That was true and beside the point: the *threshold* was shared through
`ALLOCATION_STALE_DAYS`, the *predicate* was not. CLAUDE.md records the identical
sentence for `stale_metrics: 0` on `/api/fundamentals/status`, including the tell —
a comment claiming alignment is worth checking rather than trusting.

Offline: seeded rows, no Yahoo.
"""

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.clock import utcnow
from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.security import Security
from app.services.allocation_service import (
    ALLOCATION_STALE_DAYS,
    AllocationService,
    needs_allocation_refresh,
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def _seed(db):
    """One of each state the predicate has to distinguish."""
    now = utcnow()
    rows = [
        # Never attempted — the case the old query could not see.
        Security(isin="X0000000001", symbol="NEW", exchange="NASDAQ", currency="USD", conid=9001, description="NEW Inc",
                 allocation_last_updated=None),
        # Attempted long ago.
        Security(isin="X0000000002", symbol="OLD", exchange="NASDAQ", currency="USD", conid=9002, description="OLD Inc",
                 allocation_last_updated=now - timedelta(days=ALLOCATION_STALE_DAYS + 1)),
        # Attempted recently — genuinely fresh.
        Security(isin="X0000000003", symbol="FRESH", exchange="NASDAQ", currency="USD", conid=9003, description="FRESH Inc",
                 allocation_last_updated=now - timedelta(hours=1)),
    ]
    for row in rows:
        db.add(row)
    await db.commit()


@pytest.mark.asyncio
async def test_a_never_attempted_security_counts_as_needing_a_refresh(db):
    """The whole bug in one assertion."""
    await _seed(db)
    cutoff = utcnow() - timedelta(days=ALLOCATION_STALE_DAYS)

    counted = (
        await db.execute(
            select(func.count(Security.id)).where(needs_allocation_refresh(cutoff))
        )
    ).scalar()

    assert counted == 2, "expected the never-attempted row and the stale one"

    # And the naive form is what it must not be — pinned so the reason survives the fix.
    naive = (
        await db.execute(
            select(func.count(Security.id)).where(Security.allocation_last_updated < cutoff)
        )
    ).scalar()
    assert naive == 1, (
        "sanity: the old predicate really does miss the NULL row, which is what made "
        "the endpoint report 0 while a full sync was pending"
    )


@pytest.mark.asyncio
async def test_the_status_count_equals_what_the_sync_would_fetch(db, monkeypatch):
    """
    The family form. Rather than asserting two literals agree, this drives the real
    sync and compares what it *selected* against what the status query counts — so a
    future change to either side has to keep them equal.
    """
    await _seed(db)
    service = AllocationService(db)

    attempted = []

    async def _fetch(security):
        attempted.append(security.symbol)
        return {"success": False, "error": "No data"}

    monkeypatch.setattr(service, "fetch_allocation_for_security", _fetch)
    monkeypatch.setattr(
        __import__("app.services.allocation_service", fromlist=["random"]).random,
        "uniform", lambda *_: 0,
    )

    cutoff = utcnow() - timedelta(days=ALLOCATION_STALE_DAYS)
    counted = (
        await db.execute(
            select(func.count(Security.id)).where(needs_allocation_refresh(cutoff))
        )
    ).scalar()

    await service.sync_allocation_data()

    assert sorted(attempted) == ["NEW", "OLD"], (
        f"the sync fetched {sorted(attempted)}, so the status count must match it"
    )
    assert counted == len(attempted)


@pytest.mark.asyncio
async def test_force_refresh_still_takes_everything(db, monkeypatch):
    """
    The predicate now drives selection, so the escape hatch has to be checked: a forced
    run must ignore it entirely rather than being narrowed by it.
    """
    await _seed(db)
    service = AllocationService(db)

    attempted = []

    async def _fetch(security):
        attempted.append(security.symbol)
        return {"success": False, "error": "No data"}

    monkeypatch.setattr(service, "fetch_allocation_for_security", _fetch)
    monkeypatch.setattr(
        __import__("app.services.allocation_service", fromlist=["random"]).random,
        "uniform", lambda *_: 0,
    )

    await service.sync_allocation_data(force_refresh=True)

    assert sorted(attempted) == ["FRESH", "NEW", "OLD"]
