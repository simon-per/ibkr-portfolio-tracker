"""
A closed lot with no price near its close date is left out of
`realized_rows_from_closed_lots` — and every reader that uses those rows as a window's
disposal term must say so rather than read the sale as a total loss (GitHub issue #19).

SOLD is priced at the window start and sold inside the window, but has no price in the
14 days before its close date. HELD is priced throughout. GOOD is a second sale
that *can* be priced, so the closed-positions view has a non-empty path too.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.market_price import MarketPrice
from app.services.performance_analytics_service import PerformanceAnalyticsService
from app.services.portfolio_service import PortfolioService
from app.services.tax_service import TaxService

START = date(2026, 3, 2)
END = date(2026, 3, 31)
SALE = START + timedelta(days=20)  # Sunday 2026-03-22; nearest price is 20 days earlier
SALE_ROW = "2026-03-23"            # the weekday row a Sunday flow is booked on


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


def _security(n: int, symbol: str) -> Security:
    return Security(isin=f"US00000000{n:02d}", symbol=symbol, description=symbol,
                    currency="EUR", conid=n, asset_category="STK", exchange="NASDAQ")


def _lot(sec: Security, close: Optional[date] = None) -> TaxLot:
    return TaxLot(
        security_id=sec.id, open_date=START - timedelta(days=60),
        quantity=Decimal("10"), cost_basis=Decimal("500"), price_per_unit=Decimal("50"),
        currency="EUR", cost_basis_eur=Decimal("500"),
        is_open=close is None, close_date=close,
    )


async def _seed(db, with_good_sale: bool = False):
    sold, held = _security(11, "SOLD"), _security(12, "HELD")
    db.add_all([sold, held])
    await db.flush()
    db.add_all([_lot(sold, SALE), _lot(held)])
    # HELD weekly, so it stays valued every day and only SOLD can flag a timeline row.
    held_dates = [START + timedelta(days=7 * k) for k in range(5)] + [END]
    for sec, d in [(sold, START)] + [(held, d) for d in held_dates]:
        db.add(MarketPrice(security_id=sec.id, date=d, close_price=Decimal("100"),
                           currency="EUR", source="test"))
    if with_good_sale:
        good = _security(13, "GOOD")
        db.add(good)
        await db.flush()
        db.add(_lot(good, SALE))
        db.add(MarketPrice(security_id=good.id, date=SALE - timedelta(days=2),
                           close_price=Decimal("80"), currency="EUR", source="test"))
    await db.commit()


@pytest.mark.asyncio
async def test_the_latch_names_the_skipped_lot_and_resets_on_the_next_call(db):
    await _seed(db)
    svc = PortfolioService(db)
    base_fx = await svc._load_base_fx()

    rows = await svc.realized_rows_from_closed_lots(base_fx)
    assert rows == []
    sold_id = (await db.get(TaxLot, 1)).security_id
    assert svc.last_realized_skipped == {sold_id: {SALE}}

    # A window with no closed lot takes the early return — and must not inherit the
    # previous run's skips.
    await svc.realized_rows_from_closed_lots(base_fx, start=END, end=END)
    assert svc.last_realized_skipped == {}


@pytest.mark.asyncio
async def test_attribution_excludes_the_unpriceable_sale_and_counts_it(db):
    await _seed(db)
    result = await PortfolioService(db).get_performance_attribution(START, END)
    by_symbol = {a["symbol"]: a for a in result["attributions"]}
    # Not `pnl = -start_value`: the sale whose proceeds could not be priced is
    # excluded from both sides and counted.
    assert "SOLD" not in by_symbol
    assert result["unpriced_holdings"] == 1
    assert by_symbol["HELD"]["pnl_contribution_eur"] == 0.0


@pytest.mark.asyncio
async def test_xirr_counts_the_missing_sale_proceeds(db):
    await _seed(db)
    svc = PortfolioService(db)
    await svc.calculate_xirr(START, END)
    # Start outflow and terminal inflow only — the +proceeds flow is gone.
    assert svc.last_xirr_unpriced >= 1


@pytest.mark.asyncio
async def test_the_timeline_flags_the_row_whose_flow_is_short(db):
    await _seed(db)
    rows = {r["date"]: r for r in
            await PortfolioService(db).get_portfolio_value_over_time(START, END)}
    # SOLD is no longer held on its close day, so only the skipped disposal can flag it;
    # a Sunday sale rides to Monday's row exactly as its flow would.
    assert rows[SALE_ROW]["unpriced_holdings"] == 1
    # The flag belongs to that one row, not to every day after it.
    assert rows["2026-03-24"]["unpriced_holdings"] == 0


@pytest.mark.asyncio
async def test_closed_positions_warn_when_every_sale_is_unpriceable(db):
    await _seed(db)
    out = await PerformanceAnalyticsService(db).closed_positions()
    assert out["positions"] == []
    assert any("1 closed lot could not be priced" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_closed_positions_warn_beside_the_sales_that_were_priced(db):
    await _seed(db, with_good_sale=True)
    out = await PerformanceAnalyticsService(db).closed_positions()
    assert [p["symbol"] for p in out["positions"]] == ["GOOD"]
    assert any("1 closed lot could not be priced" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_the_tax_estimate_warns_about_an_unpriceable_closed_lot(db):
    await _seed(db, with_good_sale=True)
    report = await TaxService(db).get_tax_report(2026)
    assert report["realized_source"] == "closed_lot_estimate"
    assert [r["symbol"] for r in report["realized_gains"]] == ["GOOD"]
    assert any("1 closed lot(s) could not be priced" in w for w in report["warnings"])
