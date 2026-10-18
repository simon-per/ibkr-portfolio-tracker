from sqlalchemy import Float, ForeignKey, DateTime, Index, UniqueConstraint, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime
from typing import Optional

from app.database import Base


class EarningsEvent(Base):
    """
    Stores earnings events (past and upcoming) for securities.
    Parsed from Yahoo Finance earnings_dates DataFrame.
    """
    __tablename__ = "earnings_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    earnings_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    eps_estimate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reported_eps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    surprise_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_upcoming: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    # Relationships
    security: Mapped["Security"] = relationship(back_populates="earnings_events")

    __table_args__ = (
        UniqueConstraint('security_id', 'earnings_date', name='uix_security_earnings_date'),
        # No ix_earnings_events_security_id here: `index=True` on security_id already
        # generates that exact name, and declaring it twice broke create_all().
        Index('ix_earnings_events_date', 'earnings_date'),
    )

    @property
    def beat_or_miss(self) -> Optional[str]:
        if self.surprise_percent is None:
            return None
        if self.surprise_percent > 1.0:
            return "Beat"
        elif self.surprise_percent < -1.0:
            return "Miss"
        else:
            return "Met"

    def __repr__(self) -> str:
        return (
            f"<EarningsEvent(security_id={self.security_id}, "
            f"date={self.earnings_date}, result={self.beat_or_miss})>"
        )
