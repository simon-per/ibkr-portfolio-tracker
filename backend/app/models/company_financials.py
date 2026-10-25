"""
Fundamentals history from SEC EDGAR, one company at a time (docs/company-financials.md).

Two tables. `company_financials` holds the normalised figures, one row per (security,
period, metric), in the company's reporting currency as filed. `security_ciks` maps a
security to its SEC Central Index Key and carries the EDGAR pass's stamps: a mapping
table rather than columns on `securities` because the answer "asked, and the SEC lists
no such ticker" needs a row of its own (the `IsinIdentity` / `SecurityLogo` pattern),
and because nothing else that reads `securities` has any use for EDGAR's bookkeeping.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SecurityCik(Base):
    """
    One row per security the EDGAR pass has asked about. A NULL `cik` beside
    `cik_checked_at` means *the SEC's ticker list has no such ticker* (a non-US listing,
    most funds); it is asked again after `company_financials_sync.CIK_RECHECK_AFTER`.
    Two listings of one company (GOOG, GOOGL) carry the same CIK and are fetched once.
    """

    __tablename__ = "security_ciks"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True
    )
    cik: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # The ticker it was looked up by, as the SEC spells it (BRK-B, not IBKR's "BRK B").
    sec_ticker: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    cik_checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # When companyfacts last answered with nothing this app reads (a 404, an IFRS 20-F
    # filer), and how many times running; `miss_backoff.miss_backoff_until` reads the pair.
    # Never stamped on a transport failure or a 429/5xx. A data answer clears both.
    facts_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    facts_miss_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # When this security's rows were last written from a companyfacts answer.
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<SecurityCik security_id={self.security_id} cik={self.cik}>"


class CompanyFinancial(Base):
    """
    One normalised figure: a discrete fiscal quarter (Q1-Q4) or a fiscal year (FY) of one
    metric, for one security. Q4 and FY share a `period_end`, hence `fiscal_period` in the
    key. `value` is in `unit` (a currency, `<currency>/shares`, or `shares`), split-adjusted
    to the newest filing's basis. A period a company never reported has no row.
    """

    __tablename__ = "company_financials"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False
    )
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    fiscal_period: Mapped[str] = mapped_column(String(2), nullable=False)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    source_concept: Mapped[str] = mapped_column(String(128), nullable=False)
    filed: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "security_id",
            "period_end",
            "fiscal_period",
            "metric",
            name="uq_company_financials_period_metric",
        ),
        Index("ix_company_financials_security_id", "security_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<CompanyFinancial {self.security_id} {self.fiscal_period} "
            f"{self.period_end} {self.metric}={self.value}>"
        )
