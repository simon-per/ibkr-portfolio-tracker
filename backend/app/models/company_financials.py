"""
Fundamentals history from SEC EDGAR, one company at a time (docs/company-financials.md).

Four tables, and two for its share price. `sec_companies` is one SEC filer the pass fetches — a held company, or a
Nasdaq-100 constituent the account does not hold — keyed by its Central Index Key, with
the pass's stamps. `company_financials` holds its normalised figures, one row per (company,
period, metric), in the reporting currency as filed. `security_ciks` maps a held security
to its CIK: a mapping table rather than a column on `securities` because the answer "asked,
and the SEC lists no such ticker" needs a row of its own (the `IsinIdentity` /
`SecurityLogo` pattern); `sec_tickers` does the same for a constituent's ticker.
`company_prices` and `company_splits` are Yahoo's weekly closes and split events for each
company, for its P/E (`company_price_sync`).

**Keyed by company, not by security** (2026-10-25): a constituent nobody holds has no
`securities` row, and must not get one — every Yahoo loop reads that table. Two listings
of one company (GOOG, GOOGL) share one set of rows.
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


class SecCompany(Base):
    """
    One SEC filer the EDGAR pass fetches, and its stamps. `universe_seen_at` is the pass
    that last found it among the Nasdaq-100 constituents (`company_financials_sync.
    UNIVERSE_FUNDS`): the Company tab lists a company not held only while it carries the
    newest stamp, so a company that left the index drops out of the picker.
    """

    __tablename__ = "sec_companies"

    cik: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # The SEC's spelling (BRK-B), from the ticker list or the held security's lookup.
    ticker: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # The basket's name for a constituent, else companyfacts' `entityName`.
    name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    # A constituent's ISIN from the basket, for its logo (`/api/logos/isin/...`).
    isin: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    universe_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    # When companyfacts last answered with nothing this app reads (a 404, an IFRS 20-F
    # filer), and how many times running; `miss_backoff.miss_backoff_until` reads the pair.
    # Never stamped on a transport failure or a 429/5xx. A data answer clears both.
    facts_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    facts_miss_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # When this company's rows were last written from a companyfacts answer.
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # The price pass's stamps (`company_price_sync`), the same three as the facts': when
    # Yahoo last answered with closes, and when and how often it answered with none.
    prices_fetched_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    prices_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    prices_miss_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<SecCompany cik={self.cik} {self.ticker}>"


class SecTicker(Base):
    """
    A Nasdaq-100 constituent's ticker and the SEC's answer for it, so the ticker list is
    asked once, not every pass: GOOG's CIK is stored nowhere else (Alphabet's company row
    carries GOOGL). A NULL `cik` means the SEC lists no such ticker; asked again after
    `company_financials_sync.CIK_RECHECK_AFTER`.
    """

    __tablename__ = "sec_tickers"

    ticker: Mapped[str] = mapped_column(String(20), primary_key=True)
    cik: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    def __repr__(self) -> str:
        return f"<SecTicker {self.ticker} cik={self.cik}>"


class SecurityCik(Base):
    """
    One row per held security the EDGAR pass has asked about. A NULL `cik` beside
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

    def __repr__(self) -> str:
        return f"<SecurityCik security_id={self.security_id} cik={self.cik}>"


class CompanyFinancial(Base):
    """
    One normalised figure: a discrete fiscal quarter (Q1-Q4) or a fiscal year (FY) of one
    metric, for one company. Q4 and FY share a `period_end`, hence `fiscal_period` in the
    key. `value` is in `unit` (a currency, `<currency>/shares`, or `shares`), split-adjusted
    to the newest filing's basis. A period a company never reported has no row.
    """

    __tablename__ = "company_financials"

    id: Mapped[int] = mapped_column(primary_key=True)
    cik: Mapped[int] = mapped_column(
        ForeignKey("sec_companies.cik", ondelete="CASCADE"), nullable=False
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
            "cik",
            "period_end",
            "fiscal_period",
            "metric",
            name="uq_company_financials_period_metric",
        ),
        Index("ix_company_financials_cik", "cik"),
    )

    def __repr__(self) -> str:
        return (
            f"<CompanyFinancial {self.cik} {self.fiscal_period} "
            f"{self.period_end} {self.metric}={self.value}>"
        )


class CompanyPrice(Base):
    """
    One week's last close of one company, as Yahoo split-adjusted it on the day it was
    fetched (`company_price_sync` keeps the whole history on one basis), in `currency`.
    """

    __tablename__ = "company_prices"

    cik: Mapped[int] = mapped_column(
        ForeignKey("sec_companies.cik", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    def __repr__(self) -> str:
        return f"<CompanyPrice {self.cik} {self.date} {self.close} {self.currency}>"


class CompanySplit(Base):
    """One split Yahoo reports for a company: `ratio` new shares per old (10.0 for 10:1)."""

    __tablename__ = "company_splits"

    cik: Mapped[int] = mapped_column(
        ForeignKey("sec_companies.cik", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    ratio: Mapped[float] = mapped_column(Float, nullable=False)

    def __repr__(self) -> str:
        return f"<CompanySplit {self.cik} {self.date} {self.ratio}>"
