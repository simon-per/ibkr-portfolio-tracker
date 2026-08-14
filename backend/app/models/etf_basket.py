"""
The constituent basket of one fund, and the metadata describing how good it is.

Keyed by **fund ISIN**, not `security_id`: a basket is a property of the share class, so
the same fund held on two exchanges is two `securities` rows sharing one basket; the
Xtrackers endpoint is keyed by ISIN directly; and a basket can be imported before the
fund is held, the same way `manage_mappings set` lets a ticker mapping be pinned ahead of
the statement.

**Latest-only, replaced wholesale.** An import deletes a fund's holdings and inserts the
new ones in one transaction, so there is no row-by-row merge to get wrong. Nothing on any
screen asks what a basket used to be, and keeping history would put a per-date join in
every read.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, Date, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EtfBasket(Base):
    """
    One row per fund: what its stored basket is and how much of it we trust.

    **Split from the holdings deliberately.** Deriving coverage with a `SUM(weight_pct)`
    over the holdings cannot tell "98.04 because the file carries cash and futures rows"
    — which is EMIM's real, correct number — from "98.04 because 2% of the rows failed to
    parse". Those need different responses, so the counts are recorded at import time by
    the code that actually saw the file.

    And it must **not** be denormalised onto every holding row: two rows of one fund could
    then carry different `as_of_date`, "the fund's as-of" would silently become a `MAX()`,
    and one stale row would age an otherwise fresh basket.
    """
    __tablename__ = "etf_baskets"

    fund_isin: Mapped[str] = mapped_column(String(12), primary_key=True)

    # The issuer's own as-of wherever it publishes one — iShares stamps `13/Aug/2026`,
    # Vanguard a month-end — because a fetch date would report Vanguard's six-week-old
    # basket as fresh.
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    # ...and Xtrackers/DWS publishes NO date at all: not in the CSV, not in a
    # Last-Modified header (verified 2026-08-14). Rather than make the column nullable
    # and have every staleness check special-case it, the fetch date stands in and this
    # flag says so. Know which way that errs: the true as-of can only be *older*, so a
    # stood-in date makes the basket look FRESHER than it is — the wrong direction for a
    # staleness alarm. It is tolerable only because DWS regenerates continuously, so the
    # gap is about a day; it is surfaced on the API so a stale-looking figure can be
    # explained rather than trusted.
    as_of_is_issuer_stated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    adapter: Mapped[str] = mapped_column(String(30), nullable=False)

    # Rows the file offered, rows we stored, rows we could not parse. `skipped_rows > 0`
    # is what distinguishes a genuine cash allocation from a partial parse.
    source_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stored_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    total_weight_pct: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    equity_weight_pct: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    # Share of equity weight carrying an ISIN. Some issuers publish tickers or CUSIPs
    # only, and a row with no identifier can never be folded with a direct holding — so
    # this bounds how much of a fund's look-through can be trusted to consolidate.
    identifier_coverage_pct: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)

    # False for Xtrackers/DWS, whose export has no asset-class column at all. The equity
    # whitelist then cannot be applied, so the residual is 100 − Σ(all rows) and the API
    # says so rather than implying the filter ran.
    asset_class_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    fetched_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<EtfBasket {self.fund_isin} as_of={self.as_of_date} "
            f"rows={self.stored_rows} equity={self.equity_weight_pct}>"
        )


class EtfHolding(Base):
    """
    One constituent row of one fund's basket.

    **`UNIQUE(fund_isin, line_no)`, not `(fund_isin, constituent_isin)`.** The ISIN has to
    be nullable — some issuers publish tickers or CUSIPs only — and SQLite treats NULLs as
    distinct in a unique index, so an ISIN-keyed constraint would silently permit
    duplicate NULL rows while *looking* like it prevented them. Keying on the source
    file's own row index also makes summing a genuinely repeated ISIN an explicit choice
    in the aggregator rather than an accident of insertion order: a repeated constituent
    must be **summed**, and copying `import_prices.py`'s last-row-wins rule for repeated
    dates would silently drop weight.
    """
    __tablename__ = "etf_holdings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    fund_isin: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)

    constituent_isin: Mapped[Optional[str]] = mapped_column(
        String(12), nullable=True, index=True
    )
    constituent_ticker: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    constituent_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # A PERCENT, matching `etf_mappings.py`'s convention, so a reader who knows one table
    # knows the other. Xtrackers ships fractions summing to 1.0 and iShares percents
    # summing to ~100; normalising at the adapter boundary is mandatory, because a
    # fraction landing here makes the fund contribute 1/100th of its value and read as
    # 99% cash — a plausible number, which is the dangerous kind.
    weight_pct: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)

    # Whitelisted against the equity classes, never blacklisted: a blacklist admits the
    # next new value ('Rights', 'Warrant', 'Preferred') as a company.
    asset_class: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    # Stored and deliberately UNSERVED. Real constituent files carry these, and
    # re-fetching later would mean re-scraping — but serving them would put a second
    # sector answer on the Allocation tab beside `etf_mappings.py`'s, which is this
    # codebase's dominant failure mode. See that module's docstring for the exact
    # precondition for switching the charts over.
    sector: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("fund_isin", "line_no", name="uix_etf_holding_fund_line"),
    )

    def __repr__(self) -> str:
        return (
            f"<EtfHolding {self.fund_isin}#{self.line_no} "
            f"{self.constituent_isin or self.constituent_ticker} {self.weight_pct}%>"
        )
