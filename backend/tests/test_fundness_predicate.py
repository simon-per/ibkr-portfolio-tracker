"""
"Is this holding a fund?" must have exactly one answer, and it is keyed on ISIN.

`ETF_ALLOCATIONS` is keyed by symbol for readability, and for a while three call sites in
`allocation_service` asked it that way — while `lookthrough_service` asked
`is_known_etf_isin`. So the app carried two answers to one question, which is the failure
mode CLAUDE.md opens with, and the weaker of the two was deciding a *figure*:

- **A ticker is not an identity.** This account holds the **UCITS** VanEck fund
  `IE00BMC38736`; the far better-known US `SMH` shares its ticker. A symbol lookup cannot
  tell them apart and would hand one fund the other's sector and region split.
- **Worse, a plain stock whose ticker collides with a row** would be distributed across
  eleven sectors as though it were a fund — a fabricated allocation, not a caveat. (The
  frontend's `currencyExposure.ts` also matches funds by symbol, and that one is
  *accepted* precisely because its output is a stated caveat rather than a number.)

Nothing was wrong on this account when it was found: every held fund's symbol happened to
be unique. That is what latent means, and the fix is cheap while the collision costs a
sector chart.

Offline: no Yahoo, no network.
"""
import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.etf_mappings import (
    ETF_ALLOCATIONS,
    allocation_for_fund_isin,
    is_known_etf_isin,
)
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.allocation_service import AllocationService

VT_ISIN = "US9220427424"          # a real row in the table
NOT_A_FUND = "US0000000009"       # same ticker, different instrument


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


async def _hold(db, symbol, isin, conid, sector=None, country=None):
    security = Security(
        isin=isin, symbol=symbol, description=symbol, currency="EUR", conid=conid,
        asset_category="STK", exchange="NASDAQ", sector=sector, country=country,
        asset_type="Stock",
    )
    db.add(security)
    await db.flush()
    db.add(TaxLot(
        security_id=security.id, open_date=date.today() - timedelta(days=100),
        quantity=Decimal("10"), cost_basis=Decimal("1000"),
        price_per_unit=Decimal("100"), currency="EUR",
        cost_basis_eur=Decimal("1000"), is_open=True,
    ))
    db.add(MarketPrice(
        security_id=security.id, date=date.today(), close_price=Decimal("100"),
        currency="EUR", source="test",
    ))
    await db.flush()
    return security


class TestTheAccessor:
    def test_resolves_a_declared_fund_isin(self):
        assert is_known_etf_isin(VT_ISIN)
        assert allocation_for_fund_isin(VT_ISIN)["asset_type"] == "ETF"

    def test_refuses_an_isin_the_table_does_not_declare(self):
        assert not is_known_etf_isin(NOT_A_FUND)
        assert allocation_for_fund_isin(NOT_A_FUND) is None

    def test_survives_a_missing_isin_rather_than_raising(self):
        # A security row can carry a blank ISIN, and the two-step composition this
        # replaced (`get_etf_allocation(symbol_for_fund_isin(isin))`) raises on None.
        assert allocation_for_fund_isin(None) is None
        assert allocation_for_fund_isin("") is None

    def test_is_case_insensitive_like_its_sibling(self):
        assert allocation_for_fund_isin(VT_ISIN.lower()) is not None

    def test_every_row_is_reachable_by_isin(self):
        """
        The precondition for keying on ISIN at all: a row with no `isins` would silently
        stop being a fund the moment the symbol lookup went away, and its holding would
        drop from being distributed across sectors to a single Unknown slice.
        """
        unreachable = [
            symbol for symbol, entry in ETF_ALLOCATIONS.items()
            if not any(allocation_for_fund_isin(i) is entry for i in entry.get("isins", ()))
        ]
        assert unreachable == [], (
            f"these rows cannot be reached by any declared ISIN: {unreachable}"
        )


@pytest.mark.asyncio
async def test_a_stock_sharing_a_funds_ticker_is_not_distributed_as_a_fund(db):
    """
    The fabrication the symbol lookup allowed: one holding, eleven sector slices, all
    invented from an unrelated fund's basket.
    """
    await _hold(db, symbol="VT", isin=NOT_A_FUND, conid=1,
                sector="Technology", country="United States")

    allocation = await AllocationService(db).get_portfolio_allocation()

    # Its own column, one bucket — not a basket spread across eleven.
    assert list(allocation["sector_allocation"]) == ["Technology"]
    assert list(allocation["asset_type_allocation"]) == ["Stock"]
    contributions = [
        p for cat in allocation["sector_allocation"].values() for p in cat["positions"]
    ]
    assert not any(p["is_etf_contribution"] for p in contributions)


@pytest.mark.asyncio
async def test_the_real_fund_is_still_distributed(db):
    """The other direction, so the fix cannot be 'classify nothing as a fund'."""
    await _hold(db, symbol="VT", isin=VT_ISIN, conid=1)

    allocation = await AllocationService(db).get_portfolio_allocation()

    assert list(allocation["asset_type_allocation"]) == ["ETF"]
    assert len(allocation["sector_allocation"]) > 1
    assert all(
        p["is_etf_contribution"]
        for cat in allocation["sector_allocation"].values()
        for p in cat["positions"]
    )


@pytest.mark.asyncio
async def test_two_holdings_with_one_ticker_are_classified_separately(db):
    """
    The live shape of the collision: the UCITS line and its US namesake, or a fund beside
    a stock of the same name. The symbol lookup gave both the same answer by construction.
    """
    await _hold(db, symbol="VT", isin=VT_ISIN, conid=1)
    await _hold(db, symbol="VT", isin=NOT_A_FUND, conid=2,
                sector="Technology", country="United States")

    allocation = await AllocationService(db).get_portfolio_allocation()

    # One is a fund and one is not, so both buckets exist.
    assert set(allocation["asset_type_allocation"]) == {"ETF", "Stock"}


def test_no_service_classifies_a_holding_by_ticker():
    """
    The family assertion: catch the next call site, not just the three that existed.

    `is_known_etf` and `get_etf_allocation` are symbol-keyed lookups over the table, kept
    for the table's own use and for `allocation_for_fund_isin` to build on. A *service*
    calling either is asking "is this holding a fund?" the wrong way.
    """
    services = Path(__file__).resolve().parent.parent / "app" / "services"
    banned = {"is_known_etf", "get_etf_allocation"}
    offenders = []
    for path in sorted(services.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in banned:
                    offenders.append(f"{path.name}:{node.lineno} {node.func.id}()")

    assert offenders == [], (
        "these services decide fundness by ticker instead of ISIN: "
        f"{offenders}. Use `allocation_for_fund_isin` / `is_known_etf_isin` — a symbol is "
        "not an identity, and the UCITS SMH held here shares its ticker with the US one."
    )
