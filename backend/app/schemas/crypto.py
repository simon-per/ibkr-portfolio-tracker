"""
Wire shapes for `/api/crypto/*` (docs/crypto.md).

A `response_model` is a FILTER (CLAUDE.md, *Conventions*): a key the service returns that
is not declared here is dropped silently. `tests/test_crypto_contract.py` pins each
`CryptoService` method's key set against these models in both directions, and
`tests/test_api_contract_drift.py` pairs them with the interfaces of the same names in
`frontend/src/lib/api.ts`.

Every money figure is in `base_currency`. Every `Optional` below is an unknown that stays
absent — `None` means "not reported" or "no exchange rate", never 0.
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class CryptoHoldingItem(BaseModel):
    coin_id: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    rank: Optional[int] = Field(None, description="CoinStats market-cap rank.")
    is_fiat: bool = False
    status: str = Field(..., description="`valued`, or `unpriced` when CoinStats has no price.")
    quantity: float
    price: Optional[float] = None
    value: Optional[float] = None
    weight_pct: Optional[float] = Field(
        None, description="Share of CoinStats' portfolio total — never renormalised."
    )
    change_24h_pct: Optional[float] = None
    avg_buy: Optional[float] = Field(None, description="CoinStats' USD figure at the snapshot's rate.")
    total_cost: Optional[float] = Field(None, description="CoinStats' USD figure at the snapshot's rate.")
    unrealized_pl: Optional[float] = Field(None, description="CoinStats' USD figure at the snapshot's rate.")
    unrealized_pl_pct: Optional[float] = None
    realized_pl: Optional[float] = Field(None, description="CoinStats' USD figure at the snapshot's rate.")


class CryptoPortfolioResponse(BaseModel):
    configured: bool
    base_currency: str
    as_of: Optional[str] = Field(None, description="When the snapshot was taken (UTC, ISO 8601).")
    total_value: Optional[float] = None
    itemised_value: Optional[float] = Field(None, description="Σ of the valued holdings.")
    unitemised_value: Optional[float] = Field(
        None, description="total − itemised: anything CoinStats totals but does not list."
    )
    defi_value: Optional[float] = Field(
        None, description="DeFi positions, which CoinStats reports beside `totalValue`, not in it."
    )
    total_cost: Optional[float] = None
    unrealized_pl: Optional[float] = None
    unrealized_pl_pct: Optional[float] = None
    realized_pl: Optional[float] = None
    realized_pl_pct: Optional[float] = None
    all_time_pl: Optional[float] = None
    all_time_pl_pct: Optional[float] = None
    change_24h: Optional[float] = None
    change_24h_pct: Optional[float] = None
    fx_caveat: Optional[str] = Field(
        None, description="Set whenever the base is not USD: cost and P&L are USD figures at one day's rate."
    )
    fx_unavailable: int = 0
    valued_count: int = 0
    spam_count: int = 0
    unpriced_count: int = 0
    unpriced_symbols: List[str] = []
    holdings: List[CryptoHoldingItem] = []
    color_order: List[str] = Field(
        [], description="Valued coin ids by market-cap rank: the colour identity order."
    )
    warnings: List[str] = []


class CryptoHistoryPoint(BaseModel):
    date: str
    value: Optional[float] = None
    pnl: Optional[float] = Field(None, description="CoinStats' cash-flow-adjusted P&L on that day.")


class CryptoHistoryResponse(BaseModel):
    configured: bool
    base_currency: str
    points: List[CryptoHistoryPoint] = []
    fetched_at: Optional[str] = None
    fx_caveat: Optional[str] = None
    fx_unavailable: int = 0
    warnings: List[str] = []


class CryptoLastRun(BaseModel):
    status: str
    reason: Optional[str] = None
    message: Optional[str] = None
    finished_at: Optional[str] = None


class CryptoStatusResponse(BaseModel):
    configured: bool
    last_run: Optional[CryptoLastRun] = None
    last_snapshot_at: Optional[str] = None
    next_run: Optional[str] = None
    sync_in_progress: bool = False
    manual_retry_after_seconds: int = 0
    credits_remaining: Optional[int] = None
    credits_total: Optional[int] = None
    credits_plan: Optional[str] = None
    credits_spent_last_run: Optional[int] = None


class CryptoSyncResponse(BaseModel):
    type: str
    status: str
    reason: Optional[str] = None
    message: str
    warnings: List[str] = []
    timestamp: Optional[str] = None
