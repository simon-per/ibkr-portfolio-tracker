from sqlalchemy import Integer, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from app.database import Base

#: The five recommendation counts, in Yahoo's order from most to least bullish.
RATING_COUNT_FIELDS = ('strong_buy', 'buy', 'hold', 'sell', 'strong_sell')


def consensus_of(
    strong_buy: int, buy: int, hold: int, sell: int, strong_sell: int
) -> str:
    """
    The consensus label of five recommendation counts, by their weighted average on a
    1 (Strong Buy) to 5 (Strong Sell) scale; "No Rating" when there are none. The one rule:
    a held security's rating (`AnalystRating.consensus`) and the Company tab overview's
    (`company_financials_service`) both read it.
    """
    total = strong_buy + buy + hold + sell + strong_sell
    if total == 0:
        return "No Rating"
    score = (strong_buy * 1 + buy * 2 + hold * 3 + sell * 4 + strong_sell * 5) / total
    if score <= 1.5:
        return "Strong Buy"
    if score <= 2.5:
        return "Buy"
    if score <= 3.5:
        return "Hold"
    if score <= 4.5:
        return "Sell"
    return "Strong Sell"


def rating_of(counts: Optional[Mapping[str, Optional[int]]]) -> Optional[Dict[str, Any]]:
    """
    The rating a company shows: its five counts and their consensus (`consensus_of`), or
    None when there is none to show — no counts known, or Yahoo's answer "no analysts cover
    this" (all zero, `analyst_rating_service.no_rating`). The one shape the positions
    table, the watchlist and the Company overview serve, so a company's rating reads the
    same wherever it appears.
    """
    if counts is None:
        return None
    values = {f: int(counts.get(f) or 0) for f in RATING_COUNT_FIELDS}
    if not any(values.values()):
        return None
    return {**values, 'consensus': consensus_of(**values)}


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
        """The consensus label of these counts (`consensus_of`)."""
        return consensus_of(
            self.strong_buy, self.buy, self.hold, self.sell, self.strong_sell
        )

    def __repr__(self) -> str:
        return (
            f"<AnalystRating(security_id={self.security_id}, "
            f"consensus={self.consensus}, total={self.total_ratings})>"
        )
