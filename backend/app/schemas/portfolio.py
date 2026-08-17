"""
Portfolio Schemas
Pydantic models for portfolio API responses.
"""
from pydantic import BaseModel, Field
from typing import Dict, List, Literal, Optional
from datetime import date


class TaxLotInfo(BaseModel):
    """Individual taxlot information within a position"""
    open_date: str
    quantity: float
    cost_basis: float
    cost_basis_eur: float

    class Config:
        from_attributes = True


class AnalystRatingInfo(BaseModel):
    """Analyst rating information for a security"""
    strong_buy: int
    buy: int
    hold: int
    sell: int
    strong_sell: int
    total_ratings: int
    consensus: str
    last_updated: str

    class Config:
        from_attributes = True


class PositionResponse(BaseModel):
    """Response model for individual security position"""
    security_id: int
    symbol: str
    description: str
    isin: str
    currency: str
    exchange: Optional[str]
    quantity: float
    cost_basis_eur: float
    market_value_eur: float
    market_price: Optional[float]
    gain_loss_eur: float
    gain_loss_percent: float
    taxlots: List[TaxLotInfo]
    analyst_rating: Optional[AnalystRatingInfo] = None

    class Config:
        from_attributes = True


class PortfolioValuePoint(BaseModel):
    """Single data point for portfolio value over time"""
    date: str = Field(..., description="Date in ISO format (YYYY-MM-DD)")
    cost_basis_eur: float = Field(..., description="Total amount invested up to this date (in base_currency)")
    market_value_eur: float = Field(..., description="Current market value on this date (in base_currency)")
    gain_loss_eur: float = Field(..., description="Unrealized gain/loss (in base_currency)")
    gain_loss_percent: float = Field(..., description="Percentage gain/loss")
    external_flow_eur: float = Field(
        0.0,
        description=(
            "Money entering (+) or leaving (−) the holdings on this date: purchases "
            "at cost, sales at market proceeds. Net this out of any return measure — "
            "deriving it from the cost-basis line books a sale's gain as a loss."
        ),
    )
    unpriced_holdings: int = Field(
        0,
        description=(
            "Held securities whose value could not be resolved on this date. Anything "
            "above 0 means market_value_eur is an INCOMPLETE sum while cost_basis_eur "
            "is not, so gain_loss_eur and gain_loss_percent understate by the whole "
            "value of those holdings. Fifteen days past the last cached price every "
            "holding drops out and the point reads 0 / −100%, a fabricated wipeout "
            "rather than a gap — and the partial case is worse, because a plausible "
            "+15% invites no doubt. Previously only logged, at up to ~29k lines for a "
            "730-day window over 40 securities, which is noise rather than a signal."
        ),
    )
    base_currency: str = Field("EUR", description="Currency the *_eur values are expressed in")

    class Config:
        from_attributes = True


class AnnualizedReturnResponse(BaseModel):
    """Response for XIRR annualized return calculation"""
    method: str = Field(..., description="Calculation method used (xirr)")
    annualized_return_pct: Optional[float] = Field(None, description="Annualized return percentage")
    start_date: str = Field(..., description="Start date of the calculation period")
    end_date: str = Field(..., description="End date of the calculation period")
    num_cash_flows: int = Field(..., description="Number of cash flows used in calculation")
    # Held securities that could not be valued at one of the window's two endpoints.
    # Above 0 means the return is computed from a partial valuation: the tax-lot
    # purchases are unconditional flows while the endpoint value omits those holdings,
    # so the figure understates (or overstates, when it is the start that is short).
    #
    # Declared here because a response_model is a FILTER: the router can pass this key
    # all it likes and an undeclared field is dropped from the wire in silence -- the
    # trap `test_dividend_summary_contract.py` exists for, and the same reason
    # `PerformanceAttributionResponse` carries this note. Defaulted, so the paths that
    # return before either valuation runs stay valid.
    unpriced_holdings: int = 0

    class Config:
        from_attributes = True


class BenchmarkValuePoint(BaseModel):
    """Single data point for benchmark comparison"""
    date: str
    benchmark_value_eur: float
    cost_basis_eur: float
    gain_loss_eur: float
    gain_loss_percent: float


class BenchmarkResponse(BaseModel):
    """Response for benchmark comparison endpoint"""
    benchmark_name: str
    benchmark_ticker: str
    data: List[BenchmarkValuePoint]


class BenchmarkInfo(BaseModel):
    """Available benchmark index info"""
    key: str
    name: str
    ticker: str
    currency: str


class SecurityAttribution(BaseModel):
    """P&L attribution for a single security over a time period"""
    security_id: int
    symbol: str
    description: str
    start_market_value_eur: float
    end_market_value_eur: float
    new_investment_eur: float
    # Capital returned by lots closed in the period (market price at close date).
    disposal_proceeds_eur: float = 0.0
    value_change_eur: float
    pnl_contribution_eur: float
    contribution_percent: float
    weight_percent: float

    class Config:
        from_attributes = True


class PerformanceAttributionResponse(BaseModel):
    """Response for performance attribution endpoint"""
    start_date: str
    end_date: str
    total_pnl_eur: float
    # Securities left out because they could not be valued at an endpoint. Above 0
    # means total_pnl_eur covers less than the whole book.
    #
    # Declared here because a response_model is a FILTER: the service can return this
    # key all it likes, and an undeclared field is dropped from the wire silently --
    # the trap `test_dividend_summary_contract.py` exists for. Defaulted so the two
    # early-return paths stay valid.
    unpriced_holdings: int = 0
    attributions: List[SecurityAttribution]

    class Config:
        from_attributes = True


class PortfolioSummary(BaseModel):
    """Current portfolio summary"""
    total_cost_basis_eur: float = Field(..., description="Total amount invested")
    total_market_value_eur: float = Field(..., description="Current total value")
    total_gain_loss_eur: float = Field(..., description="Total unrealized gain/loss")
    total_gain_loss_percent: float = Field(..., description="Total percentage gain/loss")
    num_positions: int = Field(..., description="Number of unique securities held")
    unpriced_holdings: int = Field(
        0,
        description=(
            "Held securities whose value could not be resolved today. Above 0 means "
            "total_market_value_eur is an INCOMPLETE sum while total_cost_basis_eur is "
            "not, so the gain and percent understate by those holdings' whole value. "
            "This is the SBI shape: deleting one security's poisoned prices took 446.93 "
            "CHF off the headline with only a sync warning to catch it. Computed by the "
            "same helper as PortfolioValuePoint.unpriced_holdings, so the total and every "
            "chart point agree about their own completeness. Note the backend fails to "
            "value a holding for two reasons — no price OR no FX rate — so a client "
            "counting `market_price === null` itself would under-report."
        ),
    )
    date: Optional[str] = Field(None, description="Date of the summary")
    base_currency: str = Field("EUR", description="Currency the *_eur values are expressed in")
    total_realized_gain_loss_eur: float = Field(0.0, description="Realized gain/loss from closed positions")
    total_realized_proceeds_eur: float = Field(0.0, description="Approximate proceeds from closed positions (qty × close-date price × FX)")
    total_realized_cost_basis_eur: float = Field(0.0, description="Cost basis of closed positions")
    num_closed_positions: int = Field(0, description="Number of unique securities with closed lots")

    class Config:
        from_attributes = True


class FundamentalMetricsResponse(BaseModel):
    """Fundamental metrics for a single security"""
    security_id: int
    symbol: str
    description: str
    exchange: Optional[str] = None
    currency: str
    quote_type: Optional[str] = None
    trailing_pe: Optional[float] = None
    forward_pe: Optional[float] = None
    peg_ratio: Optional[float] = None
    price_to_sales: Optional[float] = None
    price_to_book: Optional[float] = None
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    fwd_revenue_growth: Optional[float] = None
    fwd_eps_growth: Optional[float] = None
    profit_margins: Optional[float] = None
    gross_margins: Optional[float] = None
    operating_margins: Optional[float] = None
    market_cap: Optional[int] = None
    number_of_analysts: Optional[int] = None
    target_mean_price: Optional[float] = None
    target_high_price: Optional[float] = None
    target_low_price: Optional[float] = None
    data_currency: Optional[str] = None
    last_updated: Optional[str] = None

    class Config:
        from_attributes = True


class EarningsCalendarItem(BaseModel):
    """Upcoming earnings event"""
    security_id: int
    symbol: str
    description: str
    earnings_date: str
    eps_estimate: Optional[float] = None

    class Config:
        from_attributes = True


class EarningsHistoryItem(BaseModel):
    """Past earnings event with surprise data"""
    security_id: int
    symbol: str
    description: str
    earnings_date: str
    eps_estimate: Optional[float] = None
    reported_eps: Optional[float] = None
    surprise_percent: Optional[float] = None
    beat_or_miss: Optional[str] = None

    class Config:
        from_attributes = True


class WatchlistItemResponse(BaseModel):
    """Watchlist item with cached fundamentals and technical indicators"""
    id: int
    yahoo_ticker: str
    symbol: Optional[str] = None
    company_name: Optional[str] = None
    notes: Optional[str] = None
    target_price: Optional[float] = None
    current_price: Optional[float] = None
    currency: Optional[str] = None
    trailing_pe: Optional[float] = None
    forward_pe: Optional[float] = None
    peg_ratio: Optional[float] = None
    ev_to_ebitda: Optional[float] = None
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    fwd_revenue_growth: Optional[float] = None
    fwd_eps_growth: Optional[float] = None
    profit_margins: Optional[float] = None
    market_cap: Optional[int] = None
    analyst_target: Optional[float] = None
    analyst_rating: Optional[str] = None
    analyst_count: Optional[int] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    pct_from_52w_high: Optional[float] = None
    ma200: Optional[float] = None
    ma50: Optional[float] = None
    pct_from_ma200: Optional[float] = None
    rsi14: Optional[float] = None
    buy_score: Optional[float] = None
    data_currency: Optional[str] = None
    last_synced: Optional[str] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class AddWatchlistItemRequest(BaseModel):
    """Request to add a stock to the watchlist"""
    yahoo_ticker: str
    notes: Optional[str] = None
    target_price: Optional[float] = None


class UpdateWatchlistItemRequest(BaseModel):
    """Request to update a watchlist item"""
    notes: Optional[str] = None
    target_price: Optional[float] = None


class DividendMonthlyItem(BaseModel):
    month: str  # "YYYY-MM"
    amount_eur: float

class DividendSummaryResponse(BaseModel):
    """
    The Performance tab's *Dividend Income* card.

    **Every key the service returns must be declared here.** A `response_model` filters
    the response down to its declared fields, so an omission does not fail loudly — it
    silently blanks whatever read the missing key. Five of these were undeclared while
    the route had no `response_model` at all; attaching one without completing the model
    first would have removed the provenance footnote (`source`, `ibkr_from`,
    `total_withholding_eur`) that exists to stop net income being read as pre-tax.
    """
    monthly: List[DividendMonthlyItem]      # NET per month, era-spliced
    ytd_eur: float
    total_eur: float                        # NET (back-compat key for total_net_eur)
    total_net_eur: float
    total_gross_eur: float
    total_withholding_eur: float
    # Three-way like the tax report's flag, not a boolean: once the IBKR ledger starts,
    # the card still carries the estimated months before `ibkr_from`, so a flat "ibkr"
    # would claim real withholding for a period that has none.
    source: Literal["ibkr", "mixed", "yfinance_estimate"] = "yfinance_estimate"
    ibkr_from: Optional[str] = None
    base_currency: str = "EUR"
    last_updated: Optional[str] = None
    sync_in_progress: bool = False


class DividendMonthBar(BaseModel):
    """One month of the dividends chart: net amounts per symbol, actual vs forecast."""
    month: str                      # "YYYY-MM"
    actual: Dict[str, float]        # symbol -> net received (base currency)
    forecast: Dict[str, float]      # symbol -> projected net (cadence-based)
    actual_total_eur: float
    forecast_total_eur: float
    # Growth of the REALIZED figure only, against the previous month and the same
    # month a year earlier. None when there is no comparable base (a zero prior
    # month, which a quarterly payer produces constantly) or when this month holds
    # only forecast — a projection's flat median would otherwise read as a real
    # change in the payout schedule. Computed over the whole history, so they stay
    # correct when the response is windowed to one year.
    mom_pct: Optional[float] = None
    yoy_pct: Optional[float] = None


class DividendSecurityRow(BaseModel):
    """Per-security dividend totals for the selected window."""
    security_id: int
    symbol: str
    # Disambiguates a ticker listed on two venues (identity is isin + exchange).
    exchange: Optional[str] = None
    description: str
    payouts: int
    gross_eur: float
    withholding_eur: float
    net_eur: float
    forecast_payouts: int
    forecast_net_eur: float
    trailing_yield_pct: Optional[float] = None  # TTM net / current market value
    # The forward counterpart: the security's projection for the NEXT 12 MONTHS over
    # its current market value. Weight these by market value and they average to
    # `DividendForwardYield.pct`, which is what makes that headline auditable.
    # Deliberately unwindowed, unlike `forecast_net_eur` above — see the service.
    forward_yield_pct: Optional[float] = None
    # True when the position wasn't held for the whole trailing year, so the yield
    # above divides a partial year's income by a full position value and reads low.
    # Not annualized: scaling up would invent income the schedule may not support.
    trailing_yield_partial: bool = False
    days_held_in_ttm: Optional[int] = None
    # The projected next-12-month rate over what the position cost — the same numerator
    # as `forward_yield_pct`, so the two differ only by their denominator and the gap
    # between them is the holding's own appreciation.
    #
    # Deliberately NOT trailing income over current cost. Those describe different
    # positions whenever the size changed inside the window: adding to a holding divides
    # a smaller position's income by the finished position's cost and reads far too low,
    # and a sell-and-rebuy is that at its most extreme. It also disagreed with the
    # portfolio-level card, which has always been forward-over-cost.
    yield_on_cost_pct: Optional[float] = None
    share_pct: Optional[float] = None       # of the window's total (actual + forecast)
    next_pay_date: Optional[str] = None     # earliest projected payment, if any
    source: Optional[str] = None    # 'ibkr' | 'estimate' | 'mixed'; None = forecast-only
    # 'net' = sized from dividends actually received; 'gross_estimate' = from
    # yfinance's gross per-share only, so withholding isn't deducted.
    forecast_basis: Optional[str] = None
    # How thin the projection's inference is: how many dated payments defined the
    # schedule, and the median gap it settled on. Two samples is a guess with a
    # schedule attached — SBI's five projected payouts rested on exactly two rows
    # from the wrong ticker, and nothing on the page said so.
    forecast_samples: Optional[int] = None
    forecast_cadence_days: Optional[int] = None


class DividendDelta(BaseModel):
    """A figure beside the comparable it is measured against."""
    net_eur: float
    prev_net_eur: Optional[float] = None
    # None whenever the base is zero. A percentage against zero is undefined, not
    # large, and rendering one would put a fabricated number next to a real one.
    pct: Optional[float] = None


class DividendAnnualRow(BaseModel):
    """One calendar year of the year-over-year comparison."""
    year: int
    net_eur: float                  # actually received
    forecast_net_eur: float         # projected inside this year
    total_eur: float                # net + forecast — what the bar length shows

    # yoy_pct compares total_eur against the previous row's total_eur: one rule for
    # every row, degrading to plain actual-vs-actual for two complete past years.
    yoy_pct: Optional[float] = None
    # True when this row or the one it is compared against contains projection, so
    # the chip can be marked as forward-looking rather than measured.
    yoy_includes_forecast: bool = False
    # True when the prior year's realized figure did not cover a full year, which
    # makes the percentage overstate growth. On this account 2024 holds only 7
    # months of income, so 2025/2024 reads +1300% and means very little.
    yoy_vs_partial: bool = False
    # This year's realized figure covers less than the whole calendar year — it is
    # still running, or the income history begins inside it.
    partial: bool = False


class DividendLatestMonth(BaseModel):
    """The most recent month with realized income, and how it moved."""
    month: str
    net_eur: float
    mom_pct: Optional[float] = None
    yoy_pct: Optional[float] = None


class DividendGrowth(BaseModel):
    """
    Growth of dividend income, computed over the FULL history regardless of the
    year filter — with year=2026 selected the client holds no 2025 months and
    could not derive any of this itself.
    """
    # Trailing 12 months against the 12 before it. The headline: dividends here
    # are quarterly, so a raw month-over-month swings +/-90% on cadence alone and
    # cannot carry the summary.
    ttm: DividendDelta
    # The two 12-month windows straddle the era-splice boundary, so one side is
    # IBKR actuals and the other yfinance estimates — comparable in size, not in
    # provenance. Badge it rather than presenting it as like-for-like.
    ttm_crosses_era: bool = False
    # Jan 1 -> today against Jan 1 -> the same calendar day last year. Strictly
    # like-for-like: comparing a part-year against a whole prior year is not growth.
    ytd: DividendDelta
    avg_month: DividendDelta        # ttm / 12 against prior ttm / 12
    next_12m_eur: float             # projected over the next 365 days
    next_12m_vs_ttm_pct: Optional[float] = None   # forecast vs measured — mark it
    annual: List[DividendAnnualRow]
    latest_month: Optional[DividendLatestMonth] = None


class DividendUpcomingPayment(BaseModel):
    """One projected payment, dated — the dividend calendar."""
    date: str
    security_id: int
    symbol: str
    net_eur: float
    basis: Optional[str] = None     # 'net' | 'gross_estimate'


class DividendForwardYield(BaseModel):
    """
    What the portfolio as held today will pay over the next twelve months, as a rate.

    A sibling of {@link DividendGrowth} rather than a member of it: growth is derived
    from the unwindowed payment *history*, and a figure that moves with a market price
    is neither growth nor history.

    The whole object is absent — not zeroed — when no projection was run or nothing is
    priced. That distinction cannot be carried by the fields: `paying_holdings: 0`
    beside `pct: None` reads as "nothing in this portfolio pays a dividend" when the
    truth is "there was nothing to divide by", and a count of 0 out of 0 holdings on an
    account with 36 of them is the *most* misleading form of that.
    """
    # Projected net income over the next 365 days, base currency — restricted to
    # priced holdings, so it is the exact numerator of both ratios below rather than
    # `growth.next_12m_eur`, which counts unpriced ones too.
    annual_eur: float
    pct: float                              # annual_eur / market value of priced holdings
    # Same income over what those holdings cost. Higher than `pct` on an appreciated
    # book, and the gap between the two is exactly that appreciation — which only holds
    # because both denominators cover the same set of securities.
    on_cost_pct: Optional[float] = None
    # Coverage. An accumulating ETF counts as priced and not as paying, which is the
    # correct answer for it: it makes a low yield legible as "most of this book does
    # not distribute" instead of looking like missing data.
    paying_holdings: int
    priced_holdings: int
    # Held but carrying no usable market value. Excluded from both sides, because a
    # position valued at 0.00 would add its projected income to the numerator and
    # nothing to the denominator — the SBI shape, which reads the yield *high*.
    unpriced_holdings: int
    # How much of `annual_eur` is sized from yfinance gross per-share, which deducts no
    # withholding. Quantified rather than flagged, so the caveat can be stated in a
    # footnote instead of only asserted.
    gross_estimate_eur: float
    basis: str                              # 'net' | 'mixed' | 'gross_estimate'


class DividendBreakdownResponse(BaseModel):
    years: List[int]
    year: Optional[int] = None      # None = all time
    months: List[DividendMonthBar]
    securities: List[DividendSecurityRow]
    total_net_eur: float
    total_forecast_net_eur: float
    # Era-splice boundary: yfinance estimates strictly before, IBKR rows from here.
    ibkr_from: Optional[str] = None
    base_currency: str
    growth: Optional[DividendGrowth] = None
    # None = no projection was run, or nothing held is priced. Never a zeroed object.
    forward_yield: Optional[DividendForwardYield] = None
    upcoming: List[DividendUpcomingPayment] = []


class ContributionWindow(BaseModel):
    """Money in per month over one trailing window."""
    label: str          # "all" | "12m" | "6m" | "3m"
    months: float       # divisor actually used (clamped to available history)
    partial: bool       # True when history is shorter than the nominal window

    # The answer. Spliced at the deposit-coverage boundary: lot cost basis before it,
    # real deposits from it onward — so a position rotation cannot inflate it.
    money_in_eur: float
    avg_money_in_per_month_eur: float
    money_in_method: str    # "deposits" | "spliced" | "deployed"
    deposits_eur: float     # the post-boundary part, for the tooltip

    # Secondary: capital put to work. Exceeds money_in once positions are rotated,
    # and that gap is why it stays on screen.
    deployed_eur: float
    avg_deployed_per_month_eur: float

    net_eur: float      # deployed minus released; tooltip + cost-basis identity check


class ContributionMonthlyItem(BaseModel):
    month: str          # "YYYY-MM"
    deployed_eur: float  # cost basis of lots opened in the month
    net_eur: float       # deployed minus released in the month


class ContributionsResponse(BaseModel):
    windows: List[ContributionWindow]
    monthly: List[ContributionMonthlyItem]
    first_contribution_date: Optional[str] = None
    # The splice point: from here the deposit ledger is complete, so money_in switches
    # from lot cost basis to real deposits. What the statements claim to cover, clamped
    # forward to the ledger's first row — the account is younger than the statement
    # period that reports it, and the days before it opened are not covered by anything.
    coverage_from: Optional[str] = None
    # The first actual deposit row (>= coverage_from). Earlier deposits went to the
    # previous brokers, so nothing before this is knowable from IBKR.
    deposits_from: Optional[str] = None
    # Context for the pre-ledger era: when the holdings arrived from the previous
    # brokers. Not the boundary — deposits usually predate the positions.
    transfer_in_date: Optional[str] = None
    base_currency: str


class FundamentalsStatus(BaseModel):
    """Status of fundamentals data cache"""
    total_securities: int
    securities_with_data: int
    securities_without_data: int
    stale_metrics: int
    total_earnings_events: int
    oldest_update: Optional[str] = None
    newest_update: Optional[str] = None

    class Config:
        from_attributes = True
