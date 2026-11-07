"""
The analyst ratings pass backs off a security whose fetch keeps failing.

A fetch that failed with something not a rate limit returned None and the loop left the
security unstamped "so the next pass asks again" — every pass, for good, about a ticker
Yahoo cannot serve: CLAUDE.md's "bound every retry and every re-ask". It now goes through
the shared `miss_backoff` rule on its own pair of columns, as prices and fundamentals do:
an error is a miss, a stored answer clears it, a rate limit stamps nothing, and `/status`
counts through the same predicate the work list applies.

Offline: `yf.Ticker` is patched, so the real error path through
`fetch_rating_for_security` stays in the picture.
"""

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

import app.models  # noqa: F401  register all mappers
import app.services.analyst_rating_service as ratings_module
from app.clock import utcnow
from app.models.analyst_rating import AnalystRating
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.analyst_rating_repository import AnalystRatingRepository
from app.services.analyst_rating_service import AnalystRatingService


async def _no_sleep(*_a, **_kw):
    return None


class _RateLimited(Exception):
    def __str__(self):
        return "429 Too Many Requests"


async def _held(db, **stamps):
    security = Security(
        isin="US0000000099",
        symbol="FAIL",
        description="Fail Inc",
        currency="USD",
        conid=9099,
        asset_category="STK",
        exchange="NASDAQ",
        **stamps,
    )
    db.add(security)
    await db.flush()
    db.add(
        TaxLot(
            security_id=security.id,
            open_date=date(2025, 1, 2),
            is_open=True,
            quantity=Decimal("10"),
            cost_basis=Decimal("1000"),
            price_per_unit=Decimal("100"),
            currency="USD",
            cost_basis_eur=Decimal("900"),
        )
    )
    await db.commit()
    return security.id


def _ticker_raising(error, calls):
    class _Ticker:
        def __init__(self, ticker):
            calls.append(ticker)

        @property
        def recommendations(self):
            raise error

    return _Ticker


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(ratings_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(ratings_module.random, "uniform", lambda *_a, **_kw: 0)


@pytest.mark.asyncio
async def test_a_failing_security_is_asked_once_then_waits_out_its_backoff(
    db, monkeypatch
):
    security_id = await _held(db)
    calls = []
    monkeypatch.setattr(
        ratings_module.yf, "Ticker", _ticker_raising(ValueError("404 Not Found"), calls)
    )

    first = await AnalystRatingService(db).sync_stale_ratings()
    assert len(calls) == 1
    assert first["errors"] == 1
    security = await db.get(Security, security_id)
    await db.refresh(security)
    assert security.ratings_miss_count == 1
    assert security.ratings_checked_at is not None

    second = await AnalystRatingService(db).sync_stale_ratings()
    assert len(calls) == 1, "re-asked a security still backing off"
    assert second["backing_off"] == 1

    # One day, less the slack, after one miss: past it, the next pass asks again.
    security.ratings_checked_at = utcnow() - timedelta(days=2)
    await db.commit()
    await AnalystRatingService(db).sync_stale_ratings()
    assert len(calls) == 2
    await db.refresh(security)
    assert security.ratings_miss_count == 2


@pytest.mark.asyncio
async def test_a_rate_limit_stamps_nothing(db, monkeypatch):
    security_id = await _held(db)
    calls = []
    monkeypatch.setattr(
        ratings_module.yf, "Ticker", _ticker_raising(_RateLimited(), calls)
    )

    result = await AnalystRatingService(db).sync_stale_ratings()

    assert result["rate_limited"] is True
    security = await db.get(Security, security_id)
    await db.refresh(security)
    assert security.ratings_checked_at is None
    assert security.ratings_miss_count is None


@pytest.mark.asyncio
async def test_an_answer_clears_the_backoff(db, monkeypatch):
    # Three misses, the last ten days ago: a four-day back-off, long expired.
    security_id = await _held(
        db, ratings_miss_count=3, ratings_checked_at=utcnow() - timedelta(days=10)
    )
    frame = pd.DataFrame(
        [["0m", 5, 10, 3, 0, 0]],
        columns=["period", "strongBuy", "buy", "hold", "sell", "strongSell"],
    )

    class _Ticker:
        def __init__(self, ticker):
            self.recommendations = frame

    monkeypatch.setattr(ratings_module.yf, "Ticker", _Ticker)

    result = await AnalystRatingService(db).sync_stale_ratings()

    assert result["ratings_updated"] == 1
    security = await db.get(Security, security_id)
    await db.refresh(security)
    assert security.ratings_checked_at is None
    assert security.ratings_miss_count is None
    rating = await AnalystRatingRepository(db).get_by_security_id(security_id)
    assert (rating.strong_buy, rating.buy, rating.hold) == (5, 10, 3)


@pytest.mark.asyncio
async def test_status_and_the_sync_agree_on_a_stale_row_backing_off(db, monkeypatch):
    """A stale rating of a security waiting out a miss is neither fetched nor counted."""
    security_id = await _held(db, ratings_miss_count=1, ratings_checked_at=utcnow())
    db.add(
        AnalystRating(
            security_id=security_id,
            strong_buy=1,
            buy=1,
            hold=1,
            sell=0,
            strong_sell=0,
            last_updated=utcnow() - timedelta(days=5),
        )
    )
    await db.commit()
    calls = []
    monkeypatch.setattr(
        ratings_module.yf, "Ticker", _ticker_raising(ValueError("404 Not Found"), calls)
    )

    repo = AnalystRatingRepository(db)
    assert await repo.count_stale_ratings() == 0
    assert await repo.get_stale_ratings() == []
    await AnalystRatingService(db).sync_stale_ratings()
    assert calls == []
