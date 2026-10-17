"""
The Fundamentals tab describes what is **held**, not every security ever traded.

`get_fundamentals_for_portfolio` read every `Security` row until 2026-10-17, so a
position sold long ago stayed on the tab as a row of dashes, and `/status` counted it
among the "holdings" with no data yet — a warning about something the portfolio no
longer owns. The earnings lists had the same scope. All four reads now share one
predicate, `security_repository.held`: at least one open lot, in any account.

Offline: nothing here syncs, so yfinance is never reached.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.clock import utcnow
from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.earnings_event import EarningsEvent
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.routers.fundamentals import get_fundamentals_status
from app.services.fundamentals_service import FundamentalsService

HELD, SOLD, HELD_NO_DATA = 1, 2, 3


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
    now = utcnow()
    for sid, symbol in ((HELD, "HELD"), (SOLD, "SOLD"), (HELD_NO_DATA, "NEW")):
        session.add(Security(
            id=sid, isin=f"US000000000{sid}", symbol=symbol, description=symbol,
            currency="USD", conid=sid, asset_category="STK", exchange="NASDAQ",
        ))
    opened = date.today() - timedelta(days=400)
    for sid, closed in ((HELD, None), (SOLD, date.today() - timedelta(days=90)),
                        (HELD_NO_DATA, None)):
        session.add(TaxLot(
            security_id=sid, open_date=opened, close_date=closed, is_open=closed is None,
            quantity=Decimal("1"), cost_basis=Decimal("100"), price_per_unit=Decimal("100"),
            currency="USD", cost_basis_eur=Decimal("90"),
        ))
    for sid in (HELD, SOLD):
        session.add(FundamentalMetrics(
            security_id=sid, trailing_pe=20.0, quote_type="EQUITY", last_updated=now,
        ))
        session.add(EarningsEvent(
            security_id=sid, earnings_date=now + timedelta(days=10), eps_estimate=1.0,
            is_upcoming=True,
        ))
        session.add(EarningsEvent(
            security_id=sid, earnings_date=now - timedelta(days=30), eps_estimate=1.0,
            reported_eps=1.1, surprise_percent=10.0,
        ))
    await session.commit()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_table_lists_held_securities_only(db):
    rows = await FundamentalsService(db).get_fundamentals_for_portfolio()
    assert sorted(r["symbol"] for r in rows) == ["HELD", "NEW"]
    # A holding with no stored metrics is still named, its figures absent.
    new = next(r for r in rows if r["symbol"] == "NEW")
    assert new["trailing_pe"] is None


@pytest.mark.asyncio
async def test_both_earnings_lists_leave_out_a_sold_position(db):
    service = FundamentalsService(db)
    assert [e["symbol"] for e in await service.get_earnings_calendar()] == ["HELD"]
    assert [e["symbol"] for e in await service.get_earnings_history()] == ["HELD"]


@pytest.mark.asyncio
async def test_status_counts_holdings_without_data_not_every_security(db):
    status = await get_fundamentals_status(db=db)
    assert status["total_securities"] == 2
    assert status["securities_with_data"] == 1
    # NEW, and not SOLD — which has data anyway, and is not a holding either way.
    assert status["securities_without_data"] == 1
