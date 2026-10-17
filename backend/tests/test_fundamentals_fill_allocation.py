"""
The fundamentals pass writes the allocation columns from the `.info` it already holds
(GitHub issue #11, item c), so the allocation sync — three requests a security, run only
from the Allocation tab's button — has nothing left to ask about. Offline: the
fundamentals fetch is faked and the allocation sync's `yf.Ticker` is a recording stub.
"""
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.services.allocation_service as allocation_module
import app.services.fundamentals_service as fundamentals_module
from app.database import Base
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.allocation_service import AllocationService, asset_type_for_quote_type
from app.services.fundamentals_service import FundamentalsService

INFO = {
    "CO": {"quoteType": "EQUITY", "sector": "Technology", "industry": "Semiconductors",
           "country": "Taiwan", "trailingPE": 20.0},
    "MF": {"quoteType": "MUTUALFUND"},
    "IWDA": {"quoteType": "ETF"},
}
ISINS = {"CO": "US0000000001", "MF": "US0000000002", "IWDA": "IE00B4L5Y983"}


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    for i, symbol in enumerate(INFO):
        security = Security(
            isin=ISINS[symbol], symbol=symbol, description=symbol, currency="USD",
            conid=700 + i, asset_category="STK", exchange="NASDAQ",
        )
        session.add(security)
        await session.flush()
        session.add(TaxLot(
            security_id=security.id, open_date=date(2025, 1, 2), is_open=True,
            quantity=Decimal("1"), cost_basis=Decimal("10"), price_per_unit=Decimal("10"),
            currency="USD", cost_basis_eur=Decimal("9"),
        ))
    await session.commit()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    async def _fetch(_self, ticker):
        return INFO[ticker], None, None, None, None

    async def _ticker(_self, security, _market=None):
        return security.symbol

    monkeypatch.setattr(FundamentalsService, "_fetch_yahoo_data", _fetch)
    monkeypatch.setattr(FundamentalsService, "_get_yahoo_ticker", _ticker)
    for module in (fundamentals_module, allocation_module):
        monkeypatch.setattr(module.random, "uniform", lambda *_a, **_kw: 0)


async def _securities(db):
    db.expire_all()
    rows = (await db.execute(select(Security))).scalars().all()
    return {s.symbol: s for s in rows}


@pytest.mark.asyncio
async def test_one_fundamentals_pass_fills_the_allocation_columns(db):
    result = await FundamentalsService(db).sync_fundamentals_data()

    assert result["allocations_updated"] == 2
    sec = await _securities(db)
    co = sec["CO"]
    assert (co.asset_type, co.sector, co.industry, co.country) == (
        "Stock", "Technology", "Semiconductors", "Taiwan")
    assert co.allocation_last_updated is not None
    # A mutual fund is a fund, through the one classifier.
    assert sec["MF"].asset_type == "ETF"


@pytest.mark.asyncio
async def test_an_etf_mapped_isin_is_left_to_its_mapping(db):
    await FundamentalsService(db).sync_fundamentals_data()

    iwda = (await _securities(db))["IWDA"]
    assert iwda.allocation_last_updated is None
    assert iwda.sector is None and iwda.country is None


@pytest.mark.asyncio
async def test_the_allocation_sync_then_asks_yahoo_about_none_of_them(db, monkeypatch):
    asked = []

    class _Ticker:
        def __init__(self, ticker):
            asked.append(ticker)

        @property
        def info(self):
            return {}

    monkeypatch.setattr(allocation_module.yf, "Ticker", _Ticker)
    await FundamentalsService(db).sync_fundamentals_data()

    result = await AllocationService(db).sync_allocation_data()

    assert asked == []
    # Only the ETF-mapped ISIN was left, and its mapping answers without Yahoo.
    assert result["securities_processed"] == 1
    assert (await _securities(db))["IWDA"].asset_type == "ETF"


@pytest.mark.parametrize("quote_type, expected", [
    ("EQUITY", "Stock"), ("ETF", "ETF"), ("MUTUALFUND", "ETF"), (None, "Stock"),
])
def test_the_asset_type_follows_the_fund_classifier(quote_type, expected):
    assert asset_type_for_quote_type(quote_type) == expected
