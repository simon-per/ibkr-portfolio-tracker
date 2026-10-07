"""
The recorded history of the dividend forecast (docs/dividends.md, *The forecast's
history*).

The *Next 12 months* figure is recomputed on every read, so its past was unknowable.
A snapshot row per day fixes that — and the ways it could go wrong are the usual ones
here: a second sum over the forecast that drifts from the tile it records, a stored
figure in whatever base currency happened to be selected, and a zero standing in for
"there was nothing to forecast from".
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.dividend_forecast_snapshot_repository import (
    DividendForecastSnapshotRepository,
)
from app.repositories.dividend_repository import DividendRepository
from app.schemas.portfolio import (
    DividendForecastHistoryPoint,
    DividendForecastHistoryResponse,
)
from app.services.base_fx import BaseFx
from app.services.dividend_service import DividendService
from app.services.portfolio_service import PortfolioService

AS_OF = date(2026, 7, 29)
NEXT_DAY = date(2026, 7, 30)
CHF = BaseFx("CHF", {AS_OF: Decimal("0.90"), NEXT_DAY: Decimal("0.80")})


async def _make_session(with_history: bool = True):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="Alpha Corp",
                         currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    session.add(TaxLot(
        security_id=1, open_date=date(2024, 1, 1), quantity=Decimal("100"),
        cost_basis=Decimal("1000"), cost_basis_eur=Decimal("1000"),
        price_per_unit=Decimal("10"), currency="EUR", is_open=True,
    ))
    await session.flush()
    if with_history:
        # A clean quarterly per-share series, so a cadence is inferable.
        for d in [date(2025, 8, 15), date(2025, 11, 15),
                  date(2026, 2, 15), date(2026, 5, 15)]:
            await DividendRepository(session).upsert_payment({
                "security_id": 1, "ex_date": d, "pay_date": d, "currency": "EUR",
                "amount_per_share": Decimal("1.00"), "shares_held": Decimal("100"),
                "gross_amount_eur": Decimal("100"), "withholding_tax_eur": Decimal("0"),
                "net_amount_eur": Decimal("100"), "source": "yfinance_estimate",
            })
    return engine, session


def _base_currency(monkeypatch, fx: BaseFx) -> None:
    async def load_fx(self):
        return fx
    monkeypatch.setattr(PortfolioService, "_load_base_fx", load_fx)


@pytest.mark.asyncio
async def test_the_snapshot_is_the_breakdowns_own_figure():
    """One implementation: the row holds what the tile's endpoint reports, in EUR."""
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        growth = (await svc.get_dividend_breakdown(as_of=AS_OF))["growth"]
        assert growth["next_12m_eur"] > 0 and growth["ttm"]["net_eur"] > 0

        stored = await svc.record_forecast_snapshot(AS_OF)

        assert stored == {
            "snapshot_date": "2026-07-29",
            "next_12m_eur": growth["next_12m_eur"],
            "ttm_net_eur": growth["ttm"]["net_eur"],
        }
        (row,) = await DividendForecastSnapshotRepository(session).get_all()
        assert row.snapshot_date == AS_OF
        assert float(row.next_12m_eur) == growth["next_12m_eur"]
        assert float(row.ttm_net_eur) == growth["ttm"]["net_eur"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_stored_figure_is_eur_whatever_the_base_currency(monkeypatch):
    """
    A row written while CHF is selected must not be a CHF amount: the base currency is
    switched at will, and a series mixing two currencies has no readable trend.
    """
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        in_eur = (await svc.get_dividend_breakdown(as_of=AS_OF))["growth"]["next_12m_eur"]

        _base_currency(monkeypatch, CHF)
        shown = (await svc.get_dividend_breakdown(as_of=AS_OF))["growth"]["next_12m_eur"]
        assert shown != in_eur  # the tile really is in CHF now

        await svc.record_forecast_snapshot(AS_OF)

        (row,) = await DividendForecastSnapshotRepository(session).get_all()
        assert float(row.next_12m_eur) == in_eur
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_second_write_the_same_day_replaces_and_the_next_day_appends():
    engine, session = await _make_session()
    try:
        repo = DividendForecastSnapshotRepository(session)
        for day, value in [(AS_OF, "100.00"), (AS_OF, "110.00"), (NEXT_DAY, "120.00")]:
            await repo.upsert({
                "snapshot_date": day, "next_12m_eur": Decimal(value),
                "ttm_net_eur": Decimal("50.00"),
            })

        rows = await repo.get_all()
        assert [(r.snapshot_date, r.next_12m_eur) for r in rows] == [
            (AS_OF, Decimal("110.00")), (NEXT_DAY, Decimal("120.00")),
        ]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_book_with_no_dividend_history_records_nothing():
    """Zero and zero would read afterwards as a forecast of nil, not as no data."""
    engine, session = await _make_session(with_history=False)
    try:
        assert await DividendService(session).record_forecast_snapshot(AS_OF) is None
        assert await DividendForecastSnapshotRepository(session).get_all() == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_projects_each_day_at_its_own_rate(monkeypatch):
    engine, session = await _make_session()
    try:
        repo = DividendForecastSnapshotRepository(session)
        for day in (AS_OF, NEXT_DAY):
            await repo.upsert({
                "snapshot_date": day, "next_12m_eur": Decimal("100.00"),
                "ttm_net_eur": Decimal("50.00"),
            })
        _base_currency(monkeypatch, CHF)

        history = await DividendService(session).get_forecast_history()

        assert history == {
            "base_currency": "CHF",
            "points": [
                {"date": "2026-07-29", "next_12m_eur": 90.0, "ttm_net_eur": 45.0},
                {"date": "2026-07-30", "next_12m_eur": 80.0, "ttm_net_eur": 40.0},
            ],
        }
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_response_model_and_the_service_agree_in_both_directions():
    """A `response_model` is a filter (test_dividend_summary_contract.py)."""
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        await svc.record_forecast_snapshot(AS_OF)
        history = await svc.get_forecast_history()

        assert set(history) == set(DividendForecastHistoryResponse.model_fields)
        assert set(history["points"][0]) == set(DividendForecastHistoryPoint.model_fields)
    finally:
        await session.close()
        await engine.dispose()
