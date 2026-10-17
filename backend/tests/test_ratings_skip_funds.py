"""
The ratings pass never asks Yahoo about a fund (GitHub issue #11, item e).

A fund always came back `no_rating`, and the 3-day staleness is shorter than the pass
interval, so every held fund cost a `recommendations` request on every pass. A fund is
known without Yahoo: the stored `fundamental_metrics.quote_type` (FUND_QUOTE_TYPES) or an
ISIN the ETF table maps. Offline: `yf.Ticker` is a recording stub.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.services.analyst_rating_service as ratings_module
from app.clock import utcnow
from app.database import Base
from app.models.analyst_rating import AnalystRating
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.analyst_rating_service import AnalystRatingService

# symbol -> (isin, stored quote_type or None)
HOLDINGS = {
    "CO": ("US0000000001", "EQUITY"),
    "NEWCO": ("US0000000002", None),        # bought today, no fundamentals yet: asked
    "ETF": ("US0000000003", "ETF"),
    "MF": ("US0000000004", "MUTUALFUND"),   # what `is_etf` used to read as a company
    "IWDA": ("IE00B4L5Y983", None),          # an ISIN the ETF table maps
}


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    for i, (symbol, (isin, quote_type)) in enumerate(HOLDINGS.items()):
        security = Security(
            isin=isin, symbol=symbol, description=symbol, currency="USD",
            conid=500 + i, asset_category="STK", exchange="NASDAQ",
        )
        session.add(security)
        await session.flush()
        session.add(TaxLot(
            security_id=security.id, open_date=date(2025, 1, 2), is_open=True,
            quantity=Decimal("1"), cost_basis=Decimal("10"), price_per_unit=Decimal("10"),
            currency="USD", cost_basis_eur=Decimal("9"),
        ))
        if quote_type:
            session.add(FundamentalMetrics(
                security_id=security.id, quote_type=quote_type, last_updated=utcnow(),
            ))
    await session.commit()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


@pytest.fixture
def asked(monkeypatch):
    calls = []

    class _Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        @property
        def recommendations(self):
            calls.append(self.ticker)
            return pd.DataFrame([{"period": "0m", "strongBuy": 3, "buy": 5, "hold": 2,
                                  "sell": 0, "strongSell": 0}])

    async def _ticker(self, security):
        return security.symbol

    monkeypatch.setattr(ratings_module.yf, "Ticker", _Ticker)
    monkeypatch.setattr(ratings_module.random, "uniform", lambda *_a, **_kw: 0)
    monkeypatch.setattr(AnalystRatingService, "_get_yahoo_ticker", _ticker)
    return calls


async def _consensus(db):
    db.expire_all()
    rows = (await db.execute(select(AnalystRating, Security.symbol).join(Security))).all()
    return {symbol: rating.consensus for rating, symbol in rows}


@pytest.mark.asyncio
async def test_a_stored_fund_is_never_asked_and_still_reads_no_rating(db, asked):
    result = await AnalystRatingService(db).sync_stale_ratings()

    assert sorted(asked) == ["CO", "NEWCO"]
    assert result["funds_not_asked"] == 3
    consensus = await _consensus(db)
    assert {consensus[s] for s in ("ETF", "MF", "IWDA")} == {"No Rating"}
    assert consensus["CO"] != "No Rating"


@pytest.mark.asyncio
async def test_a_stale_fund_row_is_re_stamped_without_a_request(db, asked):
    await AnalystRatingService(db).sync_stale_ratings()
    stale = utcnow() - timedelta(days=4)
    for rating in (await db.execute(select(AnalystRating))).scalars():
        rating.last_updated = stale
    await db.commit()
    asked.clear()

    await AnalystRatingService(db).sync_stale_ratings()

    assert sorted(asked) == ["CO", "NEWCO"]
    db.expire_all()
    stamps = (await db.execute(select(AnalystRating.last_updated))).scalars().all()
    assert all(s > stale for s in stamps), "a fund row kept its stale stamp"


@pytest.mark.asyncio
async def test_an_explicit_id_list_skips_funds_too(db, asked):
    ids = (await db.execute(select(Security.id).order_by(Security.id))).scalars().all()
    await AnalystRatingService(db).sync_ratings_for_securities(list(ids))
    assert sorted(asked) == ["CO", "NEWCO"]


def test_the_model_fund_test_is_the_shared_one():
    assert FundamentalMetrics(quote_type="MUTUALFUND").is_fund
    assert FundamentalMetrics(quote_type="ETF").is_fund
    assert not FundamentalMetrics(quote_type="EQUITY").is_fund
    assert not hasattr(FundamentalMetrics, "is_etf")
