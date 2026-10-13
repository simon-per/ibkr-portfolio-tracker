from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint, false,
)
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

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

    **`reconstructed` rows are the exception, and say so.** The days before recording
    began (2026-10-07) can only be replayed: today's rules over what the database says
    was known that day (`app.cli.backfill_dividend_forecast`). That is not what the
    application showed then, so the flag travels with the row to the response, a
    replayed figure never replaces a recorded one, and a recorded one replaces it.

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
    reconstructed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    def __repr__(self) -> str:
        return (
            f"<DividendForecastSnapshot({self.snapshot_date}: "
            f"next_12m={self.next_12m_eur} ttm={self.ttm_net_eur})>"
        )


class DividendForecastSnapshotLine(Base):
    """
    One held security's part of a day's *Next 12 months* figure.

    **Why it exists.** Two totals a day cannot say why the forecast moved, and on this
    book the answer is nearly always "more shares were bought" — which is not the same
    news as a payer raising its dividend. With the share count, the security's part of
    the forecast and the exchange rate it was sized at, the change between two days
    splits into shares, payout per share and currency by arithmetic
    (`app.services.dividend_forecast_attribution`).

    **One line per security HELD that day, payer or not.** A holding that forecasts
    nothing is stored with a zero, because the split has to tell "held, and its first
    forecast appeared" from "bought".

    **The same source and the same rules as the parent row**: the lines are
    `get_dividend_breakdown`'s own per-security figures, in EUR, written in the same
    transaction. They sum to the parent's `next_12m_eur` to the cent on a day recorded
    with them. `reconstructed` is per line because a day recorded before this table
    existed can only have its lines replayed: the parent stays what was said that day,
    and whatever separates the two is served as `unattributed`, never spread around.

    `fx_to_eur` is the newest cached rate of the security's currency on the day — the
    one rate the forecast sized every payment of that security at — or NULL when none
    was cached.
    """
    __tablename__ = "dividend_forecast_snapshot_lines"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_date", "security_id", name="uix_dividend_forecast_snapshot_line"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    next_12m_eur: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    fx_to_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8), nullable=True)
    reconstructed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    def __repr__(self) -> str:
        return (
            f"<DividendForecastSnapshotLine({self.snapshot_date} #{self.security_id}: "
            f"{self.shares} sh, next_12m={self.next_12m_eur})>"
        )
