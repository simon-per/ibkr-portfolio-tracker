"""
Wire shapes for `/api/performance/*`.

A `response_model` is a FILTER (CLAUDE.md, *Conventions*): a key the service returns
that is not declared here is dropped silently. `tests/test_performance_analytics.py`
pins each service's key set against these models in both directions.

Every `Optional[float]` below is an unknown that stays absent — a `None` is the
answer "could not be measured", never a stand-in for 0.
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class DecompositionWindow(BaseModel):
    start_date: str
    end_date: str
    start_total_value_eur: Optional[float]
    end_total_value_eur: Optional[float]
    net_flows_eur: Optional[float] = Field(
        None, description="Money paid in (+) or taken out (−) inside the window; the Money In legs."
    )
    gain_eur: Optional[float] = Field(
        None, description="end − start − net_flows: everything the account earned in the window."
    )
    gain_pct: Optional[float] = Field(
        None, description="Modified-Dietz percentage of the gain; None when there is no base to measure against."
    )
    price_effect_eur: Optional[float] = Field(
        None, description="What the holdings earned in their own currencies, converted at the window-end rate."
    )
    fx_effect_eur: Optional[float] = Field(
        None, description="What the base currency's moves against the holdings' currencies added or took away."
    )
    unsplit_eur: Optional[float] = Field(
        None, description="Gain of holdings whose price/FX split could not be built; carried whole."
    )
    unsplit_securities: int = 0
    dividends_eur: Optional[float] = Field(None, description="Net dividend cash that landed in the account (IBKR ledger).")
    fees_interest_eur: Optional[float] = Field(
        None, description="Broker interest, fees and FX spread: the corrections IBKR's measured cash balance applies to the derived one."
    )
    unexplained_eur: Optional[float] = Field(
        None, description="The remainder that makes the legs sum exactly to the change in total value. Named, never folded in."
    )
    unpriced_holdings: int = 0
    warnings: List[str] = []


class DecompositionYear(DecompositionWindow):
    year: int
    partial: bool = Field(False, description="The year is not fully covered — it is the current one, or the account began inside it.")


class ReturnDecompositionResponse(BaseModel):
    base_currency: str
    cash_source: str
    window: DecompositionWindow
    years: List[DecompositionYear]


class SegmentRow(BaseModel):
    name: str
    pnl_eur: Optional[float]
    price_effect_eur: Optional[float] = None
    fx_effect_eur: Optional[float] = None
    share_of_gain_pct: Optional[float] = None
    start_value_eur: Optional[float] = None
    end_value_eur: Optional[float] = None
    start_weight_pct: Optional[float] = None
    end_weight_pct: Optional[float] = None
    via_funds_pct: Optional[float] = Field(
        None, description="How much of the segment's end value arrived through fund baskets rather than direct holdings."
    )


class SegmentAttributionResponse(BaseModel):
    start_date: str
    end_date: str
    total_pnl_eur: Optional[float]
    start_total_value_eur: Optional[float]
    end_total_value_eur: Optional[float]
    unpriced_holdings: int = 0
    basket_as_of_oldest: Optional[str] = None
    proxied_funds: List[str] = []
    by_sector: List[SegmentRow]
    by_country: List[SegmentRow]
    warnings: List[str] = []


class ClosedPosition(BaseModel):
    security_id: int
    symbol: str
    description: str
    account: str
    still_held: bool
    lots_closed: int
    first_open_date: str
    last_close_date: str
    holding_days: Optional[int]
    cost_basis_eur: Optional[float]
    proceeds_eur: Optional[float]
    realized_pnl_eur: Optional[float]
    realized_source: str = Field(..., description="'trade' (IBKR's own FIFO figure) or 'closed_lots' (market-price approximation).")
    return_pct: Optional[float]
    post_sale_pct: Optional[float] = Field(None, description="Quote-currency move from the last close before the sale to the newest close after it; None without a later price.")
    post_sale_days: Optional[int] = None


class ClosedPositionsSummary(BaseModel):
    closed_securities: int
    winners: int
    losers: int
    hit_rate_pct: Optional[float]
    total_realized_eur: Optional[float]
    total_cost_eur: Optional[float]
    avg_holding_days: Optional[int]
    best: Optional[str]
    worst: Optional[str]
    post_sale_judged: int
    sold_then_rose: int
    sold_then_fell: int


class ClosedPositionsResponse(BaseModel):
    base_currency: str
    positions: List[ClosedPosition]
    summary: ClosedPositionsSummary
    warnings: List[str] = []
