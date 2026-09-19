from sqlalchemy import Integer, Numeric, String, Date, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from app.database import Base


class DividendAccrual(Base):
    """
    A dividend IBKR has announced and not yet paid, from the Flex Query
    ``<OpenDividendAccruals>`` section.

    **The only place both dates exist on one record.** Yahoo publishes an ex-date and no
    pay date; Flex's ``<CashTransaction>`` publishes a settle date and no ex-date — so
    `dividend_payments` stores an ex-date for yfinance rows and a pay date for IBKR ones,
    and nothing could say how far apart they were for a payment that had not settled yet.
    This section carries ``exDate``, ``payDate`` and the net amount together, which is
    exactly the window in which a dividend used to vanish: its projection expires on the
    ex-date, and `_splice_by_era` drops the estimate recording it, so for up to a month
    the payment was in no figure at all.

    **Deliberately NOT a third `source` value on `dividend_payments`.** That table is the
    income ledger, and everything reading it through `_splice_by_era` — the tax report's
    DA-1 income, XIRR, the cash balance, the Steuerwert — would then count money the
    account has not been paid. A separate table cannot be read by accident; only the
    dividend forecast's calendar consults it.

    **Replaced wholesale on every sync**, because IBKR publishes the currently-OPEN set
    rather than a log: a paid accrual simply stops appearing, so upserting would leave it
    behind for ever. Same shape as a positions snapshot, and the reason
    `DividendAccrualRepository.replace_all` refuses a partial write.

    **The section is off by default and an empty table is a supported state.** The ingest
    runs unconditionally, exactly as the Cash Report's does, so ticking the section in the
    Flex portal is the whole setup — no flag, no redeploy. Until then the forecast falls
    back to a pay date measured from history, and says which it used.

    Amounts are EUR, converted at the newest cached rate rather than at the pay date —
    that date is in the future and has no rate, and `_latest_fx_to_eur` is the same
    cache-only helper the forecast already sizes projections with.
    """
    __tablename__ = "dividend_accruals"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: IBKR's announced ex-date. Nullable because the accrual is keyed on the pay date:
    #: an accrual with no ex-date is still a dividend that is going to be paid.
    ex_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    pay_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    gross_amount_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    #: Positive, like `dividend_payments.withholding_tax_eur` — IBKR reports the accrued
    #: tax as a negative and the ingest flips it once, on the way in.
    withholding_tax_eur: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    net_amount_eur: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    #: When this row last appeared in a statement. The wholesale replace makes it equal
    #: to the last sync, so it answers "is this table being maintained at all" — which is
    #: the question an empty table cannot distinguish from a disabled section.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # One open accrual per security per pay date. A security paying twice on one day
        # is not a shape IBKR produces, and merging them would be the safer error anyway.
        UniqueConstraint('security_id', 'pay_date', name='uix_dividend_accrual_security_pay'),
    )

    def __repr__(self) -> str:
        return (
            f"<DividendAccrual(security_id={self.security_id}, "
            f"ex_date={self.ex_date}, pay_date={self.pay_date}, "
            f"net={self.net_amount_eur})>"
        )
