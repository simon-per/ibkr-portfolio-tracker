"""
The fundamentals of what the account owns, through funds as well (review item R2,
2026-11-08; `app/services/lookthrough_fundamentals.py`).

The aggregation is checked by hand on share-equivalents; the endpoint's mapping from a
look-through row to a company by ISIN, its key figures by the overview's one path, and its
key set against the response model in both directions. Pure.
"""

import pytest

from app.clock import utcnow
from app.models.company_financials import SecCompany
from app.schemas.lookthrough import LookthroughFundamentals
from app.services import lookthrough_fundamentals as module
from app.services.company_financials_service import CompanyFinancialsService
from app.services.lookthrough_fundamentals import (
    Owned,
    aggregate,
    lookthrough_fundamentals,
)
from app.services.lookthrough_service import LookthroughService

A = dict(
    market_cap=1000.0,
    net_income=50.0,
    revenue=200.0,
    revenue_yoy=0.1,
    eps_yoy=0.2,
    operating_margin=0.3,
    fcf_margin=0.2,
)
B = dict(  # a loss-maker: its earnings lower the owned total, never vanish
    market_cap=3000.0,
    net_income=-30.0,
    revenue=600.0,
    revenue_yoy=0.0,
    eps_yoy=None,
    operating_margin=0.1,
    fcf_margin=0.05,
)


def test_share_equivalents_by_hand():
    out = aggregate([Owned(100, A), Owned(300, B), Owned(100, {})], 500)
    # Owned earnings 100 x 5% + 300 x -1% = 2 over 400 covered.
    assert out["pe_ratio"] == pytest.approx(200)
    # Owned revenue 20 + 60 = 80 now, 20 / 1.1 + 60 a year before.
    assert out["revenue_growth"] == pytest.approx(80 / (20 / 1.1 + 60) - 1)
    # Only A's EPS growth is known: its owned earnings, 20% up.
    assert out["earnings_growth"] == pytest.approx(0.2)
    assert out["operating_margin"] == pytest.approx((20 * 0.3 + 60 * 0.1) / 80)
    assert out["fcf_margin"] == pytest.approx((20 * 0.2 + 60 * 0.05) / 80)
    assert out["coverage_pct"] == pytest.approx(80.0)
    assert out["companies_covered"] == 2


def test_nothing_covered_is_absent_not_zero():
    out = aggregate([Owned(100, {})], 100)
    assert {k: out[k] for k in ("pe_ratio", "revenue_growth", "earnings_growth")} == {
        "pe_ratio": None,
        "revenue_growth": None,
        "earnings_growth": None,
    }
    assert out["coverage_pct"] == 0.0
    assert aggregate([], 0)["coverage_pct"] is None


@pytest.mark.asyncio
async def test_the_endpoint_maps_rows_by_isin_to_the_overviews_figures(db, monkeypatch):
    db.add(SecCompany(cik=1, ticker="AAA", isin="US0000000001", fetched_at=utcnow()))
    db.add(SecCompany(cik=2, ticker="BBB", isin="US0000000002", fetched_at=utcnow()))
    await db.commit()

    async def view(self, limit=50):
        return {
            "as_of": "2026-11-08",
            "companies": [
                {"isins": ["US0000000001"], "value_eur": 100.0},
                {"isins": ["CH0000000009", "US0000000002"], "value_eur": 300.0},
                {"isins": ["JP0000000003"], "value_eur": 100.0},  # no SEC figures
            ],
            "shown_value_eur": 450.0,
            "other_companies_eur": 50.0,
            "company_count_total": 40,
        }

    async def figures(self, company):
        return "USD", {1: A, 2: B}[company.cik]

    monkeypatch.setattr(LookthroughService, "get_lookthrough", view)
    monkeypatch.setattr(CompanyFinancialsService, "key_figures", figures)
    body = await lookthrough_fundamentals(db)

    assert body["pe_ratio"] == pytest.approx(200)
    assert body["coverage_pct"] == pytest.approx(80.0)
    assert (body["companies_covered"], body["companies"], body["as_of"]) == (
        2,
        40,
        "2026-11-08",
    )
    assert set(body) == set(LookthroughFundamentals.model_fields)


def test_the_figures_come_from_the_overviews_path():
    """No copy of `key_stats` here: the module asks the service for a company's figures."""
    source = open(module.__file__, encoding="utf-8").read()
    assert "key_figures(" in source and "def key_stats" not in source
