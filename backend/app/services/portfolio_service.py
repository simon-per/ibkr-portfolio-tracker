"""
Portfolio Service
Calculates cost basis and market value for the portfolio over time.
"""
import calendar
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict, Optional, Sequence, Set, Tuple
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.models.corporate_action import CorporateAction
from app.models.taxlot import TaxLot
from app.models.security import Security
from app.models.trade import Trade
from app.services.market_data_service import MarketDataService
from app.services.currency_service import CurrencyService
from app.accounts import IBKR
from app.services.cash_service import CashService, MEASURED, UNKNOWN as CASH_UNKNOWN
from app.services.native_amounts import NativeToBase, cash_flow_in_base
from app.services.realized_trades import realized_from_sell_trades
# `BaseFx` lives in base_fx.py and is re-exported here, where eight test modules and
# benchmark_service import it from.
from app.services.base_fx import BaseFx, default_start, load_base_fx  # noqa: F401
from app.services.fx_policy import FX_MAX_CARRY_DAYS
from app.services.fx_preload import (
    RateCache, eur_rate_on, preload_eur_rates, slice_eur_rates, wanted_currencies,
)
from app.services.percentages import share_pct
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.corporate_action_repository import CorporateActionRepository
from app.repositories.trade_repository import TradeRepository
from app.repositories.security_repository import held_at, lot_held_at
from app.services.sync_helper import POSITION_CREATING_ACTIONS
from app.clock import utc_today

logger = logging.getLogger(__name__)

# Average calendar month, used only to express an elapsed span in months so a
# part-month of history isn't rounded away.
_DAYS_PER_MONTH = 365.25 / 12

# Forward-fill horizon for market prices: _preload_market_prices extends its
# query this many days before the window, and _find_latest_price_date must not
# search deeper — the search cannot see past what the preload fetched.
PRICE_LOOKBACK_DAYS = 14


@dataclass(frozen=True)
class AttributionInputs:
    """
    Everything `attribution_rows` loads, read **once** over a span that holds every
    window a caller will ask about — the decomposition asks for its window and one per
    calendar year, and used to reload all lots, both price and FX caches, `base_fx` and
    the closed lots once per window (GitHub #47). Built by `load_attribution_inputs`.

    Each window still sees exactly what its own queries would have returned:
    `_preload_market_prices`, `_preload_exchange_rates` and
    `realized_rows_from_closed_lots` cut their window's rows out of these, and query as
    before for a window the span does not hold. Cutting is what keeps the figures
    identical — a wider cache would let a price fallback reach past the 14 days the
    window's own load allowed, and let a newer row decide a mixed-currency history's
    price currency.
    """
    taxlots_with_securities: list
    base_fx: BaseFx
    security_ids: frozenset
    price_rows: list            # MarketPrice rows over [prices_from, prices_to]
    prices_from: date
    prices_to: date
    rates: RateCache            # `preload_eur_rates` over [rates_from, rates_to]
    rate_currencies: frozenset
    rates_from: date
    rates_to: date


def _shift_months(from_date: date, months: int) -> date:
    """
    The same day-of-month ``months`` before ``from_date``, clamped to the length
    of the target month (31 May - 3 months -> 28/29 Feb).
    """
    total = from_date.year * 12 + (from_date.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(from_date.day, calendar.monthrange(year, month)[1]))


class UnpricedDayLog:
    """
    The valuation walks' unpriced holdings, logged once per security per walk (GitHub #64).

    Both walks (`_calculate_timeline_swept`, `_calculate_daily_value`) used to log a
    WARNING per unpriced security per day, and the price lookup a second one: one
    5-year read with a single long-unpriced holding wrote thousands of lines into a
    container log capped by rotation, pushing out the sync evidence an incident needs.
    The response already carries the figure (`unpriced_holdings` on every point); the
    log only has to say which security, over which days and why.

    A SET of dates per security, not a counter: `_calculate_daily_value` walks lots, so
    a holding split across many lots would otherwise be counted once per lot.
    """

    def __init__(self) -> None:
        # security id -> (symbol, days, reasons)
        self._by_security: Dict[int, Tuple[str, Set[date], Set[str]]] = {}

    def note(self, security_id: int, symbol: str, on: date, reason: str) -> None:
        _, days, reasons = self._by_security.setdefault(security_id, (symbol, set(), set()))
        days.add(on)
        reasons.add(reason)

    def flush(self, context: str) -> int:
        """One WARNING per security noted, then empty. Returns how many lines it wrote."""
        for sid, (symbol, days, reasons) in sorted(self._by_security.items()):
            logger.warning(
                "%s: security %s (%s) left out of the market value on %d day(s), "
                "%s..%s: %s",
                context, sid, symbol, len(days), min(days), max(days),
                "; ".join(sorted(reasons)),
            )
        written = len(self._by_security)
        self._by_security.clear()
        return written


class PortfolioService:
    """
    Service for portfolio analytics and calculations.

    Calculates:
    - Cost basis: Total amount invested (in EUR)
    - Market value: Current worth of holdings (in EUR)
    - Unrealized gain/loss
    - Portfolio composition by security
    """

    # Securities `holdings_snapshot_as_of` had to drop on its most recent run because
    # they could not be valued at that date. A latch rather than a second return value,
    # the same shape as `MarketDataService.rate_limited` and
    # `IBKRService.last_schema_notes`; declared on the class as well so a service built
    # through `__new__` in a test can still read it.
    last_snapshot_skipped: List[str] = []

    # Held securities `calculate_xirr` could not value at one of its two window
    # endpoints, on its most recent run. Same latch shape and same reason as above:
    # one router unpacks its 5-tuple and six assertions in `tests/test_xirr.py` do
    # too, so a sixth return value would be six edits to say one thing.
    last_xirr_unpriced: int = 0

    # Closed lots `realized_rows_from_closed_lots` could not price (or convert) at their
    # close date, on its most recent run: security id -> the close dates of its skipped
    # lots. The ids feed the window readers' `unpriced` counts and the dates put the
    # timeline's flag on the day whose flow is short. A latch rather than a tuple for
    # the reason above — six callers, the tax report and the tests unpack the rows.
    last_realized_skipped: Dict[int, Set[date]] = {}

    def __init__(self, db: AsyncSession):
        self.db = db
        self.market_data_service = MarketDataService(db)
        self.currency_service = CurrencyService(db)
        self.last_snapshot_skipped = []
        self.last_xirr_unpriced = 0
        self.last_realized_skipped = {}

    async def get_base_currency(self) -> str:
        """Return the configured base (display) currency."""
        return await AppSettingsRepository(self.db).get_base_currency()

    async def _load_base_fx(self) -> BaseFx:
        """
        Build a BaseFx for the configured base currency, loading EUR->base daily
        rates over the full portfolio history (earliest lot open_date .. today).

        The loader itself is `base_fx.load_base_fx`, shared with the crypto view, which
        loads over its own window; this method only decides the stock book's window.
        """
        base_currency = await self.get_base_currency()
        if base_currency == "EUR":
            return BaseFx("EUR", {})
        min_open = (await self.db.execute(select(func.min(TaxLot.open_date)))).scalar()
        return await load_base_fx(
            self.db, self.currency_service, base_currency, default_start(min_open)
        )

    async def _load_position_start_dates(self) -> Dict[int, date]:
        """
        For each security whose position began *later* than its own tax lots claim, the
        date it actually began. Empty for every security that has always been itself.

        A spinoff's received line arrives against the parent's tax lots — the child
        inherits the parent's `open_date`, because the holding period carries over, plus
        the slice of cost basis IBKR reallocates to it. So the lot asserts ownership from
        a date when the instrument had no listing and no price, and every valuation in
        between counts it as a held security that cannot be valued. That is the exact
        shape of a stalled price feed, which is what made it expensive: MBGL, received
        from SPGI on 2026-06-30 against lots dated 2025-11-06 and 2025-12-29, reported
        `unpriced_holdings = 1` for seven and a half months, and `isMeasurable` on the
        client dropped every one of those days — six months of the monthly-returns table
        blank, November 2025 measured over three days, and a "YTD" covering six weeks.

        **Excluding the child before the action date is the arithmetic, not a workaround
        for the missing prices.** The parent's own close still carried the spun-off
        business on those days (we fetch `auto_adjust=False`, and Yahoo does not rebase
        raw `Close` for a spinoff — the same reason `PRICE_RESTATING_ACTIONS` excludes
        one), so valuing the child beside it would double-count. Its **cost stays where
        the lot puts it** for the mirror reason: IBKR reduced the parent's basis by
        exactly what the child received, so the pair sums to the original outlay on the
        original date. Deferring the cost too would understate the cost line before the
        spinoff and book a phantom purchase on the day of it.

        Two guards, both erring towards *not* flooring, because not flooring merely leaves
        the warning in place while flooring wrongly deletes a real holding from its own
        history:

        - **`quantity > 0`.** A parent-side spinoff row carries no quantity change, and
          flooring the parent at the action date is the catastrophic direction — SPGI
          would vanish from every valuation before 2026-06-30.
        - **The action must account for the whole position.** A spinoff distributing more
          of something already held must not floor the shares held before it.

        A spinoff whose `<CorporateActions>` row was never ingested (it fell outside the
        rolling Flex window) simply keeps the old, loud behaviour. That is the right
        default: the floor is driven by recorded fact, and no record means no licence to
        drop a holding quietly.

        The quantity held at each action's date is one grouped query over all of them,
        not one `SUM` per action (GitHub #47): the summary runs this on every load.
        """
        actions = [
            a for a in await CorporateActionRepository(self.db).get_by_types(
                POSITION_CREATING_ACTIONS
            )
            if a.security_id is not None and a.quantity and a.quantity > 0
        ]
        held_by_action: Dict[int, Decimal] = {}
        if actions:
            held_by_action = dict((await self.db.execute(
                select(CorporateAction.id, func.sum(TaxLot.quantity))
                .join(TaxLot, and_(
                    TaxLot.security_id == CorporateAction.security_id,
                    held_at(CorporateAction.action_date),
                ))
                .where(CorporateAction.id.in_([a.id for a in actions]))
                .group_by(CorporateAction.id)
            )).all())
        starts: Dict[int, date] = {}
        for action in actions:
            held = held_by_action.get(action.id) or Decimal("0")
            if held > action.quantity:
                continue
            previous = starts.get(action.security_id)
            if previous is None or action.action_date < previous:
                starts[action.security_id] = action.action_date
        if starts:
            logger.info(f"Position start dates from corporate actions: {starts}")
        return starts

    async def _all_lots_with_securities(self) -> list:
        """Every tax lot (open and closed) with its security, oldest open first — what
        the timeline and `attribution_rows` walk."""
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .order_by(TaxLot.open_date.asc())
        )
        return result.all()

    async def load_attribution_inputs(
        self, span_start: date, span_end: date, base_fx: Optional[BaseFx] = None
    ) -> AttributionInputs:
        """
        Read `AttributionInputs` once for every window inside ``[span_start, span_end]``.

        The price rows reach back two lookbacks: a window's effective start can sit up
        to `PRICE_LOOKBACK_DAYS` before it, and its disposals' prices look back another
        one from there. The rates reach back one price lookback plus their own carry for
        the same reason. A window the span does not hold is still answered — by its
        own queries.
        """
        lots = await self._all_lots_with_securities()
        security_ids = frozenset(sec.id for _, sec in lots)
        prices_from = span_start - timedelta(days=2 * PRICE_LOOKBACK_DAYS)
        price_rows: list = []
        if security_ids:
            from app.models.market_price import MarketPrice
            price_rows = list((await self.db.execute(
                select(MarketPrice).where(
                    and_(
                        MarketPrice.security_id.in_(security_ids),
                        MarketPrice.date >= prices_from,
                        MarketPrice.date <= span_end,
                    )
                )
            )).scalars().all())
        rate_currencies = frozenset(wanted_currencies(
            {sec.currency for _, sec in lots} | {p.currency for p in price_rows}
        ))
        rates_start = span_start - timedelta(days=PRICE_LOOKBACK_DAYS)
        rates = await preload_eur_rates(
            self.db, rate_currencies, rates_start, span_end, FX_MAX_CARRY_DAYS
        )
        return AttributionInputs(
            taxlots_with_securities=lots,
            base_fx=base_fx if base_fx is not None else await self._load_base_fx(),
            security_ids=security_ids,
            price_rows=price_rows,
            prices_from=prices_from,
            prices_to=span_end,
            rates=rates,
            rate_currencies=rate_currencies,
            rates_from=rates_start - timedelta(days=FX_MAX_CARRY_DAYS),
            rates_to=span_end,
        )

    async def get_portfolio_value_over_time(
        self,
        start_date: date,
        end_date: date,
        preloaded: Optional[AttributionInputs] = None,
        contributions: Optional[Dict] = None,
        cash_service: Optional[CashService] = None,
    ) -> List[Dict]:
        """
        Calculate portfolio value (cost basis and market value) for each day in range.
        Uses optimized caching for fast performance.
        Includes both open AND closed tax lots for correct historical values.

        `preloaded` hands over lots, caches and `base_fx` a caller already read over a
        span holding this range (the decomposition), so they are not read again.
        `contributions` is that caller's own `_contribution_inputs` under the same
        `base_fx`, and `cash_service` its instance, so the money-in splice and the cash
        ledgers are read once for the request rather than once per reader (GitHub #73).
        """
        # Get ALL taxlots (open + closed) for historical accuracy
        if preloaded is not None:
            taxlots_with_securities = preloaded.taxlots_with_securities
        else:
            taxlots_with_securities = await self._all_lots_with_securities()

        if not taxlots_with_securities:
            return []

        # Pre-load all data once
        unique_securities = {security for _, security in taxlots_with_securities}
        price_cache, price_currency_cache = await self._preload_market_prices(
            unique_securities, start_date, end_date, preloaded=preloaded
        )
        exchange_rate_cache = await self._preload_exchange_rates(
            unique_securities, start_date, end_date,
            price_currency_cache=price_currency_cache, preloaded=preloaded,
        )
        base_fx = preloaded.base_fx if preloaded is not None else await self._load_base_fx()

        # Proceeds of lots closed in the range, so each day can report the real
        # external flow rather than leaving the frontend to infer one from the
        # cost-basis line — which reads a sale as (cost − proceeds) of phantom
        # return and distorts drawdown and Sharpe.
        # Window is (start, end] under exclude-on-close, mirroring calculate_xirr
        # and the attribution endpoint: a lot sold ON start_date never entered
        # the series' value (its own close event removes it from day 0), so
        # booking its proceeds would be a flow against value the series never
        # held. Change all three together.
        disposals_by_day: Dict[date, Decimal] = {}
        for row in await self.realized_rows_from_closed_lots(
            base_fx, start=start_date + timedelta(days=1), end=end_date,
            preloaded=preloaded,
        ):
            d = row["close_date"]
            disposals_by_day[d] = disposals_by_day.get(d, Decimal("0")) + row["proceeds"]
        # A disposal that could not be priced leaves that day's flow short; the day is
        # flagged instead of claiming to be complete.
        realized_skipped_by_day: Dict[date, set] = {}
        for sid, close_dates in self.last_realized_skipped.items():
            for d in close_dates:
                realized_skipped_by_day.setdefault(d, set()).add(sid)

        # Uninvested cash, and the contributions line that is its honest partner.
        # Both are running balances over the whole history, so they are built once here
        # and swept alongside the lot events rather than recomputed per point. The lot
        # rows are handed over so the splice does not re-query what is already loaded,
        # and the splice reads the flow ledger off the same CashService as the balance.
        cash_service = cash_service or CashService(self.db)
        cash_events = await cash_service.balance_events(base_fx)
        # The fallback for days before measurement starts — see derived_source().
        cash_source = await cash_service.derived_source()
        # The service's own verdict for the measured era — MEASURED, or MIXED once
        # a second account has cash IBKR cannot see. A literal "ibkr" here stamped
        # the tail of the chart `ibkr` while the summary card said `mixed` for the
        # same balance on the same day, and the chart dropped its caveat.
        cash_measured_source = await cash_service.cash_source()
        # A trade with no FX rate leaves the sum short by the whole trade, so the balance
        # is unknown on every point — the summary's state, through the summary's rule
        # (`CashService.unconverted_trades`, GitHub #59). No partial sum reaches the wire.
        if await cash_service.unconverted_trades(base_fx):
            cash_events = []
            cash_source = cash_measured_source = CASH_UNKNOWN
        if contributions is None:
            contributions = await self._contribution_inputs(
                base_fx,
                lot_rows=[
                    (lot.open_date, lot.close_date, lot.cost_basis_eur)
                    for lot, _ in taxlots_with_securities
                ],
                cash_service=cash_service,
            )

        # One sweep over the whole range instead of a per-day × per-lot loop.
        portfolio_timeline = self._calculate_timeline_swept(
            start_date, end_date, taxlots_with_securities, price_cache,
            exchange_rate_cache, price_currency_cache, base_fx,
            disposals_by_day=disposals_by_day,
            realized_skipped_by_day=realized_skipped_by_day,
            position_start=await self._load_position_start_dates(),
            cash_events=cash_events,
            money_in_legs=contributions["money_in_legs"],
            cash_source=cash_source,
            cash_measured_from=await cash_service.measured_from(),
            cash_measured_source=cash_measured_source,
        )
        for row in portfolio_timeline:
            row["base_currency"] = base_fx.base_currency
        return portfolio_timeline

    def _calculate_timeline_swept(
        self,
        start_date: date,
        end_date: date,
        taxlots_with_securities: List,
        price_cache: Dict,
        exchange_rate_cache: Dict,
        price_currency_cache: Optional[Dict],
        base_fx: BaseFx,
        disposals_by_day: Optional[Dict[date, Decimal]] = None,
        realized_skipped_by_day: Optional[Dict[date, set]] = None,
        position_start: Optional[Dict[int, date]] = None,
        cash_events: Optional[List[Tuple[date, Decimal]]] = None,
        money_in_legs: Optional[List[Tuple[date, Decimal]]] = None,
        cash_source: str = "unknown",
        cash_measured_from: Optional[date] = None,
        cash_measured_source: str = MEASURED,
    ) -> List[Dict]:
        """
        The full timeline in one sweep — numerically identical to calling
        _calculate_daily_value per day (pinned by tests/test_timeline_equivalence),
        but O(days × securities) instead of O(days × lots): the per-lot pieces
        that never depend on the valuation date (base-converted cost at open_date,
        quantity) fold into running sums via open/close events, so each day only
        prices each held security once instead of once per lot. At ~1000 lots over
        ~570 business days that removes ~99% of the inner-loop work on the chart
        endpoint. _calculate_daily_value stays for point queries (XIRR endpoints).
        """
        events = []  # (date, security_id, qty_delta, cost_delta_base)
        securities_by_id: Dict[int, Security] = {}
        for lot, sec in taxlots_with_securities:
            securities_by_id[sec.id] = sec
            cost = base_fx.convert(lot.cost_basis_eur, lot.open_date)
            events.append((lot.open_date, sec.id, lot.quantity, cost))
            if lot.close_date:
                # Exclude-on-close: the removal applies ON the close date.
                events.append((lot.close_date, sec.id, -lot.quantity, -cost))
        events.sort(key=lambda e: e[0])

        disposals_by_day = disposals_by_day or {}
        realized_skipped_by_day = realized_skipped_by_day or {}
        position_start = position_start or {}
        # Cash and money-in are running BALANCES, not per-day flows, so events before
        # the window are carried into the opening total rather than discarded the way
        # `pending_flow` discards pre-window purchases. A chart starting in June must
        # still show the cash that arrived in May.
        cash_events = sorted(cash_events or [], key=lambda e: e[0])
        money_in_legs = sorted(money_in_legs or [], key=lambda e: e[0])
        ci = mi = 0
        cash_running = Decimal("0")
        money_in_running = Decimal("0")
        qty_by_sec: Dict[int, Decimal] = {}
        total_cost = Decimal("0")
        timeline: List[Dict] = []
        i = 0
        # Events strictly before the window seed the opening state — they are
        # history, not flows. Without this, the catch-up loop below fed every
        # pre-window purchase into pending_flow and the first row reported the
        # portfolio's whole acquisition history as that day's external flow.
        while i < len(events) and events[i][0] < start_date:
            _, sid, dq, dc = events[i]
            qty_by_sec[sid] = qty_by_sec.get(sid, Decimal("0")) + dq
            total_cost += dc
            i += 1
        d = start_date
        # Flows accumulate until the next emitted row: only weekdays are emitted,
        # so a purchase or sale dated on a weekend belongs to the following one.
        pending_flow = Decimal("0")
        # Securities whose disposal is missing from `pending_flow`; they ride to the
        # same emitted row the flow does.
        pending_skipped: set = set()
        unpriced_log = UnpricedDayLog()
        while d <= end_date:
            while i < len(events) and events[i][0] <= d:
                _, sid, dq, dc = events[i]
                qty_by_sec[sid] = qty_by_sec.get(sid, Decimal("0")) + dq
                total_cost += dc
                # Purchases enter at cost; sales are netted below at their market
                # proceeds, not at the cost they were originally bought for.
                if dc > 0:
                    pending_flow += dc
                i += 1
            pending_flow -= disposals_by_day.get(d, Decimal("0"))
            pending_skipped |= realized_skipped_by_day.get(d, set())

            while ci < len(cash_events) and cash_events[ci][0] <= d:
                cash_running += cash_events[ci][1]
                ci += 1
            while mi < len(money_in_legs) and money_in_legs[mi][0] <= d:
                money_in_running += money_in_legs[mi][1]
                mi += 1

            if d.weekday() < 5:
                mv_eur = Decimal("0")
                # Holdings whose cost is in `total_cost` but whose value could not be
                # resolved. Counted rather than only logged: a 730-day window over 40
                # securities can emit ~29k of those warnings, which is noise, while the
                # response carried no sign that its market value was a partial sum.
                # Starts from today's unpriceable disposals, a set so a partly sold
                # security that is also unpriced as a holding counts once.
                unpriced = set(pending_skipped)
                for sid, qty in qty_by_sec.items():
                    if qty <= 0:
                        continue
                    # A position that did not exist yet is neither valued nor counted as
                    # unpriced: its worth was still inside the parent's close, and its
                    # cost is already in `total_cost` above because the parent's basis was
                    # reduced by it. See _load_position_start_dates — and keep this
                    # identical to _calculate_daily_value.
                    started = position_start.get(sid)
                    if started is not None and d < started:
                        continue
                    price = self._get_market_price_with_fallback(sid, d, price_cache)
                    if not price:
                        unpriced.add(sid)
                        unpriced_log.note(
                            sid, securities_by_id[sid].symbol, d, "no market price"
                        )
                        continue
                    sec = securities_by_id[sid]
                    price_currency = (
                        price_currency_cache.get(sid, sec.currency)
                        if price_currency_cache else sec.currency
                    )
                    if price_currency != "EUR":
                        rate = self._get_exchange_rate_with_fallback(
                            price_currency, d, exchange_rate_cache
                        )
                        if not rate:
                            unpriced.add(sid)
                            unpriced_log.note(
                                sid, sec.symbol, d, f"no exchange rate for {price_currency}"
                            )
                            continue
                        mv_eur += qty * price * rate
                    else:
                        mv_eur += qty * price

                mv = base_fx.convert(mv_eur, d)
                gain = mv - total_cost
                timeline.append({
                    "date": d.isoformat(),
                    "cost_basis_eur": float(total_cost),
                    "market_value_eur": float(mv),
                    "gain_loss_eur": float(gain),
                    # None before anything is held at cost, never a flat 0% (`share_pct`);
                    # `_calculate_daily_value` answers the same, the equivalence test pins it.
                    "gain_loss_percent": share_pct(gain, total_cost),
                    # Money entering (+) or leaving (−) the tracked holdings today:
                    # purchases at cost, sales at their market proceeds. Any return
                    # measure has to net this out, and inferring it from the
                    # cost-basis line books a sale's whole gain as a loss.
                    "external_flow_eur": float(pending_flow),
                    # > 0 means this day's market value is INCOMPLETE — the unpriced
                    # holdings still count in cost_basis_eur, so gain_loss_eur and
                    # gain_loss_percent understate by their whole value. Fifteen days
                    # past the last cached price every holding drops out and the point
                    # reads market value 0 / −100%, which is a fabricated wipeout rather
                    # than a gap; the partial case is worse, because a plausible
                    # +15% invites no doubt at all. Also counts a sale today whose
                    # proceeds could not be priced, so `external_flow_eur` is short.
                    "unpriced_holdings": len(unpriced),
                    # Uninvested cash. A sale moves value from `market_value_eur` to
                    # here rather than out of the account, which is why `total_value_eur`
                    # is flat across a rotation while the holdings line falls off a
                    # cliff. See CashService for where the balance comes from and what
                    # it cannot see.
                    "cash_eur": float(cash_running),
                    "total_value_eur": float(mv + cash_running),
                    # Cumulative contributions on the same splice the contributions
                    # strip uses — lot cost basis before `coverage_from`, real deposits
                    # after. The honest partner for a line that includes cash: a trade
                    # moves value between two things `total_value_eur` already contains,
                    # so neither line steps, and the gap between them is total profit
                    # (realized + unrealized + dividends) rather than unrealized alone.
                    "money_in_eur": float(money_in_running),
                    # Per point, not per response: measured history begins whenever the
                    # Flex section was enabled, so calling the whole series measured
                    # because its tail is would overclaim the years before it. And the
                    # measured era's label is CashService's verdict, not a literal — it
                    # is `mixed` while a second account's cash is only derived, which is
                    # what the summary card says for the same day.
                    "cash_source": (
                        cash_measured_source
                        if cash_measured_from is not None and d >= cash_measured_from
                        else cash_source
                    ),
                })
                pending_flow = Decimal("0")
                pending_skipped = set()
            d += timedelta(days=1)

        unpriced_log.flush(f"Timeline {start_date}..{end_date}")
        return timeline

    async def get_current_portfolio_summary(self) -> Dict:
        """
        Get current portfolio summary with latest values.
        """
        today = utc_today()

        base_fx = await self._load_base_fx()

        # Get all open taxlots with securities
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .where(TaxLot.is_open == True)
        )
        taxlots_with_securities = result.all()

        # One CashService for the request: the realized totals and the cash balance read
        # the same `trades` rows, so they are read once (GitHub #47).
        cash_service = CashService(self.db)

        if not taxlots_with_securities:
            realized = await self.get_realized_totals(
                base_fx=base_fx, trades=await cash_service.all_trades()
            )
            # Nothing held is NOT the same as nothing owned: an account that has sold
            # everything holds its whole value in cash, which is the state this feature
            # exists for. Reporting 0.00 here would be the reassuring zero in its purest
            # form — a liquidated account rendered as an empty one.
            cash = await self._cash_summary(base_fx, today, cash_service)
            return {
                "total_cost_basis_eur": 0.0,
                "total_market_value_eur": 0.0,
                "total_gain_loss_eur": 0.0,
                # A gain on no cost at all has no percentage (`share_pct`).
                "total_gain_loss_percent": None,
                "num_positions": 0,
                "unpriced_holdings": 0,
                "base_currency": base_fx.base_currency,
                **cash,
                # None with an unknown balance: the whole value is cash, and it is unknown.
                "total_value_eur": cash["total_cash_eur"],
                **realized,
            }

        # Use optimized method
        unique_securities = {security for _, security in taxlots_with_securities}
        price_cache, price_currency_cache = await self._preload_market_prices(unique_securities, today, today)
        exchange_rate_cache = await self._preload_exchange_rates(unique_securities, today, today, price_currency_cache=price_currency_cache)

        daily_value = self._calculate_daily_value(
            today, taxlots_with_securities, price_cache, exchange_rate_cache,
            price_currency_cache=price_currency_cache, base_fx=base_fx,
            position_start=await self._load_position_start_dates(),
        )

        realized = await self.get_realized_totals(
            base_fx=base_fx, trades=await cash_service.all_trades()
        )

        cash = await self._cash_summary(base_fx, today, cash_service)

        return {
            "total_cost_basis_eur": daily_value["cost_basis_eur"],
            "total_market_value_eur": daily_value["market_value_eur"],
            "total_gain_loss_eur": daily_value["gain_loss_eur"],
            "total_gain_loss_percent": daily_value["gain_loss_percent"],
            "num_positions": len(set(security.id for _, security in taxlots_with_securities)),
            # Straight off the same helper the timeline uses, so the headline total and
            # every point on the chart agree about their own completeness rather than each
            # deciding. Above 0 means total_market_value_eur is a PARTIAL sum while
            # total_cost_basis_eur is not — the SBI shape, where 446.93 CHF left the total
            # with only a sync warning to catch it.
            "unpriced_holdings": daily_value["unpriced_holdings"],
            "date": daily_value["date"],
            "base_currency": base_fx.base_currency,
            **cash,
            # Holdings plus cash: what the account is actually worth, and what the
            # Market Value card understated by the whole idle balance until this
            # existed. Served rather than added on the client so the card and every
            # chart point come from one derivation. None while cash is unknown: holdings
            # plus an unknown is not a total, and the hero falls back to Market Value.
            "total_value_eur": (
                daily_value["market_value_eur"] + cash["total_cash_eur"]
                if cash["total_cash_eur"] is not None else None
            ),
            **realized,
        }

    async def _cash_summary(
        self, base_fx: BaseFx, on_date: date, service: Optional[CashService] = None
    ) -> Dict:
        """
        The uninvested balance and its provenance, for the headline cards.

        Split from the caller only because both return paths need it — including the
        empty-portfolio one, where cash is the *entire* value of the account. `service`
        is the caller's own instance, so its ledger rows are read once per request.

        Read through `uninvested_cash`, the helper Allocation and Look-through share, so
        the card and the two tabs agree about when the balance is unknown. Unknown ships
        as None, never as 0.00, with `cash_unconverted` naming the trades that made it so
        (GitHub #59).
        """
        service = service or CashService(self.db)
        cash = await service.uninvested_cash(base_fx, on_date)
        return {
            "total_cash_eur": float(cash.balance) if cash.balance is not None else None,
            "cash_source": cash.source,
            "cash_unconverted": cash.unconverted,
        }

    async def _contribution_inputs(
        self,
        base_fx: BaseFx,
        lot_rows: Optional[List] = None,
        cash_service: Optional[CashService] = None,
    ) -> Dict:
        """
        The raw material behind "money in", shared by the contributions strip and the
        chart's Money In line.

        Both answer the same question and must never answer it differently, so the
        splice rule lives here once rather than in a monthly aggregator and a daily
        one. `money_in_legs` is that rule expressed as events: **positive lot legs
        strictly before `coverage_from`, then deposits from it onward.** Summing them
        over any window reproduces all three branches `get_contributions` used to
        compute inline — deployed-only, deposits-only, and spliced — because the two
        ranges neither overlap nor leave a hole.

        `lot_rows` lets the chart pass the tax lots it has already loaded instead of
        re-querying them; the columns it needs are (open_date, close_date,
        cost_basis_eur). `cash_service` is the caller's instance when it has one: the
        deposits and the three ledger dates come off its one read of `cash_flows`.
        """
        if lot_rows is None:
            lot_rows = (await self.db.execute(
                select(TaxLot.open_date, TaxLot.close_date, TaxLot.cost_basis_eur)
            )).all()

        # Each lot contributes one positive leg on its open_date (the deployment)
        # and, once sold, a negative leg on its close_date (capital coming back).
        # Project into the base currency at the date the leg sits on, then
        # everything downstream is plain date arithmetic.
        legs: List[Tuple[date, Decimal]] = []
        first_open: Optional[date] = None
        for open_date, close_date, cost_basis_eur in lot_rows:
            cost = cost_basis_eur or Decimal("0")
            if open_date:
                legs.append((open_date, base_fx.convert(cost, open_date)))
                if first_open is None or open_date < first_open:
                    first_open = open_date
            if close_date:
                legs.append((close_date, -base_fx.convert(cost, close_date)))

        # External cash, if the Flex Query has been set to deliver it. Deposits only:
        # the ledger's deposits() excludes TRANSFER rows, so capital that arrived by broker
        # transfer is never read as a contribution.
        flow_ledger = await (cash_service or CashService(self.db)).flow_ledger()
        deposits_from = flow_ledger.earliest_deposit_date()
        transfer_in_date = flow_ledger.earliest_transfer_in_date()
        deposit_legs: List[Tuple[date, Decimal]] = [
            (f.flow_date, cash_flow_in_base(f, base_fx))
            for f in flow_ledger.deposits()
        ]

        # The splice point: where the deposit ledger becomes complete. Recorded from the
        # statement period start, so a covered week with no deposits still counts as
        # covered. Falls back to the first deposit for ledgers ingested before the
        # setting existed, and is None when no deposits exist at all.
        coverage_from = await AppSettingsRepository(self.db).get_cash_flows_covered_from()
        if coverage_from is None:
            coverage_from = deposits_from

        # ...but never before the ledger's first row of any kind. A YTD statement in the
        # account's first year starts on 1 January while the account was funded weeks
        # later, and in that gap an empty deposit list means the money went to another
        # broker — not that none was added. Taking the statement's word for it drops
        # those purchases from both sides: past the lot cutoff, with no deposit to
        # replace them. Clamping forward hands the gap back to lot cost basis, which is
        # the correct source for any era the ledger does not reach.
        #
        # Scoped to IBKR because `coverage_from` is an IBKR Flex claim (sync_helper is
        # its only writer). Unscoped, one pillar-3a export starting before that date
        # made the `>` test false and the clamp silently disappeared — the 3a blends
        # everywhere *except* where a per-IBKR fact is being computed.
        ledger_starts_at = flow_ledger.earliest_flow_date(account=IBKR)
        if coverage_from and ledger_starts_at and ledger_starts_at > coverage_from:
            coverage_from = ledger_starts_at

        if coverage_from is None:
            # No ledger at all: lot cost basis is the only source there is, so every
            # deployment counts. This is the "deployed" method.
            money_in_legs = [(d, a) for d, a in legs if a > 0]
        else:
            money_in_legs = (
                [(d, a) for d, a in legs if a > 0 and d < coverage_from]
                + [(d, a) for d, a in deposit_legs if d >= coverage_from]
            )
        money_in_legs.sort(key=lambda leg: leg[0])

        # History starts at the first thing that happened, which is not always a
        # purchase. Every window in `get_contributions` is clamped to `first_open`, so
        # anything earlier is outside *all* of them and silently absent from money in.
        #
        # That was invisible while there was one account: the IBKR holdings arrived by
        # in-kind transfer carrying their original 2024-2025 open dates, so the first
        # lot predates the first deposit by years and no deposit could fall outside.
        # A retirement account is the opposite shape and the common one -- you pay in,
        # and it is invested days later -- so 1,758 CHF of real deposits read as 0.00
        # money in against 1,842 EUR deployed. Not a pillar 3a quirk: an IBKR deposit
        # made before the first purchase was always dropped the same way.
        #
        # Widened rather than replaced: a transferred lot still has to anchor the
        # history it really belongs to, so this is min(), never the ledger alone.
        if money_in_legs:
            earliest_leg = money_in_legs[0][0]
            first_open = min(first_open, earliest_leg) if first_open else earliest_leg

        return {
            "legs": legs,
            "deposit_legs": deposit_legs,
            "money_in_legs": money_in_legs,
            "coverage_from": coverage_from,
            "deposits_from": deposits_from,
            "transfer_in_date": transfer_in_date,
            "first_open": first_open,
        }

    async def get_contributions(self, as_of: Optional[date] = None) -> Dict:
        """
        How much money went in per month, over several trailing windows.

        **``money_in_eur`` is the answer**, and it is spliced across two eras because
        no single source is authoritative for the whole history:

        - Before ``coverage_from``, from **lot cost basis**. That reaches back through
          the pre-IBKR years because the 2026 transfer from the previous brokers
          carried every lot across with its original open_date *and* original cost
          basis (verified: securities have as many distinct costBasisPrice values as
          they have lots, so nothing was re-based to the transfer date).
        - From ``coverage_from`` onward, from **real deposits**.

        ``coverage_from`` is what the statements claim to cover, clamped forward to the
        first row the ledger actually holds — the account is routinely younger than the
        statement period that reports it.

        The split exists because lot cost basis cannot survive a rotation: selling one
        ETF to buy another closes lots and opens new ones, so the same money is
        deployed twice. A deposit has a single leg and cannot be inflated that way, so
        the moment a real ledger exists it takes over. This matters concretely — the
        Ireland-domiciled sleeve is being switched to US-domiciled ETFs for tax
        reasons, which is one large rotation. Averaging deployment through that would
        report savings that never happened.

        Each euro is counted once: the boundary is a single date, lots are summed
        strictly before it and deposits strictly from it, so a purchase funded by a
        deposit in the same month contributes only the deposit.

        ``deployed_eur`` is kept alongside as the secondary figure. Once the rotation
        starts it will exceed ``money_in_eur``, and that divergence is exactly the
        useful signal: it is capital churn, not saving.

        ``net_eur`` (deployed minus the cost basis of lots closed in the window) is
        kept only for the tooltip and because it is what makes the cost-basis identity
        check work. Nothing averages it — doing so lets a sale retroactively erase a
        purchase that really happened.

        ``monthly[]`` carries all three per month, and it carries ``money_in_eur``
        **first** because the card that draws it is a chart of contributions. It did
        not until 2026-09-06, and a rotation is what made that matter: selling the
        Ireland-domiciled sleeve to buy US ETFs deployed ~31k in one month against a
        few hundred francs of new money, drawn as a bar four times the height of any
        real contribution the account has ever made. The gross figure was right and it
        was the only one on the chart. ``Σ monthly[].money_in_eur`` equals the ``all``
        window's ``money_in_eur``, which is the identity that keeps the two honest.

        ``as_of`` defaults to today and exists so tests can pin the windows. Both
        monthly loops clamp at it, as the windows already did — a month past the
        question being asked is not history, and without the clamp that identity holds
        only when no row is future-dated.
        """
        as_of = as_of or utc_today()
        base_fx = await self._load_base_fx()

        inputs = await self._contribution_inputs(base_fx)
        legs = inputs["legs"]
        deposit_legs = inputs["deposit_legs"]
        money_in_legs = inputs["money_in_legs"]
        coverage_from = inputs["coverage_from"]
        deposits_from = inputs["deposits_from"]
        transfer_in_date = inputs["transfer_in_date"]
        first_open = inputs["first_open"]

        if first_open is None:
            return {
                "windows": [],
                "monthly": [],
                "first_contribution_date": None,
                "deposits_from": deposits_from.isoformat() if deposits_from else None,
                "coverage_from": coverage_from.isoformat() if coverage_from else None,
                "transfer_in_date": transfer_in_date.isoformat() if transfer_in_date else None,
                "base_currency": base_fx.base_currency,
            }

        monthly_net: Dict[str, Decimal] = defaultdict(Decimal)
        monthly_gross: Dict[str, Decimal] = defaultdict(Decimal)
        monthly_money_in: Dict[str, Decimal] = defaultdict(Decimal)
        for on_date, amount in legs:
            if on_date > as_of:
                continue
            month = on_date.strftime("%Y-%m")
            monthly_net[month] += amount
            if amount > 0:
                monthly_gross[month] += amount

        # The same `money_in_legs` the windows sum and the chart steps daily, bucketed
        # by month. Summed rather than re-derived: the era splice lives once, in
        # _contribution_inputs, and this is now its third reader.
        for on_date, amount in money_in_legs:
            if on_date > as_of:
                continue
            monthly_money_in[on_date.strftime("%Y-%m")] += amount

        # The UNION of both key sets, which the lot months alone are not. A month
        # carrying a deposit and no purchase has no leg, so keying on `monthly_net`
        # dropped it from the series entirely -- the contribution simply absent from a
        # chart of contributions, and `sum(monthly) != windows['all']` with nothing
        # saying so. Latent while lots came first: the in-kind transfer carried its
        # 2024-25 open dates, so every deposit had lot activity around it. A retirement
        # account is the opposite shape -- pay in one month, invested the next.
        months = sorted(set(monthly_net) | set(monthly_money_in))

        monthly = [
            {
                "month": month,
                # The answer, and what the chart draws first: rotation-proof wherever a
                # deposit ledger exists. `deployed_eur` beside it is gross on purpose.
                "money_in_eur": round(float(monthly_money_in[month]), 2),
                "deployed_eur": round(float(monthly_gross[month]), 2),
                "net_eur": round(float(monthly_net[month]), 2),
            }
            for month in months
        ]

        # Elapsed history caps every window's divisor: a four-month-old portfolio
        # must not report a 12-month average divided by 12. Floored at one day so
        # a portfolio opened today can't divide by zero.
        history_months = max((as_of - first_open).days, 1) / _DAYS_PER_MONTH

        windows = []
        for label, span in (("all", None), ("12m", 12), ("6m", 6), ("3m", 3)):
            if span is None:
                start, months, partial = first_open, history_months, False
            else:
                partial = history_months < span
                months = min(float(span), history_months)
                start = max(_shift_months(as_of, span), first_open)

            net = sum((a for d, a in legs if start <= d <= as_of), Decimal("0"))
            deployed = sum(
                (a for d, a in legs if start <= d <= as_of and a > 0), Decimal("0")
            )

            # Splice at the coverage boundary: lots strictly before it, deposits from it
            # onward. The two ranges don't overlap, so nothing is counted twice, and
            # together they cover the whole window — no clamped divisor needed. The rule
            # itself lives in _contribution_inputs, because the chart's Money In line
            # draws the same series daily and two implementations of one splice is this
            # codebase's dominant failure mode.
            money_in = sum(
                (a for d, a in money_in_legs if start <= d <= as_of), Decimal("0")
            )
            if coverage_from is None:
                method = "deployed"
                deposits = Decimal("0")
            elif start >= coverage_from:
                method = "deposits"
                deposits = sum(
                    (a for d, a in deposit_legs if start <= d <= as_of), Decimal("0")
                )
            else:
                method = "spliced"
                deposits = sum(
                    (a for d, a in deposit_legs if coverage_from <= d <= as_of),
                    Decimal("0"),
                )

            windows.append({
                "label": label,
                "months": round(months, 2),
                "partial": partial,
                # The answer: era-correct money in, immune to rotation wherever a
                # deposit ledger exists.
                "money_in_eur": round(float(money_in), 2),
                "avg_money_in_per_month_eur": (
                    round(float(money_in) / months, 2) if months > 0 else 0.0
                ),
                "money_in_method": method,
                "deposits_eur": round(float(deposits), 2),
                # Secondary: capital put to work. Exceeds money_in once positions are
                # rotated, and that gap is the point of showing it.
                "deployed_eur": round(float(deployed), 2),
                "avg_deployed_per_month_eur": (
                    round(float(deployed) / months, 2) if months > 0 else 0.0
                ),
                "net_eur": round(float(net), 2),
            })

        return {
            "windows": windows,
            "monthly": monthly,
            "first_contribution_date": first_open.isoformat(),
            "deposits_from": deposits_from.isoformat() if deposits_from else None,
            "coverage_from": coverage_from.isoformat() if coverage_from else None,
            "transfer_in_date": transfer_in_date.isoformat() if transfer_in_date else None,
            "base_currency": base_fx.base_currency,
        }

    async def _realized_from_trades(
        self, base_fx: BaseFx, trades: Optional[Sequence[Trade]] = None
    ) -> Optional[Dict]:
        """
        Compute realized totals from authoritative IBKR SELL trades, or return
        None if no trades have been ingested yet (caller then falls back to the
        market-price approximation over closed lots).

        The conversion is `realized_trades.realized_from_sell_trades`, shared with
        the tax report and the closed positions (GitHub issue #58); this reader's
        policy is to sum every converted sale-side row, so a cancelled sale nets out
        against its CANCELSELL.

        A SELL with no FX rate near its trade date is left out of the sums and
        counted in `unpriced_disposals` (GitHub issue #26): until then it was only a
        log line, and the summary's realized headline understated with nothing to
        say so — the shape `tax_service` already warns about per year.

        `trades` is every trade when the caller has already read them — the summary
        shares `CashService.all_trades()`, so the table is read once per request.
        """
        if trades is None:
            trades = await TradeRepository(self.db).get_all()
        realized = await realized_from_sell_trades(
            trades, NativeToBase(self.currency_service, base_fx)
        )
        # "Some trades exist" is not "realized figures exist": a statement can
        # carry BUYs long before any sale. Guarding on any-trade made a BUY-only
        # table return hard zeros and permanently mask the closed-lot fallback,
        # while the tax report (which picks per-year) showed real gains. A sale
        # IBKR cancelled is no sale on record either (issue #58).
        if realized.net_sales <= 0:
            return None

        total_proceeds_eur = realized.proceeds
        total_gain_eur = realized.gain
        return {
            "total_realized_gain_loss_eur": float(total_gain_eur),
            "total_realized_proceeds_eur": float(total_proceeds_eur),
            "total_realized_cost_basis_eur": float(total_proceeds_eur - total_gain_eur),
            # A security whose every sale was cancelled closed nothing.
            "num_closed_positions": sum(
                1 for sid, s in realized.by_security.items()
                if sid is not None and s.converted and s.net_sales > 0
            ),
            "unpriced_disposals": len(realized.unconvertible),
        }

    async def get_realized_totals(
        self,
        base_fx: Optional[BaseFx] = None,
        trades: Optional[Sequence[Trade]] = None,
    ) -> Dict:
        """
        Aggregate realized gain/loss from closed tax lots.

        Used as the fallback when no SELL trades have been ingested (e.g. lots
        closed before the <Trades> section was enabled): proceeds are approximated
        as quantity × market_price × fx_rate on close_date. Lots that can't
        be priced within the 14-day fallback window are left out of the sums and
        counted: `unpriced_disposals` is read off `last_realized_skipped` (GitHub
        issue #19) right after the call. The trade path counts its unconvertible
        SELLs under the same key, so the summary declares a partial realized figure
        whichever source answered — it has no `warnings[]`, so the count is the surface.

        Values are returned in the base currency (proceeds converted at close_date,
        cost basis at open_date). Output keys keep the *_eur suffix.

        When authoritative IBKR <Trades> have been ingested, realized P&L is taken
        straight from each SELL trade's fifoPnlRealized/proceeds (exact, IBKR's own
        FIFO) instead of the market-price approximation below. `trades`, when given,
        is every trade (see `_realized_from_trades`).
        """
        if base_fx is None:
            base_fx = await self._load_base_fx()

        trade_based = await self._realized_from_trades(base_fx, trades=trades)
        if trade_based is not None:
            return trade_based

        rows = await self.realized_rows_from_closed_lots(base_fx)
        unpriced_lots = sum(len(d) for d in self.last_realized_skipped.values())
        total_proceeds_eur = sum((row["proceeds"] for row in rows), Decimal("0"))
        total_cost_basis_eur = sum((row["cost_basis"] for row in rows), Decimal("0"))

        return {
            "total_realized_gain_loss_eur": float(total_proceeds_eur - total_cost_basis_eur),
            "total_realized_proceeds_eur": float(total_proceeds_eur),
            "total_realized_cost_basis_eur": float(total_cost_basis_eur),
            "num_closed_positions": len({row["security_id"] for row in rows}),
            "unpriced_disposals": unpriced_lots,
        }

    async def holdings_snapshot_as_of(
        self,
        base_fx: BaseFx,
        on_date: date,
        exclude_accounts: Optional[Sequence[str]] = None,
        only_accounts: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        """
        Per-security holdings as they stood on ``on_date``, valued at that date's prices.

        The Swiss wealth-tax base (Steuerwert) is the **31 December** value, so the tax
        report cannot just use today's positions for a past year. Lot selection uses the
        same window as _calculate_daily_value (opened on or before the date, not closed
        before it) so the tax snapshot and the portfolio timeline agree.

        Securities whose price can't be resolved near ``on_date`` are skipped rather than
        counted at zero — a thin price history degrades the total instead of lying.

        ``exclude_accounts`` **deliberately breaks the agreement promised above**, and
        only the tax report passes it. Pillar 3a capital is outside the Steuerwert
        entirely — it is taxed on withdrawal at a separate reduced rate, not as
        wealth — so a wealth-tax base that included it would be wrong on a tax
        return. Everything else must keep the default and blend, which is why this
        is an argument rather than a filter baked into the query.

        An excluded security is **not** recorded in ``last_snapshot_skipped``: that
        latch means "could not be valued", and the report turns it into a warning
        that the base is partial. A deliberate exclusion reported as a hole would
        make the one figure that goes on a tax return look broken.

        ``only_accounts`` is its mirror, so the tax report can *state* the 3a
        holdings it just excluded rather than leaving the reader to wonder where
        they went. Passing both is a contradiction and is refused.
        """
        exclude_accounts = tuple(exclude_accounts or ())
        only_accounts = tuple(only_accounts or ())
        if exclude_accounts and only_accounts:
            raise ValueError(
                "holdings_snapshot_as_of takes exclude_accounts or only_accounts, "
                "not both - they answer contradictory questions."
            )
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .where(
                and_(
                    # Exclude-on-close, same window as _calculate_daily_value: a
                    # position sold on 31 Dec is not held at year-end — it belongs
                    # to that year's realized gains, not its Steuerwert.
                    held_at(on_date),
                    *([Security.account.notin_(exclude_accounts)]
                      if exclude_accounts else []),
                    *([Security.account.in_(only_accounts)]
                      if only_accounts else []),
                )
            )
            .order_by(Security.symbol.asc())
        )
        lots = result.all()
        # Reset before the early return too, not only on the main path: the latch is
        # per-run state, and an empty snapshot that inherited a previous date's skip
        # list would report the wrong date's completeness — worse than reporting none.
        self.last_snapshot_skipped = []
        if not lots:
            return []

        securities = {security for _, security in lots}
        price_cache, price_currency_cache = await self._preload_market_prices(
            securities, on_date, on_date
        )
        exchange_rate_cache = await self._preload_exchange_rates(
            securities, on_date, on_date, price_currency_cache=price_currency_cache
        )

        position_start = await self._load_position_start_dates()

        by_security: Dict[int, Dict] = {}
        # Reset per run: this is the answer to "is the total below complete?", and a
        # stale value from a previous call would answer for the wrong date.
        skipped: set = set()
        for lot, security in lots:
            # A spun-off line was not a holding before the action that created it, so it
            # belongs in neither the Steuerwert nor the skipped list — its value sat in the
            # parent's year-end close. Without this, a 31 December before the spinoff
            # reported a *partial* wealth-tax base over a holding that did not exist.
            started = position_start.get(security.id)
            if started is not None and on_date < started:
                continue

            price = self._get_market_price_with_fallback(security.id, on_date, price_cache)
            if price is None:
                logger.warning(
                    f"Holdings snapshot {on_date}: no price for {security.symbol}, skipping lot {lot.id}"
                )
                skipped.add(security.symbol)
                continue

            price_currency = price_currency_cache.get(security.id, security.currency)
            if price_currency == "EUR":
                fx_rate = Decimal("1")
            else:
                fx_rate = self._get_exchange_rate_with_fallback(
                    price_currency, on_date, exchange_rate_cache
                )
                if fx_rate is None:
                    logger.warning(
                        f"Holdings snapshot {on_date}: no FX for {price_currency}, skipping lot {lot.id}"
                    )
                    skipped.add(security.symbol)
                    continue

            row = by_security.setdefault(security.id, {
                # The identity rides along for the tax report's logos (GitHub #63).
                "security_id": security.id,
                "isin": security.isin,
                "symbol": security.symbol,
                "quantity": Decimal("0"),
                "market_value": Decimal("0"),
                "cost_basis": Decimal("0"),
            })
            row["quantity"] += lot.quantity
            row["market_value"] += base_fx.convert(lot.quantity * price * fx_rate, on_date)
            row["cost_basis"] += base_fx.convert(lot.cost_basis_eur, lot.open_date)

        # A dropped holding makes the total below understate with nothing saying so.
        # The tax report already reports a snapshot that RAISED (holdings_snapshot_total
        # becomes None); a snapshot that quietly returned fewer rows is the partial case,
        # and the partial case is the dangerous one — a plausible number reads as an
        # answer where a missing one reads as a fault. On a Swiss wealth-tax base that
        # matters more than anywhere else in the app.
        self.last_snapshot_skipped = sorted(skipped)
        return list(by_security.values())

    async def realized_rows_from_closed_lots(
        self,
        base_fx: BaseFx,
        start: Optional[date] = None,
        end: Optional[date] = None,
        exclude_accounts: Optional[Sequence[str]] = None,
        preloaded: Optional[AttributionInputs] = None,
    ) -> List[Dict]:
        """
        Per-closed-lot realized figures using the market-price approximation
        described in get_realized_totals, optionally restricted to lots closed
        within [start, end].

        Shared by the portfolio realized totals and the tax report's fallback so
        the two views can never disagree about the same closed lots.

        A lot that can't be priced (or converted) near its close date is left out of
        the rows and **recorded in `self.last_realized_skipped`** (security id -> close
        dates), which resets on entry. Read it immediately after the call. Every window
        reader uses these rows as its disposal term, so a silent skip read the sale as
        a total loss (`pnl = -start_value`) while the response claimed to be complete:
        attribution adds the ids to its `unpriced` set, XIRR to `last_xirr_unpriced`,
        the timeline flags the close day, and closed positions and the tax report
        each carry a warning.

        ``exclude_accounts`` is passed only by the tax report, and it must be
        passed there: the fallback and the authoritative trade path answer the
        *same* question, so excluding a tax-exempt account from one and not the
        other would make `realized_source` change what the figure covers as well as
        where it came from. The portfolio's own realized totals blend, as they
        should — that is a return figure, not a tax one.

        ``preloaded`` (see `AttributionInputs`) filters the closed lots out of the lots
        already read instead of querying them; never combined with
        ``exclude_accounts``, which only the tax report passes and which queries.
        """
        # Reset before the early return too: per-run state, like `last_snapshot_skipped`.
        self.last_realized_skipped = {}
        if preloaded is not None and not exclude_accounts:
            # The query's own conditions, on the lots already in hand. `== False`
            # rather than `not`, to keep SQL's reading of a NULL flag.
            closed_lots = sorted(
                (
                    (lot, security)
                    for lot, security in preloaded.taxlots_with_securities
                    if lot.is_open == False  # noqa: E712
                    and lot.close_date is not None
                    and (start is None or lot.close_date >= start)
                    and (end is None or lot.close_date <= end)
                ),
                key=lambda pair: pair[0].close_date,
            )
        else:
            preloaded = None
            conditions = [TaxLot.is_open == False, TaxLot.close_date.isnot(None)]
            if exclude_accounts:
                conditions.append(Security.account.notin_(tuple(exclude_accounts)))
            if start is not None:
                conditions.append(TaxLot.close_date >= start)
            if end is not None:
                conditions.append(TaxLot.close_date <= end)

            result = await self.db.execute(
                select(TaxLot, Security)
                .join(Security, TaxLot.security_id == Security.id)
                .where(and_(*conditions))
                .order_by(TaxLot.close_date.asc())
            )
            closed_lots = result.all()
        if not closed_lots:
            return []

        close_dates = [lot.close_date for lot, _ in closed_lots]
        unique_securities = {security for _, security in closed_lots}
        price_cache, price_currency_cache = await self._preload_market_prices(
            unique_securities, min(close_dates), max(close_dates), preloaded=preloaded
        )
        exchange_rate_cache = await self._preload_exchange_rates(
            unique_securities, min(close_dates), max(close_dates),
            price_currency_cache=price_currency_cache, preloaded=preloaded,
        )

        rows: List[Dict] = []
        skipped: Dict[int, Set[date]] = {}
        for lot, security in closed_lots:
            price = self._get_market_price_with_fallback(
                security.id, lot.close_date, price_cache
            )
            if price is None:
                logger.warning(
                    f"Skipping realized calc for closed lot {lot.id} "
                    f"({security.symbol}): no price near {lot.close_date}"
                )
                skipped.setdefault(security.id, set()).add(lot.close_date)
                continue

            price_currency = price_currency_cache.get(security.id, security.currency)
            if price_currency == "EUR":
                fx_rate = Decimal("1")
            else:
                fx_rate = self._get_exchange_rate_with_fallback(
                    price_currency, lot.close_date, exchange_rate_cache
                )
                if fx_rate is None:
                    logger.warning(
                        f"Skipping realized calc for closed lot {lot.id} "
                        f"({security.symbol}): no FX rate for {price_currency} near {lot.close_date}"
                    )
                    skipped.setdefault(security.id, set()).add(lot.close_date)
                    continue

            proceeds_local = lot.quantity * price
            proceeds_eur = proceeds_local * fx_rate
            rows.append({
                "security_id": security.id,
                "isin": security.isin,
                "symbol": security.symbol,
                "close_date": lot.close_date,
                "open_date": lot.open_date,
                "quantity": lot.quantity,
                "proceeds": base_fx.convert(proceeds_eur, lot.close_date),
                "cost_basis": base_fx.convert(lot.cost_basis_eur, lot.open_date),
                # In the price currency, for the price/FX split in `attribution_rows`.
                "proceeds_local": proceeds_local,
                "price_currency": price_currency,
            })
        self.last_realized_skipped = skipped
        return rows

    async def get_positions_breakdown(self) -> List[Dict]:
        """
        Get breakdown of all current positions by security.
        """
        today = utc_today()

        # Get all open taxlots grouped by security
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .where(TaxLot.is_open == True)
            .order_by(Security.symbol.asc())
        )
        taxlots_with_securities = result.all()

        # Pre-load all market prices and exchange rates
        unique_securities = {security for _, security in taxlots_with_securities}
        price_cache, price_currency_cache = await self._preload_market_prices(unique_securities, today, today)
        exchange_rate_cache = await self._preload_exchange_rates(unique_securities, today, today, price_currency_cache=price_currency_cache)
        base_fx = await self._load_base_fx()

        # Pre-load analyst ratings
        from app.repositories.analyst_rating_repository import AnalystRatingRepository
        rating_repo = AnalystRatingRepository(self.db)
        all_ratings = await rating_repo.get_all()
        ratings_by_security = {rating.security_id: rating for rating in all_ratings}

        # Group by security
        positions = {}
        securities_by_id = {}

        for taxlot, security in taxlots_with_securities:
            if security.id not in positions:
                positions[security.id] = {
                    "security_id": security.id,
                    "symbol": security.symbol,
                    "description": security.description,
                    "isin": security.isin,
                    "currency": security.currency,
                    "exchange": security.exchange,
                    "account": security.account,
                    "price_source": security.price_source,
                    "quantity": Decimal("0.0"),
                    "cost_basis_eur": Decimal("0.0"),
                    "taxlots": []
                }
                securities_by_id[security.id] = security

            lot_cost_base = base_fx.convert(taxlot.cost_basis_eur, taxlot.open_date)
            positions[security.id]["quantity"] += taxlot.quantity
            positions[security.id]["cost_basis_eur"] += lot_cost_base
            positions[security.id]["taxlots"].append({
                "open_date": taxlot.open_date.isoformat(),
                "quantity": float(taxlot.quantity),
                "cost_basis": float(taxlot.cost_basis),
                "cost_basis_eur": float(lot_cost_base)
            })

        # Calculate market values using cached data with fallback
        positions_list = []
        for security_id, position in positions.items():
            security = securities_by_id[security_id]

            # Get latest market price with forward-fill fallback
            market_price = self._get_market_price_with_fallback(
                security.id, today, price_cache
            )

            if market_price:
                market_value = position["quantity"] * market_price

                # Use actual price currency if available
                price_currency = price_currency_cache.get(security_id, security.currency)

                # Convert to EUR with forward-fill fallback
                if price_currency != "EUR":
                    rate = self._get_exchange_rate_with_fallback(
                        price_currency, today, exchange_rate_cache
                    )
                    if rate:
                        market_value_eur = market_value * rate
                    else:
                        logger.warning(
                            f"No exchange rate for {price_currency} on {today}, "
                            f"cannot calculate market value for {security.symbol}"
                        )
                        market_value_eur = Decimal("0.0")
                else:
                    market_value_eur = market_value

                position["market_value_eur"] = float(base_fx.convert(market_value_eur, today))
                position["market_price"] = float(market_price)
            else:
                logger.warning(
                    f"No market price for {security.symbol} on {today}, "
                    f"setting market value to 0"
                )
                position["market_value_eur"] = 0.0
                position["market_price"] = None

            # Calculate gains
            cost_basis = position["cost_basis_eur"]
            market_value = Decimal(str(position["market_value_eur"]))
            gain_loss = market_value - cost_basis

            position["gain_loss_eur"] = float(gain_loss)
            # None on a zero cost basis (a stock distribution, a bonus issue): any gain
            # on nothing is not +0.00%.
            position["gain_loss_percent"] = share_pct(gain_loss, cost_basis)

            # Convert Decimal to float for JSON serialization
            position["quantity"] = float(position["quantity"])
            position["cost_basis_eur"] = float(position["cost_basis_eur"])

            # Add analyst rating if available. A zero-count row is the stored answer
            # "no analysts cover this" (analyst_rating_service.no_rating): absent here.
            rating = ratings_by_security.get(security_id)
            if rating and rating.total_ratings:
                position["analyst_rating"] = {
                    "strong_buy": rating.strong_buy,
                    "buy": rating.buy,
                    "hold": rating.hold,
                    "sell": rating.sell,
                    "strong_sell": rating.strong_sell,
                    "total_ratings": rating.total_ratings,
                    "consensus": rating.consensus,
                    "last_updated": rating.last_updated.isoformat()
                }
            else:
                position["analyst_rating"] = None

            positions_list.append(position)

        # Sort by market value (largest first)
        positions_list.sort(key=lambda x: x["market_value_eur"], reverse=True)

        return positions_list

    async def calculate_xirr(
        self,
        start_date: date,
        end_date: date
    ) -> Tuple[Optional[float], int, date, date, str]:
        """
        Calculate XIRR (money-weighted annualized return) for the portfolio.

        Cash flows:
        - Negative: portfolio market value on start_date (money already invested)
        - Negative: each tax lot opened in (start_date, end_date] at its cost_basis_eur
        - Positive: proceeds of each lot closed inside the window (market price at
          close date — same pricing as the valuations, so a rotation nets to ~zero
          instead of counting the re-buy as fresh money and crushing the return)
        - Positive: net dividends received in the window (era-spliced sources)
        - Positive: portfolio market value on end_date (terminal value)

        Returns: (return_pct or None, num_cash_flows, eff_start, eff_end, method)
        where method is "xirr" (annualized) or "simple_period" (<30 days — a raw
        period return, which the UI must not label as annual).

        **Completeness rides on `self.last_xirr_unpriced`, not on the tuple.** Both
        endpoint valuations can be partial, and this is the last member of the
        `unpriced_holdings` family to learn it: the timeline, `/summary` and
        `/attribution` all report their own incompleteness, and a return computed from
        an incomplete terminal value was still being served as a measurement. The
        purchase outflows come from `taxlots` and are unconditional, so an unpriced
        holding drops out of the terminal inflow while its cost stays in the flow
        list, and the return reads LOW — or high, when it is the *start* that is short.

        Reported rather than excluded, unlike `/attribution`: dropping a security from
        the valuations while keeping its purchases as flows is strictly worse than
        saying the valuation is partial, which is the choice the timeline and the
        summary card both make.
        """
        import pyxirr

        # Reset on entry, not only in __init__: a caller measuring two windows off one
        # service would otherwise read the first window's completeness against the
        # second window's number. Same reason `last_snapshot_skipped` resets on its
        # early-return path.
        self.last_xirr_unpriced = 0

        # Get ALL taxlots (open + closed) for correct historical XIRR
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .order_by(TaxLot.open_date.asc())
        )
        taxlots_with_securities = result.all()

        if not taxlots_with_securities:
            return None, 0, start_date, end_date, "xirr"

        # Pre-load caches for start and end dates
        unique_securities = {security for _, security in taxlots_with_securities}
        price_cache, price_currency_cache = await self._preload_market_prices(unique_securities, start_date, end_date)
        exchange_rate_cache = await self._preload_exchange_rates(unique_securities, start_date, end_date, price_currency_cache=price_currency_cache)
        base_fx = await self._load_base_fx()

        # Find the effective end date: latest date with actual market prices
        # This handles stale price data (e.g., prices only go up to Feb 2 but end_date is Feb 27)
        effective_end_date = self._find_latest_price_date(end_date, price_cache)
        if effective_end_date is None or effective_end_date <= start_date:
            logger.warning(f"No usable price data found near {end_date}")
            return None, 0, start_date, end_date, "xirr"

        # Similarly find effective start date
        effective_start_date = self._find_latest_price_date(start_date, price_cache)
        if effective_start_date is None:
            effective_start_date = start_date

        # Get portfolio market values on effective start and end dates
        position_start = await self._load_position_start_dates()
        unpriced_log = UnpricedDayLog()
        start_value = self._calculate_daily_value(
            effective_start_date, taxlots_with_securities, price_cache, exchange_rate_cache,
            price_currency_cache=price_currency_cache, base_fx=base_fx,
            position_start=position_start, unpriced_log=unpriced_log,
        )
        end_value = self._calculate_daily_value(
            effective_end_date, taxlots_with_securities, price_cache, exchange_rate_cache,
            price_currency_cache=price_currency_cache, base_fx=base_fx,
            position_start=position_start, unpriced_log=unpriced_log,
        )
        unpriced_log.flush(f"XIRR {effective_start_date}..{effective_end_date}")

        start_mv = start_value["market_value_eur"]
        end_mv = end_value["market_value_eur"]

        # The count at the WORSE endpoint, not a union of the two: the endpoints are
        # separate valuations and `_calculate_daily_value` returns a count rather than
        # the set of ids, so there is nothing to union. Either endpoint being short is
        # enough to make the return an understatement, which is all the caller needs.
        self.last_xirr_unpriced = max(
            start_value["unpriced_holdings"], end_value["unpriced_holdings"]
        )

        # Build cash flows
        dates = []
        amounts = []

        # Initial outflow: portfolio value at effective start date
        if start_mv > 0:
            dates.append(effective_start_date)
            amounts.append(-start_mv)

        # Intermediate outflows: tax lots opened during (start_date, effective_end_date]
        for taxlot, security in taxlots_with_securities:
            if effective_start_date < taxlot.open_date <= effective_end_date:
                dates.append(taxlot.open_date)
                amounts.append(-float(base_fx.convert(taxlot.cost_basis_eur, taxlot.open_date)))

        # Inflows: capital released by sales inside the window, priced like the
        # valuations (market price at close date). Window is (start, end], matching
        # exclude-on-close: a lot sold ON the start date belongs to the prior
        # period (its value never entered start_mv), while one sold ON the end
        # date yields proceeds here precisely because end_mv no longer carries it.
        realized = await self.realized_rows_from_closed_lots(
            base_fx,
            start=effective_start_date + timedelta(days=1),
            end=effective_end_date,
        )
        for row in realized:
            dates.append(row["close_date"])
            amounts.append(float(row["proceeds"]))
        # A sale that could not be priced is a missing inflow, so the return reads low
        # exactly as it does for an unpriced terminal value. Added to the endpoint count
        # rather than unioned for the reason given above — there are no ids to union.
        self.last_xirr_unpriced += len(self.last_realized_skipped)

        # Inflows: net dividends received in the window. Era-spliced exactly like
        # the dividend views, so the two sources never double-count a payment.
        from app.repositories.dividend_repository import DividendRepository
        from app.services.dividend_service import DividendService
        raw_dividends = await DividendRepository(self.db).get_computed_dividends()
        payments, _ = DividendService._splice_by_era(raw_dividends)
        # Via the shared helpers, not the columns: rows predating the
        # withholding-fields migration carry gross with a NULL net, and keying on
        # net alone silently dropped that income here. An estimate's net is its gross
        # less the ladder withholding (`IncomeSplit`, issue #61) — what reached the
        # account, as on the Dividends tab.
        income = await DividendService(self.db).income_split(raw_dividends)
        for p in payments:
            on_date = p.pay_date or p.ex_date
            if effective_start_date < on_date <= effective_end_date \
                    and DividendService._is_income(p):
                dates.append(on_date)
                amounts.append(float(base_fx.convert(income.net(p), on_date)))

        # Terminal inflow: portfolio value at effective end date
        if end_mv > 0:
            dates.append(effective_end_date)
            amounts.append(end_mv)

        num_cash_flows = len(dates)

        # XIRR is well-posed as soon as money goes in and value comes out. A
        # window may legitimately start at zero (before the first purchase) or
        # end at zero (fully liquidated — the sale proceeds carry the return);
        # what it needs is a sign change, not positive valuations at both ends.
        if not (any(a < 0 for a in amounts) and any(a > 0 for a in amounts)):
            return None, num_cash_flows, effective_start_date, effective_end_date, "xirr"

        # Very short periods (< 30 days) would annualize a small move into an
        # absurd figure, so return a plain period return — and say so via the
        # method field instead of letting the UI label it "annual".
        days_diff = (effective_end_date - effective_start_date).days
        if days_diff < 30:
            total_out = -sum(a for a in amounts if a < 0)
            total_in = sum(a for a in amounts if a > 0)
            if total_out > 0:
                simple_return = (total_in / total_out - 1) * 100
                return simple_return, num_cash_flows, effective_start_date, effective_end_date, "simple_period"
            return None, num_cash_flows, effective_start_date, effective_end_date, "simple_period"

        try:
            xirr_result = pyxirr.xirr(dates, amounts)
            if xirr_result is None:
                logger.warning("XIRR calculation did not converge")
                return None, num_cash_flows, effective_start_date, effective_end_date, "xirr"
            return xirr_result * 100, num_cash_flows, effective_start_date, effective_end_date, "xirr"
        except Exception as e:
            logger.warning(f"XIRR calculation failed: {e}")
            return None, num_cash_flows, effective_start_date, effective_end_date, "xirr"

    async def attribution_rows(
        self,
        start_date: date,
        end_date: date,
        preloaded: Optional[AttributionInputs] = None,
    ) -> Dict:
        """
        The per-security window arithmetic behind `/api/portfolio/attribution`, kept as
        Decimals and in BOTH the base currency and each security's own price currency.

        `get_performance_attribution` presents it; `PerformanceAnalyticsService` splits
        it into price and FX effects and folds it through the look-through. One
        implementation on purpose — the return decomposition and the per-security chart
        must agree about every security's gain to the cent, and two loops over the same
        lots is how such figures stop agreeing (CLAUDE.md, *two implementations*).

        Returns::

            {
              "empty": bool,                    # nothing to attribute (no lots, or no prices)
              "effective_start": date, "effective_end": date,
              "base_fx": BaseFx,
              "securities": {sid: Security},
              "unpriced": set(sid),             # unvaluable at an endpoint, or a sale in
                                                # the window whose proceeds could not be priced
              "rows": {sid: {
                  "price_currency": str,
                  "start_mv_base", "end_mv_base",           # Decimal, base currency
                  "new_investment_base", "disposals_base",
                  "pnl_base",                               # end − start + disposals − new
                  "pnl_local",                              # same, in price currency, or None
                  "fx_end_to_base",                         # price ccy → base at effective_end, or None
                  "price_effect_base", "fx_effect_base",    # Decimal or None (see below)
              }},
            }

        The local-currency leg is None — never 0 — when any of its inputs is missing:
        a lot booked in a currency other than the price currency with no FX rate on
        its open date, a closed lot whose price history is in a different currency,
        or no rate for the price currency at the window end. A zero there would claim
        the whole gain was FX.

        **Price effect is the local-currency gain converted at the window-END rate;
        the FX effect is the remainder.** So "price" answers "what did the holdings
        earn in their own currency, worth today", and "FX" is what the base currency's
        move against them added or took away. Both are conventions; this one makes a
        security that did not move in its own currency contribute a price effect of
        exactly zero, which is the reading a chart viewer expects.

        `preloaded` is what a caller asking for several windows read once
        (`load_attribution_inputs`); each window's figures are the same either way.
        """
        empty = {
            "empty": True,
            "effective_start": start_date,
            "effective_end": end_date,
            "base_fx": None,
            "securities": {},
            "unpriced": set(),
            "rows": {},
        }
        # Get ALL taxlots (open + closed) for correct historical attribution
        if preloaded is not None:
            taxlots_with_securities = preloaded.taxlots_with_securities
        else:
            taxlots_with_securities = await self._all_lots_with_securities()
        if not taxlots_with_securities:
            return empty

        # Pre-load caches
        unique_securities = {security for _, security in taxlots_with_securities}
        price_cache, price_currency_cache = await self._preload_market_prices(
            unique_securities, start_date, end_date, preloaded=preloaded
        )
        exchange_rate_cache = await self._preload_exchange_rates(
            unique_securities, start_date, end_date,
            price_currency_cache=price_currency_cache, preloaded=preloaded,
        )
        base_fx = preloaded.base_fx if preloaded is not None else await self._load_base_fx()
        empty["base_fx"] = base_fx

        # Find effective dates with actual price data
        effective_end = self._find_latest_price_date(end_date, price_cache)
        if effective_end is None or effective_end <= start_date:
            return empty

        effective_start = self._find_latest_price_date(start_date, price_cache)
        if effective_start is None:
            effective_start = start_date

        ONE = Decimal("1")

        def eur_rate(currency: str, on_date: date) -> Optional[Decimal]:
            """`currency` → EUR on `on_date`, 1 for EUR itself, None when unavailable."""
            if currency == "EUR":
                return ONE
            return self._get_exchange_rate_with_fallback(currency, on_date, exchange_rate_cache)

        # Group tax lots by security
        security_map: Dict[int, Dict] = {}
        securities_by_id: Dict[int, Security] = {}

        # Securities held at an endpoint that could not be valued there. A SET of ids
        # rather than a counter, for the same reason `_calculate_daily_value` uses one:
        # this walks tax LOTS, so incrementing would report 110 for a holding split
        # across 110 lots.
        unpriced_securities: set = set()

        for taxlot, security in taxlots_with_securities:
            sid = security.id
            if sid not in security_map:
                security_map[sid] = {
                    "price_currency": price_currency_cache.get(sid, security.currency),
                    "start_market_value": Decimal("0.0"),   # EUR
                    "end_market_value": Decimal("0.0"),     # EUR
                    "start_mv_local": Decimal("0.0"),
                    "end_mv_local": Decimal("0.0"),
                    "new_investment": Decimal("0.0"),       # base
                    "new_investment_local": Decimal("0.0"),
                    "local_complete": True,
                }
                securities_by_id[sid] = security
            entry = security_map[sid]
            ccy = entry["price_currency"]

            # Returns None, never 0.0, when the holding cannot be valued. A zero is not
            # a small error on this endpoint: `value_change = end_mv - start_mv`, so an
            # unvaluable END makes a still-held position read as `-start_value` — the
            # exact shape the disposal term was added to fix for sales, arriving by the
            # other route and never covered. An unvaluable START is the mirror image and
            # fabricates a gain. Either then renders as the largest bar on a
            # per-security chart, which is the most legible place in the app to publish
            # a wrong number.
            #
            # Two distinct causes, which is why `price is None` alone is not the test:
            # no cached price, or a price whose currency has no FX rate. `rebalance.ts`
            # learned the same asymmetry the same way.
            def valued(qty, price, target_date) -> Optional[Tuple[Decimal, Decimal]]:
                """(EUR value, local value) or None."""
                if price is None:
                    return None
                local = qty * price
                rate = eur_rate(ccy, target_date)
                if rate is None:
                    return None
                return local * rate, local

            # Tax lot contributes to start value if held at effective_start's close
            # (opened on or before it, exclude-on-close)
            if lot_held_at(taxlot, effective_start):
                price = self._get_market_price_with_fallback(sid, effective_start, price_cache)
                pair = valued(taxlot.quantity, price, effective_start)
                if pair is None:
                    unpriced_securities.add(sid)
                else:
                    entry["start_market_value"] += pair[0]
                    entry["start_mv_local"] += pair[1]

            # Tax lot contributes to end value if held at effective_end's close
            #
            # A lot held at NEITHER date never reaches `valued`, so a fully-sold
            # position keeps its legitimate zero end value and is never confused with
            # one that simply could not be priced.
            if lot_held_at(taxlot, effective_end):
                price = self._get_market_price_with_fallback(sid, effective_end, price_cache)
                pair = valued(taxlot.quantity, price, effective_end)
                if pair is None:
                    unpriced_securities.add(sid)
                else:
                    entry["end_market_value"] += pair[0]
                    entry["end_mv_local"] += pair[1]

            # New investment: opened during (effective_start, effective_end] (base, at open_date)
            if effective_start < taxlot.open_date <= effective_end:
                entry["new_investment"] += base_fx.convert(taxlot.cost_basis_eur, taxlot.open_date)
                # The lot's own cost is exact when it was booked in the price currency
                # (the normal case); otherwise it is re-expressed through EUR at the
                # open date, and a missing rate leaves the local leg unknown.
                if taxlot.currency == ccy and taxlot.cost_basis is not None:
                    entry["new_investment_local"] += taxlot.cost_basis
                else:
                    rate = eur_rate(ccy, taxlot.open_date)
                    if rate is None or rate == 0:
                        entry["local_complete"] = False
                    else:
                        entry["new_investment_local"] += taxlot.cost_basis_eur / rate

        # Capital returned by sales inside the window, priced like the valuations.
        # Window is (start, end] under exclude-on-close — mirrors calculate_xirr;
        # change both together if the close-date convention ever moves again.
        disposals_by_sec: Dict[int, Decimal] = {}
        disposals_local_by_sec: Dict[int, Decimal] = {}
        for r in await self.realized_rows_from_closed_lots(
            base_fx, start=effective_start + timedelta(days=1), end=effective_end,
            preloaded=preloaded,
        ):
            sid = r["security_id"]
            disposals_by_sec[sid] = disposals_by_sec.get(sid, Decimal("0")) + r["proceeds"]
            entry = security_map.get(sid)
            if entry is None:
                continue
            if r["price_currency"] == entry["price_currency"]:
                disposals_local_by_sec[sid] = (
                    disposals_local_by_sec.get(sid, Decimal("0")) + r["proceeds_local"]
                )
            else:
                # A mixed-currency price history (a repair state) — the base figure is
                # still right, the local split is not knowable.
                entry["local_complete"] = False
        # A sale inside the window is held at neither endpoint, so the endpoint checks
        # above can never see one whose proceeds could not be priced — and without its
        # disposal it reads `pnl = -start_value`. Excluded from both sides and counted.
        unpriced_securities |= set(self.last_realized_skipped)

        rows: Dict[int, Dict] = {}
        for sid, entry in security_map.items():
            start_b = base_fx.convert(entry["start_market_value"], effective_start)
            end_b = base_fx.convert(entry["end_market_value"], effective_end)
            new_b = entry["new_investment"]
            disp_b = disposals_by_sec.get(sid, Decimal("0"))
            pnl_b = end_b - start_b + disp_b - new_b

            ccy = entry["price_currency"]
            end_rate = eur_rate(ccy, effective_end)
            fx_end_to_base = (
                base_fx.convert(end_rate, effective_end) if end_rate is not None else None
            )
            pnl_local: Optional[Decimal] = None
            price_effect: Optional[Decimal] = None
            fx_effect: Optional[Decimal] = None
            if entry["local_complete"] and fx_end_to_base is not None:
                pnl_local = (
                    entry["end_mv_local"] - entry["start_mv_local"]
                    + disposals_local_by_sec.get(sid, Decimal("0"))
                    - entry["new_investment_local"]
                )
                price_effect = pnl_local * fx_end_to_base
                fx_effect = pnl_b - price_effect

            rows[sid] = {
                "price_currency": ccy,
                "start_mv_base": start_b,
                "end_mv_base": end_b,
                "new_investment_base": new_b,
                "disposals_base": disp_b,
                "pnl_base": pnl_b,
                "pnl_local": pnl_local,
                "fx_end_to_base": fx_end_to_base,
                "price_effect_base": price_effect,
                "fx_effect_base": fx_effect,
            }

        return {
            "empty": False,
            "effective_start": effective_start,
            "effective_end": effective_end,
            "base_fx": base_fx,
            "securities": securities_by_id,
            "unpriced": unpriced_securities,
            "rows": rows,
        }

    async def get_performance_attribution(
        self,
        start_date: date,
        end_date: date
    ) -> Dict:
        """
        Calculate per-security P&L attribution over a time period.

        For each security, computes:
        - Market value at start and end dates
        - New investment during the period (tax lots opened)
        - Disposal proceeds during the period (tax lots closed, priced at close date)
        - Pure P&L contribution = value_change + disposals - new_investment
        - Contribution % of total portfolio P&L
        - The price / FX split of that contribution (see `attribution_rows`)

        Without the disposal term a position sold mid-period contributed 0 to the
        end value while its full start value stayed on the books, so it read as
        pnl = -start_value — a CHF 40k position sold at a profit showed as a
        CHF -40k contributor and corrupted every contribution_percent.
        """
        computed = await self.attribution_rows(start_date, end_date)
        if computed["empty"]:
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "total_pnl_eur": 0.0,
                "unpriced_holdings": 0,
                "attributions": []
            }
        effective_start = computed["effective_start"]
        effective_end = computed["effective_end"]

        # Excluded from BOTH sides, exactly as the forward yield excludes an unpriced
        # holding: leaving one in contributes a fabricated `-start_value` to total P&L
        # and takes a 0% weight, which also inflates every other security's weight
        # against a denominator its own value is missing from.
        priced = {
            sid: r for sid, r in computed["rows"].items() if sid not in computed["unpriced"]
        }

        total_end_mv = sum((r["end_mv_base"] for r in priced.values()), Decimal("0"))
        attributions = []

        for sid, r in priced.items():
            sec = computed["securities"][sid]
            start_mv = float(r["start_mv_base"])
            end_mv = float(r["end_mv_base"])
            new_inv = float(r["new_investment_base"])
            disposal = float(r["disposals_base"])
            value_change = end_mv - start_mv
            pnl = value_change + disposal - new_inv

            attributions.append({
                "security_id": sid,
                "symbol": sec.symbol,
                "description": sec.description or sec.symbol,
                "start_market_value_eur": round(start_mv, 2),
                "end_market_value_eur": round(end_mv, 2),
                "new_investment_eur": round(new_inv, 2),
                "disposal_proceeds_eur": round(disposal, 2),
                "value_change_eur": round(value_change, 2),
                "pnl_contribution_eur": round(pnl, 2),
                "contribution_percent": None,  # set below
                # None when everything attributed ends the window at zero: a share of
                # nothing is absent, not 0% (`share_pct`).
                "weight_percent": share_pct(r["end_mv_base"], total_end_mv),
                "price_currency": r["price_currency"],
                # None when the local-currency leg could not be built — absent, not 0.
                "price_effect_eur": (
                    round(float(r["price_effect_base"]), 2)
                    if r["price_effect_base"] is not None else None
                ),
                "fx_effect_eur": (
                    round(float(r["fx_effect_base"]), 2)
                    if r["fx_effect_base"] is not None else None
                ),
            })

        # Summed in Decimal over the published two-decimal figures, so contributions that
        # cancel give an exact zero rather than a float residue that would divide into a
        # huge percentage. With a zero total every share is None, not 0%: a +100 and a
        # -100 would otherwise both read 0.0% (`share_pct`).
        pnl_parts = [Decimal(str(a["pnl_contribution_eur"])) for a in attributions]
        total_pnl_exact = sum(pnl_parts, Decimal("0"))
        total_pnl = float(total_pnl_exact)

        for a, part in zip(attributions, pnl_parts):
            a["contribution_percent"] = share_pct(part, total_pnl_exact)

        # Sort by absolute P&L contribution descending
        attributions.sort(key=lambda a: abs(a["pnl_contribution_eur"]), reverse=True)

        return {
            "start_date": effective_start.isoformat(),
            "end_date": effective_end.isoformat(),
            "total_pnl_eur": round(total_pnl, 2),
            # > 0 means securities were left out because they could not be valued at an
            # endpoint or at a sale inside the window, so total_pnl_eur covers less than
            # the whole book. Same signal
            # and same name as the timeline's and the summary's.
            "unpriced_holdings": len(computed["unpriced"]),
            "attributions": attributions
        }

    def _find_latest_price_date(
        self,
        target_date: date,
        price_cache: Dict,
        max_lookback_days: int = PRICE_LOOKBACK_DAYS
    ) -> Optional[date]:
        """
        Find the latest date on or before target_date that has price data
        for at least one security in the cache. Bounded by PRICE_LOOKBACK_DAYS:
        the search cannot see past what _preload_market_prices fetched.
        """
        for days_back in range(0, max_lookback_days + 1):
            check_date = target_date - timedelta(days=days_back)
            for security_id, dates_dict in price_cache.items():
                if check_date in dates_dict:
                    return check_date
        return None

    async def _preload_market_prices(
        self,
        securities: set,
        start_date: date,
        end_date: date,
        lookback_days: int = PRICE_LOOKBACK_DAYS,
        preloaded: Optional[AttributionInputs] = None,
    ) -> Tuple[Dict, Dict]:
        """
        Pre-load all market prices for securities in date range.

        Extends the date range backwards by lookback_days to support
        forward-fill fallback logic when prices are missing.

        With `preloaded` holding these securities over this range, the same rows are
        cut from it instead of queried (see `AttributionInputs`).

        Returns: (
            {security_id: {date: price}},
            {security_id: currency}  -- actual price currency from DB
        )
        """
        from app.models.market_price import MarketPrice

        security_ids = [s.id for s in securities]

        # Extend start_date backwards to support forward-fill
        extended_start_date = start_date - timedelta(days=lookback_days)

        if (
            preloaded is not None
            and preloaded.security_ids.issuperset(security_ids)
            and preloaded.prices_from <= extended_start_date
            and end_date <= preloaded.prices_to
        ):
            wanted = set(security_ids)
            all_prices = [
                p for p in preloaded.price_rows
                if p.security_id in wanted and extended_start_date <= p.date <= end_date
            ]
        else:
            result = await self.db.execute(
                select(MarketPrice)
                .where(
                    and_(
                        MarketPrice.security_id.in_(security_ids),
                        MarketPrice.date >= extended_start_date,
                        MarketPrice.date <= end_date
                    )
                )
            )
            all_prices = result.scalars().all()

        # Build nested dict: {security_id: {date: price}}
        price_cache = {}
        price_currency_cache = {}
        newest_priced = {}
        mixed_currencies = set()
        for price in all_prices:
            if price.security_id not in price_cache:
                price_cache[price.security_id] = {}
            price_cache[price.security_id][price.date] = price.close_price
            # The currency of the NEWEST row wins, deterministically. The query is
            # unordered, so "whatever row iterated last" used to apply an arbitrary
            # member of a mixed-currency history to the security's entire series.
            chosen = price_currency_cache.get(price.security_id)
            if chosen is not None and chosen != price.currency:
                mixed_currencies.add(price.security_id)
            if (price.security_id not in newest_priced
                    or price.date > newest_priced[price.security_id]):
                newest_priced[price.security_id] = price.date
                price_currency_cache[price.security_id] = price.currency

        # A mixed history is a repair state (a wrong mapping stamped rows in the
        # wrong currency — the SBI failure): say so instead of silently mis-scaling.
        for sid in sorted(mixed_currencies):
            logger.warning(
                f"Security {sid} has mixed price currencies cached; valuing the whole "
                f"series as {price_currency_cache[sid]} (newest row). Purge and refill "
                f"its prices to repair (manage_mappings disable --purge-prices)."
            )

        return price_cache, price_currency_cache

    async def _preload_exchange_rates(
        self,
        securities: set,
        start_date: date,
        end_date: date,
        lookback_days: int = FX_MAX_CARRY_DAYS,
        price_currency_cache: Optional[Dict] = None,
        preloaded: Optional[AttributionInputs] = None,
    ) -> Dict:
        """
        Pre-load all exchange rates for currencies in date range.

        Extends the date range backwards by lookback_days to support
        forward-fill fallback logic when rates are missing.

        Args:
            price_currency_cache: Optional {security_id: currency} from _preload_market_prices.
                If provided, also loads FX rates for price currencies (which may differ
                from security.currency for cross-listed securities).

        Returns: {(from_currency, date): rate}
        """
        currencies = {s.currency for s in securities if s.currency != 'EUR'}

        # Also include actual price currencies (may differ from security.currency)
        if price_currency_cache:
            for sec_id, price_curr in price_currency_cache.items():
                if price_curr != 'EUR':
                    currencies.add(price_curr)

        # One query over the window, extended backwards for the forward-fill; the query
        # and the fill are `fx_preload`'s, shared with the benchmark and crypto readers.
        # A `preloaded` load holding this window and these currencies is sliced instead.
        if (
            preloaded is not None
            and preloaded.rate_currencies.issuperset(wanted_currencies(currencies))
            and preloaded.rates_from <= start_date - timedelta(days=lookback_days)
            and end_date <= preloaded.rates_to
        ):
            return slice_eur_rates(
                preloaded.rates, currencies, start_date, end_date, lookback_days
            )
        return await preload_eur_rates(
            self.db, currencies, start_date, end_date, lookback_days
        )

    def _get_market_price_with_fallback(
        self,
        security_id: int,
        target_date: date,
        price_cache: Dict,
        max_lookback_days: int = PRICE_LOOKBACK_DAYS
    ) -> Optional[Decimal]:
        """
        Get market price for a date with forward-fill fallback.

        If price is missing for target_date, looks back up to max_lookback_days
        to find the most recent available price (carry-forward strategy).

        Args:
            security_id: ID of the security
            target_date: Date to get price for
            price_cache: Pre-loaded price cache {security_id: {date: price}}
            max_lookback_days: Maximum days to look back (default: PRICE_LOOKBACK_DAYS)

        Returns:
            Price as Decimal, or None if no price found within lookback window
        """
        # Try exact date first
        market_price = price_cache.get(security_id, {}).get(target_date)
        if market_price:
            return market_price

        # Try previous days (up to max_lookback_days)
        for days_back in range(1, max_lookback_days + 1):
            fallback_date = target_date - timedelta(days=days_back)
            market_price = price_cache.get(security_id, {}).get(fallback_date)
            if market_price:
                logger.debug(
                    f"Using {days_back}-day-old price for security {security_id} "
                    f"on {target_date}: €{market_price} (from {fallback_date})"
                )
                return market_price

        # No price within the lookback window. DEBUG, not WARNING: this is a lookup
        # called per security per day by the valuation walks, and every caller reports a
        # miss itself — the walks once per security (`UnpricedDayLog`), the responses
        # as `unpriced_holdings` (GitHub #64).
        logger.debug(
            f"No price found for security {security_id} on {target_date} "
            f"(checked {max_lookback_days} days back)"
        )
        return None

    def _get_exchange_rate_with_fallback(
        self,
        from_currency: str,
        target_date: date,
        exchange_rate_cache: Dict,
        max_lookback_days: int = FX_MAX_CARRY_DAYS
    ) -> Optional[Decimal]:
        """
        Get exchange rate for a date with forward-fill fallback.

        If rate is missing for target_date, looks back up to max_lookback_days
        to find the most recent available rate (carry-forward strategy).

        Args:
            from_currency: Currency to convert from (e.g., 'USD')
            target_date: Date to get rate for
            exchange_rate_cache: Pre-loaded rate cache {(currency, date): rate}
            max_lookback_days: Maximum days to look back (default: FX_MAX_CARRY_DAYS)

        Returns:
            Exchange rate as Decimal, or None if no rate found within lookback window
        """
        rate = eur_rate_on(exchange_rate_cache, from_currency, target_date, max_lookback_days)
        if rate is None:
            # DEBUG for the reason `_get_market_price_with_fallback` gives.
            logger.debug(
                f"No exchange rate found for {from_currency} on {target_date} "
                f"(checked {max_lookback_days} days back)"
            )
        return rate

    def _calculate_daily_value(
        self,
        target_date: date,
        taxlots_with_securities: List,
        price_cache: Dict,
        exchange_rate_cache: Dict,
        price_currency_cache: Optional[Dict] = None,
        base_fx: Optional[BaseFx] = None,
        position_start: Optional[Dict[int, date]] = None,
        unpriced_log: Optional[UnpricedDayLog] = None,
    ) -> Dict:
        """
        Calculate portfolio value for a specific date using cached data.
        Includes both open and closed lots — filters by open_date/close_date window.

        Values are returned in the base (display) currency: cost basis is converted
        at each lot's open_date, market value at target_date. When base is EUR this
        is a pass-through. Output keys keep the historical *_eur suffix.

        ``unpriced_log`` lets a caller valuing several dates for one request log each
        unpriced security once across all of them, and flush it itself; without one,
        this call logs its own (`UnpricedDayLog`).
        """
        own_log = unpriced_log is None
        unpriced_log = UnpricedDayLog() if own_log else unpriced_log
        base_fx = base_fx or BaseFx("EUR", {})
        position_start = position_start or {}
        total_cost_basis = Decimal("0.0")      # in base currency
        total_market_value_eur = Decimal("0.0")  # in EUR, converted once at the end
        # Securities whose cost is counted but whose value could not be resolved, kept in
        # lockstep with _calculate_timeline_swept — which must stay numerically identical
        # to this function, including what it reports about its own completeness.
        #
        # A SET of ids, not a counter: this function walks tax LOTS while the swept one
        # walks securities, so incrementing here would report 110 for a holding split
        # across 110 lots and break the equivalence the two are pinned to.
        unpriced_securities: set = set()

        for taxlot, security in taxlots_with_securities:
            # Only lots opened on or before this date, and — a lot sold on D is no
            # longer held at D's close — excluded ON the close date. This is what stops
            # a same-day rotation from double-counting the day (old A still active + new
            # B open), keeps Steuerwert and realized gains disjoint at year-end, and
            # matches the benchmark's convention.
            if not lot_held_at(taxlot, target_date):
                continue

            # Cost basis converts at the lot's open_date (stable, FX-independent line)
            total_cost_basis += base_fx.convert(taxlot.cost_basis_eur, taxlot.open_date)

            # The cost above is counted while the value is not: a spun-off position's
            # worth was still inside the parent's close before the action date, and the
            # parent's basis was reduced by exactly this lot's cost. Placed after the cost
            # line and before the price lookup so it is neither valued nor reported as an
            # unpriced holding — kept identical to _calculate_timeline_swept, which the
            # equivalence test pins.
            started = position_start.get(security.id)
            if started is not None and target_date < started:
                continue

            # Get market price with forward-fill fallback
            market_price = self._get_market_price_with_fallback(
                security.id, target_date, price_cache
            )

            if market_price:
                position_value = taxlot.quantity * market_price

                # Use actual price currency if available, fall back to security currency
                price_currency = price_currency_cache.get(security.id, security.currency) if price_currency_cache else security.currency

                # Convert to EUR with forward-fill fallback
                if price_currency != "EUR":
                    rate = self._get_exchange_rate_with_fallback(
                        price_currency, target_date, exchange_rate_cache
                    )
                    if rate:
                        total_market_value_eur += position_value * rate
                    else:
                        unpriced_securities.add(security.id)
                        unpriced_log.note(
                            security.id, security.symbol, target_date,
                            f"no exchange rate for {price_currency}",
                        )
                else:
                    total_market_value_eur += position_value
            else:
                unpriced_securities.add(security.id)
                unpriced_log.note(
                    security.id, security.symbol, target_date, "no market price"
                )

        if own_log:
            unpriced_log.flush(f"Valuation {target_date}")

        # Market value converts at the valuation date
        total_market_value = base_fx.convert(total_market_value_eur, target_date)
        gain_loss = total_market_value - total_cost_basis

        return {
            "date": target_date.isoformat(),
            "cost_basis_eur": float(total_cost_basis),
            "market_value_eur": float(total_market_value),
            "gain_loss_eur": float(gain_loss),
            # None with nothing held at cost (`share_pct`), exactly as the swept timeline.
            "gain_loss_percent": share_pct(gain_loss, total_cost_basis),
            "unpriced_holdings": len(unpriced_securities),
        }
