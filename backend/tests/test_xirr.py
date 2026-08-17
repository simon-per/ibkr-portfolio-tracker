"""
calculate_xirr must book capital coming OUT (sale proceeds, dividends), not just
capital going in — otherwise every sale is a phantom loss: selling A to buy B
adds a fresh outflow with no matching inflow, and the planned ETF rotation would
roughly halve the reported return.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.dividend_payment import DividendPayment
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.portfolio_service import PortfolioService

Y_START = date(2025, 1, 2)
MID = date(2025, 7, 2)
Y_END = date(2026, 1, 2)


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    for sid, sym in ((1, "AAA"), (2, "BBB")):
        session.add(Security(id=sid, isin=f"US000000000{sid}", symbol=sym, description=sym,
                             currency="EUR", conid=100 + sid, asset_category="STK",
                             exchange="XETRA"))
    await session.flush()
    return engine, session


def _lot(sid, open_date, qty, cost, close_date=None):
    return TaxLot(
        security_id=sid, open_date=open_date, close_date=close_date,
        is_open=close_date is None, quantity=Decimal(qty),
        cost_basis=Decimal(cost), cost_basis_eur=Decimal(cost),
        price_per_unit=Decimal(cost) / Decimal(qty), currency="EUR",
    )


def _price(sid, d, close):
    return MarketPrice(security_id=sid, date=d, close_price=Decimal(close),
                       currency="EUR", source="test")


@pytest.mark.asyncio
async def test_a_rotation_does_not_crush_xirr():
    engine, session = await _make_session()
    try:
        # A: 100 -> sold at 110 mid-year; B bought with the 110, worth 120 at year end.
        session.add_all([
            _lot(1, Y_START, "10", "100", close_date=MID),
            _lot(2, MID, "10", "110"),
            _price(1, Y_START, "10"), _price(1, MID, "11"),
            _price(2, MID, "11"), _price(2, Y_END, "12"),
        ])
        await session.flush()
        pct, n, _, _, method = await PortfolioService(session).calculate_xirr(Y_START, Y_END)
        # 100 -> 120 over one year: the sale's +110 inflow must cancel B's -110.
        # Without disposal inflows this came out deeply negative.
        assert method == "xirr"
        assert pct is not None and 15 < pct < 25
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_liquidation_still_returns_a_figure():
    engine, session = await _make_session()
    try:
        end = date(2025, 7, 20)
        session.add_all([
            _lot(1, Y_START, "10", "100", close_date=MID),
            _price(1, Y_START, "10"), _price(1, MID, "11"), _price(1, end, "11.5"),
        ])
        await session.flush()
        pct, n, _, _, method = await PortfolioService(session).calculate_xirr(Y_START, end)
        # End value is zero — the +110 proceeds carry the whole return. The old
        # end_mv <= 0 guard returned None here.
        assert method == "xirr"
        assert pct is not None and 10 < pct < 35
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_dividends_lift_the_return():
    async def _run(with_dividend: bool):
        engine, session = await _make_session()
        try:
            session.add_all([
                _lot(1, Y_START, "10", "100"),
                _price(1, Y_START, "10"), _price(1, Y_END, "12"),
            ])
            if with_dividend:
                session.add(DividendPayment(
                    security_id=1, ex_date=date(2025, 6, 30), pay_date=date(2025, 6, 30),
                    shares_held=Decimal("0"), gross_amount_eur=Decimal("5"),
                    withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("5"),
                    source="ibkr",
                ))
            await session.flush()
            pct, *_ = await PortfolioService(session).calculate_xirr(Y_START, Y_END)
            return pct
        finally:
            await session.close()
            await engine.dispose()

    without = await _run(False)
    with_div = await _run(True)
    assert without is not None and with_div is not None
    assert with_div > without


@pytest.mark.asyncio
async def test_a_legacy_dividend_row_still_counts_as_an_inflow():
    """
    Rows predating the withholding-fields migration carry gross with a NULL net.
    Keying the inflow on net alone dropped that income from the return entirely
    (and would have crashed had it not been filtered out first).
    """
    async def _run(with_dividend: bool):
        engine, session = await _make_session()
        try:
            session.add_all([
                _lot(1, Y_START, "10", "100"),
                _price(1, Y_START, "10"), _price(1, Y_END, "12"),
            ])
            if with_dividend:
                session.add(DividendPayment(
                    security_id=1, ex_date=date(2025, 6, 30), pay_date=date(2025, 6, 30),
                    shares_held=Decimal("10"), gross_amount_eur=Decimal("5"),
                    withholding_tax_eur=None, net_amount_eur=None,  # the legacy shape
                    source="yfinance_estimate",
                ))
            await session.flush()
            pct, *_ = await PortfolioService(session).calculate_xirr(Y_START, Y_END)
            return pct
        finally:
            await session.close()
            await engine.dispose()

    without = await _run(False)
    with_div = await _run(True)
    assert without is not None and with_div is not None
    assert with_div > without


@pytest.mark.asyncio
async def test_short_windows_are_labelled_simple_period():
    engine, session = await _make_session()
    try:
        d2 = date(2026, 1, 12)
        session.add_all([
            _lot(1, date(2025, 6, 2), "10", "100"),
            _price(1, Y_END, "10"), _price(1, d2, "10.5"),
        ])
        await session.flush()
        pct, n, _, _, method = await PortfolioService(session).calculate_xirr(Y_END, d2)
        assert method == "simple_period"
        assert pct == pytest.approx(5.0, abs=0.01)  # NOT annualized
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unpriced_endpoint_is_reported_rather_than_hidden():
    """
    A holding the backend cannot value at the window end drops out of the terminal
    inflow while its purchase stays in the flow list, so the return understates — the
    same shape `/attribution` and the timeline already report. XIRR read only
    `market_value_eur` and threw the count away, so nothing on the wire said the
    figure was measured against a partial book.
    """
    engine, session = await _make_session()
    try:
        session.add_all([
            _lot(1, Y_START, "10", "100"),
            _lot(2, Y_START, "10", "100"),
            _price(1, Y_START, "10"), _price(1, Y_END, "12"),
            # BBB is priced at the start and goes quiet: a year past its last close it
            # is far outside the lookback, so the end valuation cannot include it.
            _price(2, Y_START, "10"),
        ])
        await session.flush()
        service = PortfolioService(session)
        pct, _, _, _, method = await service.calculate_xirr(Y_START, Y_END)

        assert service.last_xirr_unpriced == 1
        # Still served: a partial valuation is the best available answer, and the
        # count is what stops it reading as a complete one.
        assert method == "xirr" and pct is not None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_fully_priced_window_reports_no_unpriced_holdings():
    """The other side of the same field: it must be able to say 'complete'."""
    engine, session = await _make_session()
    try:
        session.add_all([
            _lot(1, Y_START, "10", "100"),
            _price(1, Y_START, "10"), _price(1, Y_END, "12"),
        ])
        await session.flush()
        service = PortfolioService(session)
        await service.calculate_xirr(Y_START, Y_END)
        assert service.last_xirr_unpriced == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_unpriced_latch_does_not_leak_between_windows():
    """
    One service, two windows: the second answer must not inherit the first's
    completeness. `last_snapshot_skipped` learned this on its early-return path, and a
    latch that only resets in __init__ is the same bug waiting for a second caller.
    """
    engine, session = await _make_session()
    try:
        session.add_all([
            _lot(1, Y_START, "10", "100"),
            _lot(2, Y_START, "10", "100"),
            _price(1, Y_START, "10"), _price(1, MID, "11"), _price(1, Y_END, "12"),
            _price(2, Y_START, "10"), _price(2, MID, "11"),
        ])
        await session.flush()
        service = PortfolioService(session)

        await service.calculate_xirr(Y_START, Y_END)      # BBB unvaluable at Y_END
        assert service.last_xirr_unpriced == 1
        await service.calculate_xirr(Y_START, MID)        # both priced at MID
        assert service.last_xirr_unpriced == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_window_starting_before_the_first_purchase_is_well_posed():
    engine, session = await _make_session()
    try:
        session.add_all([
            _lot(1, Y_START, "10", "100"),
            _price(1, Y_START, "10"), _price(1, Y_END, "12"),
        ])
        await session.flush()
        pct, n, _, _, method = await PortfolioService(session).calculate_xirr(
            date(2024, 6, 1), Y_END
        )
        # Start value is legitimately zero; the old start_mv <= 0 guard blanked this.
        assert method == "xirr"
        assert pct is not None and pct > 0
    finally:
        await session.close()
        await engine.dispose()
