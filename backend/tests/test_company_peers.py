"""
The Peers card (review item R1, 2026-11-08): the companies the tab lists in a company's
SEC industry, with the overview's key figures.

Its figures come from the one path the overview's do (`_priced`, `key_stats`): a peer's
row equals that company's own overview, which is the family check. The response is
compared with its models in both directions. Pure DB.
"""

from datetime import date
from itertools import product

import pytest

from app.clock import utcnow
from app.models.company_financials import CompanyFinancial, SecCompany
from app.schemas.company_financials import (
    CompanyFinancialsKeyStats,
    CompanyPeer,
    CompanyPeers,
)
from app.services import company_financials_service
from app.services.company_financials_service import CompanyFinancialsService

QUARTERS = (date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31))


async def _company(db, cik, ticker, sic, revenue, listed=True):
    db.add(
        SecCompany(
            cik=cik,
            ticker=ticker,
            name=f"{ticker} Inc",
            sic=sic,
            sic_description="Semiconductors" if sic else None,
            universe_seen_at=STAMP if listed else None,
            fetched_at=utcnow(),
        )
    )
    for end, metric in product(QUARTERS, ("revenue", "net_income")):
        db.add(
            CompanyFinancial(
                cik=cik,
                period_end=end,
                fiscal_year=2025,
                fiscal_period=f"Q{QUARTERS.index(end) + 1}",
                metric=metric,
                value=revenue if metric == "revenue" else revenue / 5,
                unit="USD",
                source_concept="X",
                filed=end,
                fetched_at=utcnow(),
            )
        )


STAMP = utcnow()


async def _book(db):
    await _company(db, 1, "AAA", 3674, 50.0)
    await _company(db, 2, "BBB", 3674, 90.0)
    await _company(db, 3, "CCC", 3672, 70.0)  # the three-digit group
    await _company(db, 4, "DDD", 7372, 99.0)  # another industry
    await _company(db, 5, "EEE", 3674, 10.0, listed=False)  # not on the tab
    await _company(db, 6, "FFF", None, 80.0)  # no industry
    await db.commit()


@pytest.mark.asyncio
async def test_peers_are_the_industrys_largest_widened_when_few(db):
    await _book(db)
    body = await CompanyFinancialsService(db).peers(await db.get(SecCompany, 1))

    # One other in 3674 is fewer than PEERS_MIN: the three-digit group, 367x.
    assert body["widened"] is True and body["industry"] == "Semiconductors"
    assert [p["symbol"] for p in body["peers"]] == ["AAA", "BBB", "CCC"]


@pytest.mark.asyncio
async def test_a_full_industry_is_not_widened_and_is_capped(db, monkeypatch):
    monkeypatch.setattr(company_financials_service, "PEERS_MIN", 1)
    monkeypatch.setattr(company_financials_service, "PEERS_MAX", 2)
    await _book(db)
    body = await CompanyFinancialsService(db).peers(await db.get(SecCompany, 1))
    assert body["widened"] is False
    # The company itself, then the largest of the rest.
    assert [p["symbol"] for p in body["peers"]] == ["AAA", "BBB"]


@pytest.mark.asyncio
async def test_no_industry_is_no_peers(db):
    await _book(db)
    body = await CompanyFinancialsService(db).peers(await db.get(SecCompany, 6))
    assert body["peers"] == [] and body["sic"] is None


@pytest.mark.asyncio
async def test_a_peers_figures_are_its_own_overviews(db):
    await _book(db)
    service = CompanyFinancialsService(db)
    body = await service.peers(await db.get(SecCompany, 1))
    for peer in body["peers"]:
        own = await service.get_financials(await db.get(SecCompany, peer["cik"]))
        assert peer["key_stats"] == own["key_stats"], peer["symbol"]
        assert peer["currency"] == own["currency"]


@pytest.mark.asyncio
async def test_the_response_declares_every_key_the_service_returns(db):
    await _book(db)
    body = await CompanyFinancialsService(db).peers(await db.get(SecCompany, 1))
    assert set(body) == set(CompanyPeers.model_fields)
    for peer in body["peers"]:
        assert set(peer) == set(CompanyPeer.model_fields)
        assert set(peer["key_stats"]) == set(CompanyFinancialsKeyStats.model_fields)
