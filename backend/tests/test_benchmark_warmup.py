"""
The scheduled benchmark warm-up, driven end to end: the real job gate, the real
`sync_benchmark_prices`, the real `_ensure_prices_available`, an in-memory price table,
and only `yf.Ticker` stubbed (CLAUDE.md rule 1 — nothing here reaches Yahoo).

Until 2026-10-17 the warm-up never fetched anything (issue #4): it ran inside
`_gated_job`, which holds SYNC_PIPELINE, and the fetch entered SYNC_PIPELINE again, so
every ticker raised SyncBusy, was served from cache and was still counted as synced. The
tests that should have caught it stubbed `_ensure_prices_available` whole, or held the
gate from outside — each half was right on its own. These run the two halves together.
"""
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.services.benchmark_service as bs
import app.services.scheduler_service as sched_mod
from app.database import Base
from app.models.benchmark_price import BenchmarkPrice
from app.models.exchange_rate import ExchangeRate
from app.services.benchmark_service import (
    BenchmarkService, last_finished_session, reset_upstream_throttle,
)
from app.services.currency_service import CurrencyService
from app.services.scheduler_service import SchedulerService
from app.single_flight import SYNC_PIPELINE, is_running, single_flight

SESSION = last_finished_session(date.today())


@pytest.fixture(autouse=True)
def _clear_throttle():
    reset_upstream_throttle()
    yield
    reset_upstream_throttle()


class _Yahoo:
    """`yf.Ticker`, recording every construction. `answer` is what `history` does."""

    def __init__(self):
        self.calls = []
        self.answer = lambda ticker: pd.DataFrame()

    def __call__(self, ticker):
        outer = self

        class _Ticker:
            def history(self, **kwargs):
                outer.calls.append((ticker, kwargs))
                return outer.answer(ticker)

        return _Ticker()

    @property
    def tickers(self):
        return [t for t, _ in self.calls]


@pytest.fixture
def yahoo(monkeypatch):
    stub = _Yahoo()
    monkeypatch.setattr(bs.yf, "Ticker", stub)
    monkeypatch.setattr(bs.random, "uniform", lambda a, b: 0)
    return stub


def _bar(day: date, close: float = 5100.0):
    return pd.DataFrame({"Close": [close]}, index=pd.DatetimeIndex([pd.Timestamp(day)]))


@pytest_asyncio.fixture
async def db(monkeypatch):
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
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(sched_mod, "AsyncSessionLocal", factory)
    yield factory
    await engine.dispose()


async def _seed(factory, ticker: str, through: date, days: int = 14):
    """Every weekday in the fortnight up to and including ``through``."""
    async with factory() as s:
        d = through - timedelta(days=days)
        while d <= through:
            if d.weekday() < 5:
                s.add(BenchmarkPrice(ticker=ticker, date=d,
                                     close_price=Decimal("5000"), currency="USD"))
            d += timedelta(days=1)
        await s.commit()


async def _close_on(factory, ticker: str, day: date):
    async with factory() as s:
        row = (await s.execute(
            select(BenchmarkPrice).where(
                BenchmarkPrice.ticker == ticker, BenchmarkPrice.date == day
            )
        )).scalar_one_or_none()
        return None if row is None else row.close_price


async def _run_under_the_job_gate(svc: SchedulerService) -> dict:
    """What `_market_data_only_locked` does, reduced to the step under test."""
    return await svc._gated_job("market_data_only", svc.sync_benchmark_prices)


def test_the_last_finished_session_is_the_previous_weekday():
    assert last_finished_session(date(2026, 10, 13)) == date(2026, 10, 12)  # Tue -> Mon
    assert last_finished_session(date(2026, 10, 12)) == date(2026, 10, 9)   # Mon -> Fri
    assert last_finished_session(date(2026, 10, 11)) == date(2026, 10, 9)   # Sun -> Fri
    assert last_finished_session(date(2026, 10, 10)) == date(2026, 10, 9)   # Sat -> Fri


@pytest.mark.asyncio
async def test_the_warmup_reaches_yahoo_under_its_own_jobs_gate(db, yahoo):
    await _seed(db, "^GSPC", through=last_finished_session(SESSION))
    yahoo.answer = lambda t: _bar(SESSION)

    result = await _run_under_the_job_gate(SchedulerService())

    assert yahoo.tickers == ["^GSPC"], "the warm-up never reached Yahoo"
    assert await _close_on(db, "^GSPC", SESSION) == Decimal("5100.000000")
    assert result["status"] == "success"
    assert result["benchmarks_fetched"] == 1
    assert result["benchmark_prices_written"] == 1
    assert result["benchmarks_total"] == 1
    assert result["rate_limited"] is False
    assert "warnings" not in result
    assert not is_running(SYNC_PIPELINE)


@pytest.mark.asyncio
async def test_the_request_ends_at_the_last_finished_session(db, yahoo):
    """Today's bar is missing until the close: ask for it and every slot asks again."""
    await _seed(db, "^GSPC", through=last_finished_session(SESSION))
    yahoo.answer = lambda t: _bar(SESSION)

    await _run_under_the_job_gate(SchedulerService())

    (_, kwargs), = yahoo.calls
    # yfinance's `end` is exclusive: the last day asked for is the day before it.
    last_day_asked = date.fromisoformat(kwargs["end"]) - timedelta(days=1)
    assert last_day_asked == SESSION
    assert last_day_asked < date.today()


@pytest.mark.asyncio
async def test_with_only_today_missing_the_warmup_asks_nothing(db, yahoo):
    """A benchmark with nothing missing is checked, not fetched."""
    await _seed(db, "^GSPC", through=SESSION)

    result = await _run_under_the_job_gate(SchedulerService())

    assert yahoo.calls == []
    assert result["benchmarks_fetched"] == 0
    assert result["benchmark_prices_written"] == 0
    assert result["benchmarks_total"] == 1


@pytest.mark.asyncio
async def test_a_session_yahoo_has_no_bar_for_is_asked_once_not_every_slot(db, yahoo):
    """
    A holiday at the range end stays missing after Yahoo answers, and so does one inside
    the range until HOLIDAY_GRACE_DAYS. Without the per-session memo each of the day's
    six slots would ask again.
    """
    await _seed(db, "^GSPC", through=last_finished_session(SESSION))
    svc = SchedulerService()

    first = await _run_under_the_job_gate(svc)
    reset_upstream_throttle()          # the next slot is hours later, not 300s
    second = await _run_under_the_job_gate(svc)

    assert yahoo.tickers == ["^GSPC"]
    assert first["benchmarks_fetched"] == 1      # asked and answered, with nothing
    assert first["benchmark_prices_written"] == 0
    assert second["benchmarks_fetched"] == 0


@pytest.mark.asyncio
async def test_a_rate_limit_stops_the_pass_at_the_first_benchmark(db, yahoo):
    for ticker in ("^GSPC", "^IXIC"):
        await _seed(db, ticker, through=last_finished_session(SESSION))

    def _refuse(ticker):
        raise Exception("HTTP Error 429: Too Many Requests")

    yahoo.answer = _refuse
    svc = SchedulerService()
    result = await _run_under_the_job_gate(svc)

    assert yahoo.tickers == ["^GSPC"], "a 429 must abandon the pass, not ask the next one"
    assert result["status"] == "success"          # the step ran; the job is not a failure
    assert result["rate_limited"] is True
    assert result["benchmarks_fetched"] == 0
    assert result["benchmarks_total"] == 2
    assert len(result["warnings"]) == 1
    assert "0 of 2" in result["warnings"][0]
    assert "Do not retry manually" in result["warnings"][0]

    # Nothing was answered, so nothing is marked asked: the next slot resumes both.
    yahoo.answer = lambda t: _bar(SESSION)
    reset_upstream_throttle()
    resumed = await _run_under_the_job_gate(svc)
    assert yahoo.tickers == ["^GSPC", "^GSPC", "^IXIC"]
    assert resumed["benchmarks_fetched"] == 2


@pytest.mark.asyncio
async def test_a_warmup_outside_the_gate_says_so_instead_of_skipping_quietly(db, yahoo):
    """
    It always runs under its job's gate. If that ever stops being true, `gate_held=True`
    is a false claim: refused before any request, and named on the result rather than
    logged as "serving cached prices" the way the SyncBusy skip was.
    """
    await _seed(db, "^GSPC", through=last_finished_session(SESSION))

    result = await SchedulerService().sync_benchmark_prices()

    assert yahoo.calls == []
    assert result["benchmarks_fetched"] == 0
    assert len(result["warnings"]) == 1
    assert "^GSPC" in result["warnings"][0]


# ── The attempt memo is stamped only when a request goes out ──────────────────


@pytest.mark.asyncio
async def test_a_busy_gate_does_not_throttle_the_next_price_fetch(db, yahoo):
    """
    The 300s memo was stamped before the gate, so a fetch the gate refused still
    throttled the next caller for five minutes over a request that never happened.
    """
    yahoo.answer = lambda t: _bar(SESSION)
    async with db() as s:
        svc = BenchmarkService(s)
        with single_flight(SYNC_PIPELINE):
            busy = await svc._ensure_prices_available("^GSPC", SESSION, SESSION)
        assert busy is None
        assert bs._last_upstream_attempt == {}

        written = await svc._ensure_prices_available("^GSPC", SESSION, SESSION)

    assert written == 1
    assert yahoo.tickers == ["^GSPC"]
    assert "price:^GSPC" in bs._last_upstream_attempt


@pytest.mark.asyncio
async def test_a_busy_gate_does_not_throttle_the_next_fx_fetch(db, monkeypatch):
    calls = []

    async def _record(self, from_currency, target_date, to_currency="EUR", days_back=30):
        calls.append(target_date)

    monkeypatch.setattr(CurrencyService, "_batch_fetch_rates", _record)
    async with db() as s:
        svc = BenchmarkService(s)
        with single_flight(SYNC_PIPELINE):
            await svc._ensure_fx_rates_available("USD", SESSION, SESSION)
        assert calls == []
        assert bs._last_upstream_attempt == {}

        await svc._ensure_fx_rates_available("USD", SESSION, SESSION)

    assert calls == [SESSION]
    assert "fx:USD" in bs._last_upstream_attempt


@pytest.mark.asyncio
async def test_claiming_a_gate_nobody_holds_is_refused(db, yahoo):
    async with db() as s:
        with pytest.raises(RuntimeError, match="not held"):
            await BenchmarkService(s)._ensure_prices_available(
                "^GSPC", SESSION, SESSION, gate_held=True
            )
    assert yahoo.calls == []
    assert bs._last_upstream_attempt == {}
