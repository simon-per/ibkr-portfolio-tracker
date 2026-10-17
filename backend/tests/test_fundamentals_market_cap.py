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
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.models.watchlist_item import WatchlistItem
from app.services.market_cap import (
    MARKET_CAP_CURRENCIES,
    QUOTED_REFRESH_DAYS,
    market_cap_to_base,
    quoted_currencies,
    quoted_currencies_to_warm,
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

    to_base = await market_cap_to_base(db)

    assert await to_base(1_000, "USD") == 846
    assert await to_base(1_000, "EUR") == 940
    assert await to_base(1_000, "CHF") == 1_000


@pytest.mark.asyncio
@pytest.mark.parametrize("currency", ["GBp", "ZAc", "ILA"])
async def test_a_minor_unit_listing_is_absent_not_guessed(db, currency):
    """Whether Yahoo's marketCap is in pence or pounds there is unverified: 100x apart."""
    _rate(db, "GBP", "EUR", "1.17")
    await db.commit()

    to_base = await market_cap_to_base(db)

    assert await to_base(1_000, currency) is None


@pytest.mark.asyncio
async def test_a_missing_or_stale_rate_is_absent(db):
    _rate(db, "JPY", "EUR", "0.006", age_days=45)
    await db.commit()

    to_base = await market_cap_to_base(db)

    assert await to_base(1_000, "TWD") is None
    assert await to_base(1_000, "JPY") is None
    assert await to_base(None, "USD") is None
    assert await to_base(1_000, None) is None


# ---- The currencies the converter needs, kept in the cache (`sync_exchange_rates`) ----

async def _quoted_rows(db):
    db.add(Security(id=1, isin="KR7005930003", symbol="005930", description="Samsung",
                    currency="KRW", conid=1, asset_category="STK", exchange="KSE"))
    db.add(Security(id=2, isin="GB0000000001", symbol="LSE", description="London",
                    currency="GBP", conid=2, asset_category="STK", exchange="LSE"))
    db.add(FundamentalMetrics(security_id=1, data_currency="KRW", last_updated=utcnow()))
    db.add(FundamentalMetrics(security_id=2, data_currency="GBp", last_updated=utcnow()))
    db.add(WatchlistItem(yahoo_ticker="BHP.AX", data_currency="AUD"))
    db.add(WatchlistItem(yahoo_ticker="SAP.DE", data_currency="EUR"))
    await db.commit()


@pytest.mark.asyncio
async def test_the_quoted_currencies_are_every_served_cap_plus_the_safety_set(db):
    await _quoted_rows(db)

    quoted = await quoted_currencies(db)

    assert {"KRW", "AUD"} <= quoted
    assert MARKET_CAP_CURRENCIES <= quoted
    assert "EUR" not in quoted
    assert "GBp" not in quoted                      # a minor unit is never converted


@pytest.mark.asyncio
async def test_a_quoted_currency_is_warmed_only_when_its_rate_is_days_old(db):
    """Frankfurter is one request per currency per call: a cap's currency nothing is
    held in must not cost one on each of the day's slots."""
    await _quoted_rows(db)
    today = utcnow().date()
    _rate(db, "AUD", "EUR", "0.6")                                   # fresh
    _rate(db, "JPY", "EUR", "0.006", age_days=QUOTED_REFRESH_DAYS)   # due
    await db.commit()

    to_warm = await quoted_currencies_to_warm(db, today, warmed_anyway={"KRW", "TWD"})

    assert "AUD" not in to_warm
    assert "JPY" in to_warm
    assert "CNY" in to_warm                                          # never fetched yet
    assert not {"KRW", "TWD"} & to_warm                              # warmed anyway
