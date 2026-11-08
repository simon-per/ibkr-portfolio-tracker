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
    security_ids: List[int] = Field(
        default_factory=list,
        description="Every held listing of the company (GOOG and GOOGL); empty when not held.",
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
    #: A valuation multiple's median over the last 3, 5 and 10 years; null elsewhere and
    #: where the span holds fewer than three values (review item R3).
    medians: CompanyFinancialsCagrs


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
    net_cash: Optional[float] = Field(
        None,
        description="Cash and short-term investments less debt at the newest quarter end; "
        "null when either side is not known.",
    )
    enterprise_value: Optional[float] = Field(None, description="Market cap less net cash.")
    ev_ebit: Optional[float] = Field(
        None, description="Enterprise value / TTM operating income; null when either <= 0."
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
    #: Short-term investments: what net cash counts beside cash (cash + investments -
    #: debt = net cash).
    investments: CompanyFinancialsSeries
    debt: CompanyFinancialsSeries
    net_cash: CompanyFinancialsSeries
    pe_ratio: CompanyFinancialsSeries
    ps_ratio: CompanyFinancialsSeries
    pfcf_ratio: CompanyFinancialsSeries
    dividend_yield: CompanyFinancialsSeries
    roe: CompanyFinancialsSeries
    price: CompanyFinancialsSeries = Field(
        ...,
        description="The market closes (weekly, and a held listing's newest daily one); each "
        "point's `label` is its date. The range applies, the period does not.",
    )


class CompanyFinancialsRating(BaseModel):
    """The current period's analyst recommendation counts; all zero is "no coverage"."""

    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int
    consensus: str = Field(
        ..., description="`analyst_rating.consensus_of`, the label every rating shows."
    )


class CompanyFinancialsProfile(BaseModel):
    """
    The overview's company-level figures (docs/company-financials.md, *The overview*): the
    SEC's industry and fiscal year end, the price line from the market closes, and Yahoo's
    figures — a held company's from its listing's rows, any other's from the 05:00 chunk.
    Growth and returns are fractions (0.25 is 25%); anything unknown is null.
    """

    industry: Optional[str] = Field(None, description="The SEC's SIC description.")
    fiscal_year_end: Optional[str] = Field(None, description="`MMDD`, as the SEC states it.")
    description: Optional[str] = Field(None, description="Yahoo's business summary.")
    price: Optional[float] = Field(None, description="The newest market close, `currency`.")
    price_date: Optional[str] = None
    return_1m: Optional[float] = None
    return_1y: Optional[float] = None
    return_5y: Optional[float] = Field(None, description="Annualised.")
    return_10y: Optional[float] = Field(None, description="Annualised.")
    week52_low: Optional[float] = Field(None, description="Of the closes, not intraday.")
    week52_high: Optional[float] = Field(None, description="Of the closes, not intraday.")
    forward_pe: Optional[float] = None
    peg_ratio: Optional[float] = None
    fwd_revenue_growth: Optional[float] = None
    fwd_eps_growth: Optional[float] = None
    rating: Optional[CompanyFinancialsRating] = None
    analysts: Optional[int] = None
    target_low: Optional[float] = None
    target_mean: Optional[float] = None
    target_high: Optional[float] = None
    target_upside: Optional[float] = Field(
        None,
        description="The mean target over the newest close; null unless both are in one "
        "currency and the close is current.",
    )
    yahoo_currency: Optional[str] = Field(
        None, description="The currency of the targets and the EPS estimate (Yahoo's quote)."
    )
    next_earnings_date: Optional[str] = Field(None, description="UTC.")
    next_eps_estimate: Optional[float] = None
    yahoo_as_of: Optional[str] = Field(None, description="When Yahoo's figures were read, UTC.")


class CompanyDividendTrack(BaseModel):
    """One held listing's company dividend record (`/dividend-track`, review item FD4)."""

    #: The fiscal years' dividends per share, 5-year compound growth (0.08 is 8%).
    dps_cagr_5y: Optional[float] = None
    #: Fiscal years in a row, newest back, that paid more per share than the one before.
    raise_years: Optional[int] = None
    #: TTM dividends per share over TTM diluted EPS (0.35 is 35%).
    payout_ratio: Optional[float] = None


class CompanyFinancialsLatestQuarter(BaseModel):
    """The newest quarter on its own: the overview's "Latest quarter" row (R4)."""

    label: str
    period_end: str
    filed: Optional[str] = None
    revenue_yoy: Optional[float] = None
    eps_yoy: Optional[float] = None
    fcf_yoy: Optional[float] = None
    #: The operating margin against the same quarter a year before, as a fraction of
    #: revenue (0.012 is +1.2 points).
    operating_margin_change: Optional[float] = None


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
    range: str = Field(..., description="`3y`, `5y`, `10y` or `max`.")
    as_of: Optional[str] = Field(None, description="When EDGAR was last read, UTC.")
    warnings: List[str]
    key_stats: CompanyFinancialsKeyStats
    latest_quarter: Optional[CompanyFinancialsLatestQuarter] = None
    profile: CompanyFinancialsProfile
    series: CompanyFinancialsSeriesSet
