from sqlalchemy import Integer, Numeric, String, Date, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import date, datetime
from typing import Optional
from decimal import Decimal

from app.database import Base


class DividendPayment(Base):
    """
    Caches yfinance dividend data (ex-date + per-share amount) and computed
    income based on shares held from tax lots. All amounts converted to EUR.
    """
    __tablename__ = "dividend_payments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_per_share: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    shares_held: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    gross_amount_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    # Real withholding tax and net (gross - withholding), populated from IBKR
    # cash transactions. Null for legacy yfinance-estimated rows.
    withholding_tax_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    net_amount_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    pay_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # 'ibkr' | 'yfinance_estimate'
    # What IBKR's cash line states about an `ibkr` row, parsed from its description
    # ("… CASH DIVIDEND USD 0.25 PER SHARE (Ordinary Dividend)"): the gross rate per
    # share in the payment's own currency, and the kind (ordinary / special / bonus /
    # return_of_capital / other). NULL on yfinance rows, on IBKR rows ingested before
    # 2026-10-04 that no statement has re-delivered since, and whenever the
    # description does not parse — the amounts above are real either way.
    #
    # Deliberately NOT `amount_per_share`: a non-NULL `amount_per_share` is what marks
    # a row as part of the Yahoo ex-date series the forecast reads its schedule from,
    # and an IBKR row is dated on its pay date.
    per_share_native: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    dividend_kind: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    last_computed: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    # Relationships
    security: Mapped["Security"] = relationship(back_populates="dividend_payments")

    __table_args__ = (
        # Source-aware on purpose. `ex_date` holds a real ex-date for yfinance rows
        # and the *pay* date for IBKR ones, so a shared (security_id, ex_date) key
        # let the two overwrite each other whenever a payer's ex-to-pay lag landed
        # on another record's date — relabelling real withholding as an estimate,
        # or nulling amount_per_share and dropping the security below the
        # two-sample threshold the forecast needs. Mastercard's 29-day lag already
        # exceeds a monthly payer's whole cycle, so that was reachable.
        UniqueConstraint(
            'security_id', 'source', 'ex_date',
            name='uix_dividend_security_source_exdate',
        ),
        # NOTE: security_id already has a column-level index (index=True above);
        # do not re-declare ix_dividend_payments_security_id here or create_all
        # emits a duplicate CREATE INDEX. The ex_date index has no column-level
        # counterpart, so it stays.
        Index('ix_dividend_payments_ex_date', 'ex_date'),
    )

    def __repr__(self) -> str:
        return (
            f"<DividendPayment(security_id={self.security_id}, "
            f"ex_date={self.ex_date}, amount={self.amount_per_share})>"
        )
