"""
How often the dividend pass may ask Yahoo about a security (GitHub issue #6).

The pass is meant to ask about each security at most once a week. Its guard keyed on
`max(dividend_payments.created_at)` — set on insert only — so a non-payer, or a payer
between ex-dates, was asked on every pass, sold positions included. And
`GET /api/dividends/summary`, which is public, enqueued another pass whenever the card's
`last_updated` was a day old: a timestamp only a Flex re-delivery moves, never the Yahoo
pass. On a day with no dividend in the Flex window that was up to four passes a day on
top of 18:00's.

Now every answer stamps `securities.dividends_checked_at`, a rate limit stamps nothing,
only held securities are asked, and the loop and the route select through one predicate
(`needs_dividend_check`). Offline: `yf.Ticker` is a recording stub and the clock is
patched; nothing reaches the network.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.routers.dividends as div_router
import app.services.dividend_service as ds
import app.services.yahoo_rate_limit as yahoo_hold
import app.single_flight as sf
from app.clock import utcnow as real_utcnow
from app.database import Base
from app.models.dividend_payment import DividendPayment
from app.models.security import PRICE_SOURCE_MANUAL, Security
from app.models.taxlot import TaxLot
from app.services.dividend_service import DividendService

PAYER, NON_PAYER, SOLD, OFF_YAHOO = 1, 2, 3, 4
START = datetime(2026, 10, 5, 0, 0)  # a Monday, naive UTC like every stored stamp
SCHEDULED_HOUR_UTC = 16  # 18:00 Berlin (CEST): full_sync_job runs sync_dividend_data
PAYER_EX_DATES = [date(2025, 9, 1), date(2025, 12, 1), date(2026, 3, 2), date(2026, 6, 1)]


async def _session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)

    def _security(sid, symbol, **kw):
        session.add(Security(
            id=sid, isin=f"US000000000{sid}", symbol=symbol, description=symbol,
            currency="USD", conid=100 + sid, asset_category="STK", exchange="NASDAQ", **kw,
        ))

    def _lot(sid, is_open=True):
        session.add(TaxLot(
            security_id=sid, open_date=date(2025, 6, 2), quantity=Decimal("10"),
            cost_basis=Decimal("100"), cost_basis_eur=Decimal("100"),
            price_per_unit=Decimal("10"), currency="USD", is_open=is_open,
            close_date=None if is_open else date(2026, 6, 1),
        ))

    _security(PAYER, "PAY")
    _security(NON_PAYER, "ACC")
    _security(SOLD, "GONE")
    _security(OFF_YAHOO, "P3A", price_source=PRICE_SOURCE_MANUAL)
    for sid in (PAYER, NON_PAYER, OFF_YAHOO):
        _lot(sid)
    _lot(SOLD, is_open=False)

    # The payers' estimates are already stored, inserted months ago: Yahoo has nothing
    # new for either this week. Plus one IBKR row computed three days ago, so the card's
    # `last_updated` is stale — a week with no dividend inside the Flex window.
    for sid in (PAYER, SOLD):
        for ex in PAYER_EX_DATES:
            session.add(DividendPayment(
                security_id=sid, ex_date=ex, source="yfinance_estimate",
                amount_per_share=Decimal("1"), currency="USD",
                created_at=datetime(2026, 7, 1), last_computed=datetime(2026, 7, 1),
            ))
    session.add(DividendPayment(
        security_id=PAYER, ex_date=date(2026, 6, 15), source="ibkr", currency="USD",
        amount_per_share=Decimal("1"), shares_held=Decimal("10"),
        gross_amount_eur=Decimal("9"), withholding_tax_eur=Decimal("1"),
        net_amount_eur=Decimal("8"), created_at=datetime(2026, 7, 1),
        last_computed=real_utcnow() - timedelta(days=3),
    ))
    await session.commit()
    return engine, session


class _Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


def _install(monkeypatch, session, clock, answers):
    """Stub Yahoo, the clock, the background session and the pipeline cooldown."""
    asked = []

    class _Ticker:
        def __init__(self, ticker):
            asked.append(ticker)
            self.ticker = ticker

        @property
        def dividends(self):
            answer = answers[self.ticker]
            if isinstance(answer, Exception):
                raise answer
            return answer

    async def _no_sleep(*_a, **_kw):
        return None

    async def _ticker(self, security):
        return security.symbol

    async def _no_compute(self):
        return {}

    class _SharedSession:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(ds.yf, "Ticker", _Ticker)
    monkeypatch.setattr(ds.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(ds, "utcnow", clock)
    monkeypatch.setattr(div_router, "utcnow", clock)
    # The process-wide hold a 429 starts (issue #57) runs on the same simulated clock.
    monkeypatch.setattr(yahoo_hold, "utcnow", clock)
    monkeypatch.setattr(DividendService, "_get_yahoo_ticker", _ticker)
    monkeypatch.setattr(DividendService, "compute_dividend_income", _no_compute)
    monkeypatch.setattr(div_router, "AsyncSessionLocal", lambda: _SharedSession())
    monkeypatch.setattr(div_router, "_last_auto_sync", None)
    monkeypatch.setattr(div_router, "_sync_in_progress", False)
    return asked


def _payer_series():
    return pd.Series(
        [1.0] * len(PAYER_EX_DATES),
        index=pd.to_datetime([datetime(d.year, d.month, d.day) for d in PAYER_EX_DATES]),
    )


def _answers():
    return {"PAY": _payer_series(), "ACC": pd.Series([], dtype=float),
            "GONE": _payer_series(), "P3A": _payer_series()}


async def _load_summary(session):
    """One dashboard load: the route, then whatever it queued, as Starlette would."""
    tasks = BackgroundTasks()
    await div_router.get_dividend_summary(tasks, db=session)
    # Real time between loads is far more than the pipeline's 300 s cooldown.
    sf._last_start.clear()
    await tasks()
    return len(tasks.tasks)


async def _simulate(session, clock, hours, loads_per_hour=1):
    passes_enqueued = 0
    for h in range(hours):
        for k in range(loads_per_hour):
            clock.now = START + timedelta(hours=h, minutes=60 * k // loads_per_hour)
            if clock.now.hour == SCHEDULED_HOUR_UTC and k == 0:
                await DividendService(session).sync_dividend_data()
            passes_enqueued += await _load_summary(session)
    return passes_enqueued


@pytest.mark.asyncio
async def test_a_week_of_dashboard_loads_and_scheduled_passes_asks_each_holding_once(
    monkeypatch,
):
    """
    Two dashboard loads an hour for a week, plus the 18:00 pass every day.

    Before the fix this asked Yahoo 105 times (five passes a day — 18:00 and four
    6-hour throttle windows — times three securities, the sold one included). Now each
    held, eligible security is asked exactly once: the first load after deploy finds
    both unchecked, and nothing is due again inside the week.
    """
    engine, session = await _session()
    try:
        clock = _Clock()
        asked = _install(monkeypatch, session, clock, _answers())

        enqueued = await _simulate(session, clock, hours=7 * 24 - 1, loads_per_hour=2)

        assert sorted(asked) == ["ACC", "PAY"], f"asked Yahoo {len(asked)} times: {asked}"
        assert enqueued == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_after_the_recheck_interval_each_holding_is_asked_again_once(monkeypatch):
    """`DIVIDENDS_RECHECK_AFTER`: a week until issue #11, a month since the price pass
    captures new ex-dates in between."""
    engine, session = await _session()
    try:
        clock = _Clock()
        asked = _install(monkeypatch, session, clock, _answers())

        await DividendService(session).sync_dividend_data()
        assert sorted(asked) == ["ACC", "PAY"]

        clock.now = START + ds.DIVIDENDS_RECHECK_AFTER - timedelta(minutes=1)
        await DividendService(session).sync_dividend_data()
        assert len(asked) == 2, "a security checked inside the interval was asked again"

        clock.now = START + ds.DIVIDENDS_RECHECK_AFTER + timedelta(minutes=1)
        await DividendService(session).sync_dividend_data()
        assert sorted(asked) == ["ACC", "ACC", "PAY", "PAY"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_sold_security_is_never_asked_and_a_new_purchase_is_asked_next_pass(
    monkeypatch,
):
    engine, session = await _session()
    try:
        clock = _Clock()
        asked = _install(monkeypatch, session, clock, _answers())

        await DividendService(session).sync_dividend_data()
        assert "GONE" not in asked and "P3A" not in asked

        # Bought back: an open lot, and a stamp from before it was sold — older than
        # the window, so the next pass asks. A re-buy inside the window was asked
        # while it was still held, so there is nothing new to learn.
        session.add(TaxLot(
            security_id=SOLD, open_date=date(2026, 10, 6), quantity=Decimal("1"),
            cost_basis=Decimal("10"), cost_basis_eur=Decimal("10"),
            price_per_unit=Decimal("10"), currency="USD", is_open=True,
        ))
        await session.commit()
        clock.now = START + timedelta(days=1)
        await DividendService(session).sync_dividend_data()
        assert asked.count("GONE") == 1
        assert asked.count("PAY") == 1 and asked.count("ACC") == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_rate_limit_stamps_nothing_from_the_429_on(monkeypatch):
    engine, session = await _session()
    try:
        clock = _Clock()
        answers = _answers()
        answers["ACC"] = Exception("HTTP Error 429: Too Many Requests")
        asked = _install(monkeypatch, session, clock, answers)

        result = await DividendService(session).sync_dividend_data()
        assert result["rate_limited"] is True
        stamps = {s.symbol: s.dividends_checked_at for s in await _securities(session)}
        assert stamps["ACC"] is None
        # Rows are asked in id order, so the payer was answered before the 429.
        assert stamps["PAY"] == START

        answers["ACC"] = pd.Series([], dtype=float)
        clock.now = START + timedelta(hours=1)
        await DividendService(session).sync_dividend_data()
        assert asked == ["PAY", "ACC", "ACC"], "the next pass must resume, not restart"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_error_that_is_not_a_rate_limit_is_stamped_so_the_re_ask_is_bounded(
    monkeypatch,
):
    engine, session = await _session()
    try:
        clock = _Clock()
        answers = _answers()
        answers["ACC"] = Exception("HTTP Error 404: Not Found")
        asked = _install(monkeypatch, session, clock, answers)

        result = await DividendService(session).sync_dividend_data()
        assert result["errors"] == 1 and result["rate_limited"] is False

        clock.now = START + timedelta(days=1)
        await DividendService(session).sync_dividend_data()
        assert asked == ["PAY", "ACC"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_summary_enqueues_only_while_a_holding_is_due(monkeypatch):
    engine, session = await _session()
    try:
        clock = _Clock()
        _install(monkeypatch, session, clock, _answers())

        # Nothing checked yet: due.
        assert await _load_summary(session) == 1

        # Right after a pass every held security is fresh — no enqueue, however long
        # the 6-hour throttle has been open.
        monkeypatch.setattr(div_router, "_last_auto_sync", None)
        clock.now = START + timedelta(hours=1)
        assert await _load_summary(session) == 0

        # A new purchase is due.
        session.add(Security(
            id=5, isin="US0000000005", symbol="NEW", description="NEW",
            currency="USD", conid=105, asset_category="STK", exchange="NASDAQ",
        ))
        session.add(TaxLot(
            security_id=5, open_date=date(2026, 10, 5), quantity=Decimal("1"),
            cost_basis=Decimal("10"), cost_basis_eur=Decimal("10"),
            price_per_unit=Decimal("10"), currency="USD", is_open=True,
        ))
        await session.commit()
        assert await _load_summary(session) == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_purging_estimates_re_arms_the_check(monkeypatch):
    """A ticker fix purges the estimates; the corrected ticker is asked next pass."""
    from app.repositories.dividend_repository import DividendRepository

    engine, session = await _session()
    try:
        clock = _Clock()
        asked = _install(monkeypatch, session, clock, _answers())

        await DividendService(session).sync_dividend_data()
        await DividendRepository(session).delete_estimates_for_security(PAYER)
        await session.commit()

        clock.now = START + timedelta(hours=1)
        await DividendService(session).sync_dividend_data()
        assert asked == ["PAY", "ACC", "PAY"]
    finally:
        await session.close()
        await engine.dispose()


async def _securities(session):
    from sqlalchemy import select

    session.expire_all()
    return (await session.execute(select(Security).order_by(Security.id))).scalars().all()
