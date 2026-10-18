"""
Folding a fund constituent whose issuer published no ISIN.

Three of the four US adapters publish a nine-character identifier instead — and most of those
are CINS or SEDOLs, which cannot be converted locally. Left there, Eaton shows twice: once as
GRID's `G29183103` and once as MSCI World's `IE00B8KQN827`. This is the pass that joins them,
and the assertions below are all about it doing so *without inventing an identifier*.

Every OpenFIGI fact here was measured against the live API on 2026-08-16 rather than assumed:
`ID_CINS G29183103` returns 104 venue rows sharing shareClassFIGI `BBG001S5QZ45`, the same
value `ID_ISIN IE00B8KQN827` returns; the identical job asked as `ID_CUSIP` returns **zero**.

Offline: the network is never reached. The request loops run against a fake `httpx` where a
test needs them (`_FakeProviders`).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.etf_basket import EtfHolding
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.services.company_identity import IdentityMember, company_groups
from app.services.etf_basket_parsers import ConstituentRow, ParsedBasket
from app.services.identity_service import (
    OPENFIGI_BATCH,
    OPENFIGI_BATCH_KEYED,
    OPENFIGI_ID_TYPES,
    IdentityService,
    rotating_window,
)
from tests.memory_db import memory_engine

GRID = "US33737A1088"
EATON_CINS = "G29183103"
EATON_ISIN = "IE00B8KQN827"
EATON_FIGI = "BBG001S5QZ45"


def basket(rows):
    return ParsedBasket(
        fund_isin=GRID, as_of_date=date(2026, 8, 14), source="first_trust",
        adapter="first_trust", rows=rows, source_rows=len(rows),
        asset_class_available=False,
    )


def row(line_no, name, weight, isin=None, identifier=None):
    return ConstituentRow(
        line_no=line_no, name=name, weight_pct=Decimal(weight), isin=isin,
        identifier=identifier,
    )


# ------------------------------------------------------------------------------- storage

@pytest.mark.asyncio
async def test_an_unconvertible_identifier_survives_the_import(db):
    """
    Without this the value is lost at import time and unrecoverable afterwards: the parser has
    already decided it is not an ISIN, so nothing downstream would know a CINS had been there.
    """
    await EtfBasketRepository(db).replace_basket(basket([
        row(1, "Eaton Corporation Plc", "60", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
    ]))
    stored = (await db.execute(
        select(EtfHolding).order_by(EtfHolding.line_no)
    )).scalars().all()

    assert stored[0].constituent_isin is None
    assert stored[0].constituent_identifier == EATON_CINS
    # Never guessed at import time — that is what the resolver is for.
    assert stored[0].constituent_share_class_figi is None
    assert stored[1].constituent_identifier is None


@pytest.mark.asyncio
async def test_only_the_rows_with_no_identity_at_all_are_pending(db):
    repo = EtfBasketRepository(db)
    await repo.replace_basket(basket([
        row(1, "Eaton Corporation Plc", "40", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
        row(3, "Global Unichip Corp", "20", identifier="B056381"),
    ]))

    assert await repo.unresolved_identifiers() == ["B056381", EATON_CINS]


@pytest.mark.asyncio
async def test_a_resolved_identifier_is_not_asked_about_twice(db):
    repo = EtfBasketRepository(db)
    await repo.replace_basket(basket([
        row(1, "Eaton Corporation Plc", "60", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
    ]))
    assert await repo.apply_resolved_identifiers({EATON_CINS: EATON_FIGI}) == 1

    assert await repo.unresolved_identifiers() == []
    stored = (await db.execute(
        select(EtfHolding).where(EtfHolding.line_no == 1)
    )).scalars().one()
    assert stored.constituent_share_class_figi == EATON_FIGI
    # The published identifier is not overwritten, and no ISIN is invented for it.
    assert stored.constituent_identifier == EATON_CINS
    assert stored.constituent_isin is None


@pytest.mark.asyncio
async def test_one_identifier_resolves_every_fund_that_carries_it(db):
    """A company inside two funds is one question, and one answer must reach both rows."""
    repo = EtfBasketRepository(db)
    await repo.replace_basket(basket([
        row(1, "Eaton Corporation Plc", "60", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
    ]))
    other = basket([
        row(1, "Eaton Corp Plc", "50", identifier=EATON_CINS),
        row(2, "Cloudflare Inc", "50", isin="US18915M1071"),
    ])
    other.fund_isin = "US26922A4206"
    await repo.replace_basket(other)

    assert await repo.unresolved_identifiers() == [EATON_CINS]
    assert await repo.apply_resolved_identifiers({EATON_CINS: EATON_FIGI}) == 2


@pytest.mark.asyncio
async def test_a_re_import_keeps_the_resolution_and_the_published_identifier(db):
    """
    Until 2026-10-15 a replace dropped the FIGI, on the grounds that it was re-derivable from
    one request. That stopped holding once the evening pass was capped at
    `SCHEDULED_IDENTITY_LIMIT` while the Invesco baskets were replaced on every refresh: each
    replacement undid more than the pass restored, and ASML stood as three companies. A CINS names one
    security for good, so OpenFIGI's earlier answer is still the answer. A new identifier in
    the fresher basket is still pending, and the raw identifier is never dropped.
    """
    repo = EtfBasketRepository(db)
    await repo.replace_basket(basket([
        row(1, "Eaton Corporation Plc", "60", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
    ]))
    await repo.apply_resolved_identifiers({EATON_CINS: EATON_FIGI})

    fresher = basket([
        row(1, "Eaton Corporation Plc", "50", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
        row(3, "ASML Holding NV", "10", identifier="N07059210"),
    ])
    fresher.as_of_date = date(2026, 8, 15)
    await repo.replace_basket(fresher)

    assert await repo.unresolved_identifiers() == ["N07059210"]
    stored = (await db.execute(
        select(EtfHolding).where(EtfHolding.line_no == 1)
    )).scalars().one()
    assert stored.constituent_share_class_figi == EATON_FIGI
    assert stored.constituent_identifier == EATON_CINS


@pytest.mark.asyncio
async def test_another_funds_answer_reaches_a_newly_imported_basket(db):
    """One identifier is one security whichever fund lists it — the same rule as the update."""
    repo = EtfBasketRepository(db)
    await repo.replace_basket(basket([
        row(1, "Eaton Corporation Plc", "100", identifier=EATON_CINS),
    ]))
    await repo.apply_resolved_identifiers({EATON_CINS: EATON_FIGI})

    other = basket([
        row(1, "Eaton Corp Plc", "50", identifier=EATON_CINS),
        row(2, "Cloudflare Inc", "50", isin="US18915M1071"),
    ])
    other.fund_isin = "US26922A4206"
    await repo.replace_basket(other)

    assert await repo.unresolved_identifiers() == []


# ------------------------------------------------------------------- the bounded window

def test_the_window_reaches_past_a_prefix_of_permanent_misses():
    """
    The starvation it replaced: `pending[:limit]` of a sorted list asked the same first
    identifiers every evening, so with `limit` permanent misses sorting ahead (SEDOLs lead
    with digits) a CINS starting `N` was never asked at all.
    """
    misses = [f"{n:07d}" for n in range(25)]
    pending = misses + ["N07059210"]
    asked = set()
    for turn in range(2):
        asked.update(rotating_window(pending, 25, turn))
    assert "N07059210" in asked


@pytest.mark.parametrize("size,limit", [(1, 25), (25, 25), (26, 25), (101, 25), (7, 3)])
def test_consecutive_turns_cover_every_item(size, limit):
    items = [f"I{n:04d}" for n in range(size)]
    turns = -(-size // limit)
    asked = [i for turn in range(turns) for i in rotating_window(items, limit, turn)]
    assert set(asked) == set(items)
    assert all(len(rotating_window(items, limit, t)) == min(limit, size) for t in range(turns))


def test_an_empty_or_zero_window_asks_nothing():
    assert rotating_window([], 25, 3) == []
    assert rotating_window(["A"], 0, 3) == []


# --------------------------------------------------------------------------- the fold

def test_a_figi_folds_a_constituent_with_the_same_company_held_directly():
    """
    The point of the whole pass. Eaton's CINS resolves to the same shareClassFIGI its ISIN
    does, so `company_groups` unions the two into one company — with no ISIN for the fund's
    row anywhere in the system.
    """
    groups = company_groups([
        IdentityMember(ref="sec:1", isin=EATON_ISIN, share_class_figi=EATON_FIGI,
                       fallback_name="EATON CORP PLC", weight=Decimal("100")),
        IdentityMember(ref=f"{GRID}#1", share_class_figi=EATON_FIGI,
                       fallback_name="Eaton Corporation Plc", weight=Decimal("40")),
    ], {})

    assert len(groups) == 1
    assert {m.ref for m in groups[0].members} == {"sec:1", f"{GRID}#1"}
    assert groups[0].key_type == "share_class_figi"


def test_an_unresolved_constituent_stands_alone_rather_than_folding_wrongly():
    """
    The safe direction, and the one this feature always takes: an understatement (one company
    on two rows) rather than a fabrication (two companies on one).
    """
    groups = company_groups([
        IdentityMember(ref="sec:1", isin=EATON_ISIN, share_class_figi=EATON_FIGI,
                       fallback_name="EATON CORP PLC", weight=Decimal("100")),
        IdentityMember(ref=f"{GRID}#1", fallback_name="Eaton Corporation Plc",
                       weight=Decimal("40")),
    ], {})

    assert len(groups) == 2


# ------------------------------------------------------------------------- the id types

@pytest.mark.parametrize("identifier,id_type", [
    ("67066G104", "ID_CUSIP"),    # NVIDIA, a real CUSIP
    ("G29183103", "ID_CINS"),     # Eaton — as ID_CUSIP this returns zero rows
    ("B056381", "ID_SEDOL"),      # Global Unichip
])
def test_each_shape_is_asked_as_its_own_id_type(identifier, id_type):
    from app.services.security_identifiers import identifier_kind

    assert OPENFIGI_ID_TYPES[identifier_kind(identifier)] == id_type


def test_a_cins_is_never_asked_as_a_cusip():
    """
    Measured, and the reason this file exists: `ID_CUSIP G29183103` returns an empty `data`
    array with no error, so the mistake costs the fold and reports nothing. A test on the
    mapping is the only place that fact can be pinned without a network call.
    """
    assert OPENFIGI_ID_TYPES["CINS"] != OPENFIGI_ID_TYPES["CUSIP"]


# ---------------------------------------------------------------------------- the key

@pytest.mark.asyncio
async def test_the_api_key_changes_the_batch_and_the_pacing_together(db, monkeypatch):
    """
    Both or neither: a 10x batch at the keyless rate would still take an hour for a fund's
    tail, and the keyed rate with a batch of 10 wastes the key entirely.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "openfigi_api_key", "", raising=False)
    headers, batch, interval = IdentityService(db)._openfigi_pacing
    assert "X-OPENFIGI-APIKEY" not in headers
    assert batch == OPENFIGI_BATCH

    monkeypatch.setattr(settings, "openfigi_api_key", "  a-key  ", raising=False)
    keyed_headers, keyed_batch, keyed_interval = IdentityService(db)._openfigi_pacing
    assert keyed_headers["X-OPENFIGI-APIKEY"] == "a-key"
    assert keyed_batch == OPENFIGI_BATCH_KEYED
    assert keyed_interval < interval


@pytest.mark.asyncio
async def test_nothing_pending_makes_no_request_and_says_so(db):
    summary = await IdentityService(db).resolve_constituent_identifiers()

    assert summary["identifiers_pending"] == 0
    assert summary["holdings_updated"] == 0


# --------------------------------------------------------------- remembering a miss
#
# The providers are faked at the module's `httpx`, so the request loop itself runs —
# batching, positional alignment, what gets stamped — with nothing leaving the machine.

ASML_CINS = "N07059210"


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeProviders:
    """Stands in for `httpx.AsyncClient`: OpenFIGI by `idValue`, GLEIF by ISIN."""

    def __init__(self, figis=None, leis=None, fail=(), throttle=()):
        self.figis = figis or {}
        self.leis = leis or {}
        self.fail = set(fail)
        self.throttle = set(throttle)  # an OpenFIGI batch carrying one of these -> 429
        self.asked = []

    def AsyncClient(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        values = [job["idValue"] for job in json]
        self.asked.append(values)
        if self.fail & set(values):
            raise ConnectionError("openfigi unreachable")
        if self.throttle & set(values):
            return _Response({"error": "Too Many Requests"}, status_code=429)
        return _Response([
            {"data": [{"shareClassFIGI": self.figis[v]}]} if v in self.figis
            else {"warning": "No identifier found."}
            for v in values
        ])

    async def get(self, url, params):
        isin = params["filter[isin]"]
        self.asked.append(isin)
        answer = self.leis.get(isin)
        if isinstance(answer, BaseException):
            raise answer
        return _Response({"data": [{"id": answer}] if answer else []})


@pytest.fixture
def providers(monkeypatch):
    from app.services import identity_service

    fake = _FakeProviders()
    monkeypatch.setattr(identity_service, "httpx", fake)
    monkeypatch.setattr(identity_service, "OPENFIGI_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(identity_service, "GLEIF_MIN_INTERVAL_S", 0)
    return fake


@pytest.mark.asyncio
async def test_a_known_miss_is_not_re_asked_until_the_recheck_interval(
    db, providers, monkeypatch
):
    from app.services import identity_service
    from app.services.identity_service import IDENTIFIER_MISS_RECHECK_DAYS

    await EtfBasketRepository(db).replace_basket(basket([
        row(1, "Eaton Corporation Plc", "60", identifier=EATON_CINS),
        row(2, "ASML Holding NV", "40", identifier=ASML_CINS),
    ]))
    providers.figis = {EATON_CINS: EATON_FIGI}
    service = IdentityService(db)

    first = await service.resolve_constituent_identifiers()
    assert providers.asked == [[EATON_CINS, ASML_CINS]]
    assert first["identifiers_resolved"] == 1 and first["identifiers_not_found"] == 1

    second = await service.resolve_constituent_identifiers()
    assert providers.asked == [[EATON_CINS, ASML_CINS]], "a known miss was asked again"
    assert second["identifiers_pending"] == 0
    assert await service.pending_constituent_identifiers() == []

    later = identity_service.utcnow() + timedelta(days=IDENTIFIER_MISS_RECHECK_DAYS + 1)
    monkeypatch.setattr(identity_service, "utcnow", lambda: later)
    providers.figis[ASML_CINS] = "BBG000C1HM48"
    third = await service.resolve_constituent_identifiers()
    assert providers.asked[-1] == [ASML_CINS]
    assert third["identifiers_resolved"] == 1
    assert await EtfBasketRepository(db).identifier_misses_since(datetime.min) == set()


@pytest.mark.asyncio
async def test_a_failed_request_is_not_remembered_as_a_miss(db, providers):
    await EtfBasketRepository(db).replace_basket(basket([
        row(1, "ASML Holding NV", "100", identifier=ASML_CINS),
    ]))
    providers.fail = {ASML_CINS}
    service = IdentityService(db)

    summary = await service.resolve_constituent_identifiers()

    assert summary["identifiers_errors"] == 1
    assert await service.pending_constituent_identifiers() == [ASML_CINS]


# ------------------------------------------------- one OpenFIGI loop, one 429 breaker
#
# Both passes ask through `_openfigi_batches` on one paced client (issue #66): each used
# to carry its own copy of the loop and its own pacer, and `continue` past a 429.


@pytest.fixture
def one_job_batches(monkeypatch):
    """Keyless (a key in the developer's environment would change batch and pacing), one
    job per request, so each batch is one identifier."""
    from app.config import settings
    from app.services import identity_service

    monkeypatch.setattr(settings, "openfigi_api_key", "", raising=False)
    monkeypatch.setattr(identity_service, "OPENFIGI_BATCH", 1)


@pytest.mark.asyncio
async def test_a_429_in_the_isin_pass_stops_both_passes(db, providers, one_job_batches):
    from app.models.isin_identity import IsinIdentity

    await EtfBasketRepository(db).replace_basket(basket([
        row(1, "ASML Holding NV", "100", identifier=ASML_CINS),
    ]))
    apple, microsoft = "US0378331005", "US5949181045"
    providers.throttle = {apple}
    service = IdentityService(db)

    first = await service.resolve([apple, microsoft])
    second = await service.resolve_constituent_identifiers()

    openfigi_asks = [a for a in providers.asked if isinstance(a, list)]
    assert openfigi_asks == [[apple]], "asked OpenFIGI again after its 429"
    assert first["openfigi_rate_limited"] and second["openfigi_rate_limited"]
    assert first["openfigi_errors"] == 0 and second["identifiers_errors"] == 0
    # Nothing it was not asked about is remembered as answered: all of it is asked next run.
    stamped = (await db.execute(
        select(IsinIdentity).where(IsinIdentity.figi_checked_at.is_not(None))
    )).scalars().all()
    assert stamped == []
    assert await service.pending_constituent_identifiers() == [ASML_CINS]
    # GLEIF is another provider: the OpenFIGI breaker does not touch it.
    assert apple in providers.asked and microsoft in providers.asked


@pytest.mark.asyncio
async def test_a_429_in_the_identifier_pass_stops_its_remaining_batches(
    db, providers, one_job_batches
):
    await EtfBasketRepository(db).replace_basket(basket([
        row(1, "Eaton Corporation Plc", "60", identifier=EATON_CINS),
        row(2, "ASML Holding NV", "40", identifier=ASML_CINS),
    ]))
    providers.throttle = {EATON_CINS}
    service = IdentityService(db)

    summary = await service.resolve_constituent_identifiers()

    assert providers.asked == [[EATON_CINS]]
    assert summary["openfigi_rate_limited"] and summary["identifiers_errors"] == 0
    pending = await service.pending_constituent_identifiers()
    assert sorted(pending) == sorted([ASML_CINS, EATON_CINS])
    # And the same service asks OpenFIGI nothing more this run.
    await service.resolve(["US0378331005"])
    assert [a for a in providers.asked if isinstance(a, list)] == [[EATON_CINS]]


@pytest.mark.asyncio
async def test_the_second_pass_is_paced_against_the_first(db, providers, monkeypatch):
    """One client for both passes: the CINS pass's first request waits out the interval
    from the ISIN pass's last, which two pacers never did."""
    from app.config import settings
    from app.services import identity_service

    monkeypatch.setattr(settings, "openfigi_api_key", "", raising=False)
    await EtfBasketRepository(db).replace_basket(basket([
        row(1, "ASML Holding NV", "100", identifier=ASML_CINS),
    ]))
    monkeypatch.setattr(identity_service, "OPENFIGI_MIN_INTERVAL_S", 2.6)
    service = IdentityService(db)
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    service._openfigi._sleep = _sleep
    service._openfigi._clock = lambda: 100.0   # no time passes between the two requests

    await service.resolve(["US0378331005"])
    await service.resolve_constituent_identifiers()

    assert [a for a in providers.asked if isinstance(a, list)] == [
        ["US0378331005"], [ASML_CINS],
    ]
    assert sleeps == [2.6]


def test_there_is_one_openfigi_loop():
    """Every OpenFIGI request goes through `OpenFigiClient.map`, called from one place."""
    import ast
    import inspect

    from app.services import identity_service

    tree = ast.parse(inspect.getsource(identity_service))
    posts, maps = [], []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id == "OPENFIGI_URL"
                and isinstance(node.ctx, ast.Load)):
            posts.append(node.lineno)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "map"):
            maps.append(node.lineno)
    assert len(posts) == 1, f"OPENFIGI_URL used at lines {posts}: a second OpenFIGI loop"
    assert len(maps) == 1, f"OpenFigiClient.map called at lines {maps}"


# ------------------------------------------------------- a killed CLI run keeps its work

@pytest.mark.asyncio
async def test_a_cli_run_killed_mid_gleif_keeps_every_answer_already_given(providers, monkeypatch):
    """
    GLEIF is one ISIN per ~1.1 s, so a full run is up to ~45 minutes, and it used to commit
    once at the end: killed late, it re-asked everything next time. Each batch commits now.
    """
    import asyncio

    from app.cli import resolve_identities as cli
    from app.models.isin_identity import IsinIdentity

    apple, microsoft = "US0378331005", "US5949181045"
    engine = await memory_engine()

    async def _held(db, funds=False):
        return [apple, microsoft]

    monkeypatch.setattr(cli, "AsyncSessionLocal",
                        lambda: AsyncSession(engine, expire_on_commit=False))
    monkeypatch.setattr(cli, "held_isins", _held)
    providers.figis = {apple: "BBG001S5N8V8", microsoft: "BBG001S5TD05"}
    providers.leis = {apple: "HWUPKR0MPOU8FGXBT394", microsoft: asyncio.CancelledError()}

    with pytest.raises(asyncio.CancelledError):
        await cli.resolve_identities()

    async with AsyncSession(engine) as db:
        rows = {r.isin: r for r in (await db.execute(select(IsinIdentity))).scalars()}
    await engine.dispose()

    assert rows[apple].figi_checked_at and rows[microsoft].figi_checked_at
    assert rows[apple].lei == "HWUPKR0MPOU8FGXBT394"
    assert rows[microsoft].lei_checked_at is None, "the unanswered ISIN must stay unasked"


@pytest.mark.asyncio
async def test_held_isins_splits_one_held_set_into_companies_and_funds(db):
    """
    The identity pass (companies), the basket refresh (funds) and the CLI's
    `--include-funds` (both) read one held set — `security_repository.held()` — and only
    split it. Until 2026-10-18 the basket refresh queried its own copy (GitHub issue #42).
    """
    from app.models.security import Security
    from app.models.taxlot import TaxLot
    from app.services.identity_service import held_isins

    fund, company, sold = "IE00B4L5Y983", "US0378331005", "US5949181045"
    for sid, isin, closed in ((1, fund.lower() + " ", None), (2, company, None),
                              (3, sold, date(2026, 5, 4))):
        db.add(Security(id=sid, isin=isin, symbol=f"S{sid}", description=f"S{sid}",
                             currency="EUR", conid=100 + sid, asset_category="STK",
                             exchange="XETRA"))
        db.add(TaxLot(security_id=sid, open_date=date(2025, 1, 2), close_date=closed,
                           is_open=closed is None, quantity=Decimal("1"),
                           cost_basis=Decimal("1"), cost_basis_eur=Decimal("1"),
                           price_per_unit=Decimal("1"), currency="EUR"))
    await db.flush()

    assert await held_isins(db) == [company]
    assert await held_isins(db, funds=True) == [fund]
    assert await held_isins(db, funds=None) == sorted([fund, company])
