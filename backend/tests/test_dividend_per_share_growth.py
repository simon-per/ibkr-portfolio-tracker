"""
The payer's own dividend growth (docs/dividends.md, *Per-share growth*).

Every other growth figure on the Dividends tab moves when shares are bought, and on a
book still being funded that is most of the movement. This one is per share: the newest
regular payment against the same payment a year earlier. The ways it could lie are a
seasonal fund read as a cut, a special read as a raise, and a payer with no year of
history reported as 0%.
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
from app.repositories.dividend_repository import DividendRepository
from app.schemas.portfolio import DividendPerShareGrowth
from app.services.dividend_forecast import HistPayment, per_share_growth
from app.services.dividend_service import DividendService

AS_OF = date(2026, 7, 29)


def _series(points, **kwargs):
    return [HistPayment(on_date=d, per_share_eur=Decimal(a), **kwargs) for d, a in points]


def _quarters(years, amounts, day=15):
    out = []
    for y, row in zip(years, amounts):
        for m, a in zip((3, 6, 9, 12), row):
            out.append((date(y, m, day), a))
    return out


def test_a_raise_is_measured_against_the_same_payment_a_year_earlier():
    history = _series(_quarters([2025, 2026], [
        ["1.00", "1.00", "1.00", "1.00"],
        ["1.10", "1.10"],
    ]))
    assert per_share_growth(history) == Decimal("0.10")


def test_a_seasonal_funds_small_quarter_is_not_a_cut():
    """March after a large December: compared with last March, it grew."""
    history = _series(_quarters([2025, 2026], [
        ["0.40", "0.70", "0.45", "0.90"],
        ["0.42"],
    ]))
    assert per_share_growth(history) == Decimal("0.05")


def test_a_cut_is_negative():
    history = _series(_quarters([2025, 2026], [
        ["1.00", "1.00", "1.00", "1.00"],
        ["0.50", "0.50"],
    ]))
    assert per_share_growth(history) == Decimal("-0.5")


def test_a_special_is_on_neither_side():
    """IBKR labelled the large payment special: the regular level did not move."""
    history = _series(_quarters([2025, 2026], [
        ["1.00", "1.00", "1.00", "1.00"],
        ["1.00"],
    ])) + _series([(date(2026, 4, 1), "9.00")], special=True)
    assert per_share_growth(history) == Decimal("0")


def test_a_declared_and_unpaid_dividend_is_the_newest_payment():
    """An open accrual joins the history as amount-only evidence, off the cadence."""
    history = _series(_quarters([2025], [["1.00", "1.00", "1.00", "1.00"]])) \
        + _series([(date(2026, 3, 15), "1.20")], cadence=False)
    assert per_share_growth(history) == Decimal("0.20")


def test_under_a_year_of_history_is_unknown_not_zero():
    history = _series([(date(2026, 1, 15), "1.00"), (date(2026, 4, 15), "1.50"),
                       (date(2026, 7, 15), "1.50")])
    assert per_share_growth(history) is None


def test_no_cadence_is_unknown():
    assert per_share_growth(_series([(date(2026, 4, 15), "1.00")])) is None
    assert per_share_growth([]) is None


# ---- Through the service --------------------------------------------------------


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    repo = DividendRepository(session)

    async def payer(sid, symbol, shares, payments):
        session.add(Security(id=sid, isin=f"US000000000{sid}", symbol=symbol,
                             description=symbol, currency="EUR", conid=100 + sid,
                             asset_category="STK", exchange="XETRA"))
        session.add(TaxLot(
            security_id=sid, open_date=date(2024, 1, 1), quantity=Decimal(shares),
            cost_basis=Decimal("1000"), cost_basis_eur=Decimal("1000"),
            price_per_unit=Decimal("10"), currency="EUR", is_open=True,
        ))
        await session.flush()
        for d, per_share in payments:
            gross = Decimal(per_share) * Decimal(shares)
            await repo.upsert_payment({
                "security_id": sid, "ex_date": d, "pay_date": d, "currency": "EUR",
                "amount_per_share": Decimal(per_share), "shares_held": Decimal(shares),
                "gross_amount_eur": gross, "withholding_tax_eur": Decimal("0"),
                "net_amount_eur": gross, "source": "yfinance_estimate",
            })

    # Raised 1.00 -> 1.10 this year; two years of quarterly history.
    await payer(1, "RAISER", "100", [
        (date(2025, 2, 15), "1.00"), (date(2025, 5, 15), "1.00"),
        (date(2025, 8, 15), "1.00"), (date(2025, 11, 15), "1.00"),
        (date(2026, 2, 15), "1.10"), (date(2026, 5, 15), "1.10"),
    ])
    # A payer with three payments: a cadence and a forecast, and no year to compare.
    await payer(2, "RECENT", "100", [
        (date(2026, 1, 20), "0.50"), (date(2026, 4, 20), "0.50"),
        (date(2026, 7, 20), "0.50"),
    ])
    return engine, session


@pytest.mark.asyncio
async def test_each_row_carries_its_payers_growth_and_the_book_weights_them():
    engine, session = await _make_session()
    try:
        bd = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)

        by_symbol = {r["symbol"]: r for r in bd["securities"]}
        assert by_symbol["RAISER"]["per_share_growth_pct"] == 10.0
        assert by_symbol["RECENT"]["per_share_growth_pct"] is None

        lines = {ln["security_id"]: ln["next_12m_eur"] for ln in bd["forecast_by_security"]}
        assert lines[1] > 0 and lines[2] > 0
        per_share = bd["growth"]["per_share"]
        assert set(per_share) == set(DividendPerShareGrowth.model_fields)
        # Only RAISER is measurable, so the figure is its 10% — RECENT is left out and
        # the coverage says how much of the forecast that leaves unmeasured.
        assert per_share["pct"] == 10.0
        assert per_share["payers"] == 1
        assert per_share["covered_pct"] == round(
            lines[1] / (lines[1] + lines[2]) * 100, 1
        )
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_without_a_projection_there_is_no_growth_figure():
    engine, session = await _make_session()
    try:
        bd = await DividendService(session).get_dividend_breakdown(
            as_of=AS_OF, include_forecast=False
        )
        assert bd["growth"]["per_share"] is None
        assert all(r["per_share_growth_pct"] is None for r in bd["securities"])
    finally:
        await session.close()
        await engine.dispose()
