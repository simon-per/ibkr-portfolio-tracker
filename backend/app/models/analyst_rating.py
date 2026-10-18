from sqlalchemy import Integer, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime

from app.database import Base


class AnalystRating(Base):
    """
    Stores analyst recommendations for securities from Yahoo Finance.
    Cached and updated twice weekly to avoid excessive API calls.
    Shows aggregated ratings: strong buy, buy, hold, sell, strong sell counts.
    """
    __tablename__ = "analyst_ratings"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # One rating record per security
        index=True
    )
    strong_buy: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buy: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sell: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    strong_sell: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_updated: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    # Relationships
    security: Mapped["Security"] = relationship(back_populates="analyst_rating")

    # No __table_args__ index here: `index=True, unique=True` on security_id above already
    # generates ix_analyst_ratings_security_id as a unique index. Declaring it a second
    # time put two identically-named Index objects in the metadata, which made
    # Base.metadata.create_all() fail with "index ... already exists". Alembic owns the
    # real schema so production never noticed, but it broke any test that builds the full
    # schema. The on-disk index is unchanged.

    @property
    def total_ratings(self) -> int:
        """Total number of analyst ratings."""
        return self.strong_buy + self.buy + self.hold + self.sell + self.strong_sell

    @property
    def consensus(self) -> str:
        """
        Returns consensus rating based on weighted average.
        Scale: 1 (Strong Buy) to 5 (Strong Sell)
        """
        if self.total_ratings == 0:
            return "No Rating"

        # Weighted score calculation
        score = (
            self.strong_buy * 1 +
            self.buy * 2 +
            self.hold * 3 +
            self.sell * 4 +
            self.strong_sell * 5
        ) / self.total_ratings

        # Map score to consensus rating
        if score <= 1.5:
            return "Strong Buy"
        elif score <= 2.5:
            return "Buy"
        elif score <= 3.5:
            return "Hold"
        elif score <= 4.5:
            return "Sell"
        else:
            return "Strong Sell"

    def __repr__(self) -> str:
        return (
            f"<AnalystRating(security_id={self.security_id}, "
            f"consensus={self.consensus}, total={self.total_ratings})>"
        )
