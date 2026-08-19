"""
A sync that cannot measure something must clear it, not leave yesterday's value.

`WatchlistRepository.update_cached_data` iterates `data.items()` and writes what it
finds, so a key the builder simply *omits* leaves the existing column untouched. Every
field in `sync_item`'s dict was assigned unconditionally except three — `rsi14`,
`pct_from_52w_high` and `pct_from_ma200`, each written only inside the guard that
computes it. `last_synced` is always written.

So a day where `.info` came back without `fiftyTwoWeekHigh`, or `history` came back
empty, republished the previous day's technicals under a fresh timestamp. There is
nothing to see: the value is plausible, the row says it was synced minutes ago, and no
warning is raised anywhere.

It also made a security disagree with itself. `_compute_buy_score` reads the *dict*, so
an absent `rsi14` scored the neutral 5 while the table went on displaying the stale 72.4
that would have scored 0 — the same number driving two answers.

Offline: `_fetch_ticker_data` is replaced, so nothing reaches Yahoo.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.watchlist_item import WatchlistItem
from app.services.watchlist_service import WatchlistService


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


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    """The 1-3s courtesy sleep is not what these tests measure."""
    import app.services.watchlist_service as module

    monkeypatch.setattr(module.random, "uniform", lambda *_: 0)


async def _seed_stale(db) -> WatchlistItem:
    """A row carrying yesterday's readings, as a real one would."""
    item = WatchlistItem(
        yahoo_ticker="TCK",
        rsi14=72.4,
        pct_from_52w_high=-8.1,
        pct_from_ma200=12.5,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@pytest.mark.asyncio
async def test_an_unmeasurable_technical_is_cleared_rather_than_left_standing(db, monkeypatch):
    """
    Yahoo answers with a bare quote and no history: none of the three can be computed,
    so all three must end up NULL rather than keeping yesterday's numbers.
    """
    item = await _seed_stale(db)
    service = WatchlistService(db)

    async def _fetch(_ticker):
        # info without fiftyTwoWeekHigh / twoHundredDayAverage, and no history at all.
        return {"currentPrice": 100.0}, None, None, None, None

    monkeypatch.setattr(service, "_fetch_ticker_data", _fetch)

    await service.sync_item("TCK", force=True)
    await db.refresh(item)

    assert item.rsi14 is None, "yesterday's RSI survived a sync that could not compute one"
    assert item.pct_from_52w_high is None
    assert item.pct_from_ma200 is None
    assert item.last_synced is not None, "the row was still stamped as synced"


@pytest.mark.asyncio
async def test_a_measurable_technical_is_still_written(db, monkeypatch):
    """
    The mirror case, so the fix cannot be "write None always". A sync that *can* compute
    %-from-high must store it.
    """
    item = await _seed_stale(db)
    service = WatchlistService(db)

    async def _fetch(_ticker):
        return (
            {"currentPrice": 90.0, "fiftyTwoWeekHigh": 100.0, "twoHundredDayAverage": 80.0},
            None, None, None, None,
        )

    monkeypatch.setattr(service, "_fetch_ticker_data", _fetch)

    await service.sync_item("TCK", force=True)
    await db.refresh(item)

    assert item.pct_from_52w_high == pytest.approx(-10.0)
    assert item.pct_from_ma200 == pytest.approx(12.5)
    # Still no history, so RSI remains genuinely unmeasurable.
    assert item.rsi14 is None
