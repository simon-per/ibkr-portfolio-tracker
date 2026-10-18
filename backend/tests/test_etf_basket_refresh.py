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
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import List

import pytest

import app.models  # noqa: F401
from app.models.etf_basket import EtfBasket, EtfHolding
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories import etf_basket_repository as basket_repo_mod
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
from app.services.lookthrough_service import (
    ADAPTER_FETCH_DAYS,
    LookthroughService,
    fetch_after_days,
    stale_after_days,
)
from app.services.scheduler_service import SchedulerService

# Real registry entries, because the point is the registry's own rules.
IWDA = "IE00B4L5Y983"      # blackrock, republishes daily -> 7-day expectation
SOXQ = "US46138G6153"      # invesco, fetchable
VT = "US9220427424"        # vanguard_us, month-end -> 75-day expectation
VWCE = "IE00BK5BQT80"      # adapter="manual", borrows VT's basket
TODAY = date(2026, 9, 8)


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
async def test_a_daily_basket_is_fetched_long_before_it_warns(db):
    """
    GitHub #8: one threshold meant "fetch" and "warn" at once, so a daily feed was fetched
    only at eight days old — weekly, and only after the warning had fired. Two days old is
    a fetch now, and still nothing anyone is told about.
    """
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=2)
    assert await SchedulerService().find_stale_etf_baskets(db, as_of=TODAY) == []
    fetcher = _Fetcher()

    result = await _run(db, fetcher)

    assert fetcher.calls == [IWDA]
    assert result["due"] == ["IWDA"] and result["stale"] == []
    assert [r["symbol"] for r in result["refreshed"]] == ["IWDA"]


@pytest.mark.asyncio
async def test_a_basket_dated_today_is_left_alone(db):
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=0)
    fetcher = _Fetcher()

    result = await _run(db, fetcher)

    assert fetcher.calls == []
    assert result["refreshed"] == [] and result["due"] == [] and result["stale"] == []


@pytest.mark.parametrize("adapter", sorted(ADAPTER_FETCH_DAYS))
def test_every_adapter_is_fetched_before_it_warns(adapter):
    """Fetch-after at or past warn-after would put the always-present warning back."""
    after, every = fetch_after_days(adapter)
    assert after + every <= stale_after_days(adapter), (
        f"{adapter}: a stalled basket would warn before the refresh had asked again"
    )


# ── Each fund is asked at most once per interval, whatever the answer ───────────────


@pytest.fixture
def clock(monkeypatch):
    """The repository's stamp clock, set to the evening each `_evening` runs on."""
    state = {"now": datetime.combine(TODAY, time(16, 0))}
    monkeypatch.setattr(basket_repo_mod, "utcnow", lambda: state["now"])
    return state


async def _evening(db, clock, day: date, fetcher, parser):
    clock["now"] = datetime.combine(day, time(16, 0))
    return await _run(db, fetcher, parser=parser, as_of=day)


async def _holding_ids(db, isin):
    return [h.id for h in (await EtfBasketRepository(db).get_holdings([isin]))[isin]]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "refused", "unchanged"])
async def test_a_fund_asked_today_is_not_asked_again_until_its_interval(db, clock, outcome):
    await _hold(db, 1, IWDA, "IWDA")
    old = 3
    await _basket(db, IWDA, "blackrock", age_days=old)
    if outcome == "unchanged":
        # Store the file once, so the next fetch of it is the identical file.
        await _evening(db, clock, TODAY - timedelta(days=1), _Fetcher(),
                       _parser(as_of=TODAY - timedelta(days=2)))
    fetcher = _Fetcher(fail=[IWDA] if outcome == "failed" else [])
    parser = _parser(as_of=TODAY - timedelta(days=old + 10 if outcome == "refused" else 2))

    first = await _evening(db, clock, TODAY, fetcher, parser)
    assert fetcher.calls == [IWDA]
    assert len(first[outcome]) == 1

    again = await _evening(db, clock, TODAY, fetcher, parser)
    assert fetcher.calls == [IWDA], "asked twice inside one fetch interval"
    assert again["due"] == []

    await _evening(db, clock, TODAY + timedelta(days=1), fetcher, parser)
    assert fetcher.calls == [IWDA, IWDA], "the next interval must ask again"


@pytest.mark.asyncio
async def test_an_unchanged_basket_is_not_rewritten_and_starts_no_identity_pass(
    db, clock, monkeypatch
):
    constituents = []

    async def _constituents(self, limit=None):
        constituents.append(limit)
        return {"holdings_updated": 0}

    monkeypatch.setattr(IdentityService, "resolve_constituent_identifiers", _constituents)
    monkeypatch.setattr(refresh_mod, "new_client", _Client)
    monkeypatch.setattr(refresh_mod, "fetch_bodies", _Fetcher())
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=3)
    file_date = TODAY - timedelta(days=2)
    monkeypatch.setattr(refresh_mod, "parse_bodies", _parser(as_of=file_date))

    clock["now"] = datetime.combine(TODAY, time(16, 0))
    first = await refresh_lookthrough_data(db, TODAY)
    assert [r["symbol"] for r in first["baskets"]["refreshed"]] == ["IWDA"]
    assert constituents == [SCHEDULED_IDENTITY_LIMIT]
    ids = await _holding_ids(db, IWDA)

    # The same file the next evening: nothing rewritten, no CINS/SEDOL pass.
    clock["now"] = datetime.combine(TODAY + timedelta(days=1), time(16, 0))
    second = await refresh_lookthrough_data(db, TODAY + timedelta(days=1))
    assert second["baskets"]["refreshed"] == []
    assert [r["symbol"] for r in second["baskets"]["unchanged"]] == ["IWDA"]
    assert await _holding_ids(db, IWDA) == ids, "an identical basket was rewritten"
    assert constituents == [SCHEDULED_IDENTITY_LIMIT]
    assert await _as_of(db, IWDA) == file_date

    # A changed file is still a replacement, and still starts the pass.
    monkeypatch.setattr(refresh_mod, "parse_bodies", _parser(as_of=file_date, rows=3))
    clock["now"] = datetime.combine(TODAY + timedelta(days=2), time(16, 0))
    third = await refresh_lookthrough_data(db, TODAY + timedelta(days=2))
    assert [r["symbol"] for r in third["baskets"]["refreshed"]] == ["IWDA"]
    assert await _holding_ids(db, IWDA) != ids
    assert constituents == [SCHEDULED_IDENTITY_LIMIT, SCHEDULED_IDENTITY_LIMIT]


@pytest.mark.asyncio
async def test_an_unchanged_file_moves_only_an_issuer_stated_as_of(db):
    """A stood-in date (DWS, clamped Defiance) stays put on an unchanged file: erring older."""
    repo = EtfBasketRepository(db)
    first = _parsed(IWDA, "blackrock", as_of=TODAY - timedelta(days=3))
    await repo.replace_basket(first)

    stood_in = _parsed(IWDA, "blackrock", as_of=TODAY)
    stood_in.as_of_is_issuer_stated = False
    assert (await repo.replace_basket(stood_in)).unchanged
    assert await _as_of(db, IWDA) == TODAY - timedelta(days=3)

    stated = _parsed(IWDA, "blackrock", as_of=TODAY - timedelta(days=1))
    assert (await repo.replace_basket(stated)).unchanged
    assert await _as_of(db, IWDA) == TODAY - timedelta(days=1)


@pytest.mark.asyncio
async def test_requests_per_evening_for_a_daily_feed_and_a_stalled_month_end_fund(db, clock):
    """
    The budget the fix buys, measured through the refresh itself. A daily feed whose file
    changes every day is asked once an evening (it was asked once a week, at eight days
    old). VT stuck on one month-end file past its fetch-after is asked every third evening
    and rewritten once (it was asked and rewritten every evening once past 75 days).
    """
    await _hold(db, 1, IWDA, "IWDA")
    await _hold(db, 2, VT, "VT")
    await _basket(db, IWDA, "blackrock", age_days=2)
    vt_after, _ = fetch_after_days("vanguard_us")
    vt_as_of = TODAY - timedelta(days=vt_after + 1)
    await _basket(db, VT, "vanguard_us", age_days=vt_after + 1)

    fetcher = _Fetcher()

    def parse(fund_isin, bodies, adapter, symbol, fetched_on):
        if fund_isin == VT:
            return _parsed(VT, adapter, as_of=vt_as_of)
        # A new file each day, dated the day before.
        basket = _parsed(fund_isin, adapter, as_of=fetched_on - timedelta(days=1))
        basket.rows[0] = replace(basket.rows[0], weight_pct=Decimal(fetched_on.day))
        return basket

    replaced = {IWDA: 0, VT: 0}
    for n in range(14):
        result = await _evening(db, clock, TODAY + timedelta(days=n), fetcher, parse)
        for r in result["refreshed"]:
            replaced[r["fund_isin"]] += 1
        assert result["failed"] == [] and result["refused"] == []
        assert result["stale"] == []

    assert fetcher.calls.count(IWDA) == 14
    assert replaced[IWDA] == 14
    assert fetcher.calls.count(VT) == 5      # evenings 0, 3, 6, 9, 12
    assert replaced[VT] == 1                 # the stored rows predate the fingerprint


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
    calls = {"resolve": [], "constituents": []}

    async def _resolve(self, isins, limit=None):
        calls["resolve"].append(limit)
        return {"rows_written": 0}

    async def _constituents(self, limit=None):
        # Recorded, because this pass does not cache a miss: unbounded, the
        # permanently unresolvable identifiers were re-asked of OpenFIGI every
        # evening a basket was replaced.
        calls["constituents"].append(limit)
        return {"holdings_updated": 0}

    monkeypatch.setattr(IdentityService, "resolve", _resolve)
    monkeypatch.setattr(IdentityService, "resolve_constituent_identifiers", _constituents)
    monkeypatch.setattr(refresh_mod, "new_client", _Client)
    monkeypatch.setattr(refresh_mod, "fetch_bodies", _Fetcher())
    monkeypatch.setattr(refresh_mod, "parse_bodies", _parser())

    # Nothing due: the ISIN pass still runs, bounded; the CINS/SEDOL pass does not.
    await _hold(db, 1, IWDA, "IWDA")
    await _basket(db, IWDA, "blackrock", age_days=0)
    await refresh_lookthrough_data(db, TODAY)
    assert calls["resolve"] == [SCHEDULED_IDENTITY_LIMIT]
    assert calls["constituents"] == []

    # A basket was replaced: the CINS/SEDOL pass runs, because a replacement is the only
    # thing that brings new identifiers — and it runs *bounded*, with the ISIN pass's limit.
    await db.execute(
        EtfBasket.__table__.update().values(as_of_date=TODAY - timedelta(days=40))
    )
    await db.commit()
    result = await refresh_lookthrough_data(db, TODAY)
    assert [r["symbol"] for r in result["baskets"]["refreshed"]] == ["IWDA"]
    assert calls["constituents"] == [SCHEDULED_IDENTITY_LIMIT]
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
