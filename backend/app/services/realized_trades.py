"""
Realized P&L from IBKR's own SELL executions -- the one loop that converts them.

Each SELL carries IBKR's FIFO realized figure (`fifoPnlRealized`) and its proceeds in
the trade's own currency, so both take the native->EUR->base two-step `NativeToBase`
owns. Three readers summed them, each with its own loop and its own idea of a failure
(GitHub issue #58): the `/summary` realized headline (`PortfolioService`), the tax
report's realized section (`TaxService`) and the closed positions
(`PerformanceAnalyticsService`). None of the three knew a cancellation, so a busted
sale was still counted. This module converts once and returns per-trade rows and
per-security sums; **each reader keeps its own aggregation policy** and documents it
where it applies it:

- the summary sums every converted row, counts the unconvertible ones in
  `unpriced_disposals`, and falls back to the closed lots when no sale stands;
- the tax report lists the year's converted rows and warns per unconvertible one;
- the closed positions take a security's figure only when *every* one of its rows
  converted, refusing it whole otherwise (issue #26).

**A cancellation is a row of the sale side, summed as stored.** A CANCELSELL carries
its sale's amounts negated (the convention and its assumption are stated beside
`sale_sign` in `app/models/trade.py`), so summing it with its sale nets the pair to
zero in every total, whichever window holds the two. `net_sales` counts the sales that
stand, for the readers that ask whether any sale is on record at all.

A row converts when both its proceeds and its realized figure do; a missing figure is
taken as zero, as every reader did before -- IBKR sends `fifoPnlRealized` on every
execution and the finpension importer computes it, so it is not NULL in practice.
`tests/test_trade_kind_family.py` fails a `.realized_pnl` read anywhere else in `app/`.
"""
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from app.models.trade import Trade, sale_sign
from app.services.native_amounts import NativeToBase

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


@dataclass
class ConvertedSale:
    """One sale-side row in the base currency. A cancellation's figures are negative."""

    trade: Trade
    proceeds: Decimal
    gain: Decimal
    # False when the row carried no realized figure at all, so a reader that shows the
    # row rather than summing it can leave the figure blank instead of printing 0.00.
    gain_reported: bool


@dataclass
class SecurityRealized:
    """One security's sale side, summed over its converted rows."""

    proceeds: Decimal = ZERO
    gain: Decimal = ZERO
    # Sales less cancellations, converted or not: > 0 means a sale stands.
    net_sales: int = 0
    converted: int = 0
    unconvertible: int = 0


@dataclass
class RealizedFromTrades:
    rows: List[ConvertedSale] = field(default_factory=list)
    unconvertible: List[Trade] = field(default_factory=list)
    # Keyed on `security_id`, which is None for a trade never linked to a security.
    by_security: Dict[Optional[int], SecurityRealized] = field(default_factory=dict)

    @property
    def net_sales(self) -> int:
        return sum(s.net_sales for s in self.by_security.values())

    @property
    def proceeds(self) -> Decimal:
        return sum((r.proceeds for r in self.rows), ZERO)

    @property
    def gain(self) -> Decimal:
        return sum((r.gain for r in self.rows), ZERO)


async def realized_from_sell_trades(
    trades: Iterable[Trade], to_base: NativeToBase
) -> RealizedFromTrades:
    """
    Every sale-side row of `trades` (a SELL or a CANCELSELL) in the base currency.

    Rows keep the input order; a row with no rate near its trade date is left out of
    the rows and the sums and listed in `unconvertible`, never valued on a guess.
    """
    result = RealizedFromTrades()
    for t in trades:
        sign = sale_sign(t)
        if not sign:
            continue
        sec = result.by_security.setdefault(t.security_id, SecurityRealized())
        sec.net_sales += sign
        proceeds = await to_base.convert(
            t.proceeds if t.proceeds is not None else ZERO, t.currency, t.trade_date
        )
        gain = await to_base.convert(
            t.realized_pnl if t.realized_pnl is not None else ZERO, t.currency, t.trade_date
        )
        if proceeds is None or gain is None:
            logger.warning(
                f"Realized: skipping trade {t.ib_key} -- no FX for {t.currency} near {t.trade_date}"
            )
            sec.unconvertible += 1
            result.unconvertible.append(t)
            continue
        sec.converted += 1
        sec.proceeds += proceeds
        sec.gain += gain
        result.rows.append(ConvertedSale(
            trade=t, proceeds=proceeds, gain=gain,
            gain_reported=t.realized_pnl is not None,
        ))
    return result
