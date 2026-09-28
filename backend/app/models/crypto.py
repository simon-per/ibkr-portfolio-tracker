"""
The crypto book, from CoinStats (docs/crypto.md).

**Separate tables by construction, not by filter.** Nothing here is a `Security`, a tax
lot, a trade or a cash flow, and nothing is an `account` label on those tables. Every
stock reader — valuation, XIRR, allocation, look-through, benchmark, tax, dividends,
contributions — selects from its own tables, so it cannot see a crypto row that was never
written there. That is the exclusion-by-not-writing that keeps pillar 3a fees out of the
dividend ledger, applied to a whole asset class, and it is what "clearly separate" means
in code. `tests/test_crypto_isolation.py` pins both directions.

**USD, as CoinStats keeps its books.** Per-coin cost, average buy price and P&L exist
only in USD, so the rows store what CoinStats reported and the read path projects them
into the base currency through the same `NativeToBase` every other reader uses.

**`Float`, not the `Numeric(18, 6)` the stock tables use.** SQLite stores a Numeric as a
float and SQLAlchemy reads it back rounded to the column's scale — six decimals would
turn a 1.2e-8 token price into 0, and a valued coin into one that looks unpriced. These
are display figures from a third party, not ledger amounts; a double's 15 significant
digits are what they need.
"""
from datetime import date, datetime
from typing import Any, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# `CryptoHolding.status`. Only `valued` holdings count toward the itemised total; the
# other two are excluded **and counted**, never valued at zero.
VALUED = "valued"
SPAM = "spam"          # CoinStats flags the coin `isFake` — typically an airdropped scam token
UNPRICED = "unpriced"  # CoinStats has no USD price for it


class CryptoSnapshot(Base):
    """
    The portfolio as CoinStats reported it at one successful sync.

    One row per sync, so the table doubles as an intraday value history. The totals are
    CoinStats' own (`/portfolio/value`); the holdings that belong to this snapshot are
    `CryptoHolding` rows keyed by `snapshot_id`, which is what lets a reader take the
    summary and the table from the same sync even when a newer one commits between its
    two SELECTs. Counts, credits and warnings live here rather than on the `sync_runs`
    row, because `/api/scheduler/history` is public and this table is only served by the
    admin-gated `/api/crypto/*` routes.
    """
    __tablename__ = "crypto_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # Naive UTC, from app.clock.utcnow(), like every stored timestamp here.
    taken_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    total_value_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    defi_value_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    all_time_pl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    all_time_pl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Σ of the valued holdings' 24-hour P&L, as CoinStats computes it per coin.
    pl_24h_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    valued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spam_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unpriced_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coin_pages: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # The plan's balance at the start of the run, and what the run spent by the
    # documented per-endpoint costs. Observable per sync, not only on CoinStats' dashboard.
    credits_remaining: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    credits_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    credits_plan: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    credits_spent: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    warnings: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    # What this run did about the daily history pull: `refreshed`, `failed`, `refused`
    # (the wipe guard), `skipped_credits`, or NULL when it was not due. It is what bounds
    # the pull to one *attempt* per Berlin day — a pull that fails at 08:00 is not
    # re-asked at every later slot, 35 credits a time, until it succeeds.
    history_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    holdings: Mapped[List["CryptoHolding"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", passive_deletes=True
    )


class CryptoHolding(Base):
    """
    One coin in one snapshot, in USD as CoinStats reported it.

    Only the newest snapshot's holdings are kept: each sync writes its own and deletes the
    previous snapshot's in the same transaction. The snapshot totals survive as history;
    per-coin history is not something the view draws, and keeping it would be keeping
    more of a private portfolio than anything reads.
    """
    __tablename__ = "crypto_holdings"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "coin_id", name="uix_crypto_holding_snapshot_coin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("crypto_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # CoinStats' coin identifier ("bitcoin", or a chain-and-contract key for a token).
    coin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # CoinStats' market-cap rank. The colour identity key: it belongs to the coin, not
    # to its position in this portfolio (docs/frontend.md, "colour by identity").
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_fiat: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    count: Mapped[float] = mapped_column(Float, nullable=False)
    price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_buy_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # CoinStats' own percentage, stored rather than recomputed from cost and P&L.
    unrealized_pl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pl_24h_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    change_24h_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    snapshot: Mapped[CryptoSnapshot] = relationship(back_populates="holdings")


class CryptoDailyPoint(Base):
    """
    One day of CoinStats' portfolio history: the value (`/portfolio/chart`) and the
    cash-flow-adjusted P&L (`/portfolio/pl/history`), in USD.

    Replaced wholesale on each daily history pull — CoinStats recomputes its history
    whenever a transaction syncs late, so a merge would keep numbers it has since
    corrected. The pull refuses an empty or sharply shorter series rather than replacing
    a good one with it.
    """
    __tablename__ = "crypto_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    value_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
