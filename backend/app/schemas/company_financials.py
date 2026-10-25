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
    cik: int = Field(..., description="The SEC Central Index Key; the path's id.")
    security_id: Optional[int] = Field(
        None, description="The held listing (lowest symbol); null for a company not held."
    )
    symbol: str
    name: str
    isin: Optional[str] = Field(None, description="For the logo.")
    held: bool = Field(
        ..., description="False for a Nasdaq-100 constituent the account does not hold."
    )
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


class CompanyFinancialsCagrs(BaseModel):
    """Growth to the latest point from 3, 5 and 10 years before it, over the whole history
    (not the range). Always null for a margin."""

    y3: Optional[float] = None
    y5: Optional[float] = None
    y10: Optional[float] = None


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
    cagrs: CompanyFinancialsCagrs


class CompanyFinancialsKeyStats(BaseModel):
    """The newest TTM point's figures, whatever period the charts show."""

    label: Optional[str] = None
    period_end: Optional[str] = None
    revenue: Optional[float] = None
    revenue_yoy: Optional[float] = None
    revenue_cagr_5y: Optional[float] = None
    net_income: Optional[float] = None
    eps_diluted: Optional[float] = None
    eps_yoy: Optional[float] = None
    eps_cagr_5y: Optional[float] = None
    fcf: Optional[float] = None
    fcf_per_share: Optional[float] = None
    fcf_yoy: Optional[float] = None
    gross_margin: Optional[float] = None
    operating_margin: Optional[float] = None
    net_margin: Optional[float] = None
    fcf_margin: Optional[float] = None
    diluted_shares: Optional[float] = None
    shares_yoy: Optional[float] = None
    dividends_per_share: Optional[float] = None
    payout_ratio: Optional[float] = Field(
        None,
        description="TTM dividends per share / TTM diluted EPS; null when EPS <= 0.",
    )
    pe_ratio: Optional[float] = Field(
        None,
        description="The newest close (two weeks old at most) / TTM diluted EPS; null "
        "without one, or when EPS <= 0.",
    )
    market_cap: Optional[float] = Field(
        None,
        description="The same close x shares outstanding at the newest quarter end; null when "
        "that count is more than 10% off the diluted average (one class of several).",
    )
    ps_ratio: Optional[float] = Field(None, description="The same close / TTM revenue per share.")
    pfcf_ratio: Optional[float] = Field(None, description="The same close / TTM FCF per share.")
    dividend_yield: Optional[float] = Field(
        None, description="TTM dividends per share / the same close, a fraction."
    )
    roe: Optional[float] = Field(
        None,
        description="TTM net income / mean equity over the year; null when either equity "
        "is not positive.",
    )
    buybacks: Optional[float] = None
    dividends_paid: Optional[float] = None


class CompanyFinancialsSeriesSet(BaseModel):
    revenue: CompanyFinancialsSeries
    revenue_per_share: CompanyFinancialsSeries
    operating_income: CompanyFinancialsSeries
    net_income: CompanyFinancialsSeries
    eps_diluted: CompanyFinancialsSeries
    fcf: CompanyFinancialsSeries
    fcf_after_sbc: CompanyFinancialsSeries
    fcf_per_share: CompanyFinancialsSeries
    fcf_after_sbc_per_share: CompanyFinancialsSeries
    diluted_shares: CompanyFinancialsSeries
    gross_margin: CompanyFinancialsSeries
    operating_margin: CompanyFinancialsSeries
    net_margin: CompanyFinancialsSeries
    fcf_margin: CompanyFinancialsSeries
    dividends_per_share: CompanyFinancialsSeries
    buybacks: CompanyFinancialsSeries
    dividends_paid: CompanyFinancialsSeries
    cash: CompanyFinancialsSeries
    debt: CompanyFinancialsSeries
    pe_ratio: CompanyFinancialsSeries
    ps_ratio: CompanyFinancialsSeries
    pfcf_ratio: CompanyFinancialsSeries
    dividend_yield: CompanyFinancialsSeries
    roe: CompanyFinancialsSeries


class CompanyFinancialsResponse(BaseModel):
    cik: int = Field(..., description="The SEC Central Index Key; the path's id.")
    security_id: Optional[int] = Field(
        None, description="The held listing (lowest symbol); null for a company not held."
    )
    symbol: str
    name: str
    isin: Optional[str] = Field(None, description="For the logo.")
    held: bool = Field(
        ..., description="False for a Nasdaq-100 constituent the account does not hold."
    )
    currency: Optional[str] = None
    period: str = Field(..., description="`quarterly`, `ttm` or `annual`.")
    range: str = Field(..., description="`5y`, `10y` or `max`.")
    as_of: Optional[str] = Field(None, description="When EDGAR was last read, UTC.")
    warnings: List[str]
    key_stats: CompanyFinancialsKeyStats
    series: CompanyFinancialsSeriesSet
