from typing import Dict, Optional, List
from datetime import date, timedelta
from app.clock import utcnow
import logging
import yfinance as yf
import random
import asyncio
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_price import MarketPrice
from app.models.security import Security
from app.repositories.security_repository import held
from app.repositories.watchlist_repository import WatchlistRepository
from app.services.peg_ratio import resolve_peg
from app.services.safe_numbers import safe_float, safe_int
from app.services.ttm_growth import EARNINGS_ROWS, REVENUE_ROWS, trailing_growth
from app.services.yahoo_estimates import (
    EPS_TREND_GROWTH, fetch_estimate_frames, forward_growth, read_optional,
)
from app.services.yahoo_eligibility import yahoo_eligible
from app.services.yahoo_rate_limit import is_rate_limit

logger = logging.getLogger(__name__)

# RSI-14 needs 15 closes.
RSI_MIN_CLOSES = 15


class WatchlistService:
    """Service for managing watchlist items with cached fundamentals and technicals."""

    CACHE_TTL_HOURS = 1

    # How long `quarterly_financials` and the estimates are reused before a refresh asks
    # again (`estimates_synced_at`). They move a few times a quarter; the nightly runs
    # refresh `.info` (price, P/E, margins, analyst target) and the RSI only, and build
    # PEG and the score over the stored growth columns. A week: one full fetch per five
    # nightly runs, and never a whole earnings season late.
    ESTIMATES_REFRESH_AFTER = timedelta(days=7)

    # Yahoo ticker -> security id, built once per service (a pass builds its own), keyed by
    # `held_only`: `_held_closes` reads the held map, the served rows' logo id every security.
    _ticker_index: Optional[Dict[bool, Dict[str, int]]] = None

    # Latched the first time Yahoo answers with a rate limit, mirroring
    # `MarketDataService.rate_limited`. On the class as well as the instance so a
    # service built through `__new__` in a test can still read it.
    rate_limited = False

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = WatchlistRepository(db)
        self.rate_limited = False

    # Shared so the two endpoints cannot disagree in the fifth decimal place —
    # `_safe_float` had already drifted between these services. See
    # app/services/safe_numbers.py.
    def _safe_float(self, value) -> Optional[float]:
        return safe_float(value)

    def _safe_int(self, value) -> Optional[int]:
        return safe_int(value)

    def _compute_rsi(self, closes: np.ndarray, period: int = 14) -> Optional[float]:
        """Compute RSI using Wilder's smoothing method."""
        if len(closes) < period + 1:
            return None

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # Wilder's smoothing: first average is SMA, then EMA
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            # No down days — but which of two very different things that is depends on
            # whether anything moved at all.
            #
            # Gains with no losses is a real, maximal RSI: an unbroken advance. Nothing
            # moving is not. RSI is undefined on a flat series, and returning 100 there
            # claims the strongest overbought reading the scale has —
            # `_compute_buy_score` scores it **0 of 10** on technical timing, while its
            # own `rsi is None` branch scores an unknown at a neutral **5**. So the
            # fabricated value is a full ten points worse than admitting we cannot
            # measure it, which is the wrong direction for a stand-in to err in.
            #
            # Reachable on a halted or delisted listing, a fixed-NAV fund or a very
            # illiquid one — and the watchlist is where arbitrary tickers get added, so
            # it is far more exposed to those than the portfolio is.
            if avg_gain == 0:
                return None
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return round(rsi, 2)

    # The inputs the score is built from. Every one of them has a neutral fallback, so
    # the score is defined for any subset — including the empty one, which is the
    # problem this list exists to detect.
    _SCORE_INPUTS = (
        "peg_ratio", "forward_pe", "trailing_pe", "ev_to_ebitda",
        "rsi14", "pct_from_52w_high", "pct_from_ma200",
        "profit_margins", "revenue_growth", "earnings_growth",
        "analyst_rating", "analyst_target",
    )

    def _compute_buy_score(self, data: Dict) -> Optional[float]:
        """
        Composite 0-100 buy score from valuation, technicals, quality and analyst data.

        Returns **None** when not one of `_SCORE_INPUTS` was available, rather than the
        48.0 that every neutral fallback summed to. That 48 was not a cautious estimate,
        it was a claim: `watchlistColumns.tsx` paints anything at or above 40 yellow and
        `WatchlistTab` sorts by this column *by default*, so a ticker nothing is known
        about — halted, delisted, thinly covered, or simply mistyped — sorted above every
        genuinely measured 35-47 and presented as a middling opportunity.

        The honest branch already existed on both sides: the return type says Optional,
        `api.ts` declares `buy_score: number | null`, and the column has a grey
        no-score badge. Nothing ever produced the None. Same shape as `_compute_rsi`
        answering 100 for a flat series — a stand-in whose value makes an assertion.

        Note what this deliberately does *not* do: refuse a thin score. A score built
        from two of twelve inputs is still mostly fallback, and nothing on screen says
        so. Distinguishing those needs a coverage figure on the wire (the shape
        `forecast_samples` uses for dividend cadence), which is a migration; recorded in
        STATUS.md rather than guessed at here.
        """
        if all(data.get(field) is None for field in self._SCORE_INPUTS):
            return None


        # --- Valuation (0-25) ---
        peg = data.get("peg_ratio")
        if peg is not None and peg > 0:
            peg_sub = 10 if peg <= 0.5 else 8 if peg <= 1 else 6 if peg <= 1.5 else 4 if peg <= 2 else 2 if peg <= 3 else 0
        else:
            peg_sub = 5

        fwd_pe = data.get("forward_pe")
        trail_pe = data.get("trailing_pe")
        if fwd_pe is not None and trail_pe is not None and trail_pe > 0:
            ratio = fwd_pe / trail_pe
            fwd_pe_sub = 8 if ratio < 0.85 else 6 if ratio < 0.95 else 4 if ratio < 1.1 else 2
        else:
            fwd_pe_sub = 4

        ev = data.get("ev_to_ebitda")
        if ev is not None and ev > 0:
            ev_sub = 7 if ev < 8 else 5 if ev < 12 else 3 if ev < 18 else 1 if ev < 25 else 0
        else:
            ev_sub = 3

        valuation_score = peg_sub + fwd_pe_sub + ev_sub

        # --- Technical timing (0-25) ---
        rsi = data.get("rsi14")
        if rsi is not None:
            rsi_sub = 10 if rsi < 25 else 8 if rsi < 35 else 6 if rsi < 45 else 4 if rsi < 55 else 2 if rsi < 65 else 0
        else:
            rsi_sub = 5

        pct_high = data.get("pct_from_52w_high")
        if pct_high is not None:
            high_sub = 8 if pct_high < -30 else 6 if pct_high < -20 else 4 if pct_high < -10 else 2 if pct_high < -5 else 0
        else:
            high_sub = 4

        pct_ma = data.get("pct_from_ma200")
        if pct_ma is not None:
            ma_sub = 7 if pct_ma < -20 else 5 if pct_ma < -10 else 3 if pct_ma < 0 else 1 if pct_ma < 10 else 0
        else:
            ma_sub = 3

        technical_score = rsi_sub + high_sub + ma_sub

        # --- Quality (0-25) ---
        margin = data.get("profit_margins")
        if margin is not None:
            margin_sub = 9 if margin > 0.25 else 7 if margin > 0.15 else 5 if margin > 0.08 else 3 if margin > 0 else 1
        else:
            margin_sub = 4

        rev_g = data.get("revenue_growth")
        if rev_g is not None:
            rev_sub = 8 if rev_g > 0.25 else 6 if rev_g > 0.10 else 4 if rev_g > 0.05 else 2 if rev_g > 0 else 0
        else:
            rev_sub = 4

        eps_g = data.get("earnings_growth")
        if eps_g is not None:
            eps_sub = 8 if eps_g > 0.25 else 6 if eps_g > 0.10 else 4 if eps_g > 0.05 else 2 if eps_g > 0 else 0
        else:
            eps_sub = 4

        quality_score = margin_sub + rev_sub + eps_sub

        # --- Analyst consensus (0-25) ---
        rating = data.get("analyst_rating")
        rating_map = {"strong_buy": 13, "buy": 10, "hold": 5, "sell": 2, "strong_sell": 0}
        rating_sub = rating_map.get(rating, 6) if rating else 6

        target = data.get("analyst_target")
        price = data.get("current_price")
        if target is not None and price is not None and price > 0:
            upside = (target - price) / price * 100
            upside_sub = 12 if upside > 30 else 10 if upside > 20 else 7 if upside > 10 else 4 if upside > 0 else 0
        else:
            upside_sub = 6

        analyst_score = rating_sub + upside_sub

        total = valuation_score + technical_score + quality_score + analyst_score
        return round(total, 1)

    async def _fetch_ticker_data(
        self, yahoo_ticker: str, with_estimates: bool = True, with_history: bool = True,
    ) -> tuple:
        """
        Fetch .info, 1y history and the shared estimate endpoints in a single thread.

        `.info` is three requests; history, `quarterly_financials` and the estimates one
        each. `with_estimates=False` skips the slow two (`ESTIMATES_REFRESH_AFTER`),
        `with_history=False` the history (`_held_closes` had it): None in their places.

        A rate limit on any of them propagates: `sync_item` hands its text to `sync_all`,
        which abandons the pass. These reads used to swallow every exception, a 429
        included — see `yahoo_estimates`.
        """
        def _fetch():
            ticker = yf.Ticker(yahoo_ticker)
            info = ticker.info
            history = (
                read_optional(lambda: ticker.history(period="1y"), "history", yahoo_ticker)
                if with_history else None
            )
            frames = (
                fetch_estimate_frames(ticker, info, yahoo_ticker) if with_estimates
                else {}
            )
            return (
                info, frames.get("quarterly_financials"), history,
                frames.get("revenue_estimate"), frames.get(EPS_TREND_GROWTH),
            )
        return await asyncio.to_thread(_fetch)

    async def _held_closes(self, yahoo_ticker: str) -> Optional[np.ndarray]:
        """
        A year of closes from `market_prices` when `yahoo_ticker` is a held security's own
        Yahoo ticker — the price pass already fetches them, so `history("1y")` would be a
        request for rows on disk. None when nothing held resolves to it, or fewer than the
        RSI needs are stored. Resolved like every Yahoo caller (`_get_yahoo_ticker`) over
        held, Yahoo-eligible securities, once per service.
        """
        security_id = (await self.security_ids_by_ticker(held_only=True)).get(yahoo_ticker.upper())
        if security_id is None:
            return None
        rows = (await self.db.execute(
            select(MarketPrice.close_price)
            .where(
                MarketPrice.security_id == security_id,
                MarketPrice.date >= date.today() - timedelta(days=365),
            )
            .order_by(MarketPrice.date)
        )).scalars().all()
        if len(rows) < RSI_MIN_CLOSES:
            return None
        return np.array([float(c) for c in rows])

    async def security_ids_by_ticker(self, held_only: bool = False) -> Dict[str, int]:
        """
        Upper-cased Yahoo ticker -> security id over Yahoo-eligible securities, held ones only
        when asked, resolved like every Yahoo caller (`_get_yahoo_ticker`). The held map feeds
        `_held_closes`; the full one gives a served row its security's logo (a sold holding's
        logo is stored too). Pure database; built once per service and flag.
        """
        if self._ticker_index is None:
            self._ticker_index = {}
        if held_only not in self._ticker_index:
            from app.services.market_data_service import MarketDataService

            market = MarketDataService(self.db)
            query = select(Security).where(yahoo_eligible()).order_by(Security.id)
            if held_only:
                query = query.where(held())
            index: Dict[str, int] = {}
            for security in (await self.db.execute(query)).scalars().all():
                index.setdefault((await market._get_yahoo_ticker(security)).upper(), security.id)
            self._ticker_index[held_only] = index
        return self._ticker_index[held_only]

    def _filter_outliers(self, closes: np.ndarray) -> np.ndarray:
        """Remove outlier prices (likely currency-mixed data)."""
        if len(closes) == 0:
            return closes
        median = np.median(closes)
        if median <= 0:
            return closes
        mask = (closes >= 0.2 * median) & (closes <= 5.0 * median)
        return closes[mask]

    async def sync_item(self, yahoo_ticker: str, force: bool = False) -> Dict:
        """Fetch and compute all data for a single watchlist ticker."""
        item = await self.repo.get_by_ticker(yahoo_ticker)
        if not item:
            return {}

        # Skip if cache is fresh
        if not force and item.last_synced:
            age = utcnow() - item.last_synced
            if age < timedelta(hours=self.CACHE_TTL_HOURS):
                logger.debug(f"{yahoo_ticker} is fresh ({age} old), skipping")
                return {}

        # Statements and estimates change a few times a quarter: asked weekly, and on the
        # other runs the growth columns they feed are carried from the row (GitHub issue
        # #11, item d). A held ticker's RSI reads the closes the price pass stored.
        slow = (
            item.estimates_synced_at is None
            or utcnow() - item.estimates_synced_at >= self.ESTIMATES_REFRESH_AFTER
        )
        held_closes = await self._held_closes(yahoo_ticker)

        logger.info(f"Fetching data for {yahoo_ticker}{'' if slow else ' (.info only)'}...")
        await asyncio.sleep(random.uniform(1.0, 3.0))

        try:
            info, quarterly_financials, history, revenue_estimate, eps_trend_growth = await self._fetch_ticker_data(
                yahoo_ticker, with_estimates=slow, with_history=held_closes is None,
            )
        except Exception as e:
            logger.error(f"Failed to fetch {yahoo_ticker}: {e}")
            return {"error": str(e)}

        if slow:
            # Forward estimates from analyst consensus — one parser with FundamentalsService,
            # including the forwardEps / trailingEps fallback (already in .info).
            fwd_rev, fwd_eps = forward_growth(revenue_estimate, eps_trend_growth, info, yahoo_ticker)
            # TTM growth from quarterly financials (always current, no annual lag), else .info.
            revenue_growth = trailing_growth(quarterly_financials, REVENUE_ROWS, info.get("revenueGrowth"))
            earnings_growth = trailing_growth(quarterly_financials, EARNINGS_ROWS, info.get("earningsGrowth"))
        else:
            fwd_rev, fwd_eps = item.fwd_revenue_growth, item.fwd_eps_growth
            revenue_growth, earnings_growth = item.revenue_growth, item.earnings_growth
        trailing_pe = self._safe_float(info.get("trailingPE"))

        logger.debug(f"{yahoo_ticker}: fwd_rev={fwd_rev}, fwd_eps={fwd_eps}")

        current_price = self._safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))

        data: Dict = {
            "symbol": info.get("symbol", yahoo_ticker),
            "company_name": info.get("shortName") or info.get("longName"),
            "current_price": current_price,
            "currency": info.get("currency"),
            "data_currency": info.get("currency"),
            "trailing_pe": trailing_pe,
            "forward_pe": self._safe_float(info.get("forwardPE")),
            # One tier order with FundamentalsService, in one place (`peg_ratio.resolve_peg`).
            "peg_ratio": resolve_peg(info, trailing_pe, fwd_eps),
            "ev_to_ebitda": self._safe_float(info.get("enterpriseToEbitda")),
            "revenue_growth": revenue_growth,
            "earnings_growth": earnings_growth,
            "fwd_revenue_growth": fwd_rev,
            "fwd_eps_growth": fwd_eps,
            "profit_margins": self._safe_float(info.get("profitMargins")),
            "market_cap": self._safe_int(info.get("marketCap")),
            "analyst_target": self._safe_float(info.get("targetMeanPrice")),
            "analyst_rating": info.get("recommendationKey"),
            "analyst_count": self._safe_int(info.get("numberOfAnalystOpinions")),
            "last_synced": utcnow(),
        }
        if slow:
            data["estimates_synced_at"] = utcnow()

        # Use .info for 52-week high/low and moving averages (reliable, pre-computed)
        data["week52_high"] = self._safe_float(info.get("fiftyTwoWeekHigh"))
        data["week52_low"] = self._safe_float(info.get("fiftyTwoWeekLow"))
        data["ma200"] = self._safe_float(info.get("twoHundredDayAverage"))
        data["ma50"] = self._safe_float(info.get("fiftyDayAverage"))

        # These three are assigned unconditionally, None included, and that is the whole
        # point.
        #
        # `WatchlistRepository.update_cached_data` iterates `data.items()`, so a key that
        # is simply *absent* leaves the previous column value in place — while
        # `last_synced` is always present and always refreshed. So a day on which
        # `.info` came back without `fiftyTwoWeekHigh`, or `history` came back empty,
        # republished yesterday's RSI and %-from-high under today's timestamp. Nothing
        # anywhere could tell that apart from a genuine reading.
        #
        # It also made a security disagree with itself: `_compute_buy_score` reads this
        # dict, so it scored the missing RSI at the neutral 5 while the table went on
        # displaying the stale 72.4 that would have scored 0.
        data["pct_from_52w_high"] = None
        data["pct_from_ma200"] = None
        data["rsi14"] = None

        # % from 52-week high (using current_price from .info)
        if current_price and data["week52_high"] and data["week52_high"] > 0:
            data["pct_from_52w_high"] = round(
                (current_price - data["week52_high"]) / data["week52_high"] * 100, 2
            )

        # % from 200-day MA (using current_price from .info)
        if current_price and data.get("ma200") and data["ma200"] > 0:
            data["pct_from_ma200"] = round(
                (current_price - data["ma200"]) / data["ma200"] * 100, 2
            )

        # RSI-14: from the stored closes of a held ticker, else the history fetched with .info
        try:
            closes = held_closes
            if closes is None and history is not None and not history.empty:
                closes = history["Close"].values.astype(float)
            if closes is not None:
                closes = self._filter_outliers(closes)
                if len(closes) >= RSI_MIN_CLOSES:
                    data["rsi14"] = self._compute_rsi(closes, period=14)
        except Exception as e:
            logger.warning(f"Failed to compute RSI ({yahoo_ticker}): {e}")

        # Compute composite buy score
        data["buy_score"] = self._compute_buy_score(data)

        await self.repo.update_cached_data(item.id, data)
        await self.db.commit()

        logger.info(
            f"Synced {yahoo_ticker}: price={data.get('current_price')}, "
            f"score={data.get('buy_score')}, RSI={data.get('rsi14')}, %52wH={data.get('pct_from_52w_high')}"
        )
        return data

    async def sync_all(self, force: bool = False) -> Dict:
        """Sync all watchlist items."""
        items = await self.repo.get_all()
        if not items:
            return {"synced": 0, "errors": 0, "message": "Watchlist is empty"}

        logger.info(f"Syncing {len(items)} watchlist items")

        synced = 0
        errors = 0

        for i, item in enumerate(items, 1):
            logger.info(f"[{i}/{len(items)}] {item.yahoo_ticker}")
            result = await self.sync_item(item.yahoo_ticker, force=force)
            if "error" in result:
                errors += 1
                # `sync_item` catches its own fetch failure and returns the message
                # rather than raising, so the rate limit arrives here as a string --
                # which is why `is_rate_limit` takes an object and not an exception.
                # Rule 1: stop the pass. An item this run never reached keeps its old
                # `last_synced`, so the next run treats it as stale and picks it up.
                if is_rate_limit(result.get("error")):
                    self.rate_limited = True
                    logger.warning(
                        f"Yahoo rate limit at {item.yahoo_ticker} "
                        f"({i}/{len(items)}); abandoning the rest of this pass"
                    )
                    break
            else:
                synced += 1

            if i < len(items):
                await asyncio.sleep(random.uniform(2.0, 4.0))

        out = {
            "synced": synced,
            "errors": errors,
            "rate_limited": self.rate_limited,
            "message": f"Synced {synced}/{len(items)} watchlist items",
        }
        if self.rate_limited:
            out["warnings"] = [
                'Yahoo Finance rate limit reached; the rest of this pass was abandoned. '
                'Do not retry manually — the next scheduled run resumes where it stopped.'
            ]
        return out

    async def get_all_items(self) -> List:
        return await self.repo.get_all()
