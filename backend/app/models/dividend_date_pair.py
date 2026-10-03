from sqlalchemy import Date, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date, datetime

from app.database import Base


class DividendDatePair(Base):
    """
    IBKR's own ex-date and pay date for one dividend, from the Flex
    ``<ChangeInDividendAccruals>`` section.

    **Why it exists.** The only pay date this application otherwise learns comes from a
    `<CashTransaction>` (which `ibflex` 0.15 strips of its ex-date), and the only ex-date
    from yfinance — two rows that had to be paired by proximity before the distance
    between them could be measured. Proximity needs a window, and a window is a guess:
    SK Hynix paid 33 days after its ex-date and fell outside the 30 every currency used to
    get. IBKR posts an accrual on the ex-date and reverses it on the pay date, and both
    rows carry both dates, so here the pair is a fact, not an inference.

    **A log, unlike `dividend_accruals`.** That table is IBKR's currently-open set and is
    replaced wholesale; this one only ever grows. The section covers the Flex Query's
    period, so each sync adds the dividends booked inside it and the history accumulates
    beyond any one statement. Never income: nothing here carries an amount, and only
    `DividendService._measured_pay_lags` reads it.
    """
    __tablename__ = "dividend_date_pairs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    pay_date: Mapped[date] = mapped_column(Date, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            'security_id', 'ex_date', 'pay_date', name='uix_dividend_date_pair'
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DividendDatePair(security_id={self.security_id}, "
            f"ex_date={self.ex_date}, pay_date={self.pay_date})>"
        )
