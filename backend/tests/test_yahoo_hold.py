"""
A Yahoo refusal is remembered by the whole process, and across a restart (GitHub #57).

Until then the only memory of a 429 was a per-instance latch and a per-ticker 300 s memo.
After Yahoo refused the chart load for one benchmark, the next public
`GET /api/portfolio/benchmark` for another built a new `BenchmarkService` and asked at
once, and the first was asked again five minutes later: eight requests after the first
429 in the issue's repro, against rule 1's "stop, wait 30-60 minutes". Every pass's
behaviour under the hold is pinned in `test_yahoo_rate_limit_family.py`; this file pins
the hold itself.

Offline: `yf.Ticker` is a recording fake, the clock is a variable, and the persisted
stamp goes to an in-memory database of its own.
"""
import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.services.benchmark_service as bs
import app.services.yahoo_rate_limit as hold
from app.database import Base
from app.models.app_settings import AppSetting
from app.services.benchmark_service import BENCHMARKS, BenchmarkService, reset_upstream_throttle
from app.services.yahoo_rate_limit import (
    YAHOO_HOLD_SECONDS,
    YAHOO_REFUSED_AT_KEY,
    YahooHold,
    check_yahoo_hold,
    flush_hold_writes,
    is_rate_limit,
    reset_yahoo_hold,
    yahoo_hold_until,
)

T0 = datetime(2026, 3, 10, 14, 0)


class _Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(hold, "utcnow", c)
    return c


@pytest.fixture(autouse=True)
def _clear_throttle():
    reset_upstream_throttle()
    yield
    reset_upstream_throttle()


async def _settings_store(monkeypatch):
    """Where the stamp persists, for this test: `app_settings` and nothing else."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[AppSetting.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(hold, "_session_factory", lambda: factory)
    return engine, factory


async def _stored(factory):
    async with factory() as session:
        row = await session.get(AppSetting, YAHOO_REFUSED_AT_KEY)
        return row.value if row else None


# ── The issue's first repro ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_429_on_one_benchmark_holds_every_other_request(db, monkeypatch, clock):
    asked = []

    class _Refusing:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, *_a, **_kw):
            asked.append(self.ticker)
            # yfinance 1.1's YFRateLimitError text
            raise Exception("Too Many Requests. Rate limited. Try after a while.")

    monkeypatch.setattr(bs.yf, "Ticker", _Refusing)
    monkeypatch.setattr(bs.random, "uniform", lambda *_a: 0)
    end = date(2026, 3, 9)
    start = end - timedelta(days=30)

    # The chart with the S&P 500: Yahoo says 429.
    first = BenchmarkService(db)
    assert await first._ensure_prices_available("^GSPC", start, end) is None
    assert first.rate_limited is True
    assert asked == ["^GSPC"]

    # Seconds later the user switches benchmark: a new service per request, every key.
    for key, bench in BENCHMARKS.items():
        service = BenchmarkService(db)
        fetched = await service._ensure_prices_available(
            bench["ticker"], start, end, currency=bench["currency"]
        )
        assert fetched is None
        assert service.rate_limited is True, f"{key}: held, and it must say so"

    # Five minutes later, past the per-ticker memo; and at the last second of the hold.
    for after in (timedelta(minutes=5), timedelta(seconds=YAHOO_HOLD_SECONDS - 1)):
        reset_upstream_throttle()
        clock.now = T0 + after
        await BenchmarkService(db)._ensure_prices_available("^GSPC", start, end)
    assert asked == ["^GSPC"], "Yahoo was asked again inside the hold"

    # The hold ends on time: the first chart load after it is the next real request.
    reset_upstream_throttle()
    clock.now = T0 + timedelta(seconds=YAHOO_HOLD_SECONDS)
    await BenchmarkService(db)._ensure_prices_available("^GSPC", start, end)
    assert asked == ["^GSPC", "^GSPC"]


# ── The hold's own rules ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_holds_own_refusal_reads_as_a_rate_limit_but_does_not_extend_it(clock):
    """
    Every breaker tests `is_rate_limit`, so the hold has to pass it; the watchlist and
    allocation hand it the text rather than the exception. And re-stamping on it would
    renew the hold at every refused consult, so it would never end while anyone looked.
    """
    assert is_rate_limit("HTTP Error 429: Too Many Requests")
    until = yahoo_hold_until()
    assert until == T0 + timedelta(seconds=YAHOO_HOLD_SECONDS)

    clock.now = T0 + timedelta(minutes=30)
    with pytest.raises(YahooHold) as refused:
        await check_yahoo_hold()
    assert is_rate_limit(refused.value)
    assert is_rate_limit(str(refused.value))
    assert yahoo_hold_until() == until


def test_an_ordinary_error_starts_no_hold():
    """The predicate stays narrow (see its docstring), and so does what it records."""
    for error in ("404 Not Found", "Expecting value: line 1 column 1 (char 0)", "", None):
        assert not is_rate_limit(error)
    assert yahoo_hold_until() is None


# ── Persistence ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_hold_survives_a_restart(monkeypatch, clock):
    """A deploy restarts the container; the refusal it happened during still stands."""
    engine, factory = await _settings_store(monkeypatch)
    try:
        assert is_rate_limit("429 Client Error: Too Many Requests")
        await flush_hold_writes()
        assert await _stored(factory) == T0.isoformat()

        reset_yahoo_hold(reload=True)
        clock.now = T0 + timedelta(minutes=20)
        with pytest.raises(YahooHold):
            await check_yahoo_hold()

        clock.now = T0 + timedelta(seconds=YAHOO_HOLD_SECONDS)
        await check_yahoo_hold()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_refusal_seen_in_a_worker_thread_is_written_by_the_next_consult(
    monkeypatch, clock,
):
    """
    `yahoo_estimates.read_optional` asks the predicate inside `asyncio.to_thread`, where
    no event loop runs to write from. The memory stamp holds at once; the row follows.
    """
    engine, factory = await _settings_store(monkeypatch)
    try:
        assert await asyncio.to_thread(is_rate_limit, "429 Too Many Requests")
        await flush_hold_writes()
        assert await _stored(factory) is None

        with pytest.raises(YahooHold):
            await check_yahoo_hold()
        await flush_hold_writes()
        assert await _stored(factory) == T0.isoformat()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_stamp_from_the_future_holds_no_longer_than_one_hold(monkeypatch, clock):
    """A skewed clock or a restored copy must not hold Yahoo off for days."""
    engine, factory = await _settings_store(monkeypatch)
    try:
        async with factory() as session:
            session.add(AppSetting(
                key=YAHOO_REFUSED_AT_KEY, value=(T0 + timedelta(days=3)).isoformat(),
            ))
            await session.commit()
        reset_yahoo_hold(reload=True)

        with pytest.raises(YahooHold) as refused:
            await check_yahoo_hold()
        assert refused.value.until == T0 + timedelta(seconds=YAHOO_HOLD_SECONDS)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unreadable_stamp_is_ignored_rather_than_holding_forever(monkeypatch, clock):
    engine, factory = await _settings_store(monkeypatch)
    try:
        async with factory() as session:
            session.add(AppSetting(key=YAHOO_REFUSED_AT_KEY, value="not a time"))
            await session.commit()
        reset_yahoo_hold(reload=True)
        await check_yahoo_hold()
        assert yahoo_hold_until() is None
    finally:
        await engine.dispose()
