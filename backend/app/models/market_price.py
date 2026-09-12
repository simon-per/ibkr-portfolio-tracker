from sqlalchemy import String, Integer, ForeignKey, Numeric, Date, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime, date
from decimal import Decimal

from app.database import Base


#: `source` tag for a row derived from a sibling share class: the sibling's close on that
#: date scaled by (statement NAV ÷ sibling close) at the newest statement date. Tagged
#: apart from `yahoo_finance` because it is a *derived* figure, and from the finpension
#: tags because the importer deletes and re-derives these on every upload.
PRICE_ROW_SOURCE_SIBLING_SCALED = "sibling_scaled"


class MarketPrice(Base):
    """
    Stores historical daily closing prices for securities.
    Caches market data from Alpha Vantage to reduce API costs.
    Supports incremental fetching of missing date ranges.
    """
    __tablename__ = "market_prices"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    close_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)  # Daily closing price
    currency: Mapped[str] = mapped_column(String(3), nullable=False)  # Price currency
    # 'yahoo_finance' | 'alpha_vantage' | 'ibkr' | 'manual'. Defaulted to the
    # *fallback* provider until 2026-08-01, so a row inserted without an explicit
    # source claimed to come from the one place it almost certainly did not.
    # Python-side default only, so correcting it needs no migration.
    source: Mapped[str] = mapped_column(String(50), default="unknown", nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    # Relationships
    security: Mapped["Security"] = relationship(back_populates="market_prices")

    # Unique constraint to prevent duplicate prices for same security and date
    # and indexes for fast range queries
    __table_args__ = (
        UniqueConstraint('security_id', 'date', name='uix_security_date'),
        Index('ix_date_security', 'date', 'security_id'),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketPrice(security_id={self.security_id}, date={self.date}, "
            f"close_price={self.close_price} {self.currency})>"
        )
