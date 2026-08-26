from sqlalchemy import String, Numeric, Date
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from app.database import Base


class CashBalance(Base):
    """
    IBKR's own end-of-day account summary, one row per report date, from the Flex
    Query ``<EquitySummaryInBase>`` section.

    This is the *measured* answer to "how much cash is in the account", against
    `CashService`'s derived one. The derivation is close — it agreed with the ledger to
    about a tenth of a percent when it was written — but it is built from trades,
    deposits and dividends, so it structurally cannot see broker interest, account fees,
    or the spread on an FX conversion. Those accumulate in one direction, and on this
    account they had already put the balance about 250 CHF below zero while it was
    otherwise fully deployed. This table is how that gets corrected rather than
    explained away.

    **It requires a section that is off by default**, so the table is routinely empty and
    that must stay a supported state rather than a fault: `CashService` falls back to the
    derived balance per day, and `cash_source` on every surface says which one is being
    shown. Enabling it costs one tick in the Flex portal and adds one row per day of the
    query window — trivial against the sections that scan every trade.

    ``amounts are in the account's BASE currency``, which is IBKR's own base and need not
    be the app's `app_settings.base_currency`. `currency` records which, so a mismatch is
    detectable rather than silently applied — the SBI lesson, where a price carried a
    currency label it had not earned.

    Idempotent on ``report_date``: the query window re-delivers the same days on every
    sync, and a later statement's figure for a day supersedes an earlier one.
    """
    __tablename__ = "cash_balances"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    #: The currency IBKR reported in — its account base, not necessarily the app's.
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    cash: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    #: Securities value as IBKR marked it. Not used for valuation — the app prices
    #: holdings itself from `market_prices`, and two valuations of one portfolio is this
    #: codebase's dominant failure mode. Stored because it is the cheap reconciliation:
    #: `stock` far from our own market value means our prices are wrong.
    stock: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    #: IBKR's own net liquidation. Includes accruals we do not model, so it is a
    #: cross-check rather than a figure to serve.
    total: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<CashBalance({self.report_date}: cash={self.cash} "
            f"stock={self.stock} {self.currency})>"
        )
