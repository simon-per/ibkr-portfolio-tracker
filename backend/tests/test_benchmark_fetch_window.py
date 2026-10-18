"""
The benchmark asks a provider only for what it can serve and the hypothetical can read
(GitHub issue #57).

`_missing_business_days` counts every weekday of a window as missing until a row exists,
and the public `GET /api/portfolio/benchmark` accepts any window inside 1900..2199. So
`start_date=1900-01-01&end_date=1904-12-30`, looped over the eight benchmarks, cost
eight Yahoo requests every five minutes — each holding SYNC_PIPELINE — for prices a
hypothetical that holds nothing before the first contribution can never use. The fetch
is now clamped to `benchmark_fetch_window`: from the first contribution (less the
carry-back) to the last finished session.

Driven through the real route. Offline: `yf.Ticker` and the Frankfurter fetch are
recording fakes, and the database is a file of this test's own.
"""
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
import app.services.benchmark_service as bs
from app.clock import utc_today
from app.database import Base, get_db
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.benchmark_service import (
    BENCHMARKS,
    benchmark_fetch_window,
    last_finished_session,
    reset_upstream_throttle,
)
from app.services.currency_service import CurrencyService
from app.services.portfolio_service import PRICE_LOOKBACK_DAYS

FIRST_LOT = utc_today() - timedelta(days=40)


@pytest.fixture
def yahoo(monkeypatch):
    """Every Yahoo request, answered with a bar for each weekday asked about."""
    asked = []

    class _Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, start, end, **_kw):
            asked.append((self.ticker, start, end))
            days = pd.bdate_range(start, pd.Timestamp(end) - pd.Timedelta(days=1))
            return pd.DataFrame({"Close": [100.0] * len(days)}, index=days)

    monkeypatch.setattr(bs.yf, "Ticker", _Ticker)
    monkeypatch.setattr(bs.random, "uniform", lambda *_a: 0)
    reset_upstream_throttle()
    yield asked
    reset_upstream_throttle()


@pytest.fixture
def frankfurter(monkeypatch):
    asked = []

    async def _record(self, from_currency, target_date, to_currency="EUR", days_back=30):
        asked.append((from_currency, target_date))

    monkeypatch.setattr(CurrencyService, "_batch_fetch_rates", _record)
    return asked


@pytest.fixture
def api(tmp_path):
    """One holding, bought 40 days ago; nothing cached."""
    path = (tmp_path / "benchmark.db").as_posix()
    seed = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(seed)
    with Session(seed) as session:
        session.add(Security(
            id=1, isin="DE0000000001", symbol="AAA", description="Test Co",
            currency="EUR", conid=100, asset_category="STK", exchange="XETRA",
        ))
        session.add(TaxLot(
            security_id=1, open_date=FIRST_LOT, quantity=Decimal("10"),
            cost_basis=Decimal("1000"), price_per_unit=Decimal("100"), currency="EUR",
            cost_basis_eur=Decimal("1000"), is_open=True,
        ))
        session.commit()
    seed.dispose()

    # NullPool: TestClient runs each request on an event loop of its own, and a pooled
    # aiosqlite connection belongs to the loop that opened it.
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)

    async def _session():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
            await session.commit()

    from app.main import app

    app.dependency_overrides[get_db] = _session
    # No context manager: that would run the lifespan and start the scheduler.
    yield TestClient(app)
    app.dependency_overrides.clear()


def _get(api, key, start, end, anchor="inception"):
    response = api.get(
        "/api/portfolio/benchmark",
        params={"benchmark": key, "anchor": anchor,
                "start_date": start.isoformat(), "end_date": end.isoformat()},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sweep(api, start, end):
    for key in BENCHMARKS:
        for anchor in ("inception", "window"):
            assert _get(api, key, start, end, anchor)["data"] == []
    reset_upstream_throttle()                      # the loop comes back five minutes later


def test_a_window_before_the_first_contribution_asks_no_provider(api, yahoo, frankfurter):
    """The issue's repro: every benchmark, both anchors, and the same again five
    minutes later, which used to ask all eight again."""
    for _ in range(2):
        _sweep(api, date(1900, 1, 1), date(1904, 12, 30))
    assert yahoo == []
    assert frankfurter == []


def test_a_window_after_the_last_session_asks_for_nothing_it_has(api, yahoo):
    """
    The other end of the range a route accepts. The inception series still reads every
    contribution since the first, so the first sweep may fetch that history — once per
    benchmark, what any chart load would — and never a day past the last finished
    session; the repeat finds it cached.
    """
    _sweep(api, date(2190, 1, 1), date(2194, 12, 30))
    first_sweep = len(yahoo)
    assert first_sweep <= len(BENCHMARKS)
    newest = (last_finished_session(utc_today()) + timedelta(days=1)).isoformat()
    assert all(exclusive_end <= newest for _t, _s, exclusive_end in yahoo)

    _sweep(api, date(2190, 1, 1), date(2194, 12, 30))
    assert len(yahoo) == first_sweep


def test_an_ordinary_read_fetches_a_missing_recent_close_once(api, yahoo):
    """
    The acceptance criterion's other half. Asked once, through the last finished
    session — never today's bar, which is missing until the close however often it is
    requested — and then not again, inside the per-ticker memo or after it.
    """
    today = utc_today()
    data = _get(api, "dax", today - timedelta(days=30), today)["data"]
    assert data, "the chart must still draw"
    assert len(yahoo) == 1
    _ticker, _start, exclusive_end = yahoo[0]
    assert exclusive_end <= (last_finished_session(today) + timedelta(days=1)).isoformat()

    _get(api, "dax", today - timedelta(days=30), today)
    reset_upstream_throttle()                      # five minutes later
    _get(api, "dax", today - timedelta(days=30), today)
    assert len(yahoo) == 1, "a final close was asked for again"


def test_the_window_reaches_back_only_as_far_as_the_first_contribution_can_read():
    """Unit form of the clamp, for the boundaries the route test cannot place exactly."""
    today = utc_today()
    session = last_finished_session(today)
    first = session - timedelta(days=100)

    assert benchmark_fetch_window(first - timedelta(days=900), today, first) == (
        first - timedelta(days=PRICE_LOOKBACK_DAYS), session,
    )
    assert benchmark_fetch_window(first + timedelta(days=10), today, first) == (
        first + timedelta(days=10), session,
    )
    assert benchmark_fetch_window(first - timedelta(days=900), first - timedelta(days=800), first) is None
    assert benchmark_fetch_window(today, today, first) is None, "today has no finished session"
    assert benchmark_fetch_window(first, today, None) is None, "nothing invested, nothing to price"
