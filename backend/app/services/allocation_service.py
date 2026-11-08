"""
Allocation service for fetching and caching sector/geographic data for securities.
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal
from app.clock import utcnow
from typing import Optional, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select
import yfinance as yf

from app.models.security import Security
from app.services.yahoo_eligibility import yahoo_eligible
from app.services.percentages import share_pct
from app.services.sector_taxonomy import UNKNOWN
from app.services.yahoo_estimates import is_fund_quote_type
from app.services.yahoo_rate_limit import check_yahoo_hold, is_rate_limit
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


def needs_allocation_refresh(cutoff: datetime):
    """
    The one SQL predicate for "this security's allocation data needs fetching".

    `IS NULL` is the half that kept getting lost. SQL comparison against NULL is not
    true, so `allocation_last_updated < cutoff` silently excludes every security that
    has never been attempted — which on a fresh install is all of them, since nothing
    schedules `sync_allocation_data`. `/api/allocation/status` counted that way and
    answered `stale_securities: 0` beside a sync that was about to issue forty Yahoo
    requests, and its comment claimed it used "the same threshold the sync selects on".
    The *threshold* was shared; the predicate was not. Exactly the sentence CLAUDE.md
    already records for `stale_metrics: 0`, and the same tell — a comment asserting
    alignment.

    Returns a clause rather than a bool so the status count and the sync's own selection
    are the same expression evaluated by the same engine.
    """
    return or_(
        Security.allocation_last_updated.is_(None),
        Security.allocation_last_updated < cutoff,
    )


def asset_type_for_quote_type(quote_type: Optional[str]) -> str:
    """'ETF' for every `FUND_QUOTE_TYPES` (a MUTUALFUND was 'Stock' here), else 'Stock'."""
    return 'ETF' if is_fund_quote_type(quote_type) else 'Stock'


def allocation_from_info(info: Dict) -> Dict:
    """The allocation columns a Yahoo `.info` carries, keyed by `Security` attribute."""
    return {
        'asset_type': asset_type_for_quote_type(info.get('quoteType')),
        'sector': info.get('sector'),
        'industry': info.get('industry'),
        'country': info.get('country'),
    }


def apply_allocation(security: Security, allocation: Dict) -> None:
    """
    Write `allocation_from_info`'s columns and stamp them fresh.

    Shared by this sync and the daily fundamentals pass, which already holds the same
    `.info` (GitHub issue #11, item c): a new holding gets its sector and country the
    next morning instead of staying 'Unknown' until someone presses the Allocation
    tab's button, and this sync then skips it — three requests saved per security.
    """
    security.asset_type = allocation['asset_type']
    security.sector = allocation.get('sector')
    security.industry = allocation.get('industry')
    security.country = allocation.get('country')
    security.allocation_last_updated = utcnow()


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
            # A refusal anywhere in the process holds this too (issue #57); its text
            # reaches the loop's `is_rate_limit` like a 429's.
            await check_yahoo_hold()

            # Fetch data from yfinance (in thread to avoid blocking event loop)
            def _fetch():
                ticker = yf.Ticker(yahoo_ticker)
                return ticker.info

            info = await asyncio.to_thread(_fetch)

            if not info:
                logger.warning(f"No info data for {yahoo_ticker}")
                return {'success': False, 'error': 'No data'}

            allocation = allocation_from_info(info)
            logger.info(f"{yahoo_ticker}: {allocation['sector']}, {allocation['country']}")

            return {'success': True, **allocation}

        except Exception as e:
            logger.error(f"Failed to fetch {yahoo_ticker}: {e}")
            return {'success': False, 'error': str(e)}

    async def sync_allocation_data(self, force_refresh: bool = False) -> Dict:
        """
        Sync allocation data for all securities.
        Only fetches data older than ALLOCATION_STALE_DAYS unless force_refresh=True.
        """
        logger.info("Syncing allocation data for securities")

        # Selected through the shared predicate rather than filtered in Python, so
        # `/api/allocation/status` and this loop cannot disagree about which securities
        # are pending — see needs_allocation_refresh.
        cutoff_date = utcnow() - timedelta(days=ALLOCATION_STALE_DAYS)
        # yahoo_eligible(): this loop fetches `.info` per security. A fund Yahoo
        # has never heard of has no sector and no country there either.
        query = select(Security).where(yahoo_eligible())
        if not force_refresh:
            query = query.where(needs_allocation_refresh(cutoff_date))
        result = await self.db.execute(query)
        securities_to_update = list(result.scalars().all())

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
                apply_allocation(security, result)
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
        The Sectors, Regions and Asset types breakdowns, each a partition of the book, with a
        per-holding drill-down under every slice.

        **Sectors and regions are a rollup of the look-through's decomposition**
        (`LookthroughService.decompose`, since 2026-11-08): every company a holding is made of —
        a fund's through its stored basket — classified by the value-weighted vote the
        Look-through treemap groups on. A slice here is therefore the sum of the treemap's
        companies in it, and the two tabs cannot disagree about one. Until then this tab spread
        each fund by `etf_mappings.py`'s hand-written percentages while the Look-through measured
        the same funds, and its Regions chart printed Yahoo's countries beside those hand-written
        regions ("United States" 62% next to "North America" 12%). Value no company carries —
        a decomposed fund's cash and nested funds, or all of a fund with no basket — is `Unknown`
        under that fund, never spread by an estimate.

        **Asset types are per holding**: the fund registry's answer, by ISIN
        (`allocation_for_fund_isin`), else the security's own column, else `Unknown`.

        **A holding that cannot be valued is excluded and named.** It used to be included
        at a 0% weight, which is the quietest form of this app's most repeated failure:
        every slice is labelled "% of portfolio", the three breakdowns still sum to exactly
        100, and the missing holding is simply *absent* from a picture that looks complete.
        The decomposition applies `cash_service.is_valuable`, the predicate the forward yield
        asks too, and `unpriced_holdings` names what it left out.

        **Uninvested cash is a bucket in all three breakdowns**, so the three share one
        denominator — holdings plus positive cash, the base Positions and the Look-through
        divide by. Cash has no sector and no country, so it is named `Cash` rather than folded
        into `Unknown`. A **negative** balance (a margin debit) is left out of both the slice
        and the denominator and reported as `cash_eur`: a pie cannot draw a negative slice, and
        renormalising around one would quietly inflate every other holding.

        **Every amount is summed unrounded and published once.** A basket whose weights add up
        a hair over 100 (IWDA publishes 100.01) leaves its fund a negative remainder of cents;
        it nets inside `Unknown`, and a drill-down row or a slice that comes to nothing or less
        is not drawn rather than drawn negative.
        """
        from app.services.lookthrough_service import LookthroughService

        decomposition = await LookthroughService(self.db).decompose()
        cash = decomposition.cash
        # Absent when unknown, never 0.00 — the Look-through's reading of the same balance.
        cash_eur = round(float(cash.balance), 2) if cash.balance is not None else None
        # Only a positive balance is a slice — see the docstring.
        cash_slice = cash.in_book
        unvaluable = decomposition.unvaluable

        if decomposition.empty and not cash_slice:
            return {
                'sector_allocation': {},
                'geographic_allocation': {},
                'asset_type_allocation': {},
                'total_market_value_eur': 0.0,
                'cash_eur': cash_eur,
                'cash_source': cash.source,
                'total_value_eur': 0.0,
                'unpriced_holdings': len(unvaluable),
                'unpriced_symbols': [p['symbol'] for p in unvaluable],
            }

        holdings_value = decomposition.total
        total_value = holdings_value + cash_slice

        securities: Dict[int, Security] = {}
        if decomposition.holdings:
            result = await self.db.execute(
                select(Security).where(Security.id.in_(list(decomposition.holdings)))
            )
            securities = {sec.id: sec for sec in result.scalars().all()}

        # Each store: {name: {"value": Decimal, "positions": {symbol: row}}}
        sector_alloc: Dict[str, Dict] = {}
        geo_alloc: Dict[str, Dict] = {}
        asset_alloc: Dict[str, Dict] = {}

        def _add(store: Dict[str, Dict], category: str, value: Decimal,
                 holding: Optional[Dict], via_fund: bool) -> None:
            bucket = store.setdefault(category, {"value": Decimal("0"), "positions": {}})
            bucket["value"] += value
            # One row per symbol: two listings sharing a symbol merge, and the row keeps the
            # first listing's security for its logo. Cash has none.
            symbol = holding["symbol"] if holding else "CASH"
            row = bucket["positions"].get(symbol)
            if row is None:
                row = bucket["positions"][symbol] = {
                    "symbol": symbol,
                    "security_id": holding["security_id"] if holding else None,
                    "description": holding["description"] if holding else "Uninvested cash",
                    "value": Decimal("0"),
                    "is_etf_contribution": via_fund,
                }
            row["value"] += value

        for security_id, holding in decomposition.holdings.items():
            registry = allocation_for_fund_isin(holding["isin"])
            security = securities.get(security_id)
            asset_type = (
                registry['asset_type'] if registry
                else (security.asset_type if security else None)
            ) or UNKNOWN
            _add(asset_alloc, asset_type, holding["value"], holding, via_fund=False)

        for company in decomposition.companies:
            for security_id, value in company["_holdings"].items():
                if not value:
                    continue  # a constituent its issuer published at 0.00%
                holding = decomposition.holdings[security_id]
                _add(sector_alloc, company["sector"], value, holding, holding["is_fund"])
                _add(geo_alloc, company["_region"], value, holding, holding["is_fund"])

        for security_id, value in decomposition.unattributed.items():
            if not value:
                continue
            holding = decomposition.holdings[security_id]
            _add(sector_alloc, UNKNOWN, value, holding, via_fund=True)
            _add(geo_alloc, UNKNOWN, value, holding, via_fund=True)

        # Cash, into all three, so the three denominators stay one denominator.
        if cash_slice > 0:
            for store in (sector_alloc, geo_alloc, asset_alloc):
                _add(store, 'Cash', cash_slice, None, via_fund=False)

        def _finalize(store: Dict[str, Dict]) -> Dict:
            """
            Largest first (ties on the name), each share through `share_pct`, money rounded
            once. Every share is that money over `total_value`, rounded in Decimal by the one
            rule (issue #71's family).
            """
            out = {}
            ordered = sorted(store.items(), key=lambda kv: (-kv[1]["value"], kv[0]))
            for name, bucket in ordered:
                if bucket["value"] <= 0:
                    continue
                rows = sorted(
                    (r for r in bucket["positions"].values() if r["value"] > 0),
                    key=lambda r: (-r["value"], r["symbol"]),
                )
                out[name] = {
                    "percentage": share_pct(bucket["value"], total_value),
                    "market_value_eur": float(round(bucket["value"], 2)),
                    "positions": [
                        {
                            "symbol": r["symbol"],
                            "security_id": r["security_id"],
                            "description": r["description"],
                            "weight": share_pct(r["value"], total_value),
                            "market_value_eur": float(round(r["value"], 2)),
                            "is_etf_contribution": r["is_etf_contribution"],
                        }
                        for r in rows
                    ],
                }
            return out

        return {
            'sector_allocation': _finalize(sector_alloc),
            'geographic_allocation': _finalize(geo_alloc),
            'asset_type_allocation': _finalize(asset_alloc),
            # Holdings only, kept under its historical name. The percentages above are
            # shares of `total_value_eur`, which adds cash — read that one as the base.
            'total_market_value_eur': float(round(holdings_value, 2)),
            'cash_eur': cash_eur,
            'cash_source': cash.source,
            'total_value_eur': float(round(total_value, 2)),
            # The completeness of all three breakdowns above. Every percentage is a share
            # of what could be *valued*, so anything above 0 means these charts describe
            # less than the portfolio while labelling each slice "% of portfolio".
            'unpriced_holdings': len(unvaluable),
            'unpriced_symbols': [p['symbol'] for p in unvaluable],
        }
