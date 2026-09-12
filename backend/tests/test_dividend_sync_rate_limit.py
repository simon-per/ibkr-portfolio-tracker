"""
The dividend fetch was the one Yahoo loop that latched `rate_limited`, abandoned the
pass correctly — and then reported nothing: no flag, no warning, and a
`securities_processed` computed as "everything not skipped", so a pass Yahoo killed at
1 of 2 claimed 2. Indistinguishable afterwards from a complete run.
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.dividend_service import DividendService
import app.services.dividend_service as ds


async def _session(n_securities: int):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    for i in range(1, n_securities + 1):
        session.add(Security(
            id=i, isin=f"US000000000{i}", symbol=f"S{i}", description=f"S{i}",
            currency="USD", conid=100 + i, asset_category="STK", exchange="NASDAQ",
        ))
        session.add(TaxLot(
            security_id=i, open_date=date(2025, 6, 2), quantity=Decimal("10"),
            cost_basis=Decimal("100"), cost_basis_eur=Decimal("100"),
            price_per_unit=Decimal("10"), currency="USD", is_open=True,
        ))
    await session.flush()
    return engine, session


async def _no_sleep(*_a, **_kw):
    return None


async def _ticker(*_a, **_kw):
    return "S"


@pytest.mark.asyncio
async def test_an_abandoned_pass_says_so_and_counts_only_what_it_asked(monkeypatch):
    engine, session = await _session(n_securities=2)
    try:
        asked = []

        class _Refusing:
            def __init__(self, ticker):
                asked.append(ticker)

            @property
            def dividends(self):
                raise Exception("HTTP Error 429: Too Many Requests")

        monkeypatch.setattr(ds.yf, "Ticker", _Refusing)
        monkeypatch.setattr(ds.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(DividendService, "_get_yahoo_ticker", _ticker)

        result = await DividendService(session).sync_dividend_data()

        assert len(asked) == 1, "the pass kept asking Yahoo after a 429"
        assert result["rate_limited"] is True
        assert result["securities_processed"] == 1
        assert result["errors"] == 1
        assert len(result["warnings"]) == 1
        assert "Do not retry manually" in result["warnings"][0]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_clean_pass_carries_the_flag_but_no_warning(monkeypatch):
    engine, session = await _session(n_securities=1)
    try:
        class _Empty:
            def __init__(self, ticker):
                pass

            @property
            def dividends(self):
                return pd.Series([], dtype=float)

        monkeypatch.setattr(ds.yf, "Ticker", _Empty)
        monkeypatch.setattr(ds.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(DividendService, "_get_yahoo_ticker", _ticker)

        result = await DividendService(session).sync_dividend_data()

        assert result["rate_limited"] is False
        assert result["securities_processed"] == 1
        assert "warnings" not in result
    finally:
        await session.close()
        await engine.dispose()
