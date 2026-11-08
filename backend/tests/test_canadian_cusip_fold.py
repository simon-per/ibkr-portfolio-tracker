"""
A Canadian company's CUSIP in a US fund's file folds with its Canadian listing (review item
L2, 2026-11-08).

QQQM publishes Shopify as CUSIP 82509L107; the parser derived the US ISIN US82509L1070,
which belongs to nothing, and threw the CUSIP away. Both identity providers then missed that
ISIN, so Shopify read as two rows: the QQQM line (sector Unknown) and the iShares line under
CA82509L1076. The CUSIP is now kept beside the derived ISIN; when both providers have missed
the ISIN, the CUSIP is asked of OpenFIGI, and its share-class FIGI folds the two. Pure DB.
"""

from decimal import Decimal

import pytest

import app.models  # noqa: F401
from app.clock import utcnow
from app.models.etf_basket import EtfHolding
from app.models.isin_identity import IsinIdentity
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.services.company_identity import IdentityMember
from app.services.etf_basket_parsers import _resolve_identifier
from app.services.identity_service import group_companies

QQQM, IQQ = "US46138G6492", "IE00B0M63177"
SHOP_FIGI = "BBG0094HZH71"


def test_the_cusip_stays_beside_the_isin_derived_from_it():
    assert _resolve_identifier("82509L107") == ("US82509L1070", "82509L107")


async def _book(db, *, us_missed=True):
    for fund, line, isin, identifier in (
        (QQQM, 0, "US82509L1070", "82509L107"),
        (QQQM, 1, "US0378331005", "037833100"),  # Apple: its derived ISIN is real
        (IQQ, 0, "CA82509L1076", None),
    ):
        db.add(
            EtfHolding(
                fund_isin=fund,
                line_no=line,
                constituent_isin=isin,
                constituent_identifier=identifier,
                constituent_name="Shopify",
                weight_pct=Decimal("1"),
                asset_class="Equity",
            )
        )
    now = utcnow()
    db.add(
        IsinIdentity(
            isin="US82509L1070",
            lei=None,
            share_class_figi=None,
            lei_checked_at=now if us_missed else None,
            figi_checked_at=now,
        )
    )
    db.add(
        IsinIdentity(
            isin="US0378331005",
            lei="HWUPKR0MPOU8FGXBT394",
            share_class_figi="BBG001S5N8V8",
            lei_checked_at=now,
            figi_checked_at=now,
        )
    )
    db.add(
        IsinIdentity(
            isin="CA82509L1076",
            lei=None,
            share_class_figi=SHOP_FIGI,
            lei_checked_at=now,
            figi_checked_at=now,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_a_cusip_whose_derived_isin_both_providers_missed_is_asked(db):
    await _book(db)
    assert await EtfBasketRepository(db).unresolved_identifiers() == ["82509L107"]


@pytest.mark.asyncio
async def test_not_while_a_provider_has_yet_to_answer(db):
    await _book(db, us_missed=False)
    assert await EtfBasketRepository(db).unresolved_identifiers() == []


@pytest.mark.asyncio
async def test_its_figi_folds_the_two_listings_into_one_company(db):
    await _book(db)
    repo = EtfBasketRepository(db)
    assert await repo.apply_resolved_identifiers({"82509L107": SHOP_FIGI}) == 1
    await db.commit()

    holdings = await repo.get_holdings([QQQM, IQQ])
    shop = [
        row
        for rows in holdings.values()
        for row in rows
        if row.constituent_isin in ("US82509L1070", "CA82509L1076")
    ]
    members = [
        IdentityMember(
            ref=f"{row.fund_isin}:{row.line_no}",
            isin=row.constituent_isin,
            share_class_figi=row.constituent_share_class_figi,
            fallback_name=row.constituent_name,
            weight=Decimal("1"),
        )
        for row in shop
    ]
    groups = await group_companies(db, members)
    assert len(groups) == 1
    assert sorted(groups[0].isins) == ["CA82509L1076", "US82509L1070"]
