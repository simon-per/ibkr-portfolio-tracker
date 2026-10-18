"""
Two benchmark cache leaks, both offline:

- The FX prefetch tiled the range with `while current <= end: fetch(current);
  current += 30`, leaving up to 29 days uncovered at the tail — the newest
  chart points dropped past the carry-forward window and were recomputed on
  every request, never caching.
- expected_dates counted every weekday, so a market holiday kept the benchmark
  "missing" and every cold request re-hit Yahoo.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.benchmark_price import BenchmarkPrice
from app.models.exchange_rate import ExchangeRate
from app.services.benchmark_service import BenchmarkService, reset_upstream_throttle
from app.services.currency_service import CurrencyService
from app.single_flight import SYNC_PIPELINE, single_flight
from tests.memory_db import memory_engine


async def _session():
    engine = await memory_engine(tables=[BenchmarkPrice.__table__, ExchangeRate.__table__])
    return engine, AsyncSession(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _clear_throttle():
    """The attempt memo is process-lifetime state; tests must not inherit it."""
    reset_upstream_throttle()
    yield
    reset_upstream_throttle()


@pytest.mark.asyncio
async def test_fx_prefetch_covers_the_range_tail(monkeypatch):
    calls = []

    async def _record(self, from_currency, target_date, to_currency="EUR", days_back=30):
        calls.append((target_date, days_back))

    monkeypatch.setattr(CurrencyService, "_batch_fetch_rates", _record)
    engine, session = await _session()
    try:
        start, end = date(2026, 1, 5), date(2026, 3, 20)  # 74 days: not a 30-multiple
        await BenchmarkService(session)._ensure_fx_rates_available("USD", start, end)

        targets = sorted(t for t, _ in calls)
        assert targets[0] == start          # first chunk covers the lookback buffer
        assert targets[-1] == end           # the tail is covered — this was the hole
        gaps = [(b - a).days for a, b in zip(targets, targets[1:])]
        assert all(g <= 30 for g in gaps)   # no uncovered stretch in between
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_settled_benchmark_range_makes_no_yahoo_request(monkeypatch):
    import app.services.benchmark_service as bs

    def _forbidden(*args, **kwargs):
        raise AssertionError("Yahoo must not be called for a settled range")

    monkeypatch.setattr(bs.yf, "Ticker", _forbidden)

    engine, session = await _session()
    try:
        # A full trading week ~3 months back, Thursday missing (a holiday).
        week = [date(2026, 4, 20), date(2026, 4, 21), date(2026, 4, 22), date(2026, 4, 24)]
        for d in week:
            session.add(BenchmarkPrice(ticker="^GSPC", date=d,
                                       close_price=Decimal("5000"), currency="USD"))
        await session.flush()

        fetched = await BenchmarkService(session)._ensure_prices_available(
            "^GSPC", week[0], week[-1], currency="USD"
        )
        assert fetched is None  # Yahoo was not asked, which is not "answered with nothing"
    finally:
        await session.close()
        await engine.dispose()


# ── The public GET shares the sync gate ────────────────────────────────
#
# /api/portfolio/benchmark is public, unauthenticated and lazy-fetches Yahoo and
# Frankfurter on a cache miss. It was the one upstream-touching route with no
# gate at all: looping the eight benchmarks over the five-year span the route
# allows could run straight beside the 08:00 full_sync.


@pytest.mark.asyncio
async def test_a_cold_benchmark_fetch_does_not_run_beside_another_pipeline(monkeypatch):
    import app.services.benchmark_service as bs

    def _forbidden(*args, **kwargs):
        raise AssertionError("Yahoo must not be called while the pipeline is held")

    monkeypatch.setattr(bs.yf, "Ticker", _forbidden)

    engine, session = await _session()
    try:
        # Nothing cached at all, so this range is as cold as it gets.
        with single_flight(SYNC_PIPELINE):
            fetched = await BenchmarkService(session)._ensure_prices_available(
                "^GSPC", date(2026, 4, 20), date(2026, 4, 24), currency="USD"
            )
        assert fetched is None  # served from cache, not a 429 in the caller's face
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_cold_fx_span_does_not_run_beside_another_pipeline(monkeypatch):
    calls = []

    async def _record(self, from_currency, target_date, to_currency="EUR", days_back=30):
        calls.append(target_date)

    monkeypatch.setattr(CurrencyService, "_batch_fetch_rates", _record)
    engine, session = await _session()
    try:
        with single_flight(SYNC_PIPELINE):
            await BenchmarkService(session)._ensure_fx_rates_available(
                "USD", date(2026, 1, 5), date(2026, 3, 20)
            )
        assert calls == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_second_attempt_on_the_same_ticker_is_throttled(monkeypatch):
    """
    The gate serializes concurrent callers; it does not stop a sequential loop.
    Trailing weekdays the provider has no bar for stay missing by design, so the
    range end is otherwise re-requested on every single request, forever.
    """
    import app.services.benchmark_service as bs

    fetches = []

    class _Stub:
        def __init__(self, ticker):
            fetches.append(ticker)

        def history(self, **kwargs):
            import pandas as pd
            return pd.DataFrame()  # empty: nothing gets cached, so it stays missing

    monkeypatch.setattr(bs.yf, "Ticker", _Stub)
    monkeypatch.setattr(bs.random, "uniform", lambda a, b: 0)

    engine, session = await _session()
    try:
        svc = BenchmarkService(session)
        span = (date(2026, 4, 20), date(2026, 4, 24))
        for _ in range(4):
            await svc._ensure_prices_available("^GSPC", *span, currency="USD")
        assert fetches == ["^GSPC"], "a loop must not re-hit Yahoo for the same ticker"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_distinct_benchmarks_still_warm_independently(monkeypatch):
    """The throttle is per upstream target: eight benchmarks warming is legitimate."""
    import app.services.benchmark_service as bs

    fetches = []

    class _Stub:
        def __init__(self, ticker):
            fetches.append(ticker)

        def history(self, **kwargs):
            import pandas as pd
            return pd.DataFrame()

    monkeypatch.setattr(bs.yf, "Ticker", _Stub)
    monkeypatch.setattr(bs.random, "uniform", lambda a, b: 0)

    engine, session = await _session()
    try:
        svc = BenchmarkService(session)
        for ticker in ("^GSPC", "^IXIC", "^GDAXI"):
            await svc._ensure_prices_available(
                ticker, date(2026, 4, 20), date(2026, 4, 24), currency="USD"
            )
        assert fetches == ["^GSPC", "^IXIC", "^GDAXI"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_warm_fx_span_makes_no_provider_request(monkeypatch):
    """
    _batch_fetch_rates issues its request unconditionally (it dedups per row,
    after the response), so tiling five years cost ~60 requests on every chart
    load regardless of what was already cached.
    """
    calls = []

    async def _record(self, from_currency, target_date, to_currency="EUR", days_back=30):
        calls.append(target_date)

    monkeypatch.setattr(CurrencyService, "_batch_fetch_rates", _record)
    engine, session = await _session()
    try:
        start, end = date(2026, 1, 5), date(2026, 3, 20)
        d = start
        while d <= end:
            if d.weekday() < 5:
                session.add(ExchangeRate(from_currency="USD", to_currency="EUR",
                                         date=d, rate=Decimal("0.92")))
            d += timedelta(days=1)
        await session.flush()

        await BenchmarkService(session)._ensure_fx_rates_available("USD", start, end)
        assert calls == []
    finally:
        await session.close()
        await engine.dispose()
