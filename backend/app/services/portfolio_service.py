"""
Portfolio Service
Calculates cost basis and market value for the portfolio over time.
"""
import bisect
import calendar
from collections import defaultdict
from typing import List, Dict, Optional, Tuple
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.models.taxlot import TaxLot
from app.models.security import Security
from app.models.trade import Trade
from app.services.market_data_service import MarketDataService
from app.services.currency_service import CurrencyService
from app.repositories.app_settings_repository import AppSettingsRepository
from app.repositories.cash_flow_repository import CashFlowRepository
from app.repositories.corporate_action_repository import CorporateActionRepository
from app.services.sync_helper import POSITION_CREATING_ACTIONS

logger = logging.getLogger(__name__)

# Average calendar month, used only to express an elapsed span in months so a
# part-month of history isn't rounded away.
_DAYS_PER_MONTH = 365.25 / 12

# Forward-fill horizon for market prices: _preload_market_prices extends its
# query this many days before the window, and _find_latest_price_date must not
# search deeper — the search cannot see past what the preload fetched.
PRICE_LOOKBACK_DAYS = 14


def _shift_months(from_date: date, months: int) -> date:
    """
    The same day-of-month ``months`` before ``from_date``, clamped to the length
    of the target month (31 May - 3 months -> 28/29 Feb).
    """
    total = from_date.year * 12 + (from_date.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(from_date.day, calendar.monthrange(year, month)[1]))


class BaseFx:
    """
    Converts EUR-denominated amounts into the selected base (display) currency
    at a given date. The whole portfolio pipeline computes values in EUR; this
    applies a single EUR->base factor as a read-time projection.

    - Cost basis is converted at each lot's open_date (so the cost-basis line
      only moves on buys/sells, never with day-to-day FX).
    - Market value is converted at the valuation date.

    When base_currency == 'EUR' this is a no-op (rate 1.0).
    """

    def __init__(self, base_currency: str, rate_cache: Dict[date, Decimal]):
        self.base_currency = base_currency
        self.rate_cache = rate_cache  # {date: EUR->base rate}
        self._sorted_dates = sorted(rate_cache.keys())

    def _rate_on(self, on_date: date) -> Optional[Decimal]:
        rate = self.rate_cache.get(on_date)
        if rate is not None:
            return rate
        if not self._sorted_dates:
            return None
        # Carry-forward: most recent rate on/before on_date
        idx = bisect.bisect_right(self._sorted_dates, on_date)
        if idx > 0:
            return self.rate_cache[self._sorted_dates[idx - 1]]
        # on_date precedes all cached rates: carry the earliest back
        return self.rate_cache[self._sorted_dates[0]]

    def convert(self, amount_eur: Decimal, on_date: date) -> Decimal:
        if self.base_currency == "EUR" or not amount_eur:
            return amount_eur
        rate = self._rate_on(on_date)
        if rate is None:
            # No rate available anywhere: fall back to EUR value rather than zero.
            return amount_eur
        return amount_eur * rate


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

    def __init__(self, db: AsyncSession):
        self.db = db
        self.market_data_service = MarketDataService(db)
        self.currency_service = CurrencyService(db)
        self.last_snapshot_skipped = []
        self.last_xirr_unpriced = 0

    async def get_base_currency(self) -> str:
        """Return the configured base (display) currency."""
        return await AppSettingsRepository(self.db).get_base_currency()

    async def _load_base_fx(self) -> BaseFx:
        """
        Build a BaseFx for the configured base currency, loading EUR->base daily
        rates over the full portfolio history (earliest lot open_date .. today).

        Reads cached ExchangeRate rows; if none exist yet (base just switched and
        backfill was skipped), fetches the whole range once from Frankfurter.
        """
        base_currency = await self.get_base_currency()
        if base_currency == "EUR":
            return BaseFx("EUR", {})

        from app.models.exchange_rate import ExchangeRate

        today = date.today()
        min_open = (await self.db.execute(select(func.min(TaxLot.open_date)))).scalar()
        start = min_open or (today - timedelta(days=365))

        async def load_cache() -> Dict[date, Decimal]:
            rows = (await self.db.execute(
                select(ExchangeRate).where(
                    ExchangeRate.from_currency == "EUR",
                    ExchangeRate.to_currency == base_currency,
                    ExchangeRate.date >= start,
                    ExchangeRate.date <= today,
                )
            )).scalars().all()
            return {r.date: r.rate for r in rows}

        cache = await load_cache()
        if not cache:
            # Safety net: populate EUR->base history once, then reload.
            try:
                await self.currency_service._batch_fetch_rates(
                    from_currency="EUR",
                    target_date=today,
                    to_currency=base_currency,
                    days_back=max((today - start).days, 30),
                )
                cache = await load_cache()
            except Exception as e:
                logger.warning(f"Could not backfill EUR->{base_currency} rates: {e}")

        return BaseFx(base_currency, cache)

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
        """
        actions = await CorporateActionRepository(self.db).get_by_types(
            POSITION_CREATING_ACTIONS
        )
        starts: Dict[int, date] = {}
        for action in actions:
            if action.security_id is None or not action.quantity or action.quantity <= 0:
                continue
            held = (await self.db.execute(
                select(func.sum(TaxLot.quantity)).where(
                    and_(
                        TaxLot.security_id == action.security_id,
                        TaxLot.open_date <= action.action_date,
                        or_(
                            TaxLot.close_date.is_(None),
                            TaxLot.close_date > action.action_date,
                        ),
                    )
                )
            )).scalar() or Decimal("0")
            if held > action.quantity:
                continue
            previous = starts.get(action.security_id)
            if previous is None or action.action_date < previous:
                starts[action.security_id] = action.action_date
        if starts:
            logger.info(f"Position start dates from corporate actions: {starts}")
        return starts

    async def get_portfolio_value_over_time(
        self,
        start_date: date,
        end_date: date
    ) -> List[Dict]:
        """
        Calculate portfolio value (cost basis and market value) for each day in range.
        Uses optimized caching for fast performance.
        Includes both open AND closed tax lots for correct historical values.
        """
        # Get ALL taxlots (open + closed) for historical accuracy
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .order_by(TaxLot.open_date.asc())
        )
        taxlots_with_securities = result.all()

        if not taxlots_with_securities:
            return []

        # Pre-load all data once
        unique_securities = {security for _, security in taxlots_with_securities}
        price_cache, price_currency_cache = await self._preload_market_prices(unique_securities, start_date, end_date)
        exchange_rate_cache = await self._preload_exchange_rates(unique_securities, start_date, end_date, price_currency_cache=price_currency_cache)
        base_fx = await self._load_base_fx()

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
            base_fx, start=start_date + timedelta(days=1), end=end_date
        ):
            d = row["close_date"]
            disposals_by_day[d] = disposals_by_day.get(d, Decimal("0")) + row["proceeds"]

        # One sweep over the whole range instead of a per-day × per-lot loop.
        portfolio_timeline = self._calculate_timeline_swept(
            start_date, end_date, taxlots_with_securities, price_cache,
            exchange_rate_cache, price_currency_cache, base_fx,
            disposals_by_day=disposals_by_day,
            position_start=await self._load_position_start_dates(),
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
        position_start: Optional[Dict[int, date]] = None,
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
        position_start = position_start or {}
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

            if d.weekday() < 5:
                mv_eur = Decimal("0")
                # Holdings whose cost is in `total_cost` but whose value could not be
                # resolved. Counted rather than only logged: a 730-day window over 40
                # securities can emit ~29k of those warnings, which is noise, while the
                # response carried no sign that its market value was a partial sum.
                unpriced = 0
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
                        unpriced += 1
                        sec = securities_by_id[sid]
                        logger.warning(
                            f"Skipping position for security {sid} ({sec.symbol}) on {d}: "
                            f"no market price available"
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
                            unpriced += 1
                            logger.warning(
                                f"Skipping position for security {sid} on {d}: "
                                f"no exchange rate for {price_currency}"
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
                    "gain_loss_percent": float(
                        (gain / total_cost * 100) if total_cost > 0 else 0
                    ),
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
                    # +15% invites no doubt at all.
                    "unpriced_holdings": unpriced,
                })
                pending_flow = Decimal("0")
            d += timedelta(days=1)

        return timeline

    async def get_current_portfolio_summary(self) -> Dict:
        """
        Get current portfolio summary with latest values.
        """
        today = date.today()

        base_fx = await self._load_base_fx()

        # Get all open taxlots with securities
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .where(TaxLot.is_open == True)
        )
        taxlots_with_securities = result.all()

        if not taxlots_with_securities:
            realized = await self.get_realized_totals(base_fx=base_fx)
            return {
                "total_cost_basis_eur": 0.0,
                "total_market_value_eur": 0.0,
                "total_gain_loss_eur": 0.0,
                "total_gain_loss_percent": 0.0,
                "num_positions": 0,
                "unpriced_holdings": 0,
                "base_currency": base_fx.base_currency,
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

        realized = await self.get_realized_totals(base_fx=base_fx)

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
            **realized,
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

        ``as_of`` defaults to today and exists so tests can pin the windows.
        """
        as_of = as_of or date.today()
        base_fx = await self._load_base_fx()

        rows = (await self.db.execute(
            select(TaxLot.open_date, TaxLot.close_date, TaxLot.cost_basis_eur)
        )).all()

        # Each lot contributes one positive leg on its open_date (the deployment)
        # and, once sold, a negative leg on its close_date (capital coming back).
        # Project into the base currency at the date the leg sits on, then
        # everything downstream is plain date arithmetic.
        legs: List[Tuple[date, Decimal]] = []
        first_open: Optional[date] = None

        for open_date, close_date, cost_basis_eur in rows:
            cost = cost_basis_eur or Decimal("0")
            if open_date:
                legs.append((open_date, base_fx.convert(cost, open_date)))
                if first_open is None or open_date < first_open:
                    first_open = open_date
            if close_date:
                legs.append((close_date, -base_fx.convert(cost, close_date)))

        # External cash, if the Flex Query has been set to deliver it. Deposits only:
        # get_deposits() excludes TRANSFER rows, so capital that arrived by broker
        # transfer is never read as a contribution.
        flow_repo = CashFlowRepository(self.db)
        deposits_from = await flow_repo.earliest_deposit_date()
        transfer_in_date = await flow_repo.earliest_transfer_in_date()
        deposit_legs: List[Tuple[date, Decimal]] = [
            (f.flow_date, base_fx.convert(f.amount_eur or Decimal("0"), f.flow_date))
            for f in await flow_repo.get_deposits()
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
        ledger_starts_at = await flow_repo.earliest_flow_date()
        if coverage_from and ledger_starts_at and ledger_starts_at > coverage_from:
            coverage_from = ledger_starts_at

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
        for on_date, amount in legs:
            month = on_date.strftime("%Y-%m")
            monthly_net[month] += amount
            if amount > 0:
                monthly_gross[month] += amount

        monthly = [
            {
                "month": month,
                "net_eur": round(float(monthly_net[month]), 2),
                "deployed_eur": round(float(monthly_gross[month]), 2),
            }
            for month in sorted(monthly_net)
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
            # together they cover the whole window — no clamped divisor needed.
            if coverage_from is None:
                method = "deployed"
                deposits = Decimal("0")
                money_in = deployed
            elif start >= coverage_from:
                method = "deposits"
                deposits = sum(
                    (a for d, a in deposit_legs if start <= d <= as_of), Decimal("0")
                )
                money_in = deposits
            else:
                method = "spliced"
                deposits = sum(
                    (a for d, a in deposit_legs if coverage_from <= d <= as_of),
                    Decimal("0"),
                )
                pre = sum(
                    (a for d, a in legs if start <= d < coverage_from and a > 0),
                    Decimal("0"),
                )
                money_in = pre + deposits

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

    async def _realized_from_trades(self, base_fx: BaseFx) -> Optional[Dict]:
        """
        Compute realized totals from authoritative IBKR SELL trades, or return
        None if no trades have been ingested yet (caller then falls back to the
        market-price approximation over closed lots).

        Each SELL trade already carries IBKR's own FIFO realized P&L
        (fifoPnlRealized) and proceeds in the trade currency; we convert both to
        EUR at the trade date, then project into the base currency.
        """
        trades = (await self.db.execute(select(Trade))).scalars().all()
        # "Some trades exist" is not "realized figures exist": a statement can
        # carry BUYs long before any sale. Guarding on any-trade made a BUY-only
        # table return hard zeros and permanently mask the closed-lot fallback,
        # while the tax report (which picks per-year) showed real gains.
        if not any((t.buy_sell or "").upper() == "SELL" for t in trades):
            return None

        total_proceeds_eur = Decimal("0")
        total_gain_eur = Decimal("0")
        closed_security_ids: set = set()

        for t in trades:
            if (t.buy_sell or "").upper() != "SELL":
                continue
            currency = t.currency or "EUR"
            proceeds = t.proceeds if t.proceeds is not None else Decimal("0")
            gain = t.realized_pnl if t.realized_pnl is not None else Decimal("0")

            try:
                if currency == "EUR":
                    proceeds_eur = proceeds
                    gain_eur = gain
                else:
                    proceeds_eur = await self.currency_service.convert_to_eur(
                        amount=proceeds, from_currency=currency, target_date=t.trade_date
                    )
                    gain_eur = await self.currency_service.convert_to_eur(
                        amount=gain, from_currency=currency, target_date=t.trade_date
                    )
            except ValueError:
                logger.warning(
                    f"Realized: skipping trade {t.ib_key} — no FX for {currency} near {t.trade_date}"
                )
                continue

            total_proceeds_eur += base_fx.convert(proceeds_eur, t.trade_date)
            total_gain_eur += base_fx.convert(gain_eur, t.trade_date)
            if t.security_id is not None:
                closed_security_ids.add(t.security_id)

        return {
            "total_realized_gain_loss_eur": float(total_gain_eur),
            "total_realized_proceeds_eur": float(total_proceeds_eur),
            "total_realized_cost_basis_eur": float(total_proceeds_eur - total_gain_eur),
            "num_closed_positions": len(closed_security_ids),
        }

    async def get_realized_totals(self, base_fx: Optional[BaseFx] = None) -> Dict:
        """
        Aggregate realized gain/loss from closed tax lots.

        Used as the fallback when no SELL trades have been ingested (e.g. lots
        closed before the <Trades> section was enabled): proceeds are approximated
        as quantity × market_price × fx_rate on close_date. Lots that can't
        be priced within the 14-day fallback window are skipped and not counted.

        Values are returned in the base currency (proceeds converted at close_date,
        cost basis at open_date). Output keys keep the *_eur suffix.

        When authoritative IBKR <Trades> have been ingested, realized P&L is taken
        straight from each SELL trade's fifoPnlRealized/proceeds (exact, IBKR's own
        FIFO) instead of the market-price approximation below.
        """
        if base_fx is None:
            base_fx = await self._load_base_fx()

        trade_based = await self._realized_from_trades(base_fx)
        if trade_based is not None:
            return trade_based

        rows = await self.realized_rows_from_closed_lots(base_fx)
        total_proceeds_eur = sum((row["proceeds"] for row in rows), Decimal("0"))
        total_cost_basis_eur = sum((row["cost_basis"] for row in rows), Decimal("0"))

        return {
            "total_realized_gain_loss_eur": float(total_proceeds_eur - total_cost_basis_eur),
            "total_realized_proceeds_eur": float(total_proceeds_eur),
            "total_realized_cost_basis_eur": float(total_cost_basis_eur),
            "num_closed_positions": len({row["security_id"] for row in rows}),
        }

    async def holdings_snapshot_as_of(self, base_fx: BaseFx, on_date: date) -> List[Dict]:
        """
        Per-security holdings as they stood on ``on_date``, valued at that date's prices.

        The Swiss wealth-tax base (Steuerwert) is the **31 December** value, so the tax
        report cannot just use today's positions for a past year. Lot selection uses the
        same window as _calculate_daily_value (opened on or before the date, not closed
        before it) so the tax snapshot and the portfolio timeline agree.

        Securities whose price can't be resolved near ``on_date`` are skipped rather than
        counted at zero — a thin price history degrades the total instead of lying.
        """
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .where(
                and_(
                    TaxLot.open_date <= on_date,
                    # Exclude-on-close, same window as _calculate_daily_value: a
                    # position sold on 31 Dec is not held at year-end — it belongs
                    # to that year's realized gains, not its Steuerwert.
                    or_(TaxLot.close_date.is_(None), TaxLot.close_date > on_date),
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
    ) -> List[Dict]:
        """
        Per-closed-lot realized figures using the market-price approximation
        described in get_realized_totals, optionally restricted to lots closed
        within [start, end].

        Shared by the portfolio realized totals and the tax report's fallback so
        the two views can never disagree about the same closed lots. Lots that
        can't be priced (or converted) are skipped, exactly as before.
        """
        conditions = [TaxLot.is_open == False, TaxLot.close_date.isnot(None)]
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
            unique_securities, min(close_dates), max(close_dates)
        )
        exchange_rate_cache = await self._preload_exchange_rates(
            unique_securities, min(close_dates), max(close_dates),
            price_currency_cache=price_currency_cache,
        )

        rows: List[Dict] = []
        for lot, security in closed_lots:
            price = self._get_market_price_with_fallback(
                security.id, lot.close_date, price_cache
            )
            if price is None:
                logger.warning(
                    f"Skipping realized calc for closed lot {lot.id} "
                    f"({security.symbol}): no price near {lot.close_date}"
                )
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
                    continue

            proceeds_eur = lot.quantity * price * fx_rate
            rows.append({
                "security_id": security.id,
                "symbol": security.symbol,
                "close_date": lot.close_date,
                "quantity": lot.quantity,
                "proceeds": base_fx.convert(proceeds_eur, lot.close_date),
                "cost_basis": base_fx.convert(lot.cost_basis_eur, lot.open_date),
            })
        return rows

    async def get_positions_breakdown(self) -> List[Dict]:
        """
        Get breakdown of all current positions by security.
        """
        today = date.today()

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
            position["gain_loss_percent"] = float(
                (gain_loss / cost_basis * 100) if cost_basis > 0 else 0
            )

            # Convert Decimal to float for JSON serialization
            position["quantity"] = float(position["quantity"])
            position["cost_basis_eur"] = float(position["cost_basis_eur"])

            # Add analyst rating if available
            rating = ratings_by_security.get(security_id)
            if rating:
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
        start_value = self._calculate_daily_value(
            effective_start_date, taxlots_with_securities, price_cache, exchange_rate_cache,
            price_currency_cache=price_currency_cache, base_fx=base_fx,
            position_start=position_start,
        )
        end_value = self._calculate_daily_value(
            effective_end_date, taxlots_with_securities, price_cache, exchange_rate_cache,
            price_currency_cache=price_currency_cache, base_fx=base_fx,
            position_start=position_start,
        )

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

        # Inflows: net dividends received in the window. Era-spliced exactly like
        # the dividend views, so the two sources never double-count a payment.
        from app.repositories.dividend_repository import DividendRepository
        from app.services.dividend_service import DividendService
        payments, _ = DividendService._splice_by_era(
            await DividendRepository(self.db).get_computed_dividends()
        )
        # Via the shared helpers, not the columns: rows predating the
        # withholding-fields migration carry gross with a NULL net, and keying on
        # net alone silently dropped that income here.
        for p in payments:
            on_date = p.pay_date or p.ex_date
            if effective_start_date < on_date <= effective_end_date \
                    and DividendService._is_income(p):
                dates.append(on_date)
                amounts.append(float(base_fx.convert(DividendService._net_eur(p), on_date)))

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

        Without the disposal term a position sold mid-period contributed 0 to the
        end value while its full start value stayed on the books, so it read as
        pnl = -start_value — a CHF 40k position sold at a profit showed as a
        CHF -40k contributor and corrupted every contribution_percent.
        """
        # Get ALL taxlots (open + closed) for correct historical attribution
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .order_by(TaxLot.open_date.asc())
        )
        taxlots_with_securities = result.all()

        if not taxlots_with_securities:
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "total_pnl_eur": 0.0,
                "unpriced_holdings": 0,
                "attributions": []
            }

        # Pre-load caches
        unique_securities = {security for _, security in taxlots_with_securities}
        price_cache, price_currency_cache = await self._preload_market_prices(unique_securities, start_date, end_date)
        exchange_rate_cache = await self._preload_exchange_rates(unique_securities, start_date, end_date, price_currency_cache=price_currency_cache)
        base_fx = await self._load_base_fx()

        # Find effective dates with actual price data
        effective_end = self._find_latest_price_date(end_date, price_cache)
        if effective_end is None or effective_end <= start_date:
            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "total_pnl_eur": 0.0,
                "unpriced_holdings": 0,
                "attributions": []
            }

        effective_start = self._find_latest_price_date(start_date, price_cache)
        if effective_start is None:
            effective_start = start_date

        # Group tax lots by security
        security_map: Dict[int, Dict] = {}
        securities_by_id: Dict[int, Security] = {}

        # Securities held at an endpoint that could not be valued there. A SET of ids
        # rather than a counter, for the same reason `_calculate_daily_value` uses one:
        # this walks tax LOTS, so incrementing would report 110 for a holding split
        # across 110 lots.
        unpriced_securities: set = set()

        for taxlot, security in taxlots_with_securities:
            if security.id not in security_map:
                security_map[security.id] = {
                    "start_market_value": Decimal("0.0"),
                    "end_market_value": Decimal("0.0"),
                    "new_investment": Decimal("0.0"),
                }
                securities_by_id[security.id] = security

            sid = security.id
            entry = security_map[sid]

            # Get FX rate helper — uses actual price currency, not security currency.
            #
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
            def get_eur_value(qty, price, sec, target_date) -> Optional[Decimal]:
                if price is None:
                    return None
                val = qty * price
                price_currency = price_currency_cache.get(sec.id, sec.currency)
                if price_currency != "EUR":
                    rate = self._get_exchange_rate_with_fallback(
                        price_currency, target_date, exchange_rate_cache
                    )
                    return val * rate if rate else None
                return val

            # Tax lot contributes to start value if opened on or before effective_start
            # AND still held at effective_start (exclude-on-close)
            if taxlot.open_date <= effective_start:
                if not (taxlot.close_date and taxlot.close_date <= effective_start):
                    price = self._get_market_price_with_fallback(sid, effective_start, price_cache)
                    value = get_eur_value(taxlot.quantity, price, security, effective_start)
                    if value is None:
                        unpriced_securities.add(sid)
                    else:
                        entry["start_market_value"] += value

            # Tax lot contributes to end value if opened on or before effective_end
            # AND still held at effective_end (exclude-on-close)
            #
            # A lot held at NEITHER date never reaches `get_eur_value`, so a fully-sold
            # position keeps its legitimate zero end value and is never confused with
            # one that simply could not be priced.
            if taxlot.open_date <= effective_end:
                if not (taxlot.close_date and taxlot.close_date <= effective_end):
                    price = self._get_market_price_with_fallback(sid, effective_end, price_cache)
                    value = get_eur_value(taxlot.quantity, price, security, effective_end)
                    if value is None:
                        unpriced_securities.add(sid)
                    else:
                        entry["end_market_value"] += value

            # New investment: opened during (effective_start, effective_end] (base, at open_date)
            if effective_start < taxlot.open_date <= effective_end:
                entry["new_investment"] += base_fx.convert(taxlot.cost_basis_eur, taxlot.open_date)

        # Capital returned by sales inside the window, priced like the valuations.
        # Window is (start, end] under exclude-on-close — mirrors calculate_xirr;
        # change both together if the close-date convention ever moves again.
        disposals_by_sec: Dict[int, Decimal] = {}
        for r in await self.realized_rows_from_closed_lots(
            base_fx, start=effective_start + timedelta(days=1), end=effective_end,
        ):
            disposals_by_sec[r["security_id"]] = (
                disposals_by_sec.get(r["security_id"], Decimal("0")) + r["proceeds"]
            )

        # Excluded from BOTH sides, exactly as the forward yield excludes an unpriced
        # holding: leaving one in contributes a fabricated `-start_value` to total P&L
        # and takes a 0% weight, which also inflates every other security's weight
        # against a denominator its own value is missing from.
        priced = {sid: e for sid, e in security_map.items() if sid not in unpriced_securities}

        # Calculate totals — market values converted to base at their effective date
        total_end_mv = sum(
            float(base_fx.convert(v["end_market_value"], effective_end))
            for v in priced.values()
        )
        attributions = []

        for sid, entry in priced.items():
            sec = securities_by_id[sid]
            start_mv = float(base_fx.convert(entry["start_market_value"], effective_start))
            end_mv = float(base_fx.convert(entry["end_market_value"], effective_end))
            new_inv = float(entry["new_investment"])
            disposal = float(disposals_by_sec.get(sid, Decimal("0")))
            value_change = end_mv - start_mv
            pnl = value_change + disposal - new_inv
            weight = (end_mv / total_end_mv * 100) if total_end_mv > 0 else 0.0

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
                "contribution_percent": 0.0,  # set below
                "weight_percent": round(weight, 2),
            })

        total_pnl = sum(a["pnl_contribution_eur"] for a in attributions)

        # Set contribution percentages
        for a in attributions:
            a["contribution_percent"] = round(
                (a["pnl_contribution_eur"] / total_pnl * 100) if total_pnl != 0 else 0.0, 2
            )

        # Sort by absolute P&L contribution descending
        attributions.sort(key=lambda a: abs(a["pnl_contribution_eur"]), reverse=True)

        return {
            "start_date": effective_start.isoformat(),
            "end_date": effective_end.isoformat(),
            "total_pnl_eur": round(total_pnl, 2),
            # > 0 means securities were left out because they could not be valued at an
            # endpoint, so total_pnl_eur covers less than the whole book. Same signal
            # and same name as the timeline's and the summary's.
            "unpriced_holdings": len(unpriced_securities),
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
        lookback_days: int = PRICE_LOOKBACK_DAYS
    ) -> Tuple[Dict, Dict]:
        """
        Pre-load all market prices for securities in date range.

        Extends the date range backwards by lookback_days to support
        forward-fill fallback logic when prices are missing.

        Returns: (
            {security_id: {date: price}},
            {security_id: currency}  -- actual price currency from DB
        )
        """
        from app.models.market_price import MarketPrice

        security_ids = [s.id for s in securities]

        # Extend start_date backwards to support forward-fill
        extended_start_date = start_date - timedelta(days=lookback_days)

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
        lookback_days: int = 14,
        price_currency_cache: Optional[Dict] = None
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
        from app.models.exchange_rate import ExchangeRate

        currencies = {s.currency for s in securities if s.currency != 'EUR'}

        # Also include actual price currencies (may differ from security.currency)
        if price_currency_cache:
            for sec_id, price_curr in price_currency_cache.items():
                if price_curr != 'EUR':
                    currencies.add(price_curr)

        if not currencies:
            return {}

        # Extend start_date backwards to support forward-fill
        extended_start_date = start_date - timedelta(days=lookback_days)

        result = await self.db.execute(
            select(ExchangeRate)
            .where(
                and_(
                    ExchangeRate.from_currency.in_(currencies),
                    ExchangeRate.to_currency == 'EUR',
                    ExchangeRate.date >= extended_start_date,
                    ExchangeRate.date <= end_date
                )
            )
        )

        all_rates = result.scalars().all()

        # Build dict: {(from_currency, date): rate}
        rate_cache = {}
        for rate in all_rates:
            rate_cache[(rate.from_currency, rate.date)] = rate.rate

        return rate_cache

    def _get_market_price_with_fallback(
        self,
        security_id: int,
        target_date: date,
        price_cache: Dict,
        max_lookback_days: int = 14
    ) -> Optional[Decimal]:
        """
        Get market price for a date with forward-fill fallback.

        If price is missing for target_date, looks back up to max_lookback_days
        to find the most recent available price (carry-forward strategy).

        Args:
            security_id: ID of the security
            target_date: Date to get price for
            price_cache: Pre-loaded price cache {security_id: {date: price}}
            max_lookback_days: Maximum days to look back (default: 7)

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

        # No price found within lookback window
        logger.warning(
            f"No price found for security {security_id} on {target_date} "
            f"(checked {max_lookback_days} days back)"
        )
        return None

    def _get_exchange_rate_with_fallback(
        self,
        from_currency: str,
        target_date: date,
        exchange_rate_cache: Dict,
        max_lookback_days: int = 14
    ) -> Optional[Decimal]:
        """
        Get exchange rate for a date with forward-fill fallback.

        If rate is missing for target_date, looks back up to max_lookback_days
        to find the most recent available rate (carry-forward strategy).

        Args:
            from_currency: Currency to convert from (e.g., 'USD')
            target_date: Date to get rate for
            exchange_rate_cache: Pre-loaded rate cache {(currency, date): rate}
            max_lookback_days: Maximum days to look back (default: 7)

        Returns:
            Exchange rate as Decimal, or None if no rate found within lookback window
        """
        # Try exact date first
        rate = exchange_rate_cache.get((from_currency, target_date))
        if rate:
            return rate

        # Try previous days (up to max_lookback_days)
        for days_back in range(1, max_lookback_days + 1):
            fallback_date = target_date - timedelta(days=days_back)
            rate = exchange_rate_cache.get((from_currency, fallback_date))
            if rate:
                logger.debug(
                    f"Using {days_back}-day-old exchange rate for {from_currency} "
                    f"on {target_date}: {rate} (from {fallback_date})"
                )
                return rate

        # No rate found within lookback window
        logger.warning(
            f"No exchange rate found for {from_currency} on {target_date} "
            f"(checked {max_lookback_days} days back)"
        )
        return None

    def _calculate_daily_value(
        self,
        target_date: date,
        taxlots_with_securities: List,
        price_cache: Dict,
        exchange_rate_cache: Dict,
        price_currency_cache: Optional[Dict] = None,
        base_fx: Optional[BaseFx] = None,
        position_start: Optional[Dict[int, date]] = None,
    ) -> Dict:
        """
        Calculate portfolio value for a specific date using cached data.
        Includes both open and closed lots — filters by open_date/close_date window.

        Values are returned in the base (display) currency: cost basis is converted
        at each lot's open_date, market value at target_date. When base is EUR this
        is a pass-through. Output keys keep the historical *_eur suffix.
        """
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
            # Only include taxlots opened on or before this date
            if taxlot.open_date > target_date:
                continue

            # A lot sold on D is no longer held at D's close: exclude ON the close
            # date. This is what stops a same-day rotation from double-counting the
            # day (old A still active + new B open), keeps Steuerwert and realized
            # gains disjoint at year-end, and matches the benchmark's convention.
            if taxlot.close_date and taxlot.close_date <= target_date:
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
                        # Log but skip this position if no exchange rate available
                        logger.warning(
                            f"Skipping position for security {security.id} on {target_date}: "
                            f"no exchange rate for {price_currency}"
                        )
                else:
                    total_market_value_eur += position_value
            else:
                unpriced_securities.add(security.id)
                # Log but skip this position if no market price available
                logger.warning(
                    f"Skipping position for security {security.id} ({security.symbol}) on {target_date}: "
                    f"no market price available"
                )

        # Market value converts at the valuation date
        total_market_value = base_fx.convert(total_market_value_eur, target_date)
        gain_loss = total_market_value - total_cost_basis

        return {
            "date": target_date.isoformat(),
            "cost_basis_eur": float(total_cost_basis),
            "market_value_eur": float(total_market_value),
            "gain_loss_eur": float(gain_loss),
            "gain_loss_percent": float((gain_loss / total_cost_basis * 100) if total_cost_basis > 0 else 0),
            "unpriced_holdings": len(unpriced_securities),
        }
