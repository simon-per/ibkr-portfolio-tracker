from sqlalchemy import String, Integer, ForeignKey, Numeric, Date, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime, date
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from app.accounts import IBKR
from app.database import Base


class Trade(Base):
    """
    A single executed IBKR trade (BUY/SELL), parsed from the Flex Query <Trades>
    section. This is the authoritative source of realized P&L and true sale
    dates/proceeds, replacing the market-price approximation used when only
    <OpenPositions> is available.

    Idempotent on ``ib_key`` (IBKR transactionID, falling back to tradeID). The
    ``conid`` is the durable link to a security (``security_id`` is resolved when
    the security exists locally, but a fully-sold security may be absent).
    """
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    ib_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # The source ledger's durable instrument identifier: IBKR's contract id for an
    # IBKR row, and the **ISIN** for a ledger that issues no such id (Pillar 3a).
    # Deliberately still NOT NULL -- every IBKR row honours it and weakening the
    # contract for a case that has a perfectly good stable identifier is a bad trade.
    # Safe because it is only ever *matched*, never parsed: `get_by_conid_in_range`
    # is simply never satisfied by a `CH...` string, and `persist_transactions`
    # filters unresolved conids through `.isdigit()` before `int()`.
    conid: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    security_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("securities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    symbol: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    buy_sell: Mapped[str] = mapped_column(String(16), nullable=False)  # BUY / SELL / CANCELBUY / CANCELSELL
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)  # signed: buys +, sells -
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)  # tradePrice
    proceeds: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)
    commission: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)  # ibCommission
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 6), nullable=True)  # fifoPnlRealized
    asset_category: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # See app/accounts.py. Read by the tax report, which must filter realized gains
    # on the *trade* rather than on its security: the join there is an outer one
    # because `security_id` is nullable, so a Security-side filter drops unlinked rows.
    account: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=IBKR, default=IBKR, index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_trades_conid_date", "conid", "trade_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<Trade(ib_key={self.ib_key}, conid={self.conid}, "
            f"{self.buy_sell} {self.quantity} @ {self.price} on {self.trade_date})>"
        )


# --- What a trade row is -----------------------------------------------------------
#
# `buy_sell` holds ibflex's `BuySell` member *names* (`ibkr_service._enum_name`): BUY,
# SELL, and the two rows IBKR sends when it busts an execution, CANCELBUY ("BUY (Ca.)")
# and CANCELSELL ("SELL (Ca.)"); the finpension importer writes BUY/SELL. Every reader
# asks the functions below instead of comparing the string (GitHub issue #58): the test
# was written inline seven times on the server and once more, differently, on the client,
# and no copy knew a cancellation existed, so a busted sale's P&L stayed in the realized
# headline, the tax report and the closed positions, and reconcile closed lots with it.
# `tests/test_trade_kind_family.py` fails a `buy_sell` comparison anywhere else in `app/`.
#
# **The sign convention, and the assumption it rests on.** A cancellation row carries
# the amounts of the execution it reverses *negated* -- a CANCELSELL has a positive
# quantity, negative proceeds and the sale's `fifoPnlRealized` with the sign flipped --
# so the original and its cancellation sum to zero. That is IBKR's statement convention;
# ibflex only parses the attributes and says nothing about their signs, so it is an
# assumption, and the one the derived cash balance already made (`CashService` sums
# `proceeds + commission` over every trade as stored). So a reader that *sums money*
# takes both rows as stored and the pair nets out on its own, and a reader that *counts
# sales or needs their dates* uses `sale_sign` or `uncancelled_sales`.

BUY = "BUY"
SELL = "SELL"
CANCEL_BUY = "CANCELBUY"
CANCEL_SELL = "CANCELSELL"

# The enum names are what is stored; the enum values are accepted too, so a row written
# from the raw attribute rather than the parsed member still reads as what it is.
_KIND_OF: Dict[str, str] = {
    BUY: BUY, SELL: SELL, CANCEL_BUY: CANCEL_BUY, CANCEL_SELL: CANCEL_SELL,
    "BUY (CA.)": CANCEL_BUY, "SELL (CA.)": CANCEL_SELL,
}


def trade_kind(trade) -> Optional[str]:
    """BUY, SELL, CANCELBUY or CANCELSELL; None for anything else (the parser's 'UNKNOWN')."""
    return _KIND_OF.get((trade.buy_sell or "").strip().upper())


def is_cancellation(trade) -> bool:
    """A row that reverses an earlier execution rather than being one."""
    return trade_kind(trade) in (CANCEL_BUY, CANCEL_SELL)


def sale_sign(trade) -> int:
    """
    +1 for a sale, -1 for the cancellation of one, 0 for anything else.

    Non-zero is the *sale side* of the book: the rows whose proceeds and realized P&L
    belong to the realized figures. Summed as stored they net a busted sale to zero
    (the convention above); summed as signs they count the sales that stand.
    """
    kind = trade_kind(trade)
    if kind == SELL:
        return 1
    if kind == CANCEL_SELL:
        return -1
    return 0


def buy_sign(trade) -> int:
    """
    +1 for a purchase, -1 for the cancellation of one, 0 for anything else -- the mirror
    of `sale_sign`. Summed with the proceeds as stored (a BUY's are negative, its
    CANCELBUY's the same amount positive), a busted purchase nets to zero.
    """
    kind = trade_kind(trade)
    if kind == BUY:
        return 1
    if kind == CANCEL_BUY:
        return -1
    return 0


def is_sale_side(trade) -> bool:
    """A sale or the cancellation of one -- the rows the realized sums read."""
    return sale_sign(trade) != 0


def uncancelled_sales(trades: Iterable) -> List:
    """
    The SELL rows that still stand once each CANCELSELL has struck out the sale it reverses.

    For the readers that need a sale's *date* or *quantity* rather than its money --
    reconcile's close date, the restamp's quantity match -- where netting by summation
    is not available. A cancellation pairs with a SELL of the same contract and the same
    absolute quantity, preferring the same trade date, then the same price, then the
    nearest date. One whose sale is not among `trades` (it fell outside the caller's
    window) strikes out nothing: that sale is not in the answer anyway.
    """
    rows = list(trades)
    remaining = [t for t in rows if trade_kind(t) == SELL]
    for cancel in sorted(
        (t for t in rows if trade_kind(t) == CANCEL_SELL), key=lambda t: t.trade_date
    ):
        size = abs(cancel.quantity) if cancel.quantity is not None else None
        candidates = [
            t for t in remaining
            if t.conid == cancel.conid
            and t.quantity is not None and size is not None and abs(t.quantity) == size
        ]
        if not candidates:
            continue
        match = min(candidates, key=lambda t: (
            t.trade_date != cancel.trade_date,
            t.price != cancel.price,
            abs((t.trade_date - cancel.trade_date).days),
        ))
        remaining.remove(match)
    return remaining
