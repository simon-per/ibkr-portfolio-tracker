from sqlalchemy import Integer, Float, String, ForeignKey, DateTime, BigInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional

from app.database import Base


class FundamentalMetrics(Base):
    """
    Stores fundamental growth metrics for securities from Yahoo Finance.
    One-to-one with Security. Cached and refreshed weekly.
    """
    __tablename__ = "fundamental_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True
    )

    # Valuation ratios
    trailing_pe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    forward_pe: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    peg_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_to_sales: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_to_book: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Growth rates
    revenue_growth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    earnings_growth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fwd_revenue_growth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fwd_eps_growth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Margins
    profit_margins: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gross_margins: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    operating_margins: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Size & analyst targets
    market_cap: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    number_of_analysts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    target_mean_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_high_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_low_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Classification
    quote_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # ETF, EQUITY
    data_currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    # Timestamps
    last_updated: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    # Relationships
    security: Mapped["Security"] = relationship(back_populates="fundamental_metrics")

    # security_id already declares `unique=True, index=True`, which generates
    # ix_fundamental_metrics_security_id as a unique index; re-declaring it here put the
    # same name in the metadata twice and broke Base.metadata.create_all(). Schema unchanged.

    @property
    def is_fund(self) -> bool:
        # `yahoo_estimates.FUND_QUOTE_TYPES`, not `== 'ETF'`: this was `is_etf`, a second
        # and narrower fund classifier that read a MUTUALFUND as a company.
        from app.services.yahoo_estimates import is_fund_quote_type
        return is_fund_quote_type(self.quote_type)

    def __repr__(self) -> str:
        return (
            f"<FundamentalMetrics(security_id={self.security_id}, "
            f"pe={self.trailing_pe}, peg={self.peg_ratio})>"
        )
