"""
A Steuerwert missing a holding must say so, not just be smaller.

`holdings_snapshot_as_of` omits a lot it cannot price or convert — deliberately, and
CLAUDE.md says so: a position with no resolvable price near that date is left out rather
than counted as zero. That is the right arithmetic and it was, until 2026-08-05, a
completely silent one.

The tax report already handled the snapshot *raising*: `holdings_snapshot_total` becomes
`None` and a warning says the wealth-tax base is unavailable rather than zero. It had
nothing for the snapshot quietly returning fewer rows, which is the **partial** case —
and the partial case is the dangerous one, for the same reason the value timeline's
`+15.3%` was worse than its `-100%`: a plausible number reads as an answer where a
missing one reads as a fault.

It matters more here than anywhere else in the app. This figure is the base for a Swiss
wealth-tax declaration, and `warnings[]` rides on a **successful** 200 response, so it is
structurally invisible unless something renders it — which `TaxTab` and `to_csv` do.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.market_price import MarketPrice
from app.services.portfolio_service import PortfolioService
from app.services.tax_service import TaxService
from app.clock import utc_today

YEAR = 2026
AS_OF = date(YEAR, 12, 31)
OPEN_DATE = date(YEAR, 1, 15)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
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


async def _holding(db, *, symbol, isin, conid, priced: bool, currency="EUR"):
    security = Security(
        isin=isin, symbol=symbol, description=f"{symbol} Inc", currency=currency,
        conid=conid, asset_category="STK", exchange="NASDAQ",
    )
    db.add(security)
    await db.flush()
    db.add(TaxLot(
        security_id=security.id, open_date=OPEN_DATE,
        quantity=Decimal("10"), cost_basis=Decimal("500"),
        price_per_unit=Decimal("50"), currency=currency,
        cost_basis_eur=Decimal("500"), is_open=True,
    ))
    if priced:
        # Prices around BOTH 31 December and today: `get_tax_report` values the current
        # year at `today` (a year still running has no year-end close yet) and a past
        # year at 31 December, so a fixture pinned to one of them silently exercises
        # the wrong path.
        for anchor in (AS_OF, utc_today()):
            for offset in (0, 1, 2):
                db.add(MarketPrice(
                    security_id=security.id, date=anchor - timedelta(days=offset),
                    close_price=Decimal("100"), currency=currency, source="test",
                ))
    return security


@pytest.mark.asyncio
async def test_the_snapshot_records_what_it_had_to_drop(db):
    await _holding(db, symbol="OKAY", isin="US0000000001", conid=1, priced=True)
    await _holding(db, symbol="DARK", isin="US0000000002", conid=2, priced=False)
    await db.commit()

    service = PortfolioService(db)
    base_fx = await service._load_base_fx()
    rows = await service.holdings_snapshot_as_of(base_fx, AS_OF)

    assert {r["symbol"] for r in rows} == {"OKAY"}
    assert service.last_snapshot_skipped == ["DARK"]


@pytest.mark.asyncio
async def test_a_complete_snapshot_records_nothing(db):
    """The latch must be inert on good data, and must not carry a stale value."""
    await _holding(db, symbol="AAA", isin="US0000000001", conid=1, priced=True)
    await _holding(db, symbol="BBB", isin="US0000000002", conid=2, priced=True)
    await db.commit()

    service = PortfolioService(db)
    base_fx = await service._load_base_fx()
    await service.holdings_snapshot_as_of(base_fx, AS_OF)

    assert service.last_snapshot_skipped == []


@pytest.mark.asyncio
async def test_the_latch_is_reset_between_runs(db):
    """
    Per-run state: a value left over from an earlier date would report the wrong
    snapshot's completeness, which is worse than reporting none.
    """
    await _holding(db, symbol="DARK", isin="US0000000002", conid=2, priced=False)
    await db.commit()

    service = PortfolioService(db)
    base_fx = await service._load_base_fx()
    await service.holdings_snapshot_as_of(base_fx, AS_OF)
    assert service.last_snapshot_skipped == ["DARK"]

    # A date before the lot exists: nothing to value, nothing to skip.
    await service.holdings_snapshot_as_of(base_fx, OPEN_DATE - timedelta(days=30))
    assert service.last_snapshot_skipped == []


@pytest.mark.asyncio
async def test_the_tax_report_warns_that_the_wealth_tax_base_understates(db):
    """
    The point of the whole change: the figure is still served, because it is the best
    available, but it can no longer be mistaken for the complete book.
    """
    await _holding(db, symbol="OKAY", isin="US0000000001", conid=1, priced=True)
    await _holding(db, symbol="DARK", isin="US0000000002", conid=2, priced=False)
    await db.commit()

    report = await TaxService(db).get_tax_report(YEAR)

    assert report["holdings_snapshot_total"] is not None, (
        "a partial snapshot is still the best available base — it must not become None, "
        "which is reserved for a snapshot that could not be built at all"
    )
    joined = " ".join(report["warnings"])
    assert "DARK" in joined, "the omitted security is not named"
    assert "understates" in joined
    assert len(report["holdings_snapshot"]) == 1


@pytest.mark.asyncio
async def test_a_complete_tax_report_carries_no_snapshot_warning(db):
    """The mirror direction: a warning on every clean report is one nobody reads."""
    await _holding(db, symbol="AAA", isin="US0000000001", conid=1, priced=True)
    await db.commit()

    report = await TaxService(db).get_tax_report(YEAR)

    assert not any("understates" in w for w in report["warnings"])
    assert report["holdings_snapshot_total"] is not None
