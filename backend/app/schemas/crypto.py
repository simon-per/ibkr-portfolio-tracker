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
    status: str = Field(
        ...,
        description="`valued`; `no_price` when CoinGecko has no price for it on the "
                    "snapshot's day; `unpriced` when CoinStats has none.",
    )
    quantity: float
    price: Optional[float] = Field(None, description="CoinGecko's USD price, converted at the day's rate.")
    price_source: Optional[str] = Field(
        None, description="`coingecko`, or `peg` for a stablecoin valued at its fixed peg."
    )
    value: Optional[float] = None
    weight_pct: Optional[float] = Field(
        None, description="Share of the book's total — never renormalised."
    )
    change_today_pct: Optional[float] = Field(
        None, description="Price move since the previous UTC day's close."
    )


class CryptoPortfolioResponse(BaseModel):
    configured: bool
    prices_configured: bool = False
    base_currency: str
    as_of: Optional[str] = Field(None, description="When the holdings snapshot was taken (UTC, ISO 8601).")
    prices_as_of: Optional[str] = Field(None, description="When the newest price was fetched.")
    total_value: Optional[float] = Field(
        None, description="Σ count × CoinGecko price of the snapshot's coins; unknown if any coin has no price."
    )
    defi_value: Optional[float] = Field(
        None, description="DeFi positions, which CoinStats reports beside its total, not in it."
    )
    cash_value: Optional[float] = Field(
        None, description="Fiat balances held on exchanges, beside the total, not in it."
    )
    change_today: Optional[float] = Field(
        None, description="Yesterday's coins × the price move since yesterday's UTC close."
    )
    change_today_pct: Optional[float] = None
    pnl_since_start: Optional[float] = Field(
        None, description="Σ daily P&L after `start_date`: price moves only, never deposits or transfers."
    )
    start_date: str
    basket_date: Optional[str] = Field(
        None, description="The earliest holdings set: used for every day before it."
    )
    first_snapshot_date: Optional[str] = Field(
        None, description="The first day whose holdings came from a sync, not a reconstruction."
    )
    peg_note: Optional[str] = None
    fx_unavailable: int = 0
    valued_count: int = 0
    spam_count: int = 0
    unpriced_count: int = 0
    unpriced_symbols: List[str] = []
    no_price_symbols: List[str] = []
    holdings: List[CryptoHoldingItem] = []
    color_order: List[str] = Field(
        [], description="Valued coin ids by market-cap rank: the colour identity order."
    )
    warnings: List[str] = []


class CryptoHistoryPoint(BaseModel):
    date: str
    value: Optional[float] = None
    pnl: Optional[float] = Field(
        None, description="Yesterday's coins × the day's price move; None on the first day."
    )
    reconstructed: bool = Field(
        False, description="Before the first snapshot-sourced holdings day."
    )
    excluded: List[str] = Field(
        [], description="Coins without a price that day, left out of value and P&L."
    )


class CryptoHistoryResponse(BaseModel):
    configured: bool
    prices_configured: bool = False
    base_currency: str
    points: List[CryptoHistoryPoint] = []
    start_date: str
    basket_date: Optional[str] = None
    first_snapshot_date: Optional[str] = None
    fetched_at: Optional[str] = None
    peg_note: Optional[str] = None
    fx_unavailable: int = 0
    warnings: List[str] = []


class CryptoLastRun(BaseModel):
    status: str
    reason: Optional[str] = None
    message: Optional[str] = None
    finished_at: Optional[str] = None


class CryptoStatusResponse(BaseModel):
    configured: bool
    prices_configured: bool = False
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
