"""
The recorded history of the dividend forecast (docs/dividends.md, *The forecast's
history*).

The *Next 12 months* figure is recomputed on every read, so its past was unknowable.
A snapshot row per day fixes that — and the ways it could go wrong are the usual ones
here: a second sum over the forecast that drifts from the tile it records, a stored
figure in whatever base currency happened to be selected, and a zero standing in for
"there was nothing to forecast from".
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.cli.backfill_dividend_forecast import BackfillError, backfill
from app.database import Base
import app.models  # noqa: F401
from app.models.dividend_accrual import DividendAccrual
from app.models.dividend_forecast_snapshot import DividendForecastSnapshotLine
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.app_settings_repository import AppSettingsRepository
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
            "growth_from": None,
            "points": [
                {"date": "2026-07-29", "next_12m_eur": 90.0, "ttm_net_eur": 45.0,
                 "next_12m_change_pct": None, "next_12m_mtd_pct": None,
                 "next_12m_ytd_pct": None, "mtd_from": None, "ytd_from": None,
                 "reconstructed": False},
                # The percentages are of the projected figures, so the rate's move
                # is in them: the stored EUR did not change at all.
                {"date": "2026-07-30", "next_12m_eur": 80.0, "ttm_net_eur": 40.0,
                 "next_12m_change_pct": -11.11, "next_12m_mtd_pct": -11.11,
                 "next_12m_ytd_pct": -11.11, "mtd_from": "2026-07-29",
                 "ytd_from": "2026-07-29", "reconstructed": False},
            ],
        }
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_growth_is_measured_from_the_close_before_the_period():
    """
    Month- and year-to-date start at the last snapshot before the period, and only
    fall back to the first one inside it while the series is younger than that. The
    day a period is measured from is named, and a day that is its own start has no
    growth rather than a 0.0 that would read as "flat".
    """
    engine, session = await _make_session()
    try:
        repo = DividendForecastSnapshotRepository(session)
        for day, value in [
            (date(2026, 12, 30), "100.00"), (date(2026, 12, 31), "110.00"),
            (date(2027, 1, 4), "121.00"), (date(2027, 2, 1), "133.10"),
        ]:
            await repo.upsert({
                "snapshot_date": day, "next_12m_eur": Decimal(value),
                "ttm_net_eur": Decimal("50.00"),
            })

        points = (await DividendService(session).get_forecast_history())["points"]

        growth = [
            (p["next_12m_change_pct"], p["next_12m_mtd_pct"], p["mtd_from"],
             p["next_12m_ytd_pct"], p["ytd_from"])
            for p in points
        ]
        assert growth == [
            (None, None, None, None, None),
            (10.0, 10.0, "2026-12-30", 10.0, "2026-12-30"),
            (10.0, 10.0, "2026-12-31", 10.0, "2026-12-31"),
            (10.0, 10.0, "2027-01-04", 21.0, "2026-12-31"),
        ]
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


# ---- Replayed days (app.cli.backfill_dividend_forecast) -------------------------


@pytest.mark.asyncio
async def test_a_replay_withholds_what_was_learned_after_the_day():
    """
    A past day must not see a payment made after it or an accrual open today — and
    the unreplayed read of the same day does see the accrual, which is what makes
    this a test of the cut rather than of two equal numbers.
    """
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        before = await svc.forecast_figures(AS_OF, replay=True)

        await DividendRepository(session).upsert_payment({
            "security_id": 1, "ex_date": date(2026, 8, 15), "pay_date": date(2026, 8, 15),
            "currency": "EUR", "amount_per_share": Decimal("3.00"),
            "shares_held": Decimal("100"), "gross_amount_eur": Decimal("300"),
            "withholding_tax_eur": Decimal("0"), "net_amount_eur": Decimal("300"),
            "source": "yfinance_estimate",
        })
        session.add(DividendAccrual(
            security_id=1, ex_date=date(2026, 8, 10), pay_date=date(2026, 8, 20),
            currency="EUR", quantity=Decimal("100"), gross_amount_eur=Decimal("900"),
            withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("900"),
            last_seen_at=datetime(2026, 8, 12),
        ))
        await session.flush()

        assert await svc.forecast_figures(AS_OF, replay=True) == before
        assert await svc.forecast_figures(AS_OF) != before
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_replayed_row_never_replaces_a_recorded_one():
    engine, session = await _make_session()
    try:
        repo = DividendForecastSnapshotRepository(session)

        def figures(value):
            return {"snapshot_date": AS_OF, "next_12m_eur": Decimal(value),
                    "ttm_net_eur": Decimal("50.00")}

        async def stored():
            (row,) = await repo.get_all()
            await session.refresh(row)
            return float(row.next_12m_eur), row.reconstructed

        await repo.upsert_reconstructed(figures("100.00"))
        await repo.upsert_reconstructed(figures("110.00"))
        assert await stored() == (110.0, True)      # a replay replaces a replay

        await repo.upsert(figures("120.00"))
        assert await stored() == (120.0, False)     # the day's own figure outranks it

        await repo.upsert_reconstructed(figures("130.00"))
        assert await stored() == (120.0, False)     # and is never replayed over
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_backfill_stops_before_the_first_recorded_day():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        await svc.record_forecast_snapshot(AS_OF)
        start = date(2026, 7, 27)

        dry = await backfill(session, start, dry_run=True)
        assert [r["snapshot_date"] for r in dry["rows"]] == [start, date(2026, 7, 28)]
        assert len(await DividendForecastSnapshotRepository(session).get_all()) == 1

        result = await backfill(session, start)
        points = (await svc.get_forecast_history())["points"]
        assert [(p["date"], p["reconstructed"]) for p in points] == [
            ("2026-07-27", True), ("2026-07-28", True), ("2026-07-29", False),
        ]
        # The replay of a recorded day is reported beside it, never written over it.
        assert [o["date"] for o in result["overlap"]] == [AS_OF]
        assert result["overlap"][0]["replayed"] == result["overlap"][0]["recorded"]

        with pytest.raises(BackfillError):
            await backfill(session, AS_OF)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_percentage_reaches_back_across_growth_from():
    """
    The owner names the day the book changed shape. The days before it keep their
    figures and carry no growth; the first day from it on starts the series, so its
    month- and year-to-date cannot report the change itself.
    """
    engine, session = await _make_session()
    try:
        repo = DividendForecastSnapshotRepository(session)
        for day, value in [
            (date(2026, 8, 30), "50.00"), (date(2026, 8, 31), "60.00"),
            (date(2026, 9, 1), "300.00"), (date(2026, 9, 2), "330.00"),
            (date(2026, 10, 1), "363.00"),
        ]:
            await repo.upsert({
                "snapshot_date": day, "next_12m_eur": Decimal(value),
                "ttm_net_eur": Decimal("50.00"),
            })
        await AppSettingsRepository(session).set_dividend_forecast_growth_from(
            date(2026, 9, 1)
        )

        history = await DividendService(session).get_forecast_history()

        assert history["growth_from"] == "2026-09-01"
        assert [p["next_12m_eur"] for p in history["points"]] == [
            50.0, 60.0, 300.0, 330.0, 363.0,
        ]
        assert [
            (p["next_12m_change_pct"], p["next_12m_mtd_pct"], p["mtd_from"],
             p["next_12m_ytd_pct"], p["ytd_from"])
            for p in history["points"]
        ] == [
            (None, None, None, None, None),
            (None, None, None, None, None),
            (None, None, None, None, None),
            (10.0, 10.0, "2026-09-01", 10.0, "2026-09-01"),
            (10.0, 10.0, "2026-09-02", 21.0, "2026-09-01"),
        ]
    finally:
        await session.close()
        await engine.dispose()


# ---- Per-security lines (dividend_forecast_snapshot_lines) ----------------------


async def _add_non_payer(session) -> None:
    """A second holding with no dividend history at all — an accumulating fund."""
    session.add(Security(id=2, isin="IE0000000002", symbol="ACC", description="Acc Fund",
                         currency="USD", conid=200, asset_category="ETF", exchange="LSE"))
    session.add(TaxLot(
        security_id=2, open_date=date(2024, 1, 1), quantity=Decimal("7"),
        cost_basis=Decimal("700"), cost_basis_eur=Decimal("700"),
        price_per_unit=Decimal("100"), currency="USD", is_open=True,
    ))
    await session.flush()


async def _lines(session, day):
    rows = await DividendForecastSnapshotRepository(session).get_lines()
    return rows.get(day, [])


@pytest.mark.asyncio
async def test_the_lines_are_the_breakdowns_own_and_sum_to_the_stored_total():
    """
    One implementation again: the lines are `forecast_by_security`, not a second walk
    over the forecast, and on a day recorded with them they add up to the total.
    """
    engine, session = await _make_session()
    try:
        await _add_non_payer(session)
        svc = DividendService(session)
        served = (await svc.get_dividend_breakdown(as_of=AS_OF))["forecast_by_security"]

        await svc.record_forecast_snapshot(AS_OF)

        (row,) = await DividendForecastSnapshotRepository(session).get_all()
        lines = await _lines(session, AS_OF)
        assert [
            (ln.security_id, float(ln.shares), float(ln.next_12m_eur), ln.reconstructed)
            for ln in lines
        ] == [(s["security_id"], s["shares"], s["next_12m_eur"], False) for s in served]
        assert round(sum(ln.next_12m_eur for ln in lines), 2) == row.next_12m_eur
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_holding_that_pays_nothing_still_gets_a_line():
    """
    Held and forecasting nothing is a zero line, never an absent one: the split has to
    tell a holding whose first forecast appears from a position that was bought.
    """
    engine, session = await _make_session()
    try:
        await _add_non_payer(session)
        await DividendService(session).record_forecast_snapshot(AS_OF)

        by_sec = {ln.security_id: ln for ln in await _lines(session, AS_OF)}
        assert set(by_sec) == {1, 2}
        assert by_sec[2].shares == Decimal("7") and by_sec[2].next_12m_eur == 0
        assert by_sec[1].fx_to_eur == Decimal("1")      # a EUR security
        assert by_sec[2].fx_to_eur is None              # no USD rate is cached here
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_second_write_the_same_day_replaces_the_lines_with_the_total():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        await svc.record_forecast_snapshot(AS_OF)
        session.add(TaxLot(
            security_id=1, open_date=date(2026, 7, 1), quantity=Decimal("50"),
            cost_basis=Decimal("500"), cost_basis_eur=Decimal("500"),
            price_per_unit=Decimal("10"), currency="EUR", is_open=True,
        ))
        await session.flush()
        await svc.record_forecast_snapshot(AS_OF)

        (row,) = await DividendForecastSnapshotRepository(session).get_all()
        (line,) = await _lines(session, AS_OF)
        assert line.shares == Decimal("150")
        assert round(line.next_12m_eur, 2) == row.next_12m_eur
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_replayed_lines_never_replace_recorded_ones_and_fill_a_day_without_any():
    engine, session = await _make_session()
    try:
        repo = DividendForecastSnapshotRepository(session)

        def line(value):
            return [{"security_id": 1, "shares": Decimal("100"),
                     "next_12m_eur": Decimal(value), "currency": "EUR",
                     "fx_to_eur": Decimal("1")}]

        async def stored():
            (ln,) = await _lines(session, AS_OF)
            await session.refresh(ln)
            return float(ln.next_12m_eur), ln.reconstructed

        # A day recorded before the lines table existed: a total and nothing behind it.
        await repo.upsert({"snapshot_date": AS_OF, "next_12m_eur": Decimal("400.00"),
                           "ttm_net_eur": Decimal("50.00")})
        assert await _lines(session, AS_OF) == []

        assert await repo.fill_lines(AS_OF, line("399.50")) is True
        assert await stored() == (399.5, True)
        assert await repo.fill_lines(AS_OF, line("399.80")) is True
        assert await stored() == (399.8, True)          # a replay replaces a replay
        (row,) = await repo.get_all()
        assert (row.next_12m_eur, row.reconstructed) == (Decimal("400.00"), False)

        await repo.upsert({"snapshot_date": AS_OF, "next_12m_eur": Decimal("410.00"),
                           "ttm_net_eur": Decimal("50.00")}, lines=line("410.00"))
        assert await stored() == (410.0, False)         # the day's own lines outrank it

        assert await repo.fill_lines(AS_OF, line("1.00")) is False
        assert await stored() == (410.0, False)         # and are never replayed over
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_backfill_gives_a_recorded_day_its_missing_lines_and_keeps_its_total():
    engine, session = await _make_session()
    try:
        repo = DividendForecastSnapshotRepository(session)
        # Recorded the old way: no lines.
        await repo.upsert({"snapshot_date": AS_OF, "next_12m_eur": Decimal("123.45"),
                           "ttm_net_eur": Decimal("50.00")})
        start = date(2026, 7, 28)

        dry = await backfill(session, start, dry_run=True)
        assert dry["lines_filled"] == [AS_OF]
        assert await repo.get_lines() == {}

        result = await backfill(session, start)

        assert result["lines_filled"] == [AS_OF]
        lines = await repo.get_lines()
        assert set(lines) == {start, AS_OF}
        assert all(ln.reconstructed for day in lines.values() for ln in day)
        recorded = [r for r in await repo.get_all() if not r.reconstructed]
        assert [(r.snapshot_date, r.next_12m_eur) for r in recorded] == [
            (AS_OF, Decimal("123.45")),
        ]

        # A second run has nothing left to fill on a day that now has lines.
        assert (await backfill(session, start))["lines_filled"] == []
        assert (
            await session.get(DividendForecastSnapshotLine, lines[AS_OF][0].id)
        ) is not None
    finally:
        await session.close()
        await engine.dispose()
