from sqlalchemy import Date, DateTime, Numeric
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date, datetime
from decimal import Decimal

from app.database import Base


class DividendForecastSnapshot(Base):
    """
    What the dividend forecast said on one day: the *Next 12 months* figure and the
    trailing twelve months beside it.

    **Why it exists.** Both are recomputed on every read, so nothing kept their past
    values and "is the forward income growing?" had no answer — the figure moves daily
    with FX and with estimates being replaced by announced dividends, and a number that
    only ever shows its latest value gives no feeling for its trend.

    **A log of what was computed, never an input.** Nothing reads this table except
    `DividendService.get_forecast_history`. A change to the forecast's rules therefore
    shows as a step in the series rather than being restated — the series records what
    the application said, which is the only thing it can know about a past day.

    **EUR, like every other amount**, taken from `get_dividend_breakdown` run with an
    identity `BaseFx`, and projected into the base currency on read at the snapshot
    date's rate. One row per UTC date; a later write the same day replaces the earlier.
    """
    __tablename__ = "dividend_forecast_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    next_12m_eur: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    ttm_net_eur: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<DividendForecastSnapshot({self.snapshot_date}: "
            f"next_12m={self.next_12m_eur} ttm={self.ttm_net_eur})>"
        )
