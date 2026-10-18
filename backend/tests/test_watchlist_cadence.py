"""
The watchlist's slow fields are fetched weekly, and a held ticker's RSI reads the stored
closes (GitHub issue #11, item d).

Each nightly run used to fetch `.info`, `history("1y")`, `quarterly_financials` and the
estimates for every item. Statements and estimates change a few times a quarter, and a
held ticker's closes are already in `market_prices`. Offline: `yf.Ticker` is a stub that
records every read.
"""
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.services.watchlist_service as watchlist_module
from app.clock import utc_today, utcnow
from app.database import Base
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.watchlist_item import WatchlistItem
from app.services.watchlist_service import WatchlistService

SLOW = {"quarterly_financials", "revenue_estimate"}
CLOSES = [100 + 3 * np.sin(i / 2) + i * 0.2 for i in range(40)]


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
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


@pytest.fixture
def reads(monkeypatch):
    seen = []

    class _Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        @property
        def info(self):
            seen.append("info")
            return {"quoteType": "EQUITY", "symbol": self.ticker, "trailingPE": 30.0,
                    "currentPrice": 100.0, "revenueGrowth": 0.99, "earningsGrowth": 0.99}

        def history(self, period=None):
            seen.append("history")
            return pd.DataFrame({"Close": [200.0 + i for i in range(30)]})

        @property
        def quarterly_financials(self):
            seen.append("quarterly_financials")
            return None

        @property
        def revenue_estimate(self):
            seen.append("revenue_estimate")
            return pd.DataFrame({"growth": [0.05, 0.12]}, index=["0y", "+1y"])

    monkeypatch.setattr(watchlist_module.yf, "Ticker", _Ticker)
    monkeypatch.setattr(watchlist_module.random, "uniform", lambda *_a, **_kw: 0)
    return seen


async def _item(db, ticker="TCK", estimates_age=None):
    item = WatchlistItem(
        yahoo_ticker=ticker, revenue_growth=0.21, earnings_growth=0.34,
        fwd_revenue_growth=0.15, fwd_eps_growth=0.5,
        estimates_synced_at=None if estimates_age is None else utcnow() - estimates_age,
    )
    db.add(item)
    await db.commit()
    return item


async def _hold(db, symbol, closes):
    security = Security(
        isin="US0000000099", symbol=symbol, description=symbol, currency="USD",
        conid=9999, asset_category="STK", exchange="NASDAQ",
    )
    db.add(security)
    await db.flush()
    db.add(TaxLot(
        security_id=security.id, open_date=date(2025, 1, 2), is_open=True,
        quantity=Decimal("1"), cost_basis=Decimal("10"), price_per_unit=Decimal("10"),
        currency="USD", cost_basis_eur=Decimal("9"),
    ))
    start = utc_today() - timedelta(days=len(closes))
    for i, close in enumerate(closes):
        db.add(MarketPrice(
            security_id=security.id, date=start + timedelta(days=i),
            close_price=Decimal(str(round(close, 4))), currency="USD", source="yahoo_finance",
        ))
    await db.commit()


@pytest.mark.asyncio
async def test_a_new_item_is_fetched_in_full_and_stamped(db, reads):
    item = await _item(db, estimates_age=None)

    await WatchlistService(db).sync_item("TCK", force=True)
    await db.refresh(item)

    assert SLOW <= set(reads) and "history" in reads
    assert item.estimates_synced_at is not None
    assert item.fwd_revenue_growth == 0.12


@pytest.mark.asyncio
async def test_a_fast_run_reads_info_only_and_keeps_the_slow_fields(db, reads):
    item = await _item(db, estimates_age=timedelta(days=2))
    stamp = item.estimates_synced_at

    await WatchlistService(db).sync_item("TCK", force=True)
    await db.refresh(item)

    assert not SLOW & set(reads), reads
    assert reads.count("info") == 1
    # Never nulled, never overwritten from .info's fallbacks.
    assert (item.revenue_growth, item.earnings_growth) == (0.21, 0.34)
    assert (item.fwd_revenue_growth, item.fwd_eps_growth) == (0.15, 0.5)
    assert item.estimates_synced_at == stamp
    # PEG and the score from the stored slow fields: 30 / 50%.
    assert item.peg_ratio == pytest.approx(0.6)
    assert item.buy_score is not None


@pytest.mark.asyncio
async def test_the_slow_fields_are_fetched_again_after_the_interval(db, reads):
    item = await _item(db, estimates_age=WatchlistService.ESTIMATES_REFRESH_AFTER + timedelta(minutes=1))

    await WatchlistService(db).sync_item("TCK", force=True)
    await db.refresh(item)

    assert SLOW <= set(reads)
    assert utcnow() - item.estimates_synced_at < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_a_held_tickers_rsi_reads_the_stored_closes(db, reads):
    await _hold(db, "HELD", CLOSES)
    item = await _item(db, "HELD", estimates_age=timedelta(days=1))

    service = WatchlistService(db)
    await service.sync_item("HELD", force=True)
    await db.refresh(item)

    assert "history" not in reads
    expected = service._compute_rsi(np.array([round(c, 4) for c in CLOSES]), period=14)
    assert item.rsi14 == expected


@pytest.mark.asyncio
async def test_a_held_ticker_with_too_few_stored_closes_still_asks(db, reads):
    await _hold(db, "HELD", CLOSES[:10])
    await _item(db, "HELD", estimates_age=timedelta(days=1))

    await WatchlistService(db).sync_item("HELD", force=True)

    assert "history" in reads
