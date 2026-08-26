"""
Benchmark Service
Simulates "what if I bought S&P 500 / NASDAQ instead?" using actual tax lot dates and amounts.
"""
import asyncio
import random
import logging
import time
from typing import List, Dict, Optional, Set, Tuple
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import select, and_, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

import yfinance as yf

from app.models.taxlot import TaxLot
from app.models.security import Security
from app.models.benchmark_price import BenchmarkPrice
from app.models.exchange_rate import ExchangeRate
from app.models.benchmark_timeline_cache import BenchmarkTimelineCache
from app.services.yahoo_rate_limit import is_rate_limit
from app.single_flight import SYNC_PIPELINE, SyncBusy, single_flight

logger = logging.getLogger(__name__)


# GET /api/portfolio/benchmark is public, unauthenticated and lazy-fetches both
# Yahoo and Frankfurter on a cache miss, so it needs the same protection as the
# POST routes — but it needs *two* things, because the shared gate alone is not
# enough here. The gate stops a fetch racing the 08:00 full_sync; it does not
# stop a sequential loop, which re-fetches on every iteration. Trailing weekdays
# the provider has no bar for (today before the close, a holiday at the range
# end) stay permanently "missing" by design, so the range end is re-requested
# forever — the same re-request-forever shape the holiday rule fixed for
# market_prices. So each ticker and each currency also carries its own attempt
# cooldown. Keyed per upstream target rather than globally: warming eight
# distinct benchmarks is legitimate, re-hitting one of them is not.
#
# In-process, for the same reason single_flight is: one uvicorn worker.
UPSTREAM_RETRY_COOLDOWN_SECONDS = 300

_last_upstream_attempt: Dict[str, float] = {}


def _throttled(key: str) -> bool:
    """
    True when ``key`` was attempted within the cooldown, meaning: don't fetch.

    Records the attempt when it returns False, so the caller is free to proceed.
    """
    now = time.monotonic()
    last = _last_upstream_attempt.get(key)
    if last is not None and (now - last) < UPSTREAM_RETRY_COOLDOWN_SECONDS:
        return True
    _last_upstream_attempt[key] = now
    return False


def reset_upstream_throttle() -> None:
    """Clear the attempt memo. For tests — the memo is process-lifetime state."""
    _last_upstream_attempt.clear()


def _missing_business_days(
    start_date: date, end_date: date, cached: Set[date],
    refresh_provisional: bool = False,
) -> Set[date]:
    """
    Business days in [start, end] with no cached row, minus settled holidays.

    An *interior* weekday hole (cached data either side) older than the grace
    window is a day the market never traded: every request since has already
    failed to fill it, and counting it as missing is what makes a cold range
    re-hit the provider forever. Younger holes and leading/trailing gaps stay
    missing so late data can still arrive and a purge-and-refill still heals.

    Shared by the price and FX paths so the two can't drift; same rule as
    MarketPriceRepository.get_missing_dates.

    ``refresh_provisional`` additionally re-reports the trailing
    PROVISIONAL_PRICE_DAYS even when cached, because a row written during a live
    session holds a mid-session price rather than a close. **The price path opts
    in and the FX path deliberately does not**: an ECB reference rate is published
    once a day, so there is no intraday rate to converge on, and
    `_batch_fetch_rates` dedups per row against what is already stored — it could
    not rewrite the row even if asked. Opting FX in would spend a provider request
    per chart load to change nothing, which is the exact waste this helper was
    extracted to stop.
    """
    from app.repositories.market_price_repository import MarketPriceRepository

    expected = set()
    d = start_date
    while d <= end_date:
        if d.weekday() < 5:
            expected.add(d)
        d += timedelta(days=1)

    missing = expected - cached
    if missing and cached:
        first_cached, last_cached = min(cached), max(cached)
        holiday_cutoff = date.today() - timedelta(
            days=MarketPriceRepository.HOLIDAY_GRACE_DAYS
        )
        missing = {
            d for d in missing
            if not (first_cached < d < last_cached and d < holiday_cutoff)
        }
    if refresh_provisional:
        provisional_from = date.today() - timedelta(
            days=MarketPriceRepository.PROVISIONAL_PRICE_DAYS
        )
        missing |= {d for d in (expected & cached) if d >= provisional_from}
    return missing


BENCHMARKS = {
    "sp500": {"ticker": "^GSPC", "currency": "USD", "name": "S&P 500"},
    "nasdaq": {"ticker": "^IXIC", "currency": "USD", "name": "NASDAQ Composite"},
    "msci_world": {"ticker": "URTH", "currency": "USD", "name": "MSCI World"},
    "dax": {"ticker": "^GDAXI", "currency": "EUR", "name": "DAX"},
    "euro_stoxx_50": {"ticker": "^STOXX50E", "currency": "EUR", "name": "Euro Stoxx 50"},
    "ftse100": {"ticker": "^FTSE", "currency": "GBP", "name": "FTSE 100"},
    "nikkei225": {"ticker": "^N225", "currency": "JPY", "name": "Nikkei 225"},
    "cac40": {"ticker": "^FCHI", "currency": "EUR", "name": "CAC 40"},
}


class BenchmarkService:
    # Latched the first time Yahoo answers with a rate limit, mirroring
    # `MarketDataService.rate_limited`. On the class as well as the instance so a
    # service built through `__new__` in a test can still read it.
    rate_limited = False

    def __init__(self, db: AsyncSession):
        self.db = db
        self.rate_limited = False

    # ── Price fetching / caching ───────────────────────────────────────

    async def _ensure_prices_available(
        self, ticker: str, start_date: date, end_date: date, currency: str = "USD",
        refresh_provisional: bool = True,
    ) -> int:
        """
        Lazy-sync: fetch missing benchmark prices from Yahoo Finance.

        ``refresh_provisional`` re-fetches the trailing days whose cached row may hold a
        mid-session value — see MarketPriceRepository.PROVISIONAL_PRICE_DAYS. **On by
        default for the read path and off for the scheduled warm-up**, and the asymmetry
        is a Yahoo-budget decision rather than a correctness one:

        - A read fetches the *one* benchmark being viewed, behind a 300s per-ticker
          throttle. That is where the staleness is visible: the chart draws the
          benchmark against the portfolio, so if the portfolio settles to real closes
          while the benchmark keeps whatever it was first fetched at today, the *gap*
          between the two lines drifts over the day — the one number the panel exists
          to show.
        - `sync_benchmark_prices` loops **every** warm benchmark (all eight, on this
          account) with only a 1-2s gap. Refreshing there would turn today's
          once-a-day 8-request burst into one per market-data slot, seven times a day,
          against a documented burst tolerance of ~10-20. It buys nothing a reader
          sees, so it stays off and the scheduled pass costs exactly what it does now.

        The residual is deliberate and small: a day nobody opened the chart on can keep
        a mid-session benchmark close once it ages out of the window. That is the
        pre-existing behaviour, it never affects a series being looked at, and the fix
        would be a Yahoo bill paid on every slot for a value no one read.
        """
        # Check what we already have (include buffer zone to avoid duplicate inserts)
        buffer_start = start_date - timedelta(days=10)
        result = await self.db.execute(
            select(BenchmarkPrice.date)
            .where(
                and_(
                    BenchmarkPrice.ticker == ticker,
                    BenchmarkPrice.date >= buffer_start,
                    BenchmarkPrice.date <= end_date,
                )
            )
        )
        existing_dates = {row[0] for row in result.all()}

        missing = _missing_business_days(
            start_date, end_date, existing_dates,
            refresh_provisional=refresh_provisional,
        )
        if not missing:
            return 0

        # Cheap refusals before the expensive one: a ticker fetched moments ago
        # serves cache, and a fetch never runs beside another pipeline.
        if _throttled(f"price:{ticker}"):
            logger.info(
                f"Benchmark {ticker}: {len(missing)} dates missing but last attempted "
                f"under {UPSTREAM_RETRY_COOLDOWN_SECONDS}s ago; serving cache"
            )
            return 0

        # Fetch from Yahoo Finance (entire range; yfinance returns only trading days)
        logger.info(f"Fetching benchmark {ticker} prices: {len(missing)} dates missing")
        fetch_start = min(missing) - timedelta(days=5)  # small buffer
        fetch_end = max(missing) + timedelta(days=1)

        try:
            # The gate wraps the network call only, so a full cache hit costs the
            # other routes nothing: entering it bumps the shared last-start clock
            # that their cooldowns read, and a public GET should only do that when
            # it genuinely went upstream.
            with single_flight(SYNC_PIPELINE):
                await asyncio.sleep(random.uniform(1.0, 2.0))  # rate-limit

                # In a thread like every other yfinance call site — this is reachable
                # from a public GET on any cache miss and would block the event loop.
                def _fetch(t=ticker, s=fetch_start.isoformat(), e=fetch_end.isoformat()):
                    return yf.Ticker(t).history(start=s, end=e, auto_adjust=True)

                hist = await asyncio.to_thread(_fetch)
        except SyncBusy as e:
            logger.info(f"Benchmark {ticker}: pipeline busy ({e}); serving cached prices")
            return 0
        except Exception as e:
            # Rule 1: a rate limit is latched here rather than only in the scheduler's
            # warm-up loop, because this method is also reachable from a public GET on
            # any cache miss — a chart load that 429s must not go on to tile FX for the
            # same range. The per-target `UPSTREAM_RETRY_COOLDOWN_SECONDS` memo bounds
            # re-asking for the *same* benchmark; this bounds the pass across different
            # ones, which is the axis it cannot see.
            if is_rate_limit(e):
                self.rate_limited = True
                logger.warning(
                    f"Yahoo rate limit fetching benchmark {ticker}; "
                    f"serving what is cached and leaving the rest of this range missing"
                )
            else:
                logger.error(f"Failed to fetch benchmark {ticker}: {e}")
            try:
                await self.db.rollback()
            except Exception:
                pass
            return 0

        try:
            if hist.empty:
                logger.warning(f"No data returned from Yahoo for {ticker}")
                return 0

            rows = []
            for idx, row in hist.iterrows():
                price_date = idx.date()
                # Unchanged for a date never seen before — including the few days of
                # fetch buffer that fall outside [start, end]. `missing` additionally
                # carries the trailing provisional days, which already have a row and
                # so would otherwise be skipped here for the whole day.
                if price_date in existing_dates and price_date not in missing:
                    continue
                rows.append({
                    "ticker": ticker,
                    "date": price_date,
                    "close_price": Decimal(str(round(row["Close"], 6))),
                    "currency": currency,
                    "source": "yahoo_finance",
                })

            if rows:
                # Upsert, not insert-if-absent: a provisional date by definition
                # already has a row, and `session.add` on the unique (ticker, date)
                # would raise instead of restating it.
                stmt = sqlite_insert(BenchmarkPrice).values(rows)
                await self.db.execute(stmt.on_conflict_do_update(
                    index_elements=["ticker", "date"],
                    set_={
                        "close_price": stmt.excluded.close_price,
                        "currency": stmt.excluded.currency,
                        "source": stmt.excluded.source,
                    },
                ))

            await self.db.flush()
            logger.info(f"Cached {len(rows)} {ticker} prices")
            return len(rows)

        except Exception as e:
            logger.error(f"Failed to persist benchmark {ticker} prices: {e}")
            try:
                await self.db.rollback()
            except Exception:
                pass
            return 0

    async def _preload_benchmark_prices(
        self, ticker: str, start_date: date, end_date: date, lookback_days: int = 14
    ) -> Dict[date, Decimal]:
        """Load benchmark prices into {date: price} dict."""
        extended_start = start_date - timedelta(days=lookback_days)
        result = await self.db.execute(
            select(BenchmarkPrice)
            .where(
                and_(
                    BenchmarkPrice.ticker == ticker,
                    BenchmarkPrice.date >= extended_start,
                    BenchmarkPrice.date <= end_date,
                )
            )
        )
        return {bp.date: bp.close_price for bp in result.scalars().all()}

    async def _preload_fx_rates(
        self, from_currency: str, start_date: date, end_date: date, lookback_days: int = 14
    ) -> Dict[date, Decimal]:
        """Load from_currency→EUR exchange rates into {date: rate} dict."""
        extended_start = start_date - timedelta(days=lookback_days)
        result = await self.db.execute(
            select(ExchangeRate)
            .where(
                and_(
                    ExchangeRate.from_currency == from_currency,
                    ExchangeRate.to_currency == "EUR",
                    ExchangeRate.date >= extended_start,
                    ExchangeRate.date <= end_date,
                )
            )
        )
        return {er.date: er.rate for er in result.scalars().all()}

    @staticmethod
    def _get_with_fallback(
        cache: Dict[date, Decimal], target_date: date, max_lookback: int = 14
    ) -> Optional[Decimal]:
        """Forward-fill: try exact date, then look back."""
        for days_back in range(0, max_lookback + 1):
            val = cache.get(target_date - timedelta(days=days_back))
            if val is not None:
                return val
        return None

    # ── Cache management ───────────────────────────────────────────────

    async def _read_cache(
        self, benchmark_key: str, start_date: date, end_date: date
    ) -> Dict[date, Dict]:
        """Read cached benchmark timeline data for the given range."""
        result = await self.db.execute(
            select(BenchmarkTimelineCache)
            .where(
                and_(
                    BenchmarkTimelineCache.benchmark_key == benchmark_key,
                    BenchmarkTimelineCache.date >= start_date,
                    BenchmarkTimelineCache.date <= end_date,
                )
            )
        )
        cached = {}
        for row in result.scalars().all():
            cached[row.date] = {
                "date": row.date.isoformat(),
                "benchmark_value_eur": float(row.benchmark_value_eur),
                "cost_basis_eur": float(row.cost_basis_eur),
                "gain_loss_eur": float(row.gain_loss_eur),
                "gain_loss_percent": float(row.gain_loss_percent),
            }
        return cached

    async def _write_cache(
        self, benchmark_key: str, points: List[Dict]
    ) -> None:
        """Write computed benchmark timeline points to cache."""
        for point in points:
            point_date = date.fromisoformat(point["date"])
            self.db.add(BenchmarkTimelineCache(
                benchmark_key=benchmark_key,
                date=point_date,
                benchmark_value_eur=Decimal(str(point["benchmark_value_eur"])),
                cost_basis_eur=Decimal(str(point["cost_basis_eur"])),
                gain_loss_eur=Decimal(str(point["gain_loss_eur"])),
                gain_loss_percent=Decimal(str(point["gain_loss_percent"])),
            ))
        await self.db.flush()

    async def clear_cache(self, benchmark_key: Optional[str] = None) -> int:
        """Clear cached benchmark timeline data. If benchmark_key is None, clear all."""
        if benchmark_key:
            result = await self.db.execute(
                delete(BenchmarkTimelineCache)
                .where(BenchmarkTimelineCache.benchmark_key == benchmark_key)
            )
        else:
            result = await self.db.execute(
                delete(BenchmarkTimelineCache)
            )
        await self.db.flush()
        return result.rowcount

    async def clear_cache_recent_days(self, days: int = 7) -> int:
        """Clear cache entries for the last N days (prices may have been updated)."""
        cutoff = date.today() - timedelta(days=days)
        result = await self.db.execute(
            delete(BenchmarkTimelineCache)
            .where(BenchmarkTimelineCache.date >= cutoff)
        )
        await self.db.flush()
        return result.rowcount

    async def _apply_base_currency(self, points: List[Dict]) -> List[Dict]:
        """
        Project EUR-denominated benchmark points into the configured base currency
        at each point's date. The timeline cache stays EUR; this is a display-only
        projection so switching base currency never invalidates the cache.
        """
        from app.services.portfolio_service import PortfolioService

        base_fx = await PortfolioService(self.db)._load_base_fx()
        if base_fx.base_currency == "EUR":
            return points
        for p in points:
            d = date.fromisoformat(p["date"])
            bv = base_fx.convert(Decimal(str(p["benchmark_value_eur"])), d)
            cb = base_fx.convert(Decimal(str(p["cost_basis_eur"])), d)
            gl = bv - cb
            p["benchmark_value_eur"] = float(round(bv, 2))
            p["cost_basis_eur"] = float(round(cb, 2))
            p["gain_loss_eur"] = float(round(gl, 2))
            p["gain_loss_percent"] = float(round((gl / cb * 100) if cb > 0 else 0, 2))
        return points

    # ── Core benchmark calculation ─────────────────────────────────────

    async def _ensure_fx_rates_available(
        self, currency: str, start_date: date, end_date: date
    ) -> None:
        """Lazy-fetch missing FX rates for currency→EUR via CurrencyService."""
        if currency == "EUR":
            return

        from app.services.currency_service import CurrencyService

        # Tile the range in 30-day chunks. Each fetch covers [target-30, target]:
        # the first target IS start_date (covering the carry-forward lookback
        # buffer), the last is pinned to end_date — the old `while current <=
        # end` stepping left up to 29 days uncovered at the tail, so the most
        # recent chart points dropped and were recomputed on every call.
        targets: List[date] = []
        target = start_date
        while True:
            targets.append(target)
            if target >= end_date:
                break
            target = min(target + timedelta(days=30), end_date)

        # _batch_fetch_rates issues its request unconditionally — it dedups per
        # row, after the response — so tiling a five-year span cost ~60 provider
        # requests on *every* chart load, a warm cache included. Ask what is
        # actually missing first and fetch only the tiles that cover it.
        cached = await self._cached_fx_dates(currency, start_date, end_date)
        missing = _missing_business_days(start_date, end_date, cached)
        if not missing:
            return
        targets = [
            t for t in targets
            if any(max(t - timedelta(days=30), start_date) <= m <= t for m in missing)
        ]
        if not targets:
            return

        if _throttled(f"fx:{currency}"):
            logger.info(
                f"FX {currency}→EUR: {len(missing)} dates missing but last attempted "
                f"under {UPSTREAM_RETRY_COOLDOWN_SECONDS}s ago; serving cache"
            )
            return

        currency_service = CurrencyService(self.db)
        try:
            with single_flight(SYNC_PIPELINE):
                for target in targets:
                    try:
                        await currency_service._batch_fetch_rates(
                            from_currency=currency,
                            target_date=target,
                            to_currency="EUR",
                            days_back=30,
                        )
                    except Exception as e:
                        logger.error(f"Failed to fetch FX rates {currency}→EUR for {target}: {e}")
                        try:
                            await self.db.rollback()
                        except Exception:
                            pass
        except SyncBusy as e:
            logger.info(f"FX {currency}→EUR: pipeline busy ({e}); serving cached rates")

    async def _cached_fx_dates(
        self, currency: str, start_date: date, end_date: date
    ) -> Set[date]:
        """Dates in [start, end] that already hold a currency→EUR rate."""
        result = await self.db.execute(
            select(ExchangeRate.date).where(
                and_(
                    ExchangeRate.from_currency == currency,
                    ExchangeRate.to_currency == "EUR",
                    ExchangeRate.date >= start_date,
                    ExchangeRate.date <= end_date,
                )
            )
        )
        return {row[0] for row in result.all()}

    async def calculate_benchmark_value_over_time(
        self,
        start_date: date,
        end_date: date,
        benchmark_key: str = "sp500",
    ) -> List[Dict]:
        """
        Simulate contributing the same money to the benchmark index instead.

        Uses a persistent cache: historical values never change, so we compute
        once and only recompute missing/recent days. **A change to what this
        function computes therefore requires clearing that cache** — see
        `clear_cache`, and the note in Step 5 about why the basis changed.

        For each contribution (the era-spliced `money_in` legs — lot cost basis
        before `coverage_from`, real deposits after):
          1. Convert the EUR amount → benchmark currency on the leg's own date
          2. Divide by index price on that date → hypothetical_shares
        Then for each business day:
          benchmark_value_eur = sum(shares_i * index_price) * fx_to_eur_rate

        `cost_basis_eur` on each point is the running contribution total, so it is
        the same series the portfolio chart draws as `money_in_eur`.
        """
        bench = BENCHMARKS.get(benchmark_key)
        if not bench:
            return []

        ticker = bench["ticker"]
        currency = bench["currency"]
        is_eur_benchmark = currency == "EUR"

        # ── Step 0: Check cache ──────────────────────────────────────
        cached_data = await self._read_cache(benchmark_key, start_date, end_date)

        # Build the set of expected business days in [start_date, end_date]
        expected_dates = set()
        d = start_date
        while d <= end_date:
            if d.weekday() < 5:
                expected_dates.add(d)
            d += timedelta(days=1)

        missing_dates = expected_dates - set(cached_data.keys())

        if not missing_dates:
            # Full cache hit — return cached data sorted by date
            logger.info(f"Benchmark {benchmark_key}: full cache hit ({len(cached_data)} points)")
            return await self._apply_base_currency(sorted(cached_data.values(), key=lambda x: x["date"]))

        logger.info(
            f"Benchmark {benchmark_key}: {len(cached_data)} cached, "
            f"{len(missing_dates)} to compute"
        )

        # ── Step 1: Load ALL tax lots (open + closed) for historical accuracy
        result = await self.db.execute(
            select(TaxLot, Security)
            .join(Security, TaxLot.security_id == Security.id)
            .order_by(TaxLot.open_date.asc())
        )
        taxlots_with_securities = result.all()
        if not taxlots_with_securities:
            return []

        # ── Step 1b: The contributions this hypothetical invests ─────
        # In EUR, because the whole benchmark pipeline computes in EUR and
        # `_apply_base_currency` projects once at read time. `BaseFx("EUR", {})` is a
        # documented pass-through, so this asks the shared splice for raw EUR legs.
        from app.services.portfolio_service import BaseFx, PortfolioService
        contributions = await PortfolioService(self.db)._contribution_inputs(
            BaseFx("EUR", {}),
            lot_rows=[
                (tl.open_date, tl.close_date, tl.cost_basis_eur)
                for tl, _ in taxlots_with_securities
            ],
        )
        money_in_legs = contributions["money_in_legs"]
        if not money_in_legs:
            return []

        # Prices must reach back to the first contribution, which is the first lot's
        # open date whenever the pre-coverage era contributes anything at all.
        earliest_leg_date = min(d for d, _ in money_in_legs)
        price_start = min(earliest_leg_date, start_date)

        # ── Step 2: Ensure benchmark prices are cached ───────────────
        try:
            await self._ensure_prices_available(ticker, price_start, end_date, currency=currency)
        except Exception as e:
            logger.warning(f"Could not fetch new benchmark prices for {ticker}, using cached: {e}")
            try:
                await self.db.rollback()
            except Exception:
                pass

        # ── Step 3: Ensure FX rates are available ────────────────────
        if not is_eur_benchmark:
            await self._ensure_fx_rates_available(currency, price_start, end_date)

        # ── Step 4: Pre-load caches ──────────────────────────────────
        bench_prices = await self._preload_benchmark_prices(ticker, price_start, end_date)
        fx_rates: Dict[date, Decimal] = {}
        if not is_eur_benchmark:
            fx_rates = await self._preload_fx_rates(currency, price_start, end_date)

        # ── Step 5: Turn every contribution into hypothetical index shares ─────
        #
        # **Contributions, not tax lots**, and that distinction is the whole point of
        # this block. Driving it off lots made the benchmark sell whenever the portfolio
        # sold — `-shares` on a lot's close date — which discards the *gain* those shares
        # had accumulated, permanently. Measured on the 2026-08-21 rotation: the
        # benchmark went 61,654 -> 38,766 -> 51,680 and never recovered, losing 4,193 CHF
        # of gain to a day on which no money left the account. It also cliffed on a chart
        # whose portfolio line no longer does, so the comparison read as a huge
        # outperformance that was pure artefact.
        #
        # A contribution-driven hypothetical answers the question people actually ask —
        # *what if I had put the same money into the index instead* — and it is
        # rotation-neutral by construction, because selling one holding to buy another is
        # not a contribution. That makes it the honest partner for the `Money In` line it
        # is drawn beside: both move only when money genuinely enters or leaves.
        #
        # A **negative** leg (a withdrawal) sells shares at that day's price, which is
        # right: the money left, and the hypothetical has to fund it from the index too.
        share_events: List[Tuple[date, Decimal]] = []
        cost_events: List[Tuple[date, Decimal]] = []

        for leg_date, amount_eur in money_in_legs:
            if not amount_eur:
                continue
            index_price = self._get_with_fallback(bench_prices, leg_date)
            if not index_price:
                logger.warning(f"No benchmark price for {leg_date}, skipping contribution")
                continue

            if is_eur_benchmark:
                shares = amount_eur / index_price
            else:
                fx_rate = self._get_with_fallback(fx_rates, leg_date)
                if not fx_rate or fx_rate == 0:
                    logger.warning(
                        f"Cannot compute benchmark shares for the contribution on "
                        f"{leg_date}: fx_rate={fx_rate}"
                    )
                    continue
                shares = (amount_eur / fx_rate) / index_price

            share_events.append((leg_date, shares))
            cost_events.append((leg_date, amount_eur))

        if not share_events:
            return []

        # Sort events by date
        share_events.sort(key=lambda x: x[0])
        cost_events.sort(key=lambda x: x[0])

        # ── Step 6: Walk only MISSING business days ──────────────────
        # We need cumulative state, so walk ALL days from start_date but
        # only record points for missing dates.
        new_points: List[Dict] = []
        current_date = start_date
        running_cost_basis = Decimal("0.0")
        cost_event_idx = 0
        share_event_idx = 0
        running_shares = Decimal("0.0")

        while current_date <= end_date:
            if current_date.weekday() >= 5:
                current_date += timedelta(days=1)
                continue

            # Accumulate cost basis events on or before this date
            while cost_event_idx < len(cost_events) and cost_events[cost_event_idx][0] <= current_date:
                running_cost_basis += cost_events[cost_event_idx][1]
                cost_event_idx += 1

            # Accumulate share events on or before this date
            while share_event_idx < len(share_events) and share_events[share_event_idx][0] <= current_date:
                running_shares += share_events[share_event_idx][1]
                share_event_idx += 1

            # Only compute for missing dates
            if current_date in missing_dates:
                if running_shares > 0:
                    index_price = self._get_with_fallback(bench_prices, current_date)

                    if is_eur_benchmark:
                        if index_price:
                            bench_value_eur = running_shares * index_price
                            gain_loss = bench_value_eur - running_cost_basis

                            new_points.append({
                                "date": current_date.isoformat(),
                                "benchmark_value_eur": float(round(bench_value_eur, 2)),
                                "cost_basis_eur": float(round(running_cost_basis, 2)),
                                "gain_loss_eur": float(round(gain_loss, 2)),
                                "gain_loss_percent": float(
                                    round((gain_loss / running_cost_basis * 100), 2)
                                    if running_cost_basis > 0 else 0
                                ),
                            })
                    else:
                        fx_rate = self._get_with_fallback(fx_rates, current_date)
                        if index_price and fx_rate:
                            bench_value_foreign = running_shares * index_price
                            bench_value_eur = bench_value_foreign * fx_rate
                            gain_loss = bench_value_eur - running_cost_basis

                            new_points.append({
                                "date": current_date.isoformat(),
                                "benchmark_value_eur": float(round(bench_value_eur, 2)),
                                "cost_basis_eur": float(round(running_cost_basis, 2)),
                                "gain_loss_eur": float(round(gain_loss, 2)),
                                "gain_loss_percent": float(
                                    round((gain_loss / running_cost_basis * 100), 2)
                                    if running_cost_basis > 0 else 0
                                ),
                            })
                else:
                    new_points.append({
                        "date": current_date.isoformat(),
                        "benchmark_value_eur": 0.0,
                        "cost_basis_eur": float(round(running_cost_basis, 2)),
                        "gain_loss_eur": 0.0,
                        "gain_loss_percent": 0.0,
                    })

            current_date += timedelta(days=1)

        # ── Step 7: Store new points in cache ────────────────────────
        if new_points:
            try:
                await self._write_cache(benchmark_key, new_points)
                logger.info(f"Cached {len(new_points)} new benchmark timeline points for {benchmark_key}")
            except Exception as e:
                logger.warning(f"Failed to write benchmark cache: {e}")
                try:
                    await self.db.rollback()
                except Exception:
                    pass

        # ── Step 8: Merge cached + new and return ────────────────────
        # Add new points to cached_data dict
        for point in new_points:
            cached_data[date.fromisoformat(point["date"])] = point

        return await self._apply_base_currency(sorted(cached_data.values(), key=lambda x: x["date"]))
