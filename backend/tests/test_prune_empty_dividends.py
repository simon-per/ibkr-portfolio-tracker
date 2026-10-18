"""
The prune CLI must delete only what the readers already treat as non-income, and
must never touch a row that is merely uncomputed — those have no amounts *yet*.

"The readers" now includes the forecast, which infers cadence from the RAW history, so
prune is additionally bounded by the ingest window — see
`test_prune_preserves_forecast_basis.py`. That is why this fixture carries a tax lot:
it holds 10 shares on two ex-dates, which is not a state that can exist without one, and
the cutoff is derived from `min(taxlots.open_date)`.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.cli import prune_empty_dividends as cli
from tests.memory_db import memory_engine


async def _make_db(monkeypatch):
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="A",
                         currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    # The lot the two income rows below imply. Bought 2025-10-01, so the ingest window
    # reaches back to 2022-10-01 and both pre-ownership rows sit well outside it.
    session.add(TaxLot(
        security_id=1, open_date=date(2025, 10, 1), quantity=Decimal("10"),
        cost_basis=Decimal("1000"), cost_basis_eur=Decimal("1000"),
        price_per_unit=Decimal("100"), currency="EUR", is_open=True,
    ))

    rows = [
        # empty: computed, nothing earned (pre-ownership ex-dates)
        dict(ex_date=date(1985, 8, 12), shares_held=Decimal("0"),
             gross_amount_eur=Decimal("0"), net_amount_eur=Decimal("0")),
        dict(ex_date=date(2010, 3, 1), shares_held=Decimal("0"),
             gross_amount_eur=Decimal("0"), net_amount_eur=Decimal("0")),
        # legacy shape: gross only, net NULL — real income, must survive
        dict(ex_date=date(2025, 11, 5), shares_held=Decimal("10"),
             gross_amount_eur=Decimal("4"), net_amount_eur=None),
        # real income
        dict(ex_date=date(2026, 3, 10), shares_held=Decimal("10"),
             gross_amount_eur=Decimal("6"), net_amount_eur=Decimal("5")),
        # uncomputed: no amounts YET — must survive
        dict(ex_date=date(2026, 7, 1), shares_held=None,
             gross_amount_eur=None, net_amount_eur=None),
    ]
    for r in rows:
        session.add(DividendPayment(security_id=1, currency="EUR",
                                    source="yfinance_estimate", **r))
    await session.flush()
    await session.commit()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: _Ctx())
    return engine, session


async def _remaining(session):
    rows = (await session.execute(select(DividendPayment))).scalars().all()
    return sorted(p.ex_date for p in rows)


@pytest.mark.asyncio
async def test_dry_run_deletes_nothing(monkeypatch, capsys):
    engine, session = await _make_db(monkeypatch)
    try:
        assert await cli.prune(dry_run=True) == 0
        assert len(await _remaining(session)) == 5
        assert "DRY RUN" in capsys.readouterr().out
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_only_empty_computed_rows_are_deleted(monkeypatch):
    engine, session = await _make_db(monkeypatch)
    try:
        assert await cli.prune(dry_run=False) == 0
        assert await _remaining(session) == [
            date(2025, 11, 5),   # legacy gross-only income
            date(2026, 3, 10),   # real income
            date(2026, 7, 1),    # uncomputed, no amounts yet
        ]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_clean_table_is_a_no_op(monkeypatch, capsys):
    engine, session = await _make_db(monkeypatch)
    try:
        await cli.prune(dry_run=False)
        assert await cli.prune(dry_run=False) == 0
        assert "Nothing to prune" in capsys.readouterr().out
    finally:
        await session.close()
        await engine.dispose()
