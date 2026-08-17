"""
`find_stale_etf_baskets` — the look-through's missing alarm.

Every other decaying dataset in this app has a detector contributing `warnings[]`;
baskets had only a badge on a tab someone has to open. Nine of the ten feeds republish
daily, so a week after a CLI run the whole look-through describes an older index while
the numbers stay confidently on screen.

The tests that matter are the ones about what must NOT warn: a warning that can never
clear is the pathology the permanent Flex banner already taught this codebase, and it
trains the reader to skip the banner that also carries a skipped tax lot.
"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.etf_sources import FUND_SOURCES
from app.models.etf_basket import EtfBasket
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.lookthrough_service import stale_after_days
from app.services.scheduler_service import SchedulerService

# Real registry entries, because the point of the detector is the registry's own rules.
IWDA = "IE00B4L5Y983"      # blackrock, republishes daily -> 7-day expectation
VT = "US9220427424"        # vanguard_us, month-end -> 75-day expectation
VWCE = "IE00BK5BQT80"      # adapter="manual", borrows VT's basket
SOXQ = "US46138G6153"      # invesco, fetchable
AAPL = "US0378331005"      # not a fund at all


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


async def _hold(db, security_id, isin, symbol, is_open=True):
    db.add(Security(
        id=security_id, isin=isin, symbol=symbol, description=symbol,
        currency="EUR", conid=2000 + security_id, asset_category="STK",
        exchange="XETRA",
    ))
    db.add(TaxLot(
        security_id=security_id, open_date=date(2026, 1, 5), quantity=Decimal("10"),
        cost_basis=Decimal("1000"), price_per_unit=Decimal("100"), currency="EUR",
        cost_basis_eur=Decimal("1000"), is_open=is_open,
        close_date=None if is_open else date(2026, 6, 1),
    ))
    await db.flush()


async def _basket(db, fund_isin, adapter, age_days, equity=Decimal("99")):
    db.add(EtfBasket(
        fund_isin=fund_isin,
        as_of_date=date.today() - timedelta(days=age_days),
        as_of_is_issuer_stated=True,
        source="test", adapter=adapter,
        source_rows=100, stored_rows=100, skipped_rows=0,
        total_weight_pct=Decimal("100"), equity_weight_pct=equity,
        identifier_coverage_pct=Decimal("100"), asset_class_available=True,
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_a_daily_feed_gone_a_week_stale_is_reported_with_its_age(db):
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=stale_after_days("blackrock") + 5)

    warnings = await SchedulerService().find_stale_etf_baskets(db)

    assert len(warnings) == 1
    assert "IWDA" in warnings[0]
    assert f"{stale_after_days('blackrock') + 5} days old" in warnings[0]
    # The recovery is two CLI steps in a specific order, and the order is the part
    # people get wrong: a re-import clears the CINS/SEDOL resolutions on purpose.
    assert "fetch_etf_baskets" in warnings[0]
    assert warnings[0].index("fetch_etf_baskets") < warnings[0].index("resolve_identities")


@pytest.mark.asyncio
async def test_a_fresh_basket_is_not_reported(db):
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=2)

    assert await SchedulerService().find_stale_etf_baskets(db) == []


@pytest.mark.asyncio
async def test_the_threshold_is_the_issuers_own_cadence_not_a_global_one(db):
    """
    A single global constant meant two different things and badged Vanguard permanently
    for publishing month-end exactly as documented. 30 days is stale for a daily feed
    and entirely normal for Vanguard US.
    """
    await _hold(db, 1, IWDA, "IWDA")
    await _hold(db, 2, VT, "VT")
    await _basket(db, IWDA, "blackrock", age_days=30)
    await _basket(db, VT, "vanguard_us", age_days=30)

    warnings = await SchedulerService().find_stale_etf_baskets(db)

    assert len(warnings) == 1
    assert "IWDA" in warnings[0]


@pytest.mark.asyncio
async def test_a_held_fund_with_no_basket_but_a_route_is_reported(db):
    """What a newly bought fund looks like: its whole value is missing from the company
    rows, and one CLI run fixes it."""
    await _hold(db, 1, SOXQ, "SOXQ")

    warnings = await SchedulerService().find_stale_etf_baskets(db)

    assert len(warnings) == 1
    assert "SOXQ" in warnings[0] and "has ever been fetched" in warnings[0]


@pytest.mark.asyncio
async def test_a_fund_excluded_from_look_through_never_warns(db, monkeypatch):
    """
    An excluded fund's basket is not wanted at any age, so no run of anything clears
    this — and an unclearable warning is the permanent-Flex-banner pathology. No fund
    carries `exclude_reason` today (DBPG's was dropped when it gained VOO's basket), so
    the rule is pinned against a temporarily excluded entry rather than left untested.
    """
    monkeypatch.setitem(
        FUND_SOURCES, SOXQ,
        replace(FUND_SOURCES[SOXQ], exclude_reason="Not decomposable, by design."),
    )
    await _hold(db, 1, SOXQ, "SOXQ")

    assert await SchedulerService().find_stale_etf_baskets(db) == []


@pytest.mark.asyncio
async def test_a_fund_with_no_route_at_all_never_warns(db, monkeypatch):
    """
    A hand-download-only fund with nothing to borrow: the look-through tab names it in
    the fund table, where it does not train anyone to ignore a banner.
    """
    monkeypatch.setitem(
        FUND_SOURCES, SOXQ,
        replace(FUND_SOURCES[SOXQ], adapter="manual", basket_proxy_isin=None),
    )
    await _hold(db, 1, SOXQ, "SOXQ")

    assert await SchedulerService().find_stale_etf_baskets(db) == []


@pytest.mark.asyncio
async def test_a_borrowed_basket_is_judged_by_the_file_it_actually_came_from(db):
    """
    VWCE reads VT's basket, so the age that matters is VT's and the expectation that
    matters is `vanguard_us`'s — not the 45-day default its own `manual` adapter would
    get. This is the read path's rule, reached through its own resolver rather than a
    second copy of it.
    """
    await _hold(db, 1, VWCE, "VWCE")
    await _basket(db, VT, "vanguard_us", age_days=stale_after_days("vanguard_us") + 10)

    warnings = await SchedulerService().find_stale_etf_baskets(db)

    assert len(warnings) == 1
    assert "VWCE" in warnings[0]
    assert "vanguard_us" in warnings[0]

    # ...and a VT basket inside Vanguard's own cadence keeps VWCE quiet.
    fresh = await SchedulerService().find_stale_etf_baskets(
        db, as_of=date.today() - timedelta(days=stale_after_days("vanguard_us") + 10)
    )
    assert fresh == []


@pytest.mark.asyncio
async def test_a_proxied_fund_with_no_source_basket_is_actionable(db):
    """
    The gap the first draft of this detector had: VWCE's own adapter is `manual`, so
    "has a route" keyed on it alone silently skipped the one case that IS fixable —
    VT is fetchable, and without its basket VWCE contributes nothing at all.
    """
    await _hold(db, 1, VWCE, "VWCE")
    await _hold(db, 2, SOXQ, "SOXQ")
    # SOXQ present and fresh, so only the VWCE/VT case can produce a warning.
    await _basket(db, SOXQ, "invesco", age_days=1)

    warnings = await SchedulerService().find_stale_etf_baskets(db)

    # Still silent: with no VT basket stored, VWCE's proxy has nothing to borrow, and
    # `fetch_etf_baskets --all` does fetch VT — so this must be reported.
    assert len(warnings) == 1
    assert "VWCE" in warnings[0]


@pytest.mark.asyncio
async def test_a_fund_no_longer_held_is_not_reported(db):
    """Mirrors find_stale_priced_securities: a basket for a fund nobody holds moves no
    figure, and warning about it is permanent noise."""
    await _hold(db, 1, IWDA, "IWDA", is_open=False)
    await _basket(db, IWDA, "blackrock", age_days=400)

    assert await SchedulerService().find_stale_etf_baskets(db) == []


@pytest.mark.asyncio
async def test_a_directly_held_stock_is_not_a_fund(db):
    await _hold(db, 1, AAPL, "AAPL")

    assert await SchedulerService().find_stale_etf_baskets(db) == []


@pytest.mark.asyncio
async def test_an_empty_portfolio_is_silent(db):
    assert await SchedulerService().find_stale_etf_baskets(db) == []


def test_every_detector_is_actually_wired_into_the_market_data_job():
    """
    A detector nobody calls is the same silent failure as no detector, and the wiring is
    five lines in a `try/except` that no unit test of the function itself can see.

    They hang off the **market-data** job specifically: those slots succeed while Flex is
    refusing, so hanging any of them off an IBKR job would silence it in exactly the
    outage it exists to report. Asserting over the whole family rather than the newest
    member, so the sixth detector is caught the same way.
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(SchedulerService.sync_market_data))
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for detector in (
        "find_stale_priced_securities",
        "find_dividends_predating_their_mapping",
        "find_flex_generation_gap",
        "find_stale_ibkr_sync",
        "find_stale_etf_baskets",
    ):
        assert detector in called, f"{detector} is never called by sync_market_data"
