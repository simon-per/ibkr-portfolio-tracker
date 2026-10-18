from sqlalchemy import String, ForeignKey, Boolean, Numeric, Date, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from app.database import Base


class TaxLot(Base):
    """
    Represents a tax lot (purchase) of a security.
    Tracks the original purchase date, quantity, and cost basis.
    Cost basis is stored in both original currency and EUR for performance.
    """
    __tablename__ = "taxlots"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    open_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)  # Total cost in original currency
    price_per_unit: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)  # Original purchase price
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    cost_basis_eur: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)  # Converted to EUR
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # Track if position is still open
    close_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # How a closed lot was classified during reconciliation: 'trade' (a real SELL
    # explained the drop), 'corporate_action' (split/spinoff/merger), or
    # 'heuristic' (quantity/cost-conservation inference, pre-Trades fallback).
    close_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    # Relationships
    security: Mapped["Security"] = relationship(back_populates="taxlots")

    # Indexes for performance
    __table_args__ = (
        Index('ix_open_date_security', 'open_date', 'security_id'),
        Index('ix_is_open', 'is_open'),
        # Reconciliation's hot lookup: this security's open lots.
        Index('ix_taxlots_security_open', 'security_id', 'is_open'),
        # realized_rows_from_closed_lots() ranges on close_date for five endpoints
        # plus the tax report. close_date leads so it is a range scan; is_open
        # trails so the `is_open = false` equality comes from the same index.
        Index('ix_taxlots_close_date', 'close_date', 'is_open'),
    )

    def __repr__(self) -> str:
        return (
            f"<TaxLot(id={self.id}, security_id={self.security_id}, "
            f"open_date={self.open_date}, quantity={self.quantity}, "
            f"cost_basis_eur={self.cost_basis_eur})>"
        )
