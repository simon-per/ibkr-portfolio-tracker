"""
`/api/company-financials/*` return exactly what their response models declare.

A `response_model` is a filter, not an assertion (`test_dividend_summary_contract.py` has
the story): a key the service returns that the model lacks disappears from the wire with
nothing raising, and a field the model declares that the service never fills arrives as
its default. So the service's key sets are compared with the models' in both directions,
at every level of the nested response.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.clock import utc_today, utcnow
from app.models.company_financials import CompanyFinancial, SecurityCik
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.schemas.company_financials import (
    CompanyFinancialsCompany,
    CompanyFinancialsPoint,
    CompanyFinancialsResponse,
    CompanyFinancialsSeries,
    CompanyFinancialsSeriesSet,
)
from app.services.company_financials_service import PERIODS, CompanyFinancialsService


def _fields(model):
    return set(model.model_fields)


async def _seed(db):
    db.add(
        Security(
            id=1,
            isin="US67066G1040",
            symbol="NVDA",
            description="NVIDIA CORP",
            currency="USD",
            conid=1,
            asset_category="STK",
            exchange="NASDAQ",
        )
    )
    db.add(
        TaxLot(
            security_id=1,
            open_date=utc_today() - timedelta(days=100),
            quantity=Decimal("1"),
            cost_basis=Decimal("100"),
            price_per_unit=Decimal("100"),
            currency="USD",
            cost_basis_eur=Decimal("100"),
            is_open=True,
        )
    )
    db.add(
        SecurityCik(
            security_id=1,
            cik=1045810,
            sec_ticker="NVDA",
            cik_checked_at=utcnow(),
            fetched_at=utcnow(),
        )
    )
    for i, end in enumerate(
        (
            date(2024, 4, 28),
            date(2024, 7, 28),
            date(2024, 10, 27),
            date(2025, 1, 26),
            date(2025, 4, 27),
        )
    ):
        for metric in ("revenue", "net_income", "operating_cash_flow", "capex"):
            db.add(
                CompanyFinancial(
                    security_id=1,
                    period_end=end,
                    fiscal_year=2025 + (i == 4),
                    fiscal_period=f"Q{i % 4 + 1}",
                    metric=metric,
                    value=100.0 + i,
                    unit="USD",
                    source_concept="X",
                    filed=end,
                    fetched_at=utcnow(),
                )
            )
    db.add(
        CompanyFinancial(
            security_id=1,
            period_end=date(2025, 1, 26),
            fiscal_year=2025,
            fiscal_period="FY",
            metric="revenue",
            value=406.0,
            unit="USD",
            source_concept="X",
            filed=date(2025, 2, 26),
            fetched_at=utcnow(),
        )
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("period", PERIODS)
async def test_the_response_declares_every_key_the_service_returns(db, period):
    await _seed(db)
    security = await db.get(Security, 1)
    body = await CompanyFinancialsService(db).get_financials(security, period, "max")

    assert set(body) == _fields(CompanyFinancialsResponse)
    assert set(body["series"]) == _fields(CompanyFinancialsSeriesSet)
    for series in body["series"].values():
        assert set(series) == _fields(CompanyFinancialsSeries)
        for point in series["points"]:
            assert set(point) == _fields(CompanyFinancialsPoint)
    assert body["series"]["revenue"]["points"], "the walk compared no point"
    CompanyFinancialsResponse.model_validate(body)


@pytest.mark.asyncio
async def test_the_company_list_declares_every_key_the_service_returns(db):
    await _seed(db)
    companies = await CompanyFinancialsService(db).list_companies()
    assert companies, "the walk compared no company"
    for company in companies:
        assert set(company) == _fields(CompanyFinancialsCompany)
        CompanyFinancialsCompany.model_validate(company)


@pytest.mark.asyncio
async def test_a_security_without_figures_answers_the_same_shape(db):
    await _seed(db)
    db.add(
        Security(
            id=2,
            isin="US0000000002",
            symbol="NONE",
            description="No filings",
            currency="USD",
            conid=2,
            asset_category="STK",
            exchange="NYSE",
        )
    )
    await db.commit()
    body = await CompanyFinancialsService(db).get_financials(await db.get(Security, 2))
    assert set(body) == _fields(CompanyFinancialsResponse)
    assert body["warnings"] and body["currency"] is None and body["as_of"] is None
    assert all(s["points"] == [] for s in body["series"].values())
