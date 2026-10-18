"""
A recent close must be re-fetched, because the first row of the day was winning.

Yahoo answers a daily-interval request with a bar for the session *in progress*,
whose `Close` is merely the last trade so far. `get_missing_dates()` only ever
returned dates with no row at all, so whichever job wrote that date first owned it
permanently. On production that meant every European close was the 15:00 Berlin
mid-session price — Xetra and Euronext run to 17:30 — and Korea's alternated
between mid-session and final depending on whether the 08:00 or the 15:00 job
happened to land the row first. Read from `market_prices.created_at` on
2026-08-04, not inferred.

That makes this the *precondition* for the intraday market-data slots rather than a
separate cleanup: adding an earlier slot on top of the freeze would have pinned an
even earlier price, making the number worse rather than fresher. So these tests and
`test_market_data_repriced_at_least_every_three_hours_intraday` are two halves of
one change, and neither is safe alone.

Offline: in-memory SQLite, no network.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.benchmark_price import BenchmarkPrice
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.repositories.market_price_repository import MarketPriceRepository
from app.services.benchmark_service import (
    BenchmarkService,
    _missing_business_days,
    reset_upstream_throttle,
)
from app.services.currency_service import CurrencyService
from app.clock import utc_today

WINDOW = MarketPriceRepository.PROVISIONAL_PRICE_DAYS

# A Tuesday, so "three days back" stays inside one trading week and the weekend
# rules are exercised separately below.
AS_OF = date(2026, 8, 4)


async def _repo_with(dates):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[Security.__table__, MarketPrice.__table__]
            )
        )
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="A",
                         currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    for d in dates:
        session.add(MarketPrice(security_id=1, date=d, close_price=Decimal("10"),
                                currency="EUR", source="test"))
    await session.flush()
    return engine, session, MarketPriceRepository(session)


@pytest.mark.asyncio
async def test_todays_cached_close_is_refetched():
    """
    The whole bug in one assertion: a row already exists for today and must still be
    asked for, because it holds a mid-session price.
    """
    engine, session, repo = await _repo_with([AS_OF])
    try:
        missing = await repo.get_missing_dates(1, AS_OF, AS_OF, as_of=AS_OF)
        assert missing == [AS_OF]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_settled_close_further_back_is_left_alone():
    """
    The window has to end somewhere, or every sync re-downloads the full range — the
    cost the span-narrowing in `fetch_and_cache_prices` and the holiday rule both
    exist to avoid.
    """
    old = AS_OF - timedelta(days=WINDOW + 1)
    while old.weekday() >= 5:                  # land on a weekday to be unambiguous
        old -= timedelta(days=1)
    engine, session, repo = await _repo_with([old, AS_OF])
    try:
        missing = await repo.get_missing_dates(1, old, AS_OF, as_of=AS_OF)
        assert old not in missing
        assert AS_OF in missing
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_fridays_close_is_still_provisional_on_monday():
    """
    Why the window is three days rather than one. The 22:00 Berlin slot writes a US
    close within seconds of the bell, so Friday's row is the least settled one of the
    week and nothing runs again until Saturday. A one-day window would leave it frozen
    at whatever Yahoo had mid-print.
    """
    friday, monday = date(2026, 7, 31), date(2026, 8, 3)
    assert friday.weekday() == 4 and monday.weekday() == 0
    engine, session, repo = await _repo_with([friday, monday])
    try:
        missing = await repo.get_missing_dates(1, friday, monday, as_of=monday)
        assert friday in missing
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_only_asks_nothing_on_a_warm_saturday():
    """
    The weekend full sync's mode (GitHub issue #9). On a Saturday the window re-reports
    Wednesday to Friday even when cached, so every weekend pass cost a request per
    security for rows no exchange could still move. `refresh_provisional=False` drops
    the window and keeps the gap rules: a weekday with no row is still missing.
    """
    wed, thu, fri, sat = (date(2026, 10, 14) + timedelta(days=i) for i in range(4))
    assert sat.weekday() == 5
    engine, session, repo = await _repo_with([wed, thu, fri])
    try:
        assert await repo.get_missing_dates(1, wed, sat, as_of=sat) == [wed, thu, fri]
        assert await repo.get_missing_dates(
            1, wed, sat, as_of=sat, refresh_provisional=False,
        ) == []
        # A new security, or a purge, still gets its history.
        assert await repo.get_missing_dates(
            1, wed - timedelta(days=2), sat, as_of=sat, refresh_provisional=False,
        ) == [wed - timedelta(days=2), wed - timedelta(days=1)]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_refresh_window_cannot_resurrect_an_old_holiday():
    """
    The two rules are disjoint by construction — the holiday rule needs a date older
    than HOLIDAY_GRACE_DAYS (30) and this one needs it newer than 3 — and this pins
    that rather than trusting the arithmetic. A holiday coming back would restore the
    re-request-forever bug the grace window was added to kill.
    """
    week = [date(2026, 4, 20), date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 24)]
    holiday = date(2026, 4, 23)
    engine, session, repo = await _repo_with(week)
    try:
        missing = await repo.get_missing_dates(1, week[0], week[-1], as_of=AS_OF)
        assert holiday not in missing
        assert missing == []       # settled week, so still no Yahoo request at all
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_settled_close_overwrites_the_provisional_row():
    """
    Re-asking is only useful if the answer replaces what is stored. `bulk_create`
    upserts on (security_id, date), which is the mechanism the whole refresh rests on
    — an insert-if-absent would make the re-fetch a no-op and hide that in a green
    suite.
    """
    engine, session, repo = await _repo_with([AS_OF])
    try:
        written = await repo.bulk_create([{
            "security_id": 1, "date": AS_OF, "close_price": Decimal("11.50"),
            "currency": "EUR", "source": "yahoo_finance",
        }])
        await session.commit()
        assert written == 1

        rows = (await session.execute(
            select(MarketPrice).where(MarketPrice.date == AS_OF)
        )).scalars().all()
        assert len(rows) == 1, "a second row for the same date, not a restatement"
        assert rows[0].close_price == Decimal("11.500000")
    finally:
        await session.close()
        await engine.dispose()


# ── The benchmark side ────────────────────────────────────────────────────────
#
# Same freeze, but since GitHub issue #57 no benchmark path asks for a session in
# progress: a chart load and the warm-up both end at `last_finished_session`, so neither
# writes a provisional row and `_ensure_prices_available` refreshes none by default. The
# mechanism stays pinned here — opted into, it must restate the row, not duplicate it.


def _latest_trading_day() -> date:
    """
    The newest weekday on or before today — the one row that is always provisional.

    The helper reads `utc_today()` itself and takes no injectable clock, so a test
    has to anchor on the real calendar. Anchoring *here* rather than on a literal
    keeps the assertion the same shape every day of the week: this date is inside the
    3-day window whether today is a Tuesday (itself) or a Sunday (Friday, two back).
    """
    d = utc_today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def test_the_benchmark_helper_refreshes_the_trailing_days_when_asked():
    anchor = _latest_trading_day()
    assert _missing_business_days(
        anchor, anchor, {anchor}, refresh_provisional=True
    ) == {anchor}


def test_the_benchmark_helper_does_not_refresh_unless_asked():
    """
    Default off, because the FX caller shares this helper and must not opt in — see
    the test below for why.
    """
    anchor = _latest_trading_day()
    assert _missing_business_days(anchor, anchor, {anchor}) == set()


@pytest.fixture(autouse=True)
def _clear_throttle():
    reset_upstream_throttle()
    yield
    reset_upstream_throttle()


async def _benchmark_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[BenchmarkPrice.__table__, ExchangeRate.__table__]
            )
        )
    return engine, AsyncSession(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_a_provisional_refresh_restates_the_row_rather_than_duplicating_it(monkeypatch):
    """
    Opted into, the refresh's new value must *replace* the old row rather than collide
    with the unique (ticker, date). `session.add` would have raised here, which is why
    the write became an upsert.
    """
    import app.services.benchmark_service as bs
    import pandas as pd

    anchor = _latest_trading_day()

    class _Stub:
        def __init__(self, ticker):
            pass

        def history(self, **kwargs):
            return pd.DataFrame(
                {"Close": [5100.0]}, index=pd.DatetimeIndex([pd.Timestamp(anchor)])
            )

    monkeypatch.setattr(bs.yf, "Ticker", _Stub)
    monkeypatch.setattr(bs.random, "uniform", lambda a, b: 0)

    engine, session = await _benchmark_session()
    try:
        session.add(BenchmarkPrice(ticker="^GSPC", date=anchor,
                                   close_price=Decimal("5000"), currency="USD"))
        await session.flush()

        written = await BenchmarkService(session)._ensure_prices_available(
            "^GSPC", anchor, anchor, currency="USD", refresh_provisional=True,
        )
        assert written == 1

        rows = (await session.execute(
            select(BenchmarkPrice).where(BenchmarkPrice.date == anchor)
        )).scalars().all()
        assert len(rows) == 1, "a duplicate row rather than a restatement"
        assert rows[0].close_price == Decimal("5100.000000")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_scheduled_benchmark_warmup_does_not_spend_a_request_on_a_refresh(
    monkeypatch,
):
    """
    The Yahoo-budget half, and the reason the flag exists rather than being on
    everywhere.

    `sync_benchmark_prices` asks every warm benchmark — all eight on this account — 1-2s
    apart, through the last *finished* session only, so it never writes a provisional
    row. Refreshing there would spend a request per benchmark on every first slot of the
    day even when nothing is missing, to restate rows it did not write. Deleting the
    `refresh_provisional` argument at that call site is the easy "tidy-up" that would do
    it.
    """
    import app.services.benchmark_service as bs

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "the scheduled warm-up went to Yahoo for a date it already has"
        )

    monkeypatch.setattr(bs.yf, "Ticker", _forbidden)

    engine, session = await _benchmark_session()
    try:
        anchor = _latest_trading_day()
        session.add(BenchmarkPrice(ticker="^GSPC", date=anchor,
                                   close_price=Decimal("5000"), currency="USD"))
        await session.flush()

        written = await BenchmarkService(session)._ensure_prices_available(
            "^GSPC", anchor, anchor, currency="USD", refresh_provisional=False,
        )
        assert written is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_fx_path_stays_out_of_the_refresh(monkeypatch):
    """
    FX deliberately does NOT opt in, and this is the assertion that keeps it out.

    An ECB reference rate is published once a day, so there is no intraday rate to
    converge on, and `_batch_fetch_rates` dedups per row against what is already
    stored — it could not rewrite today's row even if asked. Opting in would spend a
    provider request on every chart load to change nothing, which is precisely the
    waste `_missing_business_days` was extracted to stop.
    """
    calls = []

    async def _record(self, from_currency, target_date, to_currency="EUR", days_back=30):
        calls.append(target_date)

    monkeypatch.setattr(CurrencyService, "_batch_fetch_rates", _record)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[BenchmarkPrice.__table__, ExchangeRate.__table__]
            )
        )
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        end = utc_today()
        start = end - timedelta(days=6)
        d = start
        while d <= end:
            if d.weekday() < 5:
                session.add(ExchangeRate(
                    date=d, from_currency="USD", to_currency="EUR",
                    rate=Decimal("0.92"), source="frankfurter",
                ))
            d += timedelta(days=1)
        await session.flush()

        await BenchmarkService(session)._ensure_fx_rates_available("USD", start, end)
        assert calls == [], f"a warm FX range still went upstream: {calls}"
    finally:
        await session.close()
        await engine.dispose()
