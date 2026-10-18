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
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts import IBKR
from app.models.cash_balance import CashBalance
from app.models.cash_flow import CashFlow
from app.models.trade import Trade
from app.repositories.cash_balance_repository import CashBalanceRepository
from app.repositories.cash_flow_repository import CashFlowLedger, CashFlowRepository
from app.repositories.trade_repository import TradeRepository
from app.services.currency_service import CurrencyService
from app.services.native_amounts import NativeToBase, cash_flow_in_base

logger = logging.getLogger(__name__)

#: Computed from the trade/deposit/dividend ledgers. Excludes broker interest, account
#: fees and FX conversion spread — see the module docstring.
DERIVED = "derived"
#: Read from IBKR's own ``<EquitySummaryInBase>`` end-of-day balance.
MEASURED = "ibkr"
#: No ledger holds a single row, so there is nothing to derive from — or a trade could
#: not be converted, so the sum is not the balance (`unconverted_trades`). Distinct from
#: a derived zero, which is a real answer: an account that has deployed everything it has.
UNKNOWN = "unknown"
#: Part of the balance is IBKR's own figure and part is derived — the shape the
#: moment a second account exists, because a pillar 3a balance can never be measured
#: by IBKR. `MEASURED` over it would stamp a provenance on money IBKR never saw,
#: which is the same overclaim as a badge that cannot clear. Three-way for the same
#: reason `dividend_source` is.
MIXED = "mixed"


@dataclass(frozen=True)
class UninvestedCash:
    """
    Today's uninvested balance, as every "% of portfolio" denominator needs it.

    `balance` is the signed figure in the base currency, or None when `source` is
    `UNKNOWN` — no ledger holds a row, so the derivation's 0.00 means "no idea", not
    "fully deployed" (see `cash_source`); or `unconverted` trades had no FX rate, so the
    sum is short by each of them whole (`unconverted_trades`, GitHub #59).

    `in_book` is the part that joins holdings in a share-of-the-book denominator: a
    **positive** known balance, else zero. A negative one (a margin debit) is left out
    rather than subtracted, because a chart cannot draw a negative slice and shrinking the
    denominator around one would inflate every holding; an unknown one is left out because
    adding a stand-in would assert a completeness nothing established. Both readers of it
    (`allocation_service`, `lookthrough_service`) then keep their holdings-only base, which
    is what makes their percentages agree in every state, not only the happy one.

    Positions' weights apply the same rule client-side — `inBookCash` in
    `frontend/src/lib/portfolioCash.ts`, since the summary ships only the signed balance —
    and its test pins the same cases as `test_uninvested_cash_family.py` (GitHub #52).
    Change one side, change both.
    """
    source: str
    balance: Optional[Decimal]
    unconverted: int = 0

    @property
    def in_book(self) -> Decimal:
        if self.balance is None or self.balance <= 0:
            return Decimal("0")
        return self.balance


class CashService:
    """
    The three ledgers and the measured levels are each read **once per instance**: one
    summary request used to read `trades` three times, `cash_balances` three times and
    `cash_flows` twice (GitHub #47), and one decomposition `cash_flows` eleven times and
    the other three ledgers three times each (#73). Every question about them — the
    money-in splice's flow dates (`flow_ledger`), the first measured day, the dividend
    receipts, the derived events themselves — is answered off those rows, so a caller
    that shares one instance across a request reads each table once. The memo lives on
    the instance, never the module — an instance serves one request, so a sync landing
    between two requests is seen by the next one.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.currency_service = CurrencyService(db)
        self._trades: Optional[List[Trade]] = None
        self._flows: Optional[List[CashFlow]] = None
        self._balances: Optional[List[CashBalance]] = None
        self._dividend_receipts: Optional[List[Tuple[date, Decimal]]] = None
        # The last `_derived_events` result and the `BaseFx` it was projected with.
        self._derived: Optional[Tuple[object, tuple]] = None
        # Trades `_derived_events` could not convert, per base currency and then per
        # account — see `unconverted_trades`.
        self._unconverted: Dict[str, Dict[str, int]] = {}

    async def all_trades(self) -> List[Trade]:
        """Every trade, oldest first — read once per instance. Shared with the realized
        totals of the same summary request, so `trades` is read once for both."""
        if self._trades is None:
            self._trades = await TradeRepository(self.db).get_all()
        return self._trades

    async def _all_flows(self) -> List[CashFlow]:
        if self._flows is None:
            self._flows = await CashFlowRepository(self.db).get_all()
        return self._flows

    async def flow_ledger(self) -> CashFlowLedger:
        """The cash-flow ledger's dates and deposits for the money-in splice, off the
        rows this instance has read (or reads now, once)."""
        return CashFlowLedger(await self._all_flows())

    async def ibkr_dividend_receipts(self) -> List[Tuple[date, Decimal]]:
        """`DividendService.ibkr_cash_receipts()`, read once per instance — the third
        ledger of the balance, and the decomposition's dividend leg."""
        if self._dividend_receipts is None:
            # Imported here: dividend_service imports this module.
            from app.services.dividend_service import DividendService
            self._dividend_receipts = await DividendService(self.db).ibkr_cash_receipts()
        return self._dividend_receipts

    async def _measured_rows(self, account: Optional[str] = None) -> List[CashBalance]:
        """Every measured balance oldest first, optionally one account's — the same rows
        `CashBalanceRepository.get_all(account=)` returns, filtered from one read."""
        if self._balances is None:
            self._balances = await CashBalanceRepository(self.db).get_all()
        if account is None:
            return self._balances
        return [row for row in self._balances if row.account == account]

    async def balance_events(
        self, base_fx, account: Optional[str] = None
    ) -> List[Tuple[date, Decimal]]:
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

        `account` keeps one account's events alone (the tax report's IBKR cash line);
        None is the whole book. The per-account splice is what makes that a filter
        rather than a second derivation.
        """
        by_account, to_base = await self._derived_events(base_fx)
        # The union of accounts with derived events and accounts with measured
        # levels, not just the former: a Flex query can carry the Cash Report
        # section while <Trades> and <CashTransactions> are still off, and an
        # account whose only evidence is IBKR's own figure must still get it.
        accounted = set(by_account) | {row.account for row in await self._measured_rows()}
        if account is not None:
            accounted &= {account}
        merged: List[Tuple[date, Decimal]] = []
        for one in sorted(accounted):
            events = sorted(by_account.get(one, []), key=lambda e: e[0])
            merged += await self._apply_measured(events, to_base, one)
        merged.sort(key=lambda e: e[0])
        return merged

    async def _derived_events(self, base_fx):
        """
        The three ledgers as per-account ``(date, amount in base)`` events, unspliced,
        plus the converter that projected them. Shared by `balance_events` and
        `measured_corrections` so both read one set of derived events.

        Built once per instance and `BaseFx` (the same object, not just the same base
        currency: two loads can span different rate windows), so a request asking both —
        the decomposition (GitHub #73) — does not convert every trade twice and ask the
        rate table again for each. Callers only read the lists.
        """
        if self._derived is not None and self._derived[0] is base_fx:
            return self._derived[1]
        by_account: Dict[str, List[Tuple[date, Decimal]]] = defaultdict(list)
        to_base = NativeToBase(self.currency_service, base_fx)
        unconverted: Dict[str, int] = defaultdict(int)

        # 1. Trades. `proceeds` carries IBKR's sign — negative for a buy, positive for a
        #    sell — and `commission` is separately signed (negative), so the sum is the
        #    net cash effect. The same expression `ActivityService._trades` renders as
        #    `amount_base`, so the ledger and the balance cannot disagree about a trade.
        for t in await self.all_trades():
            amount = await to_base.convert(
                (t.proceeds or Decimal("0")) + (t.commission or Decimal("0")),
                t.currency, t.trade_date,
            )
            if amount is None:
                logger.warning(
                    "Cash: no FX for trade %s (%s near %s); the balance is unknown",
                    t.ib_key, t.currency, t.trade_date,
                )
                unconverted[t.account] += 1
                continue
            by_account[t.account].append((t.trade_date, amount))

        # 2. External cash. **Every** flow type, not `get_deposits()`'s whitelist: that
        #    one answers "was this money added", where a transfer must never count. This
        #    asks "did cash move", where a transfer's cash leg genuinely did. The in-kind
        #    rows this account holds carry a zero amount and so contribute nothing either
        #    way, which is the correct answer rather than a lucky one.
        for f in await self._all_flows():
            by_account[f.account].append((f.flow_date, cash_flow_in_base(f, base_fx)))

        # 3. Dividends actually paid into the account. IBKR rows only, and the rule for
        #    which those are lives in DividendService beside every other rule about
        #    these rows — an estimate is a guess about a payment made at a *previous*
        #    broker, so crediting it here invents cash the account never received.
        for when, net_eur in await self.ibkr_dividend_receipts():
            by_account[IBKR].append((when, base_fx.convert(net_eur, when)))

        self._unconverted[base_fx.base_currency] = dict(unconverted)
        self._derived = (base_fx, (by_account, to_base))
        return by_account, to_base

    async def unconverted_trades(self, base_fx, account: Optional[str] = None) -> int:
        """
        Trades the derived events left out because no FX rate converts them.

        Above 0 the derived balance is **unknown**, not short: a skipped buy reads as cash
        never spent, so the sum is off by the whole trade and still looks like a balance
        (GitHub #59). Every reader of the events therefore turns it into `UNKNOWN` — the
        state the surfaces already render as absent — and carries this count so the
        absence names its cause. Answered off the last `_derived_events` run of this
        instance when there was one; the count depends on the base only through
        `NativeToBase`'s same-currency short-circuit, hence the key. `account` counts
        one account's trades only, so a pillar 3a trade cannot unknow the IBKR balance.
        """
        if base_fx.base_currency not in self._unconverted:
            await self._derived_events(base_fx)
        counts = self._unconverted[base_fx.base_currency]
        if account is None:
            return sum(counts.values())
        return counts.get(account, 0)

    async def measured_corrections(self, base_fx) -> List[Tuple[date, Decimal, str]]:
        """
        Only the corrections `_apply_measured` interleaves: ``(date, amount, account)``.

        Each one is the gap between IBKR's own end-of-day balance and what the three
        ledgers add up to by then — which is, by construction, the broker interest,
        account fees and FX conversion spread none of the ledgers record. Summed over a
        window that is the "fees and interest" leg of the return decomposition; kept
        separate here so that surface reads the same corrections the cash line snaps
        to rather than re-deriving them. Empty when no measured row exists.
        """
        # The derived events `balance_events` splices, off the same per-instance memo —
        # never a cache across requests, which would let the corrections drift from the
        # balance they explain once a sync landed between the two (class docstring).
        by_account, to_base = await self._derived_events(base_fx)
        out: List[Tuple[date, Decimal, str]] = []
        for account in sorted(
            set(by_account) | {r.account for r in await self._measured_rows()}
        ):
            events = sorted(by_account.get(account, []), key=lambda e: e[0])
            for when, amount in await self._corrections_for(events, to_base, account):
                out.append((when, amount, account))
        out.sort(key=lambda e: e[0])
        return out

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
        corrections = await self._corrections_for(events, to_base, account)
        if not corrections:
            return events
        return sorted(events + corrections, key=lambda e: e[0])

    async def _corrections_for(
        self, events, to_base, account
    ) -> List[Tuple[date, Decimal]]:
        """The correction events for one account — see `_apply_measured`."""
        measured = await self._measured_rows(account)
        if not measured:
            return []

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

        return corrections

    async def cash_source(self, account: Optional[str] = None) -> str:
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

        With `account`, that account's provenance alone: `MEASURED` when IBKR reports its
        balance, `DERIVED` when it has a trade or a cash flow, else `UNKNOWN`.
        """
        if account is not None:
            if await self._measured_rows(account):
                return MEASURED
            return DERIVED if account in await self._accounts_with_activity() else UNKNOWN
        measured_accounts = {row.account for row in await self._measured_rows()}
        if not measured_accounts:
            return await self.derived_source()

        derived_only = await self._accounts_with_activity() - measured_accounts
        return MIXED if derived_only else MEASURED

    async def _accounts_with_activity(self) -> set:
        """
        Every account with a trade or a cash flow — anything a balance derives from.

        Off the memoized rows when this instance has already read them (the summary has,
        through `balance_events`), else one query for the distinct names — never every
        Trade and CashFlow loaded as an object to collect a label (GitHub #47).
        """
        if self._trades is not None and self._flows is not None:
            return {t.account for t in self._trades} | {f.account for f in self._flows}
        return set((await self.db.execute(
            union(select(Trade.account), select(CashFlow.account))
        )).scalars())

    async def derived_source(self) -> str:
        """
        `DERIVED` or `UNKNOWN`, ignoring any measured rows.

        The fallback for days *before* measurement begins, which is why it exists
        separately: using `cash_source()` there would stamp `ibkr` on the whole history
        the moment one measured row landed at the end of it.
        """
        if self._trades is not None and self._flows is not None:
            return DERIVED if self._trades or self._flows else UNKNOWN
        if await TradeRepository(self.db).count():
            return DERIVED
        if await CashFlowRepository(self.db).count():
            return DERIVED
        return UNKNOWN

    async def uninvested_cash(
        self, base_fx, on_date: date, account: Optional[str] = None
    ) -> UninvestedCash:
        """
        The balance at the end of `on_date` and its provenance, for a share-of-the-book
        denominator — the one read the Allocation and Look-through tabs share.

        It was written inline in `allocation_service` until GitHub #28, and Look-through
        divided by holdings alone beside it, so the same money read ~cash/holdings higher
        on one tab than on the other under the same "% of portfolio" header. One helper,
        so the two cannot again disagree about what counts as cash or when it is unknown;
        `tests/test_uninvested_cash_family.py` fails a service that reads it another way.

        The events are gathered first so `cash_source` answers off the rows they loaded.
        An unconvertible trade makes the balance unknown whatever the provenance: even a
        measured level that absorbs it would leave the days before it short, and the
        correction would book the trade as fees and interest.

        `account` answers for one account alone — the tax report's year-end IBKR cash,
        where pillar 3a money is outside the wealth-tax base (GitHub issue #63). Its
        events, its unconverted trades and its provenance are all that account's; None,
        every caller before that, is the whole book.
        """
        events = await self.balance_events(base_fx, account=account)
        unconverted = await self.unconverted_trades(base_fx, account=account)
        source = await self.cash_source(account=account)
        if source == UNKNOWN or unconverted:
            return UninvestedCash(source=UNKNOWN, balance=None, unconverted=unconverted)
        return UninvestedCash(source=source, balance=self.balance_as_of(events, on_date))

    async def measured_from(self) -> Optional[date]:
        """
        First day IBKR's own balance is on record, or None.

        The per-point splice: a chart point on or after this is `MEASURED`, and one
        before it is `DERIVED`, however many measured rows exist. The Flex window is
        bounded, so measured history starts whenever the section was enabled in the
        portal and can never reach back to the account's start — reporting the whole
        series as measured because the tail is would be the same overclaim as a badge
        that cannot clear.

        Off the measured rows this instance already holds (the timeline has read them
        for `balance_events`), not a query of its own (GitHub #73).
        """
        return min((row.report_date for row in await self._measured_rows()), default=None)

    @staticmethod
    def balance_as_of(
        events: List[Tuple[date, Decimal]], on_date: Optional[date] = None
    ) -> Decimal:
        """The running balance at the end of `on_date`, from `balance_events`."""
        if on_date is None:
            return sum((a for _, a in events), Decimal("0"))
        return sum((a for d, a in events if d <= on_date), Decimal("0"))
