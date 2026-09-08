"""
The benchmark invests **contributions**, not tax lots — and a rotation must not move it.

Driving it off lots meant a lot's `close_date` emitted `-shares`, which unwinds the
position at the *number of shares bought* and simultaneously removes its cost. The gain
those shares had accumulated simply vanishes. Read off production for the 2026-08-21
restructuring, an S&P 500 comparison in CHF:

    08-20   61,654      (cost basis 53,471)
    08-21   38,766      <- sold, and never recovered
    08-24   51,603      (rebought at today's price with only the cost)
    08-26   51,680

4,193 CHF of gain destroyed by a day on which **no money left the account**. On top of
that it cliffed on a chart whose portfolio line no longer does, so the picture read as a
huge outperformance that was pure artefact.

A contribution-driven hypothetical answers the question people actually ask — *what if I
had put the same money into the index instead* — and is rotation-neutral by
construction, because selling one holding to buy another is not a contribution.

`test_a_rotation_does_not_move_the_benchmark` is the whole fix in one assertion.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.app_settings import AppSetting
from app.models.benchmark_price import BenchmarkPrice
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.benchmark_service import BenchmarkService, reset_upstream_throttle


@pytest.fixture(autouse=True)
def _clear_throttle():
    reset_upstream_throttle()
    yield
    reset_upstream_throttle()


async def _session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [BenchmarkPrice.__table__, ExchangeRate.__table__, TaxLot.__table__,
              Security.__table__, AppSetting.__table__, CashFlow.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(
        id=1, isin="US0000000001", symbol="AAA", description="Test Co",
        currency="EUR", conid=100, asset_category="STK", exchange="XETRA",
    ))
    # A EUR benchmark keeps the arithmetic readable — the FX leg is covered elsewhere.
    # Flat prices everywhere EXCEPT the rotation window, so any movement the test sees
    # is the event under test rather than the index drifting.
    for day in range(1, 29):
        session.add(BenchmarkPrice(
            ticker="^GDAXI", date=date(2026, 3, day),
            close_price=Decimal("100"), currency="EUR",
        ))
    await session.flush()
    return engine, session


def _lot(open_date, cost, close_date=None):
    return TaxLot(
        security_id=1, open_date=open_date, quantity=Decimal("10"),
        cost_basis=Decimal(cost), price_per_unit=Decimal(cost) / 10, currency="EUR",
        cost_basis_eur=Decimal(cost), is_open=close_date is None,
        close_date=close_date, close_source="trade" if close_date else None,
    )


async def _series(session, start, end):
    svc = BenchmarkService(session)
    pts = await svc.calculate_benchmark_value_over_time(start, end, "dax")
    return {p["date"]: p for p in pts}


@pytest.mark.asyncio
async def test_a_rotation_does_not_move_the_benchmark():
    """
    The production case: a deposit ledger exists, so `money_in` is real deposits and a
    rotation is not one. Sell the whole position on the 10th, redeploy on the 12th at a
    higher cost — no money entered or left, so the hypothetical must sit still.

    Under the old lot-driven basis it sold on the 10th and rebought on the 12th, banking
    a permanent loss of the accrued gain.
    """
    from app.repositories.app_settings_repository import AppSettingsRepository

    engine, session = await _session()
    try:
        session.add(CashFlow(
            ib_key="D1", flow_date=date(2026, 3, 2), flow_type=DEPOSIT_WITHDRAW,
            amount=Decimal("1000"), currency="EUR", amount_eur=Decimal("1000"),
        ))
        session.add(_lot(date(2026, 3, 2), "1000", close_date=date(2026, 3, 10)))
        session.add(_lot(date(2026, 3, 12), "1400"))
        await session.flush()
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 3, 1))
        await session.flush()

        by_date = await _series(session, date(2026, 3, 3), date(2026, 3, 20))

        # Flat index, one deposit of 1000 -> 10 shares at 100 -> 1000, throughout.
        for d in ("2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-20"):
            assert by_date[d]["benchmark_value_eur"] == pytest.approx(1000), d
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_without_a_deposit_ledger_a_rotation_still_moves_it():
    """
    The honest limitation, pinned so nobody "fixes" it into a false claim.

    Before `coverage_from` there is no deposit ledger and lot cost basis is the only
    signal there is — and it cannot survive a rotation, because selling one holding to
    buy another opens new lots for the same money. `get_contributions` reports that era
    as `money_in_method: "deployed"` for exactly this reason, and the benchmark inherits
    it rather than inventing a better answer.

    On this account the rotation happened in August against a ledger starting in
    January, so the era that matters is the one above.
    """
    engine, session = await _session()
    try:
        session.add(_lot(date(2026, 3, 2), "1000", close_date=date(2026, 3, 10)))
        session.add(_lot(date(2026, 3, 12), "1400"))
        await session.flush()

        by_date = await _series(session, date(2026, 3, 3), date(2026, 3, 20))
        assert by_date["2026-03-09"]["benchmark_value_eur"] == pytest.approx(1000)
        # Both deployments count, which is the documented cost of having no ledger.
        assert by_date["2026-03-20"]["benchmark_value_eur"] == pytest.approx(2400)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_benchmark_still_tracks_the_index():
    """The mirror case: rotation-neutral must not mean inert."""
    engine, session = await _session()
    try:
        session.add(_lot(date(2026, 3, 2), "1000"))
        await session.flush()
        # The index doubles on the 15th.
        for day in range(15, 29):
            await session.execute(
                BenchmarkPrice.__table__.update()
                .where(BenchmarkPrice.date == date(2026, 3, day))
                .values(close_price=Decimal("200"))
            )
        await session.flush()

        by_date = await _series(session, date(2026, 3, 3), date(2026, 3, 20))
        assert by_date["2026-03-13"]["benchmark_value_eur"] == pytest.approx(1000)
        assert by_date["2026-03-16"]["benchmark_value_eur"] == pytest.approx(2000)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_deposit_buys_shares_and_a_withdrawal_sells_them():
    """
    Past `coverage_from` the legs are real deposits, and a withdrawal is negative. It
    must sell at that day's price — the money left, so the hypothetical has to fund it
    from the index too.
    """
    from app.repositories.app_settings_repository import AppSettingsRepository

    engine, session = await _session()
    try:
        session.add(_lot(date(2026, 3, 2), "1000"))
        session.add(CashFlow(
            ib_key="D1", flow_date=date(2026, 3, 5), flow_type=DEPOSIT_WITHDRAW,
            amount=Decimal("500"), currency="EUR", amount_eur=Decimal("500"),
        ))
        session.add(CashFlow(
            ib_key="W1", flow_date=date(2026, 3, 17), flow_type=DEPOSIT_WITHDRAW,
            amount=Decimal("-200"), currency="EUR", amount_eur=Decimal("-200"),
        ))
        await session.flush()
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 3, 4))
        await session.flush()

        by_date = await _series(session, date(2026, 3, 3), date(2026, 3, 20))
        # Lot cost before the boundary, then deposits: 1000, +500, -200.
        assert by_date["2026-03-03"]["benchmark_value_eur"] == pytest.approx(1000)
        assert by_date["2026-03-06"]["benchmark_value_eur"] == pytest.approx(1500)
        assert by_date["2026-03-18"]["benchmark_value_eur"] == pytest.approx(1300)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_baseline_is_the_same_money_in_the_chart_draws():
    """
    `cost_basis_eur` on a benchmark point is the running contribution total, so the
    hypothetical and the portfolio are measured against one baseline rather than two.
    Pinned because the two used to be different quantities — lot deployment against
    contributions — which is what made the comparison unreadable after a rotation.
    """
    from app.services.portfolio_service import BaseFx, PortfolioService

    engine, session = await _session()
    try:
        session.add(_lot(date(2026, 3, 2), "1000", close_date=date(2026, 3, 10)))
        session.add(_lot(date(2026, 3, 12), "1400"))
        await session.flush()

        by_date = await _series(session, date(2026, 3, 3), date(2026, 3, 20))
        inputs = await PortfolioService(session)._contribution_inputs(BaseFx("EUR", {}))
        money_in = sum(a for d, a in inputs["money_in_legs"] if d <= date(2026, 3, 20))

        assert by_date["2026-03-20"]["cost_basis_eur"] == pytest.approx(float(money_in))
    finally:
        await engine.dispose()
