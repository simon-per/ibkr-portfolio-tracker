"""
The look-through keeps itself current: `etf_basket_refresh`.

Until 2026-09-08 a stale basket only produced a warning, and on production that warning had
been on every market-data run for two weeks — seven funds, the same line seven times. The
data was always one keyless HTTP call away and `etf_baskets` was always the cache; what was
missing was a trigger nobody had to remember.

The tests that matter are the refusals and the isolation: the scheduled path must refuse
everything the CLI refuses (a fetch failure, a parse failure, a row-count collapse, a
backwards as-of all leave the previous basket in use), one fund's failure must be that
fund's warning and not the job's error, and the detector and the refresh must consume one
verdict — two definitions of "stale" would be the codebase's dominant failure mode arriving
by a new route.

No network: fetch and parse are injected, and the identity providers are patched out.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import List

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.etf_basket import EtfBasket, EtfHolding
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.services import etf_basket_refresh as refresh_mod
from app.services.etf_basket_parsers import ConstituentRow, ParsedBasket
from app.services.etf_basket_refresh import (
    SCHEDULED_IDENTITY_LIMIT,
    StaleBasket,
    refresh_lookthrough_data,
    refresh_stale_baskets,
    stale_basket_verdicts,
)
from app.services.identity_service import IdentityService
from app.services.lookthrough_service import LookthroughService, stale_after_days
from app.services.scheduler_service import SchedulerService

# Real registry entries, because the point is the registry's own rules.
IWDA = "IE00B4L5Y983"      # blackrock, republishes daily -> 7-day expectation
SOXQ = "US46138G6153"      # invesco, fetchable
VT = "US9220427424"        # vanguard_us, month-end -> 75-day expectation
VWCE = "IE00BK5BQT80"      # adapter="manual", borrows VT's basket
TODAY = date(2026, 9, 8)


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


@pytest.fixture(autouse=True)
def _no_identity_network(monkeypatch):
    """The identity pass is exercised by its own tests below; everywhere else it is inert."""
    async def _resolve(self, isins, limit=None):
        return {"requested": len(list(isins)), "rows_written": 0}

    async def _constituents(self, limit=None):
        return {"identifiers_pending": 0, "holdings_updated": 0}

    async def _material(self, *a, **k):
        return []

    monkeypatch.setattr(IdentityService, "resolve", _resolve)
    monkeypatch.setattr(IdentityService, "resolve_constituent_identifiers", _constituents)
    monkeypatch.setattr(LookthroughService, "material_constituent_isins", _material)


async def _hold(db, security_id, isin, symbol):
    db.add(Security(
        id=security_id, isin=isin, symbol=symbol, description=symbol,
        currency="EUR", conid=2000 + security_id, asset_category="STK", exchange="XETRA",
    ))
    db.add(TaxLot(
        security_id=security_id, open_date=date(2026, 1, 5), quantity=Decimal("10"),
        cost_basis=Decimal("1000"), price_per_unit=Decimal("100"), currency="EUR",
        cost_basis_eur=Decimal("1000"), is_open=True,
    ))
    # Committed, not flushed: the refresh rolls back on a failed fund, and production rows
    # are committed — a flushed fixture would vanish with the rollback and fake a deletion.
    await db.commit()


async def _basket(db, fund_isin, adapter, age_days, rows=2):
    db.add(EtfBasket(
        fund_isin=fund_isin,
        as_of_date=TODAY - timedelta(days=age_days),
        as_of_is_issuer_stated=True,
        source="test", adapter=adapter,
        source_rows=rows, stored_rows=rows, skipped_rows=0,
        total_weight_pct=Decimal("100"), equity_weight_pct=Decimal("99"),
        identifier_coverage_pct=Decimal("100"), asset_class_available=True,
    ))
    for n in range(1, rows + 1):
        db.add(EtfHolding(
            fund_isin=fund_isin, line_no=n, constituent_isin=f"US000000000{n}",
            constituent_name=f"Old {n}", weight_pct=Decimal("1"), asset_class="Equity",
        ))
    await db.commit()


def _parsed(fund_isin, adapter, as_of=TODAY, rows=2) -> ParsedBasket:
    return ParsedBasket(
        fund_isin=fund_isin, as_of_date=as_of, source="test", adapter=adapter,
        rows=[
            ConstituentRow(line_no=n, name=f"New {n}", weight_pct=Decimal("2"),
                           isin=f"US00000000{n}9", asset_class="Equity")
            for n in range(1, rows + 1)
        ],
        source_rows=rows,
    )


class _Client:
    """Stands in for httpx.AsyncClient; the injected fetch never calls it."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Fetcher:
    """Records every source fetched; raises for the ISINs it is told to fail."""

    def __init__(self, fail: List[str] = ()):
        self.calls: List[str] = []
        self.fail = set(fail)

    async def __call__(self, client, isin, source):
        self.calls.append(isin)
        if isin in self.fail:
            raise ConnectionError(f"{isin}: issuer unreachable")
        return [b"body"]


def _parser(as_of=TODAY, rows=2):
    def parse(fund_isin, bodies, adapter, symbol, fetched_on):
        return _parsed(fund_isin, adapter, as_of=as_of, rows=rows)
    return parse


async def _run(db, fetcher, parser=None, as_of=TODAY):
    return await refresh_stale_baskets(
        db, as_of, client_factory=_Client, fetch=fetcher, parse=parser or _parser(),
    )


async def _as_of(db, isin):
    return (await EtfBasketRepository(db).get_baskets([isin]))[isin].as_of_date


# ── Fetching what is stale, and only that ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stale_fetchable_basket_is_refetched_and_replaced(db):
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=stale_after_days("blackrock") + 5)
    fetcher = _Fetcher()

    result = await _run(db, fetcher)

    assert fetcher.calls == [IWDA]
    assert await _as_of(db, IWDA) == TODAY
    assert result["status"] == "success"
    assert [r["symbol"] for r in result["refreshed"]] == ["IWDA"]
    assert result["refreshed"][0]["for"] == ["IWDA"]
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_a_fresh_basket_is_left_alone(db):
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=2)
    fetcher = _Fetcher()

    result = await _run(db, fetcher)

    assert fetcher.calls == []
    assert result["refreshed"] == [] and result["stale"] == []


@pytest.mark.asyncio
async def test_a_never_fetched_fund_with_a_route_is_fetched(db):
    """What a newly bought fund looks like: declared, held, no basket yet."""
    await _hold(db, 1, SOXQ, "SOXQ")
    fetcher = _Fetcher()

    result = await _run(db, fetcher)

    assert fetcher.calls == [SOXQ]
    assert await _as_of(db, SOXQ) == TODAY
    assert result["refreshed"][0]["previous_rows"] == 0


@pytest.mark.asyncio
async def test_a_borrowed_basket_is_fetched_once_from_its_source(db):
    """VWCE reads VT's file, so both stale is ONE Vanguard walk, serving both."""
    await _hold(db, 1, VWCE, "VWCE")
    await _hold(db, 2, VT, "VT")
    await _basket(db, VT, "vanguard_us", age_days=stale_after_days("vanguard_us") + 5)
    fetcher = _Fetcher()

    result = await _run(db, fetcher)

    assert fetcher.calls == [VT]
    assert len(result["refreshed"]) == 1
    assert result["refreshed"][0]["symbol"] == "VT"
    assert result["refreshed"][0]["for"] == ["VT", "VWCE"]


@pytest.mark.asyncio
async def test_a_manual_basket_is_reported_but_never_fetched(db):
    """
    VWCE with a real hand-imported basket: its own file wins over the proxy, and its own
    adapter is `manual`, so there is nothing the scheduler can run. It is named so the
    hand download can happen, and nothing is fetched for it.
    """
    await _hold(db, 1, VWCE, "VWCE")
    await _basket(db, VWCE, "manual", age_days=stale_after_days("manual") + 5)
    fetcher = _Fetcher()

    result = await _run(db, fetcher)

    assert fetcher.calls == []
    assert result["needs_hand_download"] == ["VWCE"]
    assert result["stale"] == ["VWCE"]


# ── Every refusal the CLI makes, this makes; every failure stays local ──────────────


@pytest.mark.asyncio
async def test_one_funds_fetch_failure_is_its_own_warning_not_the_jobs(db):
    await _hold(db, 1, IWDA, "IWDA")
    await _hold(db, 2, SOXQ, "SOXQ")
    old = stale_after_days("blackrock") + 5
    await _basket(db, IWDA, "blackrock", age_days=old)
    await _basket(db, SOXQ, "invesco", age_days=stale_after_days("invesco") + 5)
    fetcher = _Fetcher(fail=[IWDA])

    result = await _run(db, fetcher)

    assert result["status"] == "success"
    assert [r["symbol"] for r in result["refreshed"]] == ["SOXQ"]
    assert [f["symbol"] for f in result["failed"]] == ["IWDA"]
    assert len(result["warnings"]) == 1 and "IWDA" in result["warnings"][0]
    assert "previous basket is still in use" in result["warnings"][0]
    # The failed fund's basket is exactly as it was.
    assert await _as_of(db, IWDA) == TODAY - timedelta(days=old)
    assert await _as_of(db, SOXQ) == TODAY


@pytest.mark.asyncio
async def test_a_parse_failure_stores_nothing(db):
    await _hold(db, 1, IWDA, "IWDA")
    old = stale_after_days("blackrock") + 5
    await _basket(db, IWDA, "blackrock", age_days=old)

    def bad_parse(*a, **k):
        raise ValueError("HTML where JSON was expected")

    result = await _run(db, _Fetcher(), parser=bad_parse)

    assert result["refreshed"] == []
    assert result["failed"][0]["error"].startswith("ValueError")
    assert await _as_of(db, IWDA) == TODAY - timedelta(days=old)


@pytest.mark.asyncio
async def test_a_refused_replacement_keeps_the_previous_basket(db):
    """`replace_basket`'s refusals apply unchanged: a basket may not move backwards."""
    await _hold(db, 1, IWDA, "IWDA")
    old = stale_after_days("blackrock") + 5
    await _basket(db, IWDA, "blackrock", age_days=old)
    older_still = _parser(as_of=TODAY - timedelta(days=old + 10))

    result = await _run(db, _Fetcher(), parser=older_still)

    assert result["refreshed"] == []
    assert [r["symbol"] for r in result["refused"]] == ["IWDA"]
    assert "refused" in result["warnings"][0] and "previous one is still in use" in result["warnings"][0]
    assert await _as_of(db, IWDA) == TODAY - timedelta(days=old)


@pytest.mark.asyncio
async def test_a_row_count_collapse_is_refused(db):
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=stale_after_days("blackrock") + 5, rows=100)

    result = await _run(db, _Fetcher(), parser=_parser(rows=3))

    assert result["refreshed"] == [] and len(result["refused"]) == 1
    holdings = await EtfBasketRepository(db).get_holdings([IWDA])
    assert len(holdings[IWDA]) == 100, "the collapsed basket must not have replaced the real one"


# ── One verdict, two consumers ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_detector_formats_the_same_verdicts_the_refresh_fetches_from(db, monkeypatch):
    """
    `find_stale_etf_baskets` must be built on `stale_basket_verdicts`, not beside it. Feed
    the predicate a canned answer and the detector has to echo it, one line per verdict,
    naming the fund — proof that it consumed the list rather than re-deriving one.
    """
    canned = [
        StaleBasket("IE1", "ONE", "IE1", "blackrock", "blackrock",
                    TODAY - timedelta(days=30), 30, 7),
        StaleBasket("IE2", "TWO", "IE2", "invesco", None, None, None, None),
        StaleBasket("IE3", "THREE", "IE3", "manual", "manual",
                    TODAY - timedelta(days=60), 60, 45),
    ]

    async def _canned(db_, as_of=None):
        return canned

    monkeypatch.setattr(refresh_mod, "stale_basket_verdicts", _canned)

    warnings = await SchedulerService().find_stale_etf_baskets(db, as_of=TODAY)

    assert len(warnings) == 3
    assert "ONE" in warnings[0] and "18:00" in warnings[0] and "fetch_etf_baskets" in warnings[0]
    assert "TWO" in warnings[1] and "has ever been fetched" in warnings[1]
    assert "THREE" in warnings[2] and "by hand" in warnings[2]


@pytest.mark.asyncio
async def test_verdicts_follow_the_proxy_for_a_missing_source(db):
    """A held VWCE with no VT basket anywhere: the verdict names VT's route, since that
    is the file VWCE will read once it exists, and fetching VWCE's own would store nothing
    the read path consults."""
    await _hold(db, 1, VWCE, "VWCE")

    verdicts = await stale_basket_verdicts(db, TODAY)

    assert len(verdicts) == 1
    assert verdicts[0].held_isin == VWCE and verdicts[0].source_isin == VT
    assert verdicts[0].adapter == "vanguard_us" and verdicts[0].fetchable


# ── Identity follows the baskets ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_identities_are_resolved_after_a_refresh_and_bounded(db, monkeypatch):
    calls = {"resolve": [], "constituents": 0}

    async def _resolve(self, isins, limit=None):
        calls["resolve"].append(limit)
        return {"rows_written": 0}

    async def _constituents(self, limit=None):
        calls["constituents"] += 1
        return {"holdings_updated": 0}

    monkeypatch.setattr(IdentityService, "resolve", _resolve)
    monkeypatch.setattr(IdentityService, "resolve_constituent_identifiers", _constituents)
    monkeypatch.setattr(refresh_mod, "new_client", _Client)
    monkeypatch.setattr(refresh_mod, "fetch_bodies", _Fetcher())
    monkeypatch.setattr(refresh_mod, "parse_bodies", _parser())

    # Nothing stale: the ISIN pass still runs, bounded; the CINS/SEDOL pass does not.
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=1)
    await refresh_lookthrough_data(db, TODAY)
    assert calls["resolve"] == [SCHEDULED_IDENTITY_LIMIT]
    assert calls["constituents"] == 0

    # A basket was replaced: the CINS/SEDOL pass runs, because a re-import clears them.
    await db.execute(
        EtfBasket.__table__.update().values(as_of_date=TODAY - timedelta(days=40))
    )
    await db.commit()
    result = await refresh_lookthrough_data(db, TODAY)
    assert [r["symbol"] for r in result["baskets"]["refreshed"]] == ["IWDA"]
    assert calls["constituents"] == 1
    assert calls["resolve"] == [SCHEDULED_IDENTITY_LIMIT, SCHEDULED_IDENTITY_LIMIT]


@pytest.mark.asyncio
async def test_a_failing_identity_provider_does_not_undo_the_basket_refresh(db, monkeypatch):
    async def _boom(self, isins, limit=None):
        raise ConnectionError("gleif down")

    monkeypatch.setattr(IdentityService, "resolve", _boom)
    monkeypatch.setattr(refresh_mod, "new_client", _Client)
    monkeypatch.setattr(refresh_mod, "fetch_bodies", _Fetcher())
    monkeypatch.setattr(refresh_mod, "parse_bodies", _parser())
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=stale_after_days("blackrock") + 5)

    result = await refresh_lookthrough_data(db, TODAY)

    assert result["status"] == "success"
    assert await _as_of(db, IWDA) == TODAY, "the committed basket must survive the identity failure"
    assert result["identities"]["status"] == "error"
    assert any("Identity resolution failed" in w for w in result["warnings"])
