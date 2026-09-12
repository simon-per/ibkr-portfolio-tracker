from sqlalchemy import String, Integer, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime
from typing import List, Optional

from app.accounts import IBKR
from app.database import Base


# Where this security's prices come from, and therefore whether the Yahoo sync
# loop may touch it. `sync_securities` iterates every security unfiltered and the
# variation loop can auto-save a bare-symbol mapping, so a fund whose ticker
# collides with an unrelated US listing has no way to opt out — the SBI failure
# shape. `MANUAL` is that opt-out: prices arrive from a statement import instead.
PRICE_SOURCE_YAHOO = "yahoo"
PRICE_SOURCE_MANUAL = "manual"
#: Priced from a *sibling share class* Yahoo does quote: the newest statement NAV
#: anchors the level, the sibling's closes supply every day's move after it. For a
#: pension-only tranche that has no public quote of its own and whose provider prints
#: a NAV only on transaction rows. Not `yahoo` — the sibling's dividends, fundamentals
#: and ratings are not this security's — and not `manual`, which is carried and bounded.
PRICE_SOURCE_SIBLING = "sibling"


class Security(Base):
    """
    Represents a security (stock, ETF, etc.) from Interactive Brokers.
    Uses ISIN + exchange as composite unique identifier to handle same security
    traded on different exchanges (e.g., Amazon on NASDAQ vs XETRA).
    """
    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    isin: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)  # USD, EUR, etc.
    # Nullable since 2026-09-06: an IBKR identifier, and a Pillar 3a fund has none.
    # Still unique, which SQLite applies per non-NULL value, so many accountless
    # securities coexist. `SecurityRepository.upsert` keys on it and is therefore
    # the IBKR path only — a non-IBKR security upserts on (isin, exchange).
    conid: Mapped[Optional[int]] = mapped_column(nullable=True, unique=True, index=True)
    asset_category: Mapped[str] = mapped_column(String(20), nullable=True)  # STK, OPT, FUT, etc.
    exchange: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # See app/accounts.py. Defaults to IBKR so every pre-existing row is correct
    # and so code that has never heard of accounts keeps writing valid rows.
    account: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=IBKR, default=IBKR, index=True
    )
    price_source: Mapped[str] = mapped_column(
        String(16), nullable=False,
        server_default=PRICE_SOURCE_YAHOO, default=PRICE_SOURCE_YAHOO,
    )

    # Allocation data
    sector: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    asset_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default="Stock")  # Stock, ETF, etc.
    allocation_last_updated: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    # Relationships
    taxlots: Mapped[List["TaxLot"]] = relationship(
        back_populates="security",
        cascade="all, delete-orphan"
    )
    market_prices: Mapped[List["MarketPrice"]] = relationship(
        back_populates="security",
        cascade="all, delete-orphan"
    )
    analyst_rating: Mapped[Optional["AnalystRating"]] = relationship(
        back_populates="security",
        cascade="all, delete-orphan",
        uselist=False  # One-to-one relationship
    )
    fundamental_metrics: Mapped[Optional["FundamentalMetrics"]] = relationship(
        back_populates="security",
        cascade="all, delete-orphan",
        uselist=False  # One-to-one relationship
    )
    earnings_events: Mapped[List["EarningsEvent"]] = relationship(
        back_populates="security",
        cascade="all, delete-orphan"
    )
    dividend_payments: Mapped[List["DividendPayment"]] = relationship(
        back_populates="security",
        cascade="all, delete-orphan"
    )

    # Composite unique constraint for ISIN + exchange
    # and additional indexes for performance
    __table_args__ = (
        UniqueConstraint('isin', 'exchange', name='uix_isin_exchange'),
        Index('ix_symbol_currency', 'symbol', 'currency'),
    )

    def __repr__(self) -> str:
        return f"<Security(id={self.id}, symbol={self.symbol}, isin={self.isin}, exchange={self.exchange})>"
