"""
Allocation service for fetching and caching sector/geographic data for securities.
"""
import asyncio
import logging
import random
from datetime import timedelta
from app.clock import utcnow
from typing import Optional, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import yfinance as yf

from app.models.security import Security
from app.services.yahoo_rate_limit import is_rate_limit
from app.etf_mappings import allocation_for_fund_isin

logger = logging.getLogger(__name__)

#: How old allocation data may get before a sync refreshes it.
#:
#: Shared with `routers/allocation.py`, which reports how many securities are stale,
#: because the two are one decision seen from both ends: this value picks what the
#: sync touches, and that endpoint tells the user what needs touching. Written
#: inline in both places until 2026-08-01, where nothing would have caught them
#: drifting — the tab would simply have described a different set than the sync
#: refreshed.
ALLOCATION_STALE_DAYS = 7


class AllocationService:
    """Service for managing allocation data (sector, country, etc.)"""

    # Latched the first time Yahoo answers with a rate limit, mirroring
    # `MarketDataService.rate_limited`. On the class as well as the instance so a
    # service built through `__new__` in a test can still read it.
    rate_limited = False

    def __init__(self, db: AsyncSession):
        self.db = db
        self.rate_limited = False

    async def _get_yahoo_ticker(self, security: Security) -> Optional[str]:
        """
        The same resolution the price path uses — mapping, then exchange suffix.

        This used to be its own implementation, and a much weaker one: it checked
        `ticker_mappings`, returned the bare symbol for NASDAQ/NYSE/AMEX, and
        **None for everything else**. So it ignored `EXCHANGE_SUFFIXES` entirely,
        omitted ARCA and BATS from its US list, and could not do the TSE
        Tokyo-versus-Toronto disambiguation that the SBI repair turned on.

        The consequence was quiet and large. A mapping row is only auto-saved when
        a *variation* succeeds — i.e. when the primary suffix-derived ticker
        failed — so a XETRA holding priced fine as `AMZ.DE` never gets a row, and
        this returned None for it. Every non-US security whose primary ticker works
        was therefore unresolvable here and got **no sector and no country**,
        permanently, while sitting in the "missing allocation data" count.

        `FundamentalsService`, `DividendService` and `AnalystRatingService` all
        already delegate; this was the one that did not.
        """
        from app.services.market_data_service import MarketDataService
        return await MarketDataService(self.db)._get_yahoo_ticker(security)

    async def fetch_allocation_for_security(self, security: Security) -> Dict:
        """
        Fetch allocation data for a single security.
        Uses ETF mappings for known ETFs, yfinance for stocks.
        """
        logger.info(f"Fetching allocation for {security.symbol}...")

        # Check if it's a known fund — by ISIN, never by ticker. A symbol lookup would
        # write `asset_type='ETF'` onto any holding whose ticker happens to collide with a
        # row in the table, and that write is persistent.
        etf_data = allocation_for_fund_isin(security.isin)
        if etf_data:
            logger.info(f"Using ETF mapping for {security.symbol}")
            return {
                'success': True,
                'asset_type': 'ETF',
                'sector': None,  # ETFs have multiple sectors
                'industry': None,
                'country': None,  # ETFs have multiple countries
                'etf_data': etf_data,
            }

        # Get Yahoo ticker
        yahoo_ticker = await self._get_yahoo_ticker(security)
        if not yahoo_ticker:
            logger.warning(f"No Yahoo ticker mapping for {security.symbol}")
            return {'success': False, 'error': 'No ticker mapping'}

        # Rate limiting: 1-3 second delay
        await asyncio.sleep(random.uniform(1.0, 3.0))

        try:
            # Fetch data from yfinance (in thread to avoid blocking event loop)
            def _fetch():
                ticker = yf.Ticker(yahoo_ticker)
                return ticker.info

            info = await asyncio.to_thread(_fetch)

            if not info:
                logger.warning(f"No info data for {yahoo_ticker}")
                return {'success': False, 'error': 'No data'}

            sector = info.get('sector')
            industry = info.get('industry')
            country = info.get('country')

            # Determine asset type
            quote_type = info.get('quoteType', '')
            if quote_type == 'ETF':
                asset_type = 'ETF'
            else:
                asset_type = 'Stock'

            logger.info(f"{yahoo_ticker}: {sector}, {country}")

            return {
                'success': True,
                'asset_type': asset_type,
                'sector': sector,
                'industry': industry,
                'country': country,
            }

        except Exception as e:
            logger.error(f"Failed to fetch {yahoo_ticker}: {e}")
            return {'success': False, 'error': str(e)}

    async def sync_allocation_data(self, force_refresh: bool = False) -> Dict:
        """
        Sync allocation data for all securities.
        Only fetches data older than ALLOCATION_STALE_DAYS unless force_refresh=True.
        """
        logger.info("Syncing allocation data for securities")

        # Get all securities
        result = await self.db.execute(select(Security))
        securities = list(result.scalars().all())

        # Filter securities that need updates
        cutoff_date = utcnow() - timedelta(days=ALLOCATION_STALE_DAYS)
        securities_to_update = []

        for security in securities:
            if force_refresh:
                securities_to_update.append(security)
            elif security.allocation_last_updated is None:
                securities_to_update.append(security)
            elif security.allocation_last_updated < cutoff_date:
                securities_to_update.append(security)

        if not securities_to_update:
            logger.info("All securities have fresh allocation data")
            return {
                'securities_processed': 0,
                'securities_updated': 0,
                'errors': 0,
                'message': 'All allocation data is up to date'
            }

        logger.info(f"Updating {len(securities_to_update)} securities...")

        updated_count = 0
        error_count = 0

        for i, security in enumerate(securities_to_update, 1):
            logger.info(f"[{i}/{len(securities_to_update)}] Processing {security.symbol}...")

            result = await self.fetch_allocation_for_security(security)

            # Rule 1: stop on a rate limit. This loop needs the break more than the
            # others, because its failure path stamps `allocation_last_updated` to bound
            # retries against securities Yahoo genuinely has no `.info` for. That is
            # right for a real "no data" answer and badly wrong for a 429: one rate limit
            # would mark every remaining security as attempted and suppress its sector
            # and country for the full staleness window. So the check runs BEFORE the
            # stamp, and a rate-limited security keeps its old timestamp and is retried.
            if not result['success'] and is_rate_limit(result.get('error')):
                self.rate_limited = True
                logger.warning(
                    f"Yahoo rate limit at {security.symbol} "
                    f"({i}/{len(securities_to_update)}); abandoning the rest of this pass "
                    f"without stamping the remaining securities"
                )
                error_count += 1
                break

            if result['success']:
                # Update security with allocation data
                security.asset_type = result['asset_type']
                security.sector = result.get('sector')
                security.industry = result.get('industry')
                security.country = result.get('country')
                security.allocation_last_updated = utcnow()
                updated_count += 1
            else:
                # Stamp the *attempt*, not just the success. Selection above takes
                # every security whose timestamp is null or older than the cutoff, so
                # leaving it null on failure meant a security Yahoo has no `.info`
                # for was re-fetched on every sync forever — a 1-3s wait, a request
                # against an IP-based rate limit, and a 2-4s inter-security delay,
                # for a result already known. That is the same unbounded-retry shape
                # `HOLIDAY_GRACE_DAYS` closed for market prices and
                # `UPSTREAM_RETRY_COOLDOWN_SECONDS` closed for benchmarks.
                #
                # `sector`/`country`/`asset_type` are deliberately left untouched, so
                # the timestamp never implies data that was not fetched — see
                # `securities_without_data` in routers/allocation.py, which counts
                # missing *data* rather than a missing timestamp for exactly that
                # reason.
                security.allocation_last_updated = utcnow()
                error_count += 1

            # Security delay between different securities (2-4 seconds)
            if i < len(securities_to_update):
                await asyncio.sleep(random.uniform(2.0, 4.0))

        # Commit changes
        await self.db.commit()

        logger.info(f"Allocation sync complete: {updated_count} updated, {error_count} errors")

        return {
            'securities_processed': len(securities_to_update),
            'securities_updated': updated_count,
            'errors': error_count,
            'rate_limited': self.rate_limited,
            'message': f'Updated {updated_count} securities'
        }

    async def get_portfolio_allocation(self) -> Dict:
        """
        Get current portfolio allocation breakdown by sector and geography.
        Returns weighted percentages with position-level detail for drill-down.

        **A holding that cannot be valued is excluded and named.** It used to be included
        at a 0% weight, which is the quietest form of this app's most repeated failure:
        every slice is labelled "% of portfolio", the three breakdowns still sum to exactly
        100, and the missing holding is simply *absent* from a picture that looks complete.
        `test_an_unpriced_holding_does_not_break_the_percentages` pinned the sums and could
        not see it — summing to 100 is what being wrong looks like here.

        Same predicate and same exclude-and-count rule as `LookthroughService`'s
        `_split_by_valuability` and the forward yield: `market_value_eur > 0`, which covers
        both ways the backend fails to value a holding (no price, and no FX rate for the
        price's currency) without the client-side two-clause form.
        """
        from app.services.portfolio_service import PortfolioService

        portfolio_service = PortfolioService(self.db)
        all_positions = await portfolio_service.get_positions_breakdown()

        positions = [p for p in all_positions if p['market_value_eur'] > 0]
        unvaluable = [p for p in all_positions if p['market_value_eur'] <= 0]

        if not positions:
            return {
                'sector_allocation': {},
                'geographic_allocation': {},
                'asset_type_allocation': {},
                'total_market_value_eur': 0.0,
                'unpriced_holdings': len(unvaluable),
                'unpriced_symbols': [p['symbol'] for p in unvaluable],
            }

        total_value = sum(pos['market_value_eur'] for pos in positions)

        result = await self.db.execute(select(Security))
        securities = {sec.id: sec for sec in result.scalars().all()}

        # Each category stores: {name: {"weight": float, "market_value_eur": float, "positions": [...]}}
        sector_alloc: Dict[str, Dict] = {}
        geo_alloc: Dict[str, Dict] = {}
        asset_alloc: Dict[str, Dict] = {}

        def _add_to_category(
            store: Dict[str, Dict],
            category_name: str,
            weight: float,
            market_value: float,
            symbol: str,
            description: str,
            is_etf_contribution: bool = False,
        ):
            if category_name not in store:
                store[category_name] = {"weight": 0.0, "market_value_eur": 0.0, "positions": []}
            store[category_name]["weight"] += weight
            store[category_name]["market_value_eur"] += market_value
            # Merge into existing position entry if same symbol already present (ETF contributions)
            existing = next((p for p in store[category_name]["positions"] if p["symbol"] == symbol), None)
            if existing:
                existing["weight"] += weight
                existing["market_value_eur"] += market_value
            else:
                store[category_name]["positions"].append({
                    "symbol": symbol,
                    "description": description,
                    "weight": weight,
                    "market_value_eur": market_value,
                    "is_etf_contribution": is_etf_contribution,
                })

        for position in positions:
            security_id = position['security_id']
            security = securities.get(security_id)
            if not security:
                continue

            pos_value = position['market_value_eur']
            pos_weight = pos_value / total_value if total_value > 0 else 0
            sym = security.symbol
            desc = security.description or sym

            # Asset type. The `or 'Unknown'` here is the convention the other two
            # charts must follow: a holding whose category we don't know still owns
            # its share of the portfolio, so it gets a named bucket rather than being
            # dropped. Sector and geography silently dropped it until 2026-08-05.
            #
            # The look-through table wins over the column, because otherwise this chart
            # and the two below answer "is this a fund?" by different rules. They decide
            # it from a live table lookup needing no sync, while `securities.asset_type` is
            # written *only* by `POST /api/allocation/sync`, which nothing schedules.
            # `sync_helper` never writes it, so an IBKR-ingested fund carries the column
            # default "Stock" indefinitely: it would be a Stock here and an ETF
            # distributed across eleven sectors a few pixels away, on the same tab. Every
            # entry declares `asset_type: "ETF"` and `test_etf_mappings.py` pins that, so
            # the table is the better source.
            #
            # **Resolved once, by ISIN, and reused by all three charts.** These were three
            # separate symbol-keyed lookups, which is two problems: a ticker is not an
            # identity (the UCITS SMH held here shares its ticker with the US one, and a
            # *stock* colliding with a row would be spread across eleven sectors as if it
            # were a fund), and three lookups is three chances for the charts to disagree
            # about one holding — which they have already done twice.
            etf_data = allocation_for_fund_isin(security.isin)
            asset_type = (
                etf_data['asset_type'] if etf_data else security.asset_type
            ) or 'Unknown'
            _add_to_category(asset_alloc, asset_type, pos_weight, pos_value, sym, desc)

            # Funds: distribute across sectors/regions
            if etf_data:
                for sector, pct in etf_data['sector'].items():
                    w = pos_weight * (pct / 100)
                    mv = pos_value * (pct / 100)
                    _add_to_category(sector_alloc, sector, w, mv, sym, desc, is_etf_contribution=True)

                for region, pct in etf_data['geographic'].items():
                    w = pos_weight * (pct / 100)
                    mv = pos_value * (pct / 100)
                    _add_to_category(geo_alloc, region, w, mv, sym, desc, is_etf_contribution=True)
            else:
                # `or 'Unknown'`, not `if security.sector:` — a holding with no sector on
                # record is still part of the portfolio, and dropping it made these two
                # charts sum to less than 100 while the frontend labelled every slice
                # "% of portfolio". The treemap sizes by area and so renormalises, which
                # is what kept the gap invisible: the picture looked complete and only the
                # printed percentages were short.
                #
                # This is reached routinely rather than in theory. `sync_helper` never
                # writes sector or country, so a newly bought security has both NULL,
                # while `asset_type` carries a "Stock" column default — so a new holding
                # appeared in the asset-type chart and in neither of these. Only
                # `sync_allocation_data` fills them and nothing schedules it (it needs
                # Yahoo), so the gap lasts until someone runs it by hand.
                _add_to_category(
                    sector_alloc, security.sector or 'Unknown', pos_weight, pos_value, sym, desc)
                _add_to_category(
                    geo_alloc, security.country or 'Unknown', pos_weight, pos_value, sym, desc)

        def _finalize(store: Dict[str, Dict]) -> Dict:
            """Convert weights to percentages, sort, and round."""
            out = {}
            for name, data in sorted(store.items(), key=lambda x: x[1]["weight"], reverse=True):
                pct = round(data["weight"] * 100, 2)
                # Sort positions within category by weight descending
                pos_list = sorted(data["positions"], key=lambda p: p["weight"], reverse=True)
                for p in pos_list:
                    p["weight"] = round(p["weight"] * 100, 2)
                    p["market_value_eur"] = round(p["market_value_eur"], 2)
                out[name] = {
                    "percentage": pct,
                    "market_value_eur": round(data["market_value_eur"], 2),
                    "positions": pos_list,
                }
            return out

        return {
            'sector_allocation': _finalize(sector_alloc),
            'geographic_allocation': _finalize(geo_alloc),
            'asset_type_allocation': _finalize(asset_alloc),
            'total_market_value_eur': round(total_value, 2),
            # The completeness of all three breakdowns above. Every percentage is a share
            # of `total_market_value_eur`, which is the value that could be *priced* — so
            # anything above 0 means these charts describe less than the portfolio while
            # labelling each slice "% of portfolio".
            'unpriced_holdings': len(unvaluable),
            'unpriced_symbols': [p['symbol'] for p in unvaluable],
        }
