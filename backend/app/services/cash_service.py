"""
The uninvested cash balance — what a sale becomes before it is redeployed.

Until this existed the app valued an account at the market value of its *holdings*, so
selling 25,136 CHF of positions on 2026-08-21 and redeploying 12,682 of it three days
later read as a 36% collapse followed by a partial recovery. Nothing had been lost; the
money was simply somewhere the app could not see. The headline Market Value card
understated the account by the whole idle balance — about 18% of it on the day this was
written — and every weight, share and "% of portfolio" divided by that short total.

**Cash is derived from what actually moved, not from what is held.** Three ledgers,
each an IBKR record of a real event:

    cash = sum of trades (proceeds + commission)
         + sum of cash flows (deposits, withdrawals, cash legs of transfers)
         + sum of IBKR dividend payments, net of withholding

anchored at **zero before the first record**, which is definitionally true rather than
an assumption: the account did not exist.

Deriving it from *trades* rather than from tax-lot events is what makes that anchor
work, and it is the whole reason this is not spliced at `coverage_from` the way
`get_contributions` is. This account's holdings arrived by in-kind transfer from
Trading 212, Scalable Capital and Trade Republic, carrying their original open dates and
cost bases — so a lot-event derivation would read years of pre-IBKR purchases as cash
leaving an account that had not been opened, and drive the balance deeply negative. A
transferred lot has **no trade**, so it correctly consumes no cash, and the two ledgers
that do move cash (deposits, dividends) both start when the account does.

Verified against production on 2026-08-26: 17,187.63 deposited, 5,018.95 net deployed
through trades, 60.06 of dividends -> **12,228.74 CHF**. Independently, summing the
timeline's own `external_flow_eur` from `coverage_from` gives 12,212 — two derivations
from different columns agreeing to about a tenth of a percent.

## What it cannot see, and why the figure says so

Three things move an IBKR cash balance and appear in none of the three ledgers above:
**broker interest** paid on the balance, **account fees** (the sanitizer already drops a
`type="AF"` cash transaction it cannot model), and the **spread on FX conversions**.
None is large — on this account they are tens of francs against a five-figure balance —
but they accumulate in one direction and nothing here would ever notice.

So every balance carries a `source`, and `DERIVED` means *computed by us from activity*
rather than *read from the broker*. `MEASURED` is reserved for a real
`<EquitySummaryInBase>` row, which is IBKR's own end-of-day figure and includes all
three. The distinction is not decoration: a derived balance is the right thing to show
and the wrong thing to reconcile a statement against, and the surfaces badge it.

When measured rows exist they win **per day**, and derived fills only the days before
the first of them — the Flex query carries a bounded window, so measured history begins
whenever the section was enabled and can never reach back to the account's start.
"""

import logging
from datetime import date
from decimal import Decimal
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import IBKR
from app.repositories.cash_balance_repository import CashBalanceRepository
from app.repositories.cash_flow_repository import CashFlowRepository
from app.repositories.trade_repository import TradeRepository
from app.services.currency_service import CurrencyService
from app.services.native_amounts import NativeToBase

logger = logging.getLogger(__name__)

#: Computed from the trade/deposit/dividend ledgers. Excludes broker interest, account
#: fees and FX conversion spread — see the module docstring.
DERIVED = "derived"
#: Read from IBKR's own ``<EquitySummaryInBase>`` end-of-day balance.
MEASURED = "ibkr"
#: No ledger holds a single row, so there is nothing to derive from. Distinct from a
#: derived zero, which is a real answer: an account that has deployed everything it has.
UNKNOWN = "unknown"
#: Part of the balance is IBKR's own figure and part is derived — the shape the
#: moment a second account exists, because a pillar 3a balance can never be measured
#: by IBKR. `MEASURED` over it would stamp a provenance on money IBKR never saw,
#: which is the same overclaim as a badge that cannot clear. Three-way for the same
#: reason `dividend_source` is.
MIXED = "mixed"


class CashService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.currency_service = CurrencyService(db)

    async def balance_events(self, base_fx) -> List[Tuple[date, Decimal]]:
        """
        Every cash movement as ``(date, signed amount in base currency)``, sorted.

        Each event is projected at **its own date**, matching how the timeline converts
        a lot's cost at its `open_date` rather than re-converting the running total.
        The two lines are drawn on one chart, so they have to agree about which day's
        rate applies to a franc; converting the accumulated balance at the valuation
        date instead would make a stationary cash pile drift with EURCHF while the cost
        basis beside it sat still.

        Events are gathered **per account** and spliced against that account's own
        measured levels before being merged. Doing it in one pot is not a tidiness
        question: `_apply_measured` turns a level into ``level - derived_running``,
        so an IBKR level differenced against a total carrying pillar 3a money emits
        a correction that silently subtracts the 3a balance on every measured day,
        and puts it back on every day between. The line would sawtooth and each
        individual point would look plausible.
        """
        by_account: Dict[str, List[Tuple[date, Decimal]]] = defaultdict(list)
        to_base = NativeToBase(self.currency_service, base_fx)

        # 1. Trades. `proceeds` carries IBKR's sign — negative for a buy, positive for a
        #    sell — and `commission` is separately signed (negative), so the sum is the
        #    net cash effect. The same expression `ActivityService._trades` renders as
        #    `amount_base`, so the ledger and the balance cannot disagree about a trade.
        for t in await TradeRepository(self.db).get_all():
            amount = await to_base.convert(
                (t.proceeds or Decimal("0")) + (t.commission or Decimal("0")),
                t.currency, t.trade_date,
            )
            if amount is None:
                logger.warning(
                    "Cash: no FX for trade %s (%s near %s); the balance omits it",
                    t.ib_key, t.currency, t.trade_date,
                )
                continue
            by_account[t.account].append((t.trade_date, amount))

        # 2. External cash. **Every** flow type, not `get_deposits()`'s whitelist: that
        #    one answers "was this money added", where a transfer must never count. This
        #    asks "did cash move", where a transfer's cash leg genuinely did. The in-kind
        #    rows this account holds carry a zero amount and so contribute nothing either
        #    way, which is the correct answer rather than a lucky one.
        for f in await CashFlowRepository(self.db).get_all():
            by_account[f.account].append((
                f.flow_date, base_fx.convert(f.amount_eur or Decimal("0"), f.flow_date)
            ))

        # 3. Dividends actually paid into the account. IBKR rows only, and the rule for
        #    which those are lives in DividendService beside every other rule about
        #    these rows — an estimate is a guess about a payment made at a *previous*
        #    broker, so crediting it here invents cash the account never received.
        from app.services.dividend_service import DividendService
        for when, net_eur in await DividendService(self.db).ibkr_cash_receipts():
            by_account[IBKR].append((when, base_fx.convert(net_eur, when)))

        # The union of accounts with derived events and accounts with measured
        # levels, not just the former: a Flex query can carry the Cash Report
        # section while <Trades> and <CashTransactions> are still off, and an
        # account whose only evidence is IBKR's own figure must still get it.
        accounted = set(by_account) | {
            row.account
            for row in await CashBalanceRepository(self.db).get_all()
        }
        merged: List[Tuple[date, Decimal]] = []
        for account in sorted(accounted):
            events = sorted(by_account.get(account, []), key=lambda e: e[0])
            merged += await self._apply_measured(events, to_base, account)
        merged.sort(key=lambda e: e[0])
        return merged

    async def _apply_measured(self, events, to_base, account):
        """
        Snap the derived running balance to IBKR's own figure on every day it reports one.

        Measured rows arrive as **levels** while the timeline sweeps **deltas**, so each
        one becomes a correction event: the difference between what IBKR says the balance
        was and what the derived events add up to by then. Expressing it that way is what
        keeps the whole thing a single sorted list, so the sweep, the summary and the
        allocation denominator all read one series and cannot disagree about a day.

        Corrections are interleaved rather than replacing the derived era, which matters
        on the days between two measured rows — a weekend, or a stretch where the Flex
        sync failed. Carrying the last measured level flat across those would freeze the
        balance through real trades; keeping the derived movement and re-snapping at the
        next measured row gets both halves right.

        The first correction absorbs however far the derivation had drifted, so expect a
        small one-off step in the cash line on the day measurement begins. That step is
        the accumulated broker interest, fees and FX spread this service cannot see — it
        is the correction being visible, not a fault.
        """
        measured = await CashBalanceRepository(self.db).get_all(account=account)
        if not measured:
            return events

        corrections: List[Tuple[date, Decimal]] = []
        derived_running = Decimal("0")
        applied = Decimal("0")
        i = 0
        for row in measured:
            # An unlabelled level cannot be projected: `NativeToBase` reads a None
            # currency as EUR, which is right for the EUR-converted breakout path and
            # catastrophically wrong for a base-summary row in some other currency. The
            # ingest already refuses to store one, so this is the second gate rather
            # than the first — rows written before that refusal existed still reach here.
            if not row.currency:
                logger.warning(
                    "Cash: the measured balance on %s carries no currency; keeping the "
                    "derived figure for that day", row.report_date,
                )
                continue
            level = await to_base.convert(row.cash, row.currency, row.report_date)
            if level is None:
                logger.warning(
                    "Cash: no FX for the measured balance on %s (%s); keeping the "
                    "derived figure for that day",
                    row.report_date, row.currency,
                )
                continue
            # Every derived event up to and including this report date. `measured` is
            # ordered, so the walk never rewinds and each event is counted once.
            while i < len(events) and events[i][0] <= row.report_date:
                derived_running += events[i][1]
                i += 1
            correction = level - (derived_running + applied)
            applied += correction
            corrections.append((row.report_date, correction))

        return sorted(events + corrections, key=lambda e: e[0])

    async def cash_source(self) -> str:
        """
        Whether a derived balance means anything yet for this account.

        `UNKNOWN` when no ledger holds a single row — a fresh install, or a Flex query
        with none of the cash-bearing sections enabled. A derived **zero** over real
        rows is a different state and a real answer, so the two must not collapse: one
        means "fully deployed", the other means "we have no idea", and rendering the
        second as 0.00 is the reassuring-zero failure this codebase keeps rediscovering.

        `MIXED` once a second account exists whose balance IBKR cannot measure. It is
        not a hedge: `MEASURED` means *read from the broker*, and reporting a total
        that half of which the broker has never seen as `ibkr` is precisely the
        provenance overclaim `derived_source` was split out to avoid.
        """
        measured_accounts = {
            row.account
            for row in await CashBalanceRepository(self.db).get_all()
        }
        if not measured_accounts:
            return await self.derived_source()

        derived_only = await self._accounts_with_activity() - measured_accounts
        return MIXED if derived_only else MEASURED

    async def _accounts_with_activity(self) -> set:
        """Every account with a trade or a cash flow — anything a balance derives from."""
        accounts = {t.account for t in await TradeRepository(self.db).get_all()}
        accounts |= {f.account for f in await CashFlowRepository(self.db).get_all()}
        return accounts

    async def derived_source(self) -> str:
        """
        `DERIVED` or `UNKNOWN`, ignoring any measured rows.

        The fallback for days *before* measurement begins, which is why it exists
        separately: using `cash_source()` there would stamp `ibkr` on the whole history
        the moment one measured row landed at the end of it.
        """
        if await TradeRepository(self.db).count():
            return DERIVED
        if await CashFlowRepository(self.db).count():
            return DERIVED
        return UNKNOWN

    async def measured_from(self) -> Optional[date]:
        """
        First day IBKR's own balance is on record, or None.

        The per-point splice: a chart point on or after this is `MEASURED`, and one
        before it is `DERIVED`, however many measured rows exist. The Flex window is
        bounded, so measured history starts whenever the section was enabled in the
        portal and can never reach back to the account's start — reporting the whole
        series as measured because the tail is would be the same overclaim as a badge
        that cannot clear.
        """
        return await CashBalanceRepository(self.db).earliest_date()

    @staticmethod
    def balance_as_of(
        events: List[Tuple[date, Decimal]], on_date: Optional[date] = None
    ) -> Decimal:
        """The running balance at the end of `on_date`, from `balance_events`."""
        if on_date is None:
            return sum((a for _, a in events), Decimal("0"))
        return sum((a for d, a in events if d <= on_date), Decimal("0"))
