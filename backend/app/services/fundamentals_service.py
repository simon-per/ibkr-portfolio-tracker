from typing import List, Dict, Optional
from datetime import datetime, timezone
from app.clock import utcnow
from app.repositories.sync_run_repository import utc_iso
import logging
import yfinance as yf
import random
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.etf_mappings import allocation_for_fund_isin
from app.repositories.fundamentals_repository import (
    FundamentalsRepository, backing_off, clear_fundamentals_backoff, record_fundamentals_miss,
    sync_scope,
)
from app.repositories.security_repository import held
from app.services.allocation_service import allocation_from_info, apply_allocation
from app.services.market_cap import market_cap_to_base
from app.services.peg_ratio import resolve_peg
from app.services.safe_numbers import safe_float, safe_int
from app.services.ttm_growth import EARNINGS_ROWS, REVENUE_ROWS, trailing_growth
from app.models.security import Security
from app.services.yahoo_estimates import (
    EPS_TREND_GROWTH, fetch_estimate_frames, forward_growth, is_fund, read_optional,
)
from app.services.yahoo_rate_limit import check_yahoo_hold, is_rate_limit

logger = logging.getLogger(__name__)


class FundamentalsService:
    """Service for fetching and caching fundamental metrics and earnings data."""

    # Latched the first time Yahoo answers with a rate limit, mirroring
    # `MarketDataService.rate_limited`. Declared on the class as well as assigned per
    # instance for the same reason it is there: a service built through `__new__` in a
    # test would otherwise raise on a read of a never-initialised attribute, turning a
    # safety net into a crash in the one place it gets exercised.
    rate_limited = False

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = FundamentalsRepository(db)
        # Per-run state: a pass builds its own service, so this never leaks forward.
        self.rate_limited = False

    async def _get_yahoo_ticker(self, security: Security, market_service=None) -> str:
        if market_service is None:
            from app.services.market_data_service import MarketDataService
            market_service = MarketDataService(self.db)
        return await market_service._get_yahoo_ticker(security)

    # Shared so the two endpoints cannot disagree in the fifth decimal place —
    # `_safe_float` had already drifted between these services. See
    # app/services/safe_numbers.py.
    def _safe_float(self, value) -> Optional[float]:
        return safe_float(value)

    def _safe_int(self, value) -> Optional[int]:
        return safe_int(value)

    async def _fetch_yahoo_data(self, yahoo_ticker: str) -> tuple:
        """
        Fetch .info, .earnings_dates and the shared estimate endpoints in a thread.

        Six requests for a company (`.info` is three: quoteSummary, v7 quote and the
        timeseries call for `trailingPegRatio`), three for a fund. A rate limit on any of
        them propagates to the loop, which abandons the pass — see `yahoo_estimates`.
        The fourth element is the `+1y` EPS growth read off the cached trend.
        """
        # A refusal anywhere in the process holds this too (issue #57); the loop's
        # breaker reads `YahooHold` as the rate limit it stands for.
        await check_yahoo_hold()

        def _fetch():
            ticker = yf.Ticker(yahoo_ticker)
            info = ticker.info
            earnings = (
                None if is_fund(info)
                else read_optional(lambda: ticker.earnings_dates, "earnings_dates", yahoo_ticker)
            )
            frames = fetch_estimate_frames(ticker, info, yahoo_ticker)
            return (
                info, earnings, frames["quarterly_financials"],
                frames[EPS_TREND_GROWTH], frames["revenue_estimate"],
            )

        return await asyncio.to_thread(_fetch)

    def _extract_metrics(self, security: Security, info: Dict, quarterly_financials=None,
                         eps_trend_growth=None, revenue_estimate=None) -> Optional[Dict]:
        """
        Extract fundamental metrics from a Yahoo Finance info dict, or None when it carries
        nothing: empty, or every value None — yfinance has answered an unknown symbol with
        `{'trailingPegRatio': None}`, which used to become a row of stand-ins typed EQUITY.
        """
        if not info or all(value is None for value in info.values()):
            logger.info(f"No fundamental data available for {security.symbol}")
            return None

        # Forward estimates from analyst consensus — one parser with the watchlist.
        fwd_rev, fwd_eps = forward_growth(revenue_estimate, eps_trend_growth, info, security.symbol)
        trailing_pe = self._safe_float(info.get('trailingPE'))

        metrics_data = {
            'security_id': security.id,
            'trailing_pe': trailing_pe,
            'forward_pe': self._safe_float(info.get('forwardPE')),
            # One tier order with the watchlist, in one place (`peg_ratio.resolve_peg`).
            'peg_ratio': resolve_peg(info, trailing_pe, fwd_eps),
            'price_to_sales': self._safe_float(info.get('priceToSalesTrailing12Months')),
            'price_to_book': self._safe_float(info.get('priceToBook')),
            # TTM growth from quarterly financials (always current, no lag), else .info.
            'revenue_growth': trailing_growth(quarterly_financials, REVENUE_ROWS, info.get('revenueGrowth')),
            'earnings_growth': trailing_growth(quarterly_financials, EARNINGS_ROWS, info.get('earningsGrowth')),
            'fwd_revenue_growth': fwd_rev,
            'fwd_eps_growth': fwd_eps,
            'profit_margins': self._safe_float(info.get('profitMargins')),
            'gross_margins': self._safe_float(info.get('grossMargins')),
            'operating_margins': self._safe_float(info.get('operatingMargins')),
            'market_cap': self._safe_int(info.get('marketCap')),
            'number_of_analysts': self._safe_int(info.get('numberOfAnalystOpinions')),
            'target_mean_price': self._safe_float(info.get('targetMeanPrice')),
            'target_high_price': self._safe_float(info.get('targetHighPrice')),
            'target_low_price': self._safe_float(info.get('targetLowPrice')),
            'quote_type': info.get('quoteType', 'EQUITY'),
            'data_currency': info.get('currency'),
        }

        logger.info(f"Got fundamentals for {security.symbol} (type={metrics_data['quote_type']})")
        return metrics_data

    def _extract_earnings(self, security: Security, earnings_dates) -> List[Dict]:
        """Extract earnings events from a Yahoo Finance earnings_dates DataFrame."""
        if earnings_dates is None or (hasattr(earnings_dates, 'empty') and earnings_dates.empty):
            logger.info(f"No earnings dates for {security.symbol}")
            return []

        now = utcnow()
        events = []

        for date_idx, row in earnings_dates.iterrows():
            try:
                if hasattr(date_idx, 'to_pydatetime'):
                    earnings_dt = date_idx.to_pydatetime()
                else:
                    earnings_dt = datetime.fromisoformat(str(date_idx))

                if earnings_dt.tzinfo is not None:
                    # Convert, then drop the tzinfo. `replace(tzinfo=None)` alone
                    # *discards* the offset and keeps the wall clock, which stored
                    # exchange-local time in a column `app/clock.py` declares to be naive
                    # UTC — and `get_upcoming_earnings` compares these rows against
                    # `utcnow()`.
                    #
                    # Apple reporting at 16:30 America/New_York (20:30 UTC) was stored as
                    # 16:30, so from 16:30 UTC — 12:30 ET, four hours before the release
                    # and before the US market even closes — the event dropped off the
                    # upcoming calendar and reappeared under history with
                    # `reported_eps` still NULL. The mirror case runs the other way: TSMC
                    # at 14:00 Asia/Taipei (06:00 UTC) stayed "upcoming" for eight hours
                    # after it had already happened.
                    earnings_dt = earnings_dt.astimezone(timezone.utc).replace(tzinfo=None)

                eps_estimate = self._safe_float(row.get('EPS Estimate'))
                reported_eps = self._safe_float(row.get('Reported EPS'))
                surprise_pct = self._safe_float(row.get('Surprise(%)'))

                is_upcoming = earnings_dt > now and reported_eps is None

                events.append({
                    'security_id': security.id,
                    'earnings_date': earnings_dt,
                    'eps_estimate': eps_estimate,
                    'reported_eps': reported_eps,
                    'surprise_percent': surprise_pct,
                    'is_upcoming': is_upcoming,
                })
            except Exception as e:
                logger.warning(f"Skipping earnings row for {security.symbol}: {e}")
                continue

        logger.info(f"Got {len(events)} earnings events for {security.symbol}")
        return events

    async def sync_fundamentals_data(self, force_refresh: bool = False) -> Dict:
        """Sync fundamental metrics and earnings for every held security."""
        # yahoo_eligible(): six Yahoo requests per company (`.info` is three), the most expensive loop
        # in the app. And held only (`security_repository.held`, 2026-10-17): every read
        # of these rows — the table, both earnings lists, `/status` — shows what is held,
        # and the daily pass would otherwise refetch a sold position forever. Buying it
        # again brings it back with the next sync. Its sibling
        # `get_fundamentals_for_portfolio` filters on held() only. Both through
        # `fundamentals_repository.sync_scope`, which `/status` counts with too.
        result = await self.db.execute(
            select(Security).where(sync_scope())
        )
        securities = list(result.scalars().all())

        if not securities:
            return {
                'securities_processed': 0,
                'metrics_updated': 0,
                'earnings_updated': 0,
                'errors': 0,
                'message': 'No securities found',
            }

        logger.info(f"Syncing fundamentals for {len(securities)} securities")

        metrics_updated = 0
        allocations_updated = 0
        earnings_updated = 0
        no_data = 0
        errors = 0

        # Determine which securities need updating. A security waiting out a miss
        # (`fundamentals_repository.backing_off`, issue #66) is left out whether it has a
        # row or not: `get_stale_metrics` applies the predicate to the rows, this to the
        # securities that have none. A forced refresh is a person asking: it asks all.
        now = utcnow()
        backing = 0 if force_refresh else sum(1 for s in securities if backing_off(s, now))
        if not force_refresh:
            stale_metrics = await self.repo.get_stale_metrics()
            stale_ids = {m.security_id for m in stale_metrics}
            all_metrics = await self.repo.get_all_metrics()
            existing_ids = {m.security_id for m in all_metrics}
            needs_update = [
                s for s in securities
                if s.id in stale_ids or (s.id not in existing_ids and not backing_off(s, now))
            ]
            if not needs_update:
                logger.info("All fundamentals are fresh")
                return {
                    'securities_processed': 0,
                    'metrics_updated': 0,
                    'earnings_updated': 0,
                    'no_data': 0,
                    'errors': 0,
                    'backing_off': backing,
                    'message': 'All fundamentals are up to date',
                }
        else:
            needs_update = securities

        # Create MarketDataService once for ticker resolution
        from app.services.market_data_service import MarketDataService
        market_service = MarketDataService(self.db)

        pending_ids = [s.id for s in needs_update]

        for i, security_id in enumerate(pending_ids, 1):
            # Reloaded by id each iteration rather than held across the loop.
            # `db.rollback()` in the handler below expires EVERY object in the session,
            # so the next iteration's `security.symbol` was a lazy refresh — and in async
            # SQLAlchemy an expired-attribute load outside an await raises
            # MissingGreenlet. That line sits outside the try, so one failed security
            # did not cost one security: it crashed the whole pass, uncaught. `db.get`
            # is awaited and serves the identity map on the happy path, so this costs
            # nothing when nothing has failed.
            security = await self.db.get(Security, security_id)
            if security is None:
                continue
            logger.info(f"[{i}/{len(pending_ids)}] Processing {security.symbol}")

            try:
                # Resolve ticker once
                yahoo_ticker = await self._get_yahoo_ticker(security, market_service)
                logger.info(f"Fetching data for {security.symbol} ({yahoo_ticker})...")

                # Rate limit before API call
                await asyncio.sleep(random.uniform(1.0, 3.0))

                # Single consolidated fetch in a thread (non-blocking)
                info, earnings_df, quarterly_financials, eps_trend_growth, revenue_estimate = await self._fetch_yahoo_data(yahoo_ticker)

                # Extract and save fundamentals (TTM growth from quarterly financials)
                metrics_data = self._extract_metrics(security, info, quarterly_financials, eps_trend_growth, revenue_estimate)
                if metrics_data:
                    await self.repo.upsert_metrics(metrics_data)
                    clear_fundamentals_backoff(security)
                    metrics_updated += 1
                    # The allocation sync's sector/industry/country/asset type are in this
                    # same `.info`; writing them here spares it three requests a security.
                    # Not over a fund the ETF table maps: its mapping wins there.
                    if not allocation_for_fund_isin(security.isin):
                        apply_allocation(security, allocation_from_info(info))
                        allocations_updated += 1
                else:
                    # Yahoo answered and had nothing usable. Stamped, so the next pass does
                    # not ask again before `fundamentals_backoff_until` (issue #66: it was
                    # re-asked every pass, three to six requests, for good). No row is
                    # written — the security stays absent, never a row of stand-ins — and a
                    # row it already has keeps its figures.
                    record_fundamentals_miss(security, utcnow())
                    no_data += 1

                # Extract and save earnings
                earnings_data = self._extract_earnings(security, earnings_df)
                for event_data in earnings_data:
                    await self.repo.upsert_earnings_event(event_data)
                    earnings_updated += 1

                await self.db.commit()

                # Rate limit between securities
                if i < len(needs_update):
                    delay = random.uniform(2.0, 4.0)
                    logger.debug(f"Waiting {delay:.1f}s before next security...")
                    await asyncio.sleep(delay)

            except Exception as e:
                # Read the symbol BEFORE the rollback: rollback expires every ORM
                # attribute, so touching `security.symbol` afterwards triggers a lazy
                # refresh that raises MissingGreenlet from inside this handler --
                # turning a handled rate limit into an unhandled crash.
                symbol = security.symbol
                errors += 1
                await self.db.rollback()
                # Rule 1: a rate limit means stop, not slow down. This is the most
                # expensive of the four Yahoo loops — six requests per company
                # security — so continuing past a 429 spent the rest of the pass asking
                # an IP that had already refused us, once every one to three seconds.
                # Same fix `MarketDataService.sync_securities` took on 2026-08-04; this
                # loop, the ratings loop and the watchlist loop kept the older shape.
                #
                # What was written stays written and the next run resumes on its own:
                # `sync_stale_fundamentals` selects on staleness, so the securities this
                # pass never reached are simply still stale.
                if is_rate_limit(e):
                    self.rate_limited = True
                    logger.warning(
                        f"Yahoo rate limit at {symbol} "
                        f"({i}/{len(pending_ids)}); abandoning the rest of this pass"
                    )
                    break
                logger.error(f"Error processing {symbol}: {e}")
                # Any other failure is this security's miss, as on the price path: a
                # delisted or mis-mapped ticker answering 404 is not re-asked every pass.
                await self._record_miss(security_id, symbol)
                continue

        logger.info(
            f"Fundamentals Sync Complete: "
            f"processed={len(needs_update)}, metrics={metrics_updated}, "
            f"earnings={earnings_updated}, no_data={no_data}, errors={errors}, "
            f"backing_off={backing}, rate_limited={self.rate_limited}"
        )

        result = {
            'securities_processed': len(needs_update),
            'metrics_updated': metrics_updated,
            'allocations_updated': allocations_updated,
            'earnings_updated': earnings_updated,
            'no_data': no_data,
            'errors': errors,
            'backing_off': backing,
            'rate_limited': self.rate_limited,
            'message': (
                f'Synced {metrics_updated}/{len(needs_update)} fundamentals, '
                f'{earnings_updated} earnings events'
                + (f', {no_data} with no data from Yahoo' if no_data else '')
            ),
        }
        if self.rate_limited:
            # Surfaced the same way the market-data pass surfaces it, so the UI's
            # warning banner shows why a sync stopped early instead of the run merely
            # looking incomplete.
            result['warnings'] = [
                'Yahoo Finance rate limit reached; the rest of this pass was abandoned. '
                'Do not retry manually — the next scheduled run resumes where it stopped.'
            ]
        return result

    async def _record_miss(self, security_id: int, symbol: str) -> None:
        """Stamp a failed fetch (`record_fundamentals_miss`) after the loop's rollback, as
        the only write. Its own failure costs the stamp, never the pass."""
        try:
            security = await self.db.get(Security, security_id)
            if security is not None:
                record_fundamentals_miss(security, utcnow())
                await self.db.commit()
        except Exception as e:
            await self.db.rollback()
            logger.error(f"Could not stamp the failed fundamentals fetch of {symbol}: {e}")

    async def sync_stale_fundamentals(self) -> Dict:
        """
        Sync fundamentals that are stale (older than `STALE_AFTER`) **or missing entirely**.

        A thin delegate on purpose. `sync_fundamentals_data(force_refresh=False)` already
        builds the right work list — `s.id not in existing_ids or s.id in stale_ids` — and
        has its own "all fundamentals are up to date" early return in the same shape, so
        there is nothing left for this method to decide.

        It used to pre-filter on `get_stale_metrics(days_old=1)` and bail when that came
        back empty. That query selects rows which already **exist**, so a security with no
        metrics row was invisible to it: whenever every existing row happened to be fresh,
        this path reported "No stale fundamentals to update" and a newly-bought security
        never acquired fundamentals through it at all. The union it delegates to was
        sitting one call below the guard that prevented it being reached.

        Worth knowing how that survived: `sync_stale_ratings` was fixed for this exact
        shape and its docstring cites *this* method as the sibling that "already unions
        the two sets", as did CLAUDE.md's duplicated-logic table. Both were describing the
        inner function while the entry point pre-filtered — a correct fix justified by a
        false reading of the code it was copying.
        """
        return await self.sync_fundamentals_data(force_refresh=False)

    async def get_fundamentals_for_portfolio(self) -> List[Dict]:
        """
        Fundamental metrics joined with security info, for every **held** security.

        Held means at least one open lot, in any account (`security_repository.held`).
        It read every `Security` row until 2026-10-17, so a position sold years ago
        stayed on the Fundamentals tab as a row of dashes and was counted by `/status` as
        a holding with "no data yet" -- an alarm about something the portfolio no longer
        owns. Its stored row stays. Still not filtered by `yahoo_eligible`: a holding
        Yahoo cannot price is a holding, and the tab names it with its fundamentals
        absent.
        """
        result = await self.db.execute(select(Security).where(held()))
        securities = list(result.scalars().all())

        all_metrics = await self.repo.get_all_metrics()
        metrics_map = {m.security_id: m for m in all_metrics}
        # The tab shows and sorts the base-currency figure (`market_cap.py`).
        to_base = await market_cap_to_base(self.db)

        portfolio_data = []
        for sec in securities:
            m = metrics_map.get(sec.id)
            entry = {
                'security_id': sec.id,
                'symbol': sec.symbol,
                'description': sec.description,
                'exchange': sec.exchange,
                'currency': sec.currency,
            }
            if m:
                entry.update({
                    'quote_type': m.quote_type,
                    'trailing_pe': m.trailing_pe,
                    'forward_pe': m.forward_pe,
                    'peg_ratio': m.peg_ratio,
                    'price_to_sales': m.price_to_sales,
                    'price_to_book': m.price_to_book,
                    'revenue_growth': m.revenue_growth,
                    'earnings_growth': m.earnings_growth,
                    'fwd_revenue_growth': m.fwd_revenue_growth,
                    'fwd_eps_growth': m.fwd_eps_growth,
                    'profit_margins': m.profit_margins,
                    'gross_margins': m.gross_margins,
                    'operating_margins': m.operating_margins,
                    'market_cap': m.market_cap,
                    'market_cap_base': await to_base(m.market_cap, m.data_currency),
                    'number_of_analysts': m.number_of_analysts,
                    'target_mean_price': m.target_mean_price,
                    'target_high_price': m.target_high_price,
                    'target_low_price': m.target_low_price,
                    'data_currency': m.data_currency,
                    # Naive UTC in the column; a bare isoformat() is read by the browser
                    # as local time, which the tab's as-of line prints.
                    'last_updated': utc_iso(m.last_updated),
                })
            else:
                entry.update({
                    'quote_type': None,
                    'trailing_pe': None,
                    'forward_pe': None,
                    'peg_ratio': None,
                    'price_to_sales': None,
                    'price_to_book': None,
                    'revenue_growth': None,
                    'earnings_growth': None,
                    'fwd_revenue_growth': None,
                    'fwd_eps_growth': None,
                    'profit_margins': None,
                    'gross_margins': None,
                    'operating_margins': None,
                    'market_cap': None,
                    'market_cap_base': None,
                    'number_of_analysts': None,
                    'target_mean_price': None,
                    'target_high_price': None,
                    'target_low_price': None,
                    'data_currency': None,
                    'last_updated': None,
                })

            portfolio_data.append(entry)

        return portfolio_data

    async def _held_ids(self) -> set:
        return set((await self.db.execute(
            select(Security.id).where(held())
        )).scalars().all())

    async def get_earnings_calendar(self, days_ahead: int = 90) -> List[Dict]:
        """Upcoming earnings of held securities, with security info (eagerly loaded)."""
        events = await self.repo.get_upcoming_earnings(days_ahead)
        held = await self._held_ids()
        result = []
        for ev in events:
            if ev.security_id not in held:
                continue
            result.append({
                'security_id': ev.security_id,
                'symbol': ev.security.symbol,
                'description': ev.security.description,
                'earnings_date': ev.earnings_date.isoformat(),
                'eps_estimate': ev.eps_estimate,
            })
        return result

    async def get_earnings_history(self, days_back: int = 365) -> List[Dict]:
        """Past earnings of held securities, with surprise data (eagerly loaded)."""
        events = await self.repo.get_recent_earnings(days_back)
        held = await self._held_ids()
        result = []
        for ev in events:
            if ev.security_id not in held:
                continue
            result.append({
                'security_id': ev.security_id,
                'symbol': ev.security.symbol,
                'description': ev.security.description,
                'earnings_date': ev.earnings_date.isoformat(),
                'eps_estimate': ev.eps_estimate,
                'reported_eps': ev.reported_eps,
                'surprise_percent': ev.surprise_percent,
                'beat_or_miss': ev.beat_or_miss,
            })
        return result
