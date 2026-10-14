"""
`market_cap_base` — the figure the Fundamentals tab sorts market caps on.

The stored `market_cap` is in each listing's own currency, and the tab used to sort the
raw numbers: a 1-trillion-TWD company (~30 billion USD) ranked above a 500-billion-USD one.
The converted figure is served from cached rates only, and is **absent** rather than
wrong whenever it cannot be trusted. Offline: no provider is ever reached.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.clock import utcnow
from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.app_settings import AppSetting
from app.models.exchange_rate import ExchangeRate
from app.services.fundamentals_service import FundamentalsService


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


def _rate(session, from_currency, to_currency, rate, age_days=0):
    session.add(ExchangeRate(
        date=utcnow().date() - timedelta(days=age_days), from_currency=from_currency,
        to_currency=to_currency, rate=Decimal(rate), source="test",
    ))


@pytest.mark.asyncio
async def test_converts_through_eur_into_the_base(db):
    db.add(AppSetting(key="base_currency", value="CHF"))
    _rate(db, "USD", "EUR", "0.9")
    _rate(db, "EUR", "CHF", "0.94")
    await db.commit()

    to_base = await FundamentalsService(db)._market_cap_to_base()

    assert await to_base(1_000, "USD") == 846
    assert await to_base(1_000, "EUR") == 940
    assert await to_base(1_000, "CHF") == 1_000


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["GBp", "ZAc", "ILA"])
async def test_a_minor_unit_listing_is_absent_not_guessed(db, currency):
    """Whether Yahoo's marketCap is in pence or pounds there is unverified: 100x apart."""
    _rate(db, "GBP", "EUR", "1.17")
    await db.commit()

    to_base = await FundamentalsService(db)._market_cap_to_base()

    assert await to_base(1_000, currency) is None


@pytest.mark.asyncio
async def test_a_missing_or_stale_rate_is_absent(db):
    _rate(db, "JPY", "EUR", "0.006", age_days=45)
    await db.commit()

    to_base = await FundamentalsService(db)._market_cap_to_base()

    assert await to_base(1_000, "TWD") is None
    assert await to_base(1_000, "JPY") is None
    assert await to_base(None, "USD") is None
    assert await to_base(1_000, None) is None
