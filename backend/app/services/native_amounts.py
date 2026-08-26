"""
One place that turns a **native-currency** amount into the base currency.

`trades` and `corporate_actions` store money in the trade's own currency — there is no
`_eur` column on either, unlike `cash_flows.amount_eur` and `dividend_payments.*_eur`,
which the ingest pipeline pre-converts. So those rows need two steps, native -> EUR at
the row's own date and then EUR -> base, and a single `BaseFx.convert()` is wrong twice
over: it skips the first conversion entirely and then applies the second to a number
that was never EUR. Caught against production data, where a CAD 30.27 realized gain was
reported as CHF 27.85 (the EUR->CHF factor) instead of roughly CHF 18.

It exists as a module because the two-step had grown **three** copies —
`ActivityService._to_base`, `PortfolioService._realized_from_trades`, and the cash
balance was about to be the fourth. Two of those already differed: one memoized the
rate and one issued a query per call. That is this codebase's dominant failure mode
(see CLAUDE.md), and the cheap moment to catch it is while writing the next copy.

`None` means no rate is available. Every caller reports the figure as missing rather
than substituting a zero or a mis-scaled number — the rule the tax report follows.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class NativeToBase:
    """
    A per-request converter bound to one `BaseFx` projection.

    Rates are memoized **per instance**, not per process: they are backfilled by the
    syncs, so a long-lived cache would keep serving a miss that has since been filled.
    Rows cluster heavily on a handful of (currency, date) pairs — this account's trades
    are USD on a few dozen dates — so the memo collapses what would be one query per
    row into one per pair, and on a cold cache one *provider* request per pair.
    """

    def __init__(self, currency_service, base_fx):
        self._currency_service = currency_service
        self._base_fx = base_fx
        self._rate_memo: Dict[Tuple[str, date], Optional[Decimal]] = {}

    async def rate(self, currency: str, on_date: date) -> Optional[Decimal]:
        """The native->EUR rate for one (currency, date), or None if unavailable."""
        key = (currency, on_date)
        if key in self._rate_memo:
            return self._rate_memo[key]

        rate: Optional[Decimal]
        try:
            rate = await self._currency_service.get_exchange_rate(currency, on_date)
        # The convert paths raise ValueError when neither provider covers the pair, but
        # a read path must survive anything: one unconvertible row must not 500 a page.
        except Exception as e:
            logger.warning(
                "No %s->EUR rate for %s (%s); leaving the amount blank",
                currency, on_date, e,
            )
            rate = None

        self._rate_memo[key] = rate
        return rate

    async def convert(
        self, amount: Optional[Decimal], currency: Optional[str], on_date: date
    ) -> Optional[Decimal]:
        """`amount` in `currency` on `on_date`, expressed in the base currency."""
        if amount is None:
            return None
        # Zero is zero in every currency, and demanding a rate would blank the
        # commission-free and cash-neutral rows — the same refusal
        # `get_contributions` makes for in-kind transfers, which are exactly the
        # rows with no cash and therefore no rate to find.
        if not amount:
            return Decimal("0")

        # Already in the display currency: a round trip through EUR costs a rate lookup
        # and buys only rounding error, because the stored native->EUR rate and the
        # EUR->base rate are not exact inverses. Measured on a real IBKR cash balance of
        # 12,501.58 CHF under a CHF base, which came back 12,502.03 — 0.45 out, on the
        # one figure in this app people reconcile against a broker statement line for
        # line. The EUR/EUR case was already short-circuited by the two clauses below;
        # this is the same exemption for every other base.
        if currency and currency == self._base_fx.base_currency:
            return amount

        eur = amount
        if currency and currency != "EUR":
            rate = await self.rate(currency, on_date)
            if rate is None:
                return None
            eur = amount * rate
        return self._base_fx.convert(eur, on_date)
