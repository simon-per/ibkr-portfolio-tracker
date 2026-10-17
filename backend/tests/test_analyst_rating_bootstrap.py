"""
`sync_stale_ratings` has to be able to rate a security for the first time.

Its work list came from `get_stale_ratings`, which selects rating rows older than
the cutoff — rows that already **exist**. A security that had never been rated was
therefore invisible to it forever, and against an empty table it returned "No stale
ratings to update" and fetched nothing at all.

That is the shape CLAUDE.md describes for benchmarks — "only refreshes benchmarks
that *already have rows*, so ... a first-time selection [stays] empty forever" —
and `FundamentalsService.sync_stale_fundamentals` was cited here as the sibling that
"had it right" — it did not. Its union sat below a pre-filter with this same bug, fixed
on 2026-08-05; see `tests/test_fundamentals_bootstrap.py`.

Offline: `fetch_rating_for_security` is faked, so yfinance is never reached.
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
from app.models.analyst_rating import AnalystRating
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services import analyst_rating_service as ars_mod
from app.services.analyst_rating_service import AnalystRatingService


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
    async def _sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(ars_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(ars_mod.random, "uniform", lambda *_a: 0)


def add_security(session, security_id: int, symbol: str, held: bool = True) -> Security:
    """A security with one lot: open while held, closed once sold."""
    security = Security(
        id=security_id, isin=f"US000000000{security_id}", symbol=symbol,
        description=symbol, currency="USD", conid=security_id,
        asset_category="STK", exchange="NASDAQ",
    )
    session.add(security)
    session.add(TaxLot(
        security_id=security_id, open_date=date(2025, 1, 2),
        close_date=None if held else date(2026, 1, 2), is_open=held,
        quantity=Decimal("10"), cost_basis=Decimal("1000"),
        price_per_unit=Decimal("100"), currency="USD", cost_basis_eur=Decimal("900"),
    ))
    return security


def add_rating(session, security_id: int, age_days: int) -> AnalystRating:
    rating = AnalystRating(
        security_id=security_id, strong_buy=5, buy=2, hold=1, sell=0, strong_sell=0,
        last_updated=utcnow() - timedelta(days=age_days),
    )
    session.add(rating)
    return rating


@pytest.fixture
def fetched(monkeypatch):
    """Record which securities were fetched, without touching yfinance."""
    seen = []

    async def _fake_fetch(_self, security):
        seen.append(security.symbol)
        return {
            'security_id': security.id,
            'strong_buy': 1, 'buy': 0, 'hold': 0, 'sell': 0, 'strong_sell': 0,
        }

    monkeypatch.setattr(
        AnalystRatingService, "fetch_rating_for_security", _fake_fetch
    )
    return seen


@pytest.mark.asyncio
async def test_a_never_rated_security_is_picked_up(db, fetched):
    # The bug: this security has no rating row, so the stale query cannot see it.
    add_security(db, 1, "NEW")
    await db.commit()

    await AnalystRatingService(db).sync_stale_ratings()

    assert fetched == ["NEW"]


@pytest.mark.asyncio
async def test_an_empty_table_does_not_report_nothing_to_do(db, fetched):
    """The bootstrap failure: on a fresh install this path could never populate."""
    add_security(db, 1, "AAA")
    add_security(db, 2, "BBB")
    await db.commit()

    result = await AnalystRatingService(db).sync_stale_ratings()

    assert sorted(fetched) == ["AAA", "BBB"]
    assert result['securities_processed'] == 2


@pytest.mark.asyncio
async def test_a_stale_rating_is_still_refreshed(db, fetched):
    add_security(db, 1, "OLD")
    add_rating(db, 1, age_days=5)
    await db.commit()

    await AnalystRatingService(db).sync_stale_ratings()

    assert fetched == ["OLD"]


@pytest.mark.asyncio
async def test_a_fresh_rating_is_left_alone(db, fetched):
    # Bounded, not indiscriminate — this is still a *stale* sync.
    add_security(db, 1, "FRESH")
    add_rating(db, 1, age_days=1)
    await db.commit()

    result = await AnalystRatingService(db).sync_stale_ratings()

    assert fetched == []
    assert result['securities_processed'] == 0
    assert 'No stale or missing' in result['message']


@pytest.mark.asyncio
async def test_it_fetches_only_what_needs_it(db, fetched):
    add_security(db, 1, "FRESH")
    add_rating(db, 1, age_days=1)
    add_security(db, 2, "STALE")
    add_rating(db, 2, age_days=9)
    add_security(db, 3, "NEVER")
    await db.commit()

    await AnalystRatingService(db).sync_stale_ratings()

    assert sorted(fetched) == ["NEVER", "STALE"]


@pytest.mark.asyncio
async def test_a_sold_position_is_not_rated(db, fetched):
    """The scope of the fundamentals pass this runs after: held securities only."""
    add_security(db, 1, "HELD")
    add_security(db, 2, "SOLD", held=False)
    add_security(db, 3, "SOLD_STALE", held=False)
    add_rating(db, 3, age_days=9)
    await db.commit()

    await AnalystRatingService(db).sync_stale_ratings()
    await AnalystRatingService(db).sync_ratings_for_securities()

    assert fetched == ["HELD", "HELD"]


# --- "no rating" is an answer, and it is stamped -----------------------------------


def _yahoo_answers(monkeypatch, recommendations):
    """Stand in for `yf.Ticker`, so the real fetch path decides what is stored."""
    asked = []

    class FakeTicker:
        def __init__(self, symbol):
            asked.append(symbol)

        @property
        def recommendations(self):
            if isinstance(recommendations, Exception):
                raise recommendations
            return recommendations

    async def _ticker(_self, security):
        return security.symbol

    monkeypatch.setattr(ars_mod.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(AnalystRatingService, "_get_yahoo_ticker", _ticker)
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["empty", "no current period"])
async def test_no_coverage_is_stamped_so_the_next_pass_does_not_ask_again(db, monkeypatch, answer):
    """
    An ETF has no analysts. That answer used to be dropped without a row, so the
    security stayed "never rated" and every pass asked Yahoo again for it.
    """
    import pandas as pd

    frame = pd.DataFrame() if answer == "empty" else pd.DataFrame(
        {"period": ["-1m"], "strongBuy": [3], "buy": [1], "hold": [0], "sell": [0], "strongSell": [0]}
    )
    asked = _yahoo_answers(monkeypatch, frame)
    add_security(db, 1, "VWCE")
    await db.commit()

    result = await AnalystRatingService(db).sync_stale_ratings()

    rating = (await db.execute(
        AnalystRating.__table__.select().where(AnalystRating.security_id == 1)
    )).one()
    assert (rating.strong_buy, rating.buy, rating.hold, rating.sell, rating.strong_sell) == (0,) * 5
    assert rating.last_updated is not None
    assert result['no_rating'] == 1 and result['ratings_updated'] == 0
    assert result['errors'] == 0

    # Stamped: a second pass inside the staleness window does not reach Yahoo.
    await AnalystRatingService(db).sync_stale_ratings()
    assert asked == ["VWCE"]


@pytest.mark.asyncio
async def test_a_stored_no_rating_reads_as_no_rating(db):
    db.add(AnalystRating(security_id=1, strong_buy=0, buy=0, hold=0, sell=0, strong_sell=0,
                         last_updated=utcnow()))
    add_security(db, 1, "VWCE")
    await db.commit()

    rating = await AnalystRatingService(db).get_rating_for_security(1)
    assert rating.total_ratings == 0
    assert rating.consensus == "No Rating"


@pytest.mark.asyncio
async def test_a_failed_fetch_is_not_stamped(db, monkeypatch):
    """A failure is not an answer: no row, so the next pass asks again."""
    _yahoo_answers(monkeypatch, ValueError("Expecting value: line 1 column 1 (char 0)"))
    add_security(db, 1, "AAPL")
    await db.commit()

    result = await AnalystRatingService(db).sync_stale_ratings()

    assert (await AnalystRatingService(db).get_rating_for_security(1)) is None
    assert result['errors'] == 1 and result['no_rating'] == 0
