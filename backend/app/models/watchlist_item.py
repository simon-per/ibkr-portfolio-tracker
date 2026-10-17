from sqlalchemy import Integer, Float, String, Text, DateTime, BigInteger
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional

from app.database import Base


class WatchlistItem(Base):
    """
    Stores watchlist items with cached fundamentals and technical indicators.
    Not linked to Securities table — tracks arbitrary Yahoo Finance tickers.
    """
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    yahoo_ticker: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    symbol: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Cached price data
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)

    # Cached fundamentals
    trailing_pe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    revenue_growth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    earnings_growth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fwd_revenue_growth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fwd_eps_growth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_margins: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    market_cap: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Valuation metrics
    forward_pe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    peg_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ev_to_ebitda: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Analyst consensus
    analyst_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    analyst_rating: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    analyst_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Technical indicators
    week52_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    week52_low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_from_52w_high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ma200: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ma50: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_from_ma200: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rsi14: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Composite score
    buy_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Meta
    data_currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    last_synced: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # When quarterly_financials and the estimates were last fetched; the growth columns
    # above are refreshed weekly from them (`WatchlistService.ESTIMATES_REFRESH_AFTER`).
    estimates_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<WatchlistItem(ticker={self.yahoo_ticker}, price={self.current_price})>"
