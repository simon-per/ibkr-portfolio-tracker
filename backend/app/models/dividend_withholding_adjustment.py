from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from app.database import Base


class DividendWithholdingAdjustment(Base):
    """
    A withholding line IBKR posted on a day with no dividend of the same security —
    a refund or correction of tax withheld from an EARLIER dividend (a 30% withholding
    corrected to the 15% treaty rate, a fund's year-end adjustment).

    **Why it exists.** `sync_dividends_from_cash_transactions` groups cash lines by
    (security, settle date), so such a line forms a group with no gross and used to be
    skipped without a word: the dividend it corrects kept the over-withheld amount, and
    the tax report's DA-1 figure with it (GitHub issue #60). It is now folded into that
    security's nearest preceding IBKR dividend in the same tax year — and this table is
    what makes the fold idempotent. The Flex window is ~30 days, so a refund and its
    dividend may arrive in one statement, in several overlapping ones, or in different
    ones; the dividend row's own-day lines are recomputed on every statement that
    carries them, while an adjustment applied from the database alone must never be
    applied twice. So each line is recorded once, keyed on IBKR's ``transactionID``
    (`ib_key`), and the dividend row's withholding is its own-day withholding plus the
    adjustments attached to it here.

    ``target_pay_date`` names the dividend row (`dividend_payments`, source ``ibkr``,
    ``ex_date`` = that pay date) it was folded into; NULL while no dividend qualifies,
    which the ingest counts whenever a statement delivers the line, names in
    `warnings[]` the first time, and retries on every ingest. An attachment, once made,
    is not revisited.

    ``withholding_eur`` is the change to that row's withholding, in EUR at the line's
    own date: positive when more was withheld, negative for a refund. Never income, and
    read by nothing but the ingest.

    ``line_type`` is IBKR's cash type. ``WHTAX`` is an adjustment as above. A dividend
    line (``DIVIDEND`` / ``PAYMENTINLIEU``) is a reversal posted alone on its day, kept
    only so its warning is given once: never folded, ``withholding_eur`` 0. That is the
    rule for every refused line — counted on each ingest that delivers it, warned only on
    the one that first records it, because the ~30-day window re-delivers it daily.
    """
    __tablename__ = "dividend_withholding_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    ib_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    posted_date: Mapped[date] = mapped_column(Date, nullable=False)
    line_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="WHTAX", server_default="WHTAX"
    )
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    # As IBKR reports it, in the line's own currency: negative withholds, positive refunds.
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    withholding_eur: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    target_pay_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index('ix_dividend_wht_adjustment_target', 'security_id', 'target_pay_date'),
    )

    def __repr__(self) -> str:
        return (
            f"<DividendWithholdingAdjustment(ib_key={self.ib_key}, "
            f"security_id={self.security_id}, posted={self.posted_date}, "
            f"wht_eur={self.withholding_eur}, target={self.target_pay_date})>"
        )
