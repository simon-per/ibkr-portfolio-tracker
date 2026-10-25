"""
Wire shapes for `/api/company-financials/*` (docs/company-financials.md).

A `response_model` is a FILTER (CLAUDE.md, *Conventions*): a key the service returns that
is not declared here is dropped silently. `tests/test_company_financials_contract.py` pins
the service's key sets against these models in both directions, and
`tests/test_api_contract_drift.py` pairs them with the interfaces of the same names in
`frontend/src/lib/api.ts`.

Figures are in the company's reporting currency (`currency`, USD for every filer this
reads) and are **not** projected into the base currency. Margins are fractions (0.25 is
25%). Every `None` is an unknown that stays absent, never 0.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class CompanyFinancialsCompany(BaseModel):
    security_id: int
    symbol: str
    name: str
    cik: Optional[int] = None
    currency: Optional[str] = Field(
        None, description="The reporting currency of the stored figures."
    )
    first_period_end: Optional[str] = None
    last_period_end: Optional[str] = None
    last_fetched_at: Optional[str] = Field(
        None, description="When EDGAR was last read, UTC."
    )


class CompanyFinancialsPoint(BaseModel):
    period_end: str
    label: str = Field(
        ...,
        description="`Q3 FY2025` for a quarter or a TTM point, `FY2025` for a year.",
    )
    value: Optional[float] = None
    yoy: Optional[float] = Field(
        None,
        description="This point against the one a year before it, by the series' `yoy` "
        "rule. Always null for a margin.",
    )


class CompanyFinancialsSeries(BaseModel):
    points: List[CompanyFinancialsPoint]
    cagr: Optional[float] = Field(
        None,
        description="Over the range's first and last known values; null when the start is "
        "not positive, the end is negative, or the span is under a year. "
        "Always null for a margin.",
    )
    yoy: Optional[float] = Field(
        None,
        description="The latest point against the one a year before it; null when that is "
        "missing or not positive. Always null for a margin.",
    )


class CompanyFinancialsSeriesSet(BaseModel):
    revenue: CompanyFinancialsSeries
    revenue_per_share: CompanyFinancialsSeries
    net_income: CompanyFinancialsSeries
    eps_diluted: CompanyFinancialsSeries
    fcf: CompanyFinancialsSeries
    fcf_after_sbc: CompanyFinancialsSeries
    fcf_per_share: CompanyFinancialsSeries
    fcf_after_sbc_per_share: CompanyFinancialsSeries
    diluted_shares: CompanyFinancialsSeries
    operating_margin: CompanyFinancialsSeries
    net_margin: CompanyFinancialsSeries
    fcf_margin: CompanyFinancialsSeries
    dividends_per_share: CompanyFinancialsSeries


class CompanyFinancialsResponse(BaseModel):
    security_id: int
    symbol: str
    name: str
    currency: Optional[str] = None
    period: str = Field(..., description="`quarterly`, `ttm` or `annual`.")
    range: str = Field(..., description="`5y`, `10y` or `max`.")
    as_of: Optional[str] = Field(None, description="When EDGAR was last read, UTC.")
    warnings: List[str]
    series: CompanyFinancialsSeriesSet
