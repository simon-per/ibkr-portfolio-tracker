"""
get_missing_dates must stop asking Yahoo for market holidays.

A weekday the exchange never traded (July 4th, Good Friday) can never gain a
close price, so it used to stay "missing" forever — and one permanently missing
date makes fetch_and_cache_prices re-request the security's ENTIRE range on
every one of the five daily jobs, indefinitely, against an IP-based rate limit.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.repositories.market_price_repository import MarketPriceRepository
from tests.memory_db import memory_engine

AS_OF = date(2026, 7, 28)

# A Monday..Friday trading week with the Thursday missing (a "holiday"),
# ~3 months before AS_OF — well past the grace window.
WEEK = [date(2026, 4, 20), date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 24)]
HOLIDAY = date(2026, 4, 23)


def _weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


async def _repo_with(dates):
    engine = await memory_engine(tables=[Security.__table__, MarketPrice.__table__])
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="A",
                         currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    for d in dates:
        session.add(MarketPrice(security_id=1, date=d, close_price=Decimal("10"),
                                currency="EUR", source="test"))
    await session.flush()
    return engine, session, MarketPriceRepository(session)


@pytest.mark.asyncio
async def test_an_old_interior_hole_is_a_holiday_not_missing():
    engine, session, repo = await _repo_with(WEEK)
    try:
        missing = await repo.get_missing_dates(1, WEEK[0], WEEK[-1], as_of=AS_OF)
        assert HOLIDAY not in missing
        assert missing == []  # the whole week is settled -> no Yahoo request at all
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_recent_interior_hole_is_still_missing():
    # Same shape, but the hole is only a few days old — data may arrive late.
    recent_week = [AS_OF - timedelta(days=7 - i) for i in range(5)]
    recent_week = [d for d in recent_week if d.weekday() < 5]
    hole = recent_week.pop(1)
    engine, session, repo = await _repo_with(recent_week)
    try:
        missing = await repo.get_missing_dates(
            1, min(recent_week), max(recent_week), as_of=AS_OF
        )
        assert hole in missing
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_leading_and_trailing_gaps_stay_missing():
    # Leading = a purge/backfill window; trailing = new days. Both must refetch,
    # no matter how old — that is what heals a split purge.
    engine, session, repo = await _repo_with(WEEK)
    try:
        missing = await repo.get_missing_dates(
            1, WEEK[0] - timedelta(days=4), WEEK[-1] + timedelta(days=4), as_of=AS_OF
        )
        assert any(d < WEEK[0] for d in missing)
        assert any(d > WEEK[-1] for d in missing)
        assert HOLIDAY not in missing
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_empty_cache_reports_every_weekday_missing():
    engine, session, repo = await _repo_with([])
    try:
        missing = await repo.get_missing_dates(1, WEEK[0], WEEK[-1], as_of=AS_OF)
        assert missing == [date(2026, 4, 20), date(2026, 4, 21), date(2026, 4, 22),
                           HOLIDAY, date(2026, 4, 24)]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_only_the_missing_span_is_requested(monkeypatch):
    """
    The 08:00 job passes a 730-day window, so before this it re-downloaded two
    years per security every morning just because today's close had not
    published yet. Only the span that is actually missing is asked for.
    """
    from app.services.market_data_service import MarketDataService

    # The production shape: two years of history already cached, only the most
    # recent day or two outstanding because the close hasn't published.
    window_start = WEEK[-1] - timedelta(days=730)
    cached = [d for d in _weekdays(window_start, WEEK[-1])]
    engine, session, _ = await _repo_with(cached)
    try:
        asked = {}

        async def _fake_yahoo(self, security, start_date, end_date):
            asked['start'], asked['end'] = start_date, end_date
            return []

        async def _no_sleep(*_a, **_kw):
            return None

        monkeypatch.setattr(MarketDataService, "fetch_prices_from_yahoo", _fake_yahoo)
        monkeypatch.setattr("app.services.market_data_service.asyncio.sleep", _no_sleep)

        svc = MarketDataService(session)
        security = await session.get(Security, 1)
        end = WEEK[-1] + timedelta(days=3)
        await svc.fetch_and_cache_prices(security, start_date=window_start, end_date=end)

        # Days, not two years: the request starts near the outstanding dates.
        assert asked['start'] > WEEK[-1] - timedelta(days=30)
        assert asked['end'] == end
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_early_gap_still_pulls_the_whole_span(monkeypatch):
    """A split purge or a new security leaves the gap at the start — fetch it all."""
    from app.services.market_data_service import MarketDataService

    engine, session, _ = await _repo_with([WEEK[-1]])   # only the last day cached
    try:
        asked = {}

        async def _fake_yahoo(self, security, start_date, end_date):
            asked['start'] = start_date
            return []

        async def _no_sleep(*_a, **_kw):
            return None

        monkeypatch.setattr(MarketDataService, "fetch_prices_from_yahoo", _fake_yahoo)
        monkeypatch.setattr("app.services.market_data_service.asyncio.sleep", _no_sleep)

        start = WEEK[0] - timedelta(days=60)
        svc = MarketDataService(session)
        security = await session.get(Security, 1)
        await svc.fetch_and_cache_prices(security, start_date=start, end_date=WEEK[-1])

        assert asked['start'] == start   # clamped to the window, not narrowed away
    finally:
        await session.close()
        await engine.dispose()
