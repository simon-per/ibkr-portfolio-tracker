"""
yfinance returns a security's whole dividend history. Ingesting ex-dates from
before the first lot stores rows that can only ever be worth zero: on this
account 1355 of 1446 rows, back to 1985. They are noise every reader must
filter — and when one filter was relaxed, they broke an endpoint.
"""
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.dividend_service import DividendService

BOUGHT = date(2025, 6, 2)


async def _session(with_lot: bool):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="A",
                         currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    if with_lot:
        session.add(TaxLot(
            security_id=1, open_date=BOUGHT, quantity=Decimal("10"),
            cost_basis=Decimal("100"), cost_basis_eur=Decimal("100"),
            price_per_unit=Decimal("10"), currency="EUR", is_open=True,
        ))
    await session.flush()
    return engine, session


def _patch_yf(monkeypatch, dates):
    """Stub the yfinance dividend series — no network."""
    import app.services.dividend_service as ds

    series = pd.Series(
        [1.0] * len(dates),
        index=pd.to_datetime([datetime(d.year, d.month, d.day) for d in dates]),
    )

    class _T:
        def __init__(self, ticker):
            pass

        @property
        def dividends(self):
            return series

    monkeypatch.setattr(ds.yf, "Ticker", _T)
    monkeypatch.setattr(ds.asyncio, "sleep", _no_sleep)


async def _no_sleep(*_a, **_kw):
    return None


async def _stored(session):
    rows = (await session.execute(select(DividendPayment))).scalars().all()
    return sorted(p.ex_date for p in rows)


HISTORY = [date(1985, 8, 12), date(2010, 3, 1), BOUGHT, date(2025, 12, 4), date(2026, 3, 5)]


@pytest.mark.asyncio
async def test_dividends_before_the_first_lot_are_not_ingested(monkeypatch):
    engine, session = await _session(with_lot=True)
    try:
        _patch_yf(monkeypatch, HISTORY)
        monkeypatch.setattr(DividendService, "_get_yahoo_ticker",
                            lambda self, sec: _ticker())

        result = await DividendService(session).sync_dividend_data()

        assert await _stored(session) == [BOUGHT, date(2025, 12, 4), date(2026, 3, 5)]
        assert result["pre_ownership_skipped"] == 2
        assert result["dividends_added"] == 3
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_security_with_no_lots_yet_is_not_asked(monkeypatch):
    """
    No lot means nothing to earn and no cutoff to infer. It used to keep the whole
    history; since issue #6 the pass asks only about held securities, so it is not
    asked at all until its first lot lands (an unstamped holding is due at once).
    """
    engine, session = await _session(with_lot=False)
    try:
        _patch_yf(monkeypatch, HISTORY)
        monkeypatch.setattr(DividendService, "_get_yahoo_ticker",
                            lambda self, sec: _ticker())

        result = await DividendService(session).sync_dividend_data()

        assert await _stored(session) == []
        assert result["securities_processed"] == 0
    finally:
        await session.close()
        await engine.dispose()


async def _ticker():
    return "AAA.DE"
