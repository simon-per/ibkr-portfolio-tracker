"""
Every selectable base's EUR->base history reaches back to the first purchase.

Cost basis converts at each lot's own `open_date` (`BaseFx`), so a base whose cached
history starts in 2026 converts a 2025 purchase at a carried 2026 rate — silently, since
`BaseFx` carries the nearest rate back. The switch fills a base's history when it is
chosen, but only if Frankfurter answers right then, and the daily warm-up and the crypto
book's keep shorter windows of the same pair. So the daily FX job runs
`ensure_base_history` first: one Frankfurter range request for every base that falls
short of `stock_book_start`, none once they all reach. That is also what filled GBP's
history when it joined (2026-11-14) without anyone switching to it.

Offline: the Frankfurter fake from `test_fx_incremental_warm` answers every weekday.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utc_today
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.base_fx import BASE_HISTORY_SLACK_DAYS, ensure_base_history, stock_book_start
from app.services.currency_service import CurrencyService
from tests.memory_db import memory_engine
from tests.test_fx_incremental_warm import at, frankfurter, install_http

FIRST_LOT = date(2024, 5, 28)


@pytest_asyncio.fixture
async def engine():
    engine = await memory_engine()
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(engine):
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db
    finally:
        await db.close()


def _range_start(call) -> date:
    return date.fromisoformat(call[0].rsplit("/", 1)[1].split("..")[0])


async def _lot(db, opened=FIRST_LOT):
    db.add(Security(id=1, isin="US0378331005", symbol="AAPL", description="Apple",
                    currency="USD", conid=1, asset_category="STK", exchange="NASDAQ"))
    db.add(TaxLot(security_id=1, open_date=opened, quantity=Decimal(1),
                  cost_basis=Decimal(100), price_per_unit=Decimal(100), currency="USD",
                  cost_basis_eur=Decimal(90)))
    await db.commit()


async def _oldest(db, base):
    return (await db.execute(select(func.min(ExchangeRate.date)).where(
        ExchangeRate.from_currency == "EUR", ExchangeRate.to_currency == base,
    ))).scalar()


def _seed_history(db, base, first, last):
    day = first
    while day <= last:
        db.add(ExchangeRate(date=day, from_currency="EUR", to_currency=base,
                            rate=Decimal("0.9"), source="frankfurter"))
        day += timedelta(days=1)


@pytest.mark.asyncio
async def test_the_window_starts_at_the_first_lot_or_a_year_back(session):
    assert await stock_book_start(session) == utc_today() - timedelta(days=365)
    await _lot(session)
    assert await stock_book_start(session) == FIRST_LOT


@pytest.mark.asyncio
async def test_only_the_bases_that_fall_short_are_asked_in_one_request(monkeypatch, session):
    # CHF and USD reach the first lot (as on production); GBP has only this year's
    # rates, as the crypto book's warm-up would have left it.
    at(datetime.combine(utc_today(), time(20, 0)), monkeypatch)
    calls = install_http(monkeypatch)
    await _lot(session)
    today = utc_today()
    for base in ("CHF", "USD"):
        _seed_history(session, base, FIRST_LOT, FIRST_LOT + timedelta(days=3))
    _seed_history(session, "GBP", date(2026, 1, 1), date(2026, 1, 5))
    await session.commit()

    result = await ensure_base_history(
        session, CurrencyService(session), ["EUR", "CHF", "USD", "GBP"], FIRST_LOT
    )

    assert result == {"GBP": True}
    (request,) = frankfurter(calls)
    assert request[1]["to"] == "GBP"
    assert _range_start(request) == FIRST_LOT
    assert request[0].endswith(today.isoformat())
    assert await _oldest(session, "GBP") <= FIRST_LOT + timedelta(days=BASE_HISTORY_SLACK_DAYS)

    again = await ensure_base_history(
        session, CurrencyService(session), ["CHF", "USD", "GBP"], FIRST_LOT
    )
    assert again == {}
    assert len(frankfurter(calls)) == 1, "a history that reaches is never re-asked"


@pytest.mark.asyncio
async def test_a_start_on_a_holiday_weekend_still_counts_as_reached(monkeypatch, session):
    # Good Friday 2025 to Easter Monday: the ECB published nothing for four days.
    at(datetime.combine(utc_today(), time(20, 0)), monkeypatch)
    calls = install_http(monkeypatch)
    _seed_history(session, "GBP", date(2025, 4, 22), date(2025, 4, 25))
    await session.commit()

    assert await ensure_base_history(
        session, CurrencyService(session), ["GBP"], date(2025, 4, 18)
    ) == {}
    assert frankfurter(calls) == []


@pytest.mark.asyncio
async def test_an_outage_is_reported_and_asked_again_next_slot(monkeypatch, session):
    at(datetime.combine(utc_today(), time(20, 0)), monkeypatch)
    calls = install_http(monkeypatch, frankfurter_status=503)

    result = await ensure_base_history(session, CurrencyService(session), ["GBP"], FIRST_LOT)

    assert result == {"GBP": False}
    assert await _oldest(session, "GBP") is None
    await ensure_base_history(session, CurrencyService(session), ["GBP"], FIRST_LOT)
    assert len(frankfurter(calls)) == 2


@pytest.mark.asyncio
async def test_the_job_fills_every_base_from_the_first_lot_and_reports_it(monkeypatch, engine):
    from app.services import scheduler_service as sched_mod

    monkeypatch.setattr(sched_mod, "AsyncSessionLocal",
                        async_sessionmaker(engine, expire_on_commit=False))
    at(datetime.combine(utc_today(), time(20, 0)), monkeypatch)
    calls = install_http(monkeypatch)
    async with AsyncSession(engine) as db:
        await _lot(db)

    first = await sched_mod.SchedulerService().sync_exchange_rates(days_back=7)

    assert first["base_history"] == {
        "EUR->CHF": "backfilled", "EUR->USD": "backfilled", "EUR->GBP": "backfilled",
    }
    history = frankfurter(calls)[0]
    assert _range_start(history) == FIRST_LOT
    async with AsyncSession(engine) as db:
        for base in ("CHF", "USD", "GBP"):
            assert await _oldest(db, base) <= FIRST_LOT + timedelta(days=BASE_HISTORY_SLACK_DAYS)

    second = await sched_mod.SchedulerService().sync_exchange_rates(days_back=7)
    assert "base_history" not in second
