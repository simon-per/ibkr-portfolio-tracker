"""
Folding a fund constituent whose issuer published no ISIN.

Three of the four US adapters publish a nine-character identifier instead — and most of those
are CINS or SEDOLs, which cannot be converted locally. Left there, Eaton shows twice: once as
GRID's `G29183103` and once as MSCI World's `IE00B8KQN827`. This is the pass that joins them,
and the assertions below are all about it doing so *without inventing an identifier*.

Every OpenFIGI fact here was measured against the live API on 2026-08-16 rather than assumed:
`ID_CINS G29183103` returns 104 venue rows sharing shareClassFIGI `BBG001S5QZ45`, the same
value `ID_ISIN IE00B8KQN827` returns; the identical job asked as `ID_CUSIP` returns **zero**.

Offline: the network is never reached — `_resolve_openfigi`'s HTTP half is not exercised here.
"""
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
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
)

GRID = "US33737A1088"
EATON_CINS = "G29183103"
EATON_ISIN = "IE00B8KQN827"
EATON_FIGI = "BBG001S5QZ45"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        yield db
    await engine.dispose()


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
async def test_an_unconvertible_identifier_survives_the_import(session):
    """
    Without this the value is lost at import time and unrecoverable afterwards: the parser has
    already decided it is not an ISIN, so nothing downstream would know a CINS had been there.
    """
    await EtfBasketRepository(session).replace_basket(basket([
        row(1, "Eaton Corporation Plc", "60", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
    ]))
    stored = (await session.execute(
        select(EtfHolding).order_by(EtfHolding.line_no)
    )).scalars().all()

    assert stored[0].constituent_isin is None
    assert stored[0].constituent_identifier == EATON_CINS
    # Never guessed at import time — that is what the resolver is for.
    assert stored[0].constituent_share_class_figi is None
    assert stored[1].constituent_identifier is None


@pytest.mark.asyncio
async def test_only_the_rows_with_no_identity_at_all_are_pending(session):
    repo = EtfBasketRepository(session)
    await repo.replace_basket(basket([
        row(1, "Eaton Corporation Plc", "40", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
        row(3, "Global Unichip Corp", "20", identifier="B056381"),
    ]))

    assert await repo.unresolved_identifiers() == ["B056381", EATON_CINS]


@pytest.mark.asyncio
async def test_a_resolved_identifier_is_not_asked_about_twice(session):
    repo = EtfBasketRepository(session)
    await repo.replace_basket(basket([
        row(1, "Eaton Corporation Plc", "60", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
    ]))
    assert await repo.apply_resolved_identifiers({EATON_CINS: EATON_FIGI}) == 1

    assert await repo.unresolved_identifiers() == []
    stored = (await session.execute(
        select(EtfHolding).where(EtfHolding.line_no == 1)
    )).scalars().one()
    assert stored.constituent_share_class_figi == EATON_FIGI
    # The published identifier is not overwritten, and no ISIN is invented for it.
    assert stored.constituent_identifier == EATON_CINS
    assert stored.constituent_isin is None


@pytest.mark.asyncio
async def test_one_identifier_resolves_every_fund_that_carries_it(session):
    """A company inside two funds is one question, and one answer must reach both rows."""
    repo = EtfBasketRepository(session)
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
async def test_a_re_import_drops_the_resolution_and_keeps_the_published_identifier(session):
    """
    Deliberate, and cheap: the FIGI is re-derivable from one request, while a cached identity
    outliving the basket row it described is the harder thing to reason about. What must NOT
    happen is the raw identifier going with it — that would need a re-download.
    """
    repo = EtfBasketRepository(session)
    await repo.replace_basket(basket([
        row(1, "Eaton Corporation Plc", "60", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "40", isin="US67066G1040"),
    ]))
    await repo.apply_resolved_identifiers({EATON_CINS: EATON_FIGI})

    fresher = basket([
        row(1, "Eaton Corporation Plc", "55", identifier=EATON_CINS),
        row(2, "Nvidia Corp", "45", isin="US67066G1040"),
    ])
    fresher.as_of_date = date(2026, 8, 15)
    await repo.replace_basket(fresher)

    assert await repo.unresolved_identifiers() == [EATON_CINS]


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
async def test_the_api_key_changes_the_batch_and_the_pacing_together(session, monkeypatch):
    """
    Both or neither: a 10x batch at the keyless rate would still take an hour for a fund's
    tail, and the keyed rate with a batch of 10 wastes the key entirely.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "openfigi_api_key", "", raising=False)
    headers, batch, interval = IdentityService(session)._openfigi_pacing
    assert "X-OPENFIGI-APIKEY" not in headers
    assert batch == OPENFIGI_BATCH

    monkeypatch.setattr(settings, "openfigi_api_key", "  a-key  ", raising=False)
    keyed_headers, keyed_batch, keyed_interval = IdentityService(session)._openfigi_pacing
    assert keyed_headers["X-OPENFIGI-APIKEY"] == "a-key"
    assert keyed_batch == OPENFIGI_BATCH_KEYED
    assert keyed_interval < interval


@pytest.mark.asyncio
async def test_nothing_pending_makes_no_request_and_says_so(session):
    summary = await IdentityService(session).resolve_constituent_identifiers()

    assert summary["identifiers_pending"] == 0
    assert summary["holdings_updated"] == 0
