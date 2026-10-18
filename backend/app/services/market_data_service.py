"""
Market Data Service
Fetches historical price data using Yahoo Finance (primary) and Alpha Vantage (fallback).
Handles exchange-specific ticker symbols for international stocks.
"""
from typing import List, Dict, Optional, Tuple
from datetime import date, datetime, timedelta
from decimal import Decimal
import logging
import httpx
import yfinance as yf
import random
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.repositories.market_price_repository import MarketPriceRepository
from app.repositories.security_repository import held
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.yahoo_rate_limit import is_rate_limit
from app.services.yahoo_eligibility import is_sibling_priced, is_yahoo_eligible
from sqlalchemy import func, select
from app.models.market_price import MarketPrice, PRICE_ROW_SOURCE_SIBLING_SCALED
from app.services.finpension_ingest import PRICE_SOURCE_STATEMENT

logger = logging.getLogger(__name__)

# Days of slack added before the oldest missing price when fetching. Yahoo's range
# is start-inclusive but a split or a holiday near the edge can shift what it
# returns, so a few days of overlap costs nothing and closes that gap.
PRICE_FETCH_BUFFER_DAYS = 5

# How long after its last lot closes a sold security stays in the short (7-day) price
# passes. Nothing reads a sold security's price intraday; valuation reads one on or
# shortly before the close date (`realized_rows_from_closed_lots` looks back
# `portfolio_service.PRICE_LOOKBACK_DAYS`, 14), and that close must have time to settle
# (`PROVISIONAL_PRICE_DAYS`). 14 is spelled out because portfolio_service imports this
# module; `tests/test_price_pass_scope.py` pins the sum. After the grace a sold security
# is priced by the long 18:00 pass only (`sync_securities`).
SOLD_PRICE_GRACE_DAYS = MarketPriceRepository.PROVISIONAL_PRICE_DAYS + 14

# The 18:00 pass repairs a sold security's held span (first open .. last close) while it
# has a gap. A close date the feed has no bar for — a holiday on the listing's exchange
# (a heuristic close carries the statement's US to_date), a delisting or merger effective
# date — is a trailing gap of that span, which the holiday rule never forgives, so the
# security was re-fetched up to its close every day, forever, writing nothing (GitHub
# issue #33). The span end is fixed, and valuation looks back PRICE_LOOKBACK_DAYS from a
# close date, so once the close is older than HOLIDAY_GRACE_DAYS the repair probe
# forgives a trailing run of up to this many weekdays: one trading week, the longest
# routine exchange closure (Golden Week, most Lunar New Year closures). A longer trailing
# gap, a leading one, or a younger close still repairs. `tests/test_sold_close_date_without_bar.py`
# pins that it stays inside the lookback.
REPAIR_TRAILING_GRACE_WEEKDAYS = 5

# Back-off after every provider came back empty for a sold security's missing dates: one
# day, doubling per consecutive miss, capped at 30 days. Less a few hours of slack so a
# security stamped during a pass is due again at the same slot a day later, not the one
# after (the fundamentals' 20-hour STALE_AFTER, for the same reason). A held security
# never backs off: Yahoo soft-throttling with empty answers rather than a 429 would
# otherwise freeze every holding's price for a day, and a holding with a broken mapping
# is already surfaced by the stale-price warning.
PRICE_MISS_BACKOFF_SOLD_CAP_DAYS = 30
PRICE_MISS_BACKOFF_SLACK = timedelta(hours=4)

# Alpha Vantage's free tier serves `outputsize=compact` only: the latest 100 trading
# days, which span at least 137 calendar days. A gap reaching further back than this
# cannot be filled by it, so the fallback is not asked at all.
ALPHA_VANTAGE_COMPACT_REACH_DAYS = 130


def chart_actions(hist) -> Tuple[List[Tuple], List[date]]:
    """
    The dividends and split dates a `history()` frame carried, at no extra request.

    yfinance asks `v8/finance/chart` for `events=div,splits,capitalGains` on every
    history call and merges them into the frame (`actions=True`, its default). The
    dividends come back as `(index, amount)` pairs — exactly what `Ticker.dividends`
    (the same endpoint over the whole history) iterates as — and the split dates as
    exchange-local dates. Empty when the frame has neither column.
    """
    dividends: List[Tuple] = []
    splits: List[date] = []
    if "Dividends" in hist.columns:
        column = hist["Dividends"]
        dividends = list(column[column != 0].items())
    if "Stock Splits" in hist.columns:
        column = hist["Stock Splits"]
        splits = [index.date() for index in column[column != 0].index]
    return dividends, splits


def price_backoff_until(security: Security, is_held: bool) -> Optional[datetime]:
    """When the price loop may next ask about `security`, or None when it is not backing off."""
    misses = security.price_miss_count or 0
    if is_held or security.price_checked_at is None or misses < 1:
        return None
    days = min(2 ** min(misses - 1, 10), PRICE_MISS_BACKOFF_SOLD_CAP_DAYS)
    return security.price_checked_at + timedelta(days=days) - PRICE_MISS_BACKOFF_SLACK


def clear_price_backoff(security: Security) -> None:
    """A row was written or the mapping changed: the next pass asks again."""
    security.price_checked_at = None
    security.price_miss_count = None


class MarketDataService:
    """
    Service for fetching and caching market price data.

    Primary source: Yahoo Finance (yfinance) - Free, excellent international coverage
    Fallback: Alpha Vantage API - Requires API key, primarily US stocks

    Handles exchange-specific ticker formatting:
    - US stocks: "AMZN" (no suffix)
    - German stocks (XETRA): "AMZ.DE"
    - UK stocks: "HSBA.L"
    - etc.
    """

    # Latched the first time Yahoo answers with a rate limit; see __init__ for why it
    # exists. Declared on the class as well as assigned per instance because the tests
    # build a service through `__new__` to skip the DB, and a read of a
    # never-initialised attribute would fail there rather than in production — a shape
    # that turns a safety net into a crash in the one place it is exercised.
    rate_limited = False
    # The same latch for the Alpha Vantage fallback: set when AV answers with a `Note` or
    # `Information` notice (its daily cap, or a premium-only parameter), so no later
    # security in the pass spends a call on it. Per instance, so per pass.
    alpha_vantage_capped = False

    # Mapping of IBKR exchange codes to Yahoo Finance suffixes
    EXCHANGE_SUFFIXES = {
        # US Exchanges
        'NASDAQ': '',
        'NYSE': '',
        'ARCA': '',
        'AMEX': '',
        'BATS': '',

        # European Exchanges
        'XETRA': '.DE',      # Germany (Frankfurt Electronic)
        'FWB': '.F',         # Frankfurt
        'SWB': '.STU',       # Stuttgart
        'IBIS': '.DE',       # Germany
        'IBIS2': '.DE',      # Germany (IBIS2)
        'LSE': '.L',         # London
        'LSEETF': '.L',      # London Stock Exchange ETF
        'LSEIOB1': '.L',     # London IOB
        'EURONEXT': '.PA',   # Paris (default)
        'AEB': '.AS',        # Amsterdam
        'BM': '.MC',         # Madrid
        'SBF': '.PA',        # Paris
        'EBS': '.SW',        # Swiss

        # Asian Exchanges
        'SEHK': '.HK',       # Hong Kong
        'TSE': '.T',         # Tokyo by default; CAD listings use Toronto (.TO)
        'KRX': '.KS',        # Korea
        'TWSE': '.TW',       # Taiwan

        # Other
        'TSX': '.TO',        # Toronto
        'ASX': '.AX',        # Australia
    }

    def _get_exchange_suffix(self, security: Security) -> str:
        """
        Resolve an IBKR exchange to a Yahoo Finance suffix.

        IBKR may report Toronto Stock Exchange listings as TSE, which conflicts
        with Yahoo's Tokyo suffix. Use the security currency to disambiguate.
        """
        exchange = security.exchange

        if exchange == 'TSE':
            if security.currency == 'CAD':
                return '.TO'
            if security.currency == 'JPY':
                return '.T'

        return self.EXCHANGE_SUFFIXES.get(exchange, '')

    # Per-fetch state (see `__init__`), on the class too so a service built through
    # `__new__` in a test can read it.
    yahoo_chart_actions = None
    _last_chart_actions = None

    def __init__(self, db: AsyncSession):
        self.db = db
        self.market_price_repo = MarketPriceRepository(db)
        from app.repositories.ticker_mapping_repository import TickerMappingRepository
        self.ticker_mapping_repo = TickerMappingRepository(db)

        # Latched by `fetch_prices_from_yahoo` the first time Yahoo answers with a
        # rate limit, and read by `fetch_and_cache_prices` to refuse every later
        # security in the same pass.
        #
        # CLAUDE.md already credited this service with "rate-limit detection that
        # aborts the run" and it only ever aborted the *ticker variations* for the
        # one security: the caller logged a failure and moved straight on to the
        # next of ~40, so a run that had already been told to back off kept asking
        # for several more minutes. Cheap to ignore at three market-data passes a
        # day; not at seven. The flag lives on the instance, and a pass builds its
        # own, so it is per-run state and never leaks into a later job.
        self.rate_limited = False
        self.alpha_vantage_capped = False

        # `chart_actions` of the response whose prices `fetch_prices_from_yahoo` returned,
        # or None (no Yahoo prices, or a fallback provider's). `_last_chart_actions` is
        # `(ticker, actions)` of the last `_try_fetch_yahoo` that parsed prices.
        self.yahoo_chart_actions = None
        self._last_chart_actions = None

        # yfinance 1.1.0+ handles sessions and User-Agent headers internally
        # No need to create a custom session

    async def _get_yahoo_ticker(self, security: Security) -> str:
        """
        Convert IBKR security info to Yahoo Finance ticker format.
        Checks custom ticker mappings first, then falls back to exchange suffix logic.

        Args:
            security: Security object with symbol and exchange

        Returns:
            Yahoo Finance ticker (e.g., "AMZN", "AMZ.DE", "HSBA.L", "XNAS.DE")
        """
        symbol = security.symbol
        exchange = security.exchange

        if not exchange:
            # No exchange info, try symbol as-is
            return symbol

        # First, check for custom ticker mapping in database
        mapping = await self.ticker_mapping_repo.get_mapping(symbol, exchange)
        if mapping:
            logger.debug(f"Using custom ticker mapping: {symbol}@{exchange} -> {mapping.yahoo_ticker}")
            return mapping.yahoo_ticker

        # Fall back to exchange suffix logic
        suffix = self._get_exchange_suffix(security)
        ticker = f"{symbol}{suffix}"

        return ticker

    def _get_yahoo_ticker_variations(self, security: Security) -> list[str]:
        """
        Get multiple ticker variations to try if the primary ticker fails.
        Useful for securities that might have different tickers on Yahoo.

        Args:
            security: Security object

        Returns:
            List of ticker variations to try
        """
        symbol = security.symbol
        exchange = security.exchange
        variations = []

        # Primary ticker (using suffix logic)
        suffix = self._get_exchange_suffix(security)
        primary = f"{symbol}{suffix}"
        variations.append(primary)

        # For German exchanges, try both .DE and .F suffixes
        if exchange in ['XETRA', 'IBIS', 'IBIS2', 'FWB']:
            if '.DE' not in primary:
                variations.append(f"{symbol}.DE")
            if '.F' not in primary:
                variations.append(f"{symbol}.F")

        # IBKR can use TSE for Toronto listings; Yahoo uses .TO.
        if exchange == 'TSE' and security.currency == 'CAD' and primary != f"{symbol}.TO":
            variations.append(f"{symbol}.TO")

        # Try symbol without suffix (works for some securities)
        if suffix and symbol not in variations:
            variations.append(symbol)

        return variations

    async def fetch_prices_from_yahoo(
        self,
        security: Security,
        start_date: date,
        end_date: date
    ) -> List[Dict]:
        """
        Fetch historical prices from Yahoo Finance with smart retry logic.

        Args:
            security: Security object
            start_date: Start date
            end_date: End date

        Returns:
            List of dicts with date, close_price, currency
        """
        # Get primary ticker (checks custom mappings first)
        ticker = await self._get_yahoo_ticker(security)
        self.yahoo_chart_actions = None
        self._last_chart_actions = None
        accepted = ticker

        # Try primary ticker first
        prices, rate_limited = await self._try_fetch_yahoo(ticker, security, start_date, end_date)

        # If we hit rate limit, stop immediately - don't try variations
        if rate_limited:
            logger.warning(f"Rate limit hit on {ticker}, stopping variations to avoid further blocking")
            self.rate_limited = True
            return []

        # If primary fails (but not rate limited), try variations
        if not prices:
            logger.info(f"Primary ticker {ticker} failed, trying variations...")
            variations = self._get_yahoo_ticker_variations(security)

            for alt_ticker in variations:
                if alt_ticker == ticker:
                    continue  # Already tried this one

                logger.info(f"Trying alternative ticker: {alt_ticker}")
                prices, rate_limited = await self._try_fetch_yahoo(alt_ticker, security, start_date, end_date)

                # If we hit rate limit during variations, stop immediately
                if rate_limited:
                    logger.warning(f"Rate limit hit on variation {alt_ticker}, stopping")
                    self.rate_limited = True
                    return []

                if prices:
                    # A variation that returns data is not necessarily the right
                    # instrument. The last variation tried is the bare symbol, which
                    # readily collides with an unrelated US listing of the same
                    # ticker — and because the mapping is then saved, the wrong
                    # instrument becomes sticky and shadows the suffix logic forever
                    # (this is how SBI@TSE, a Toronto CAD stock, ended up priced off a
                    # USD fund for months). The quote currency is the cheap check that
                    # catches it, so reject a mismatch instead of adopting it.
                    fetched_currency = prices[0]['currency']
                    if security.currency and fetched_currency != security.currency:
                        logger.warning(
                            f"Rejecting {alt_ticker} for {security.symbol}@{security.exchange}: "
                            f"quoted in {fetched_currency}, but the security is "
                            f"{security.currency} — almost certainly a different instrument"
                        )
                        prices = []
                        continue

                    # Success! Save this mapping for future use
                    logger.info(f"Success with {alt_ticker}, saving mapping")
                    await self.ticker_mapping_repo.upsert_mapping(
                        ibkr_symbol=security.symbol,
                        ibkr_exchange=security.exchange,
                        yahoo_ticker=alt_ticker,
                        source="auto",
                        notes=f"Auto-discovered from {ticker}"
                    )
                    accepted = alt_ticker
                    break

        # The dividends ride with the prices that were kept, never with a variation
        # the currency check rejected.
        last = self._last_chart_actions
        if prices and last is not None and last[0] == accepted:
            self.yahoo_chart_actions = last[1]
        return prices

    # Tickers where the suffix-based currency guess is wrong
    # e.g. SMH.L trades in USD on London, not GBP
    TICKER_CURRENCY_OVERRIDES = {
        'SMH.L': 'USD',
    }

    # Yahoo quotes some listings in a currency's MINOR unit and says so in the code it
    # reports: London equities in `GBp` (pence), Johannesburg in `ZAc`, Tel Aviv in
    # `ILA`. The row's *label* should still be the major code, but the *amount* has to
    # be divided or a 5-pound price is stored as 500.
    #
    # Uppercasing `GBp` to `GBP` without scaling — which is what this did — does not
    # merely miss the conversion, it **defeats the currency guard**: the normalised code
    # matches the security's own currency, so the "reject a variation whose currency
    # disagrees" check passes and nothing downstream can see a position carried 100x
    # high. That is the SBI failure with its one safeguard removed, which is why this is
    # a scale and not just a rename.
    #
    # Explicit rather than inferred from letter case: `GBp` and `ZAc` are mixed-case but
    # `ILA` is not, so "not all-uppercase means minor unit" would silently miss Tel Aviv.
    # Extend the map when a new venue appears, the way EXCHANGE_SUFFIXES is extended.
    MINOR_UNIT_CURRENCIES = {
        'GBp': ('GBP', Decimal('100')),
        'ZAc': ('ZAR', Decimal('100')),
        'ILA': ('ILS', Decimal('100')),
    }

    @classmethod
    def _minor_unit(cls, reported_currency: Optional[str]):
        """`('GBP', Decimal(100))` when Yahoo reported a minor unit, else None."""
        return cls.MINOR_UNIT_CURRENCIES.get((reported_currency or '').strip())

    def _get_currency_from_ticker(
        self,
        ticker: str,
        security: Security,
        reported_currency: Optional[str] = None
    ) -> str:
        """
        Determine the currency a Yahoo Finance ticker is quoted in.

        Precedence: explicit override > what Yahoo reported > exchange-suffix
        inference > the security's own currency.

        `reported_currency` comes straight from the price response and is right far
        more often than the suffix guess, but it still loses to an override — those
        entries exist because someone checked the listing by hand after the automatic
        answer proved wrong. The two tiers below it are guesses, and the last one is
        the guess that mislabelled a USD instrument as CAD for SBI@TSE.
        """
        # Check explicit overrides first
        if ticker in self.TICKER_CURRENCY_OVERRIDES:
            return self.TICKER_CURRENCY_OVERRIDES[ticker]

        # A minor-unit report names its major currency here; `fetch_prices_from_yahoo`
        # divides the amount to match. The two must move together — labelling pence GBP
        # without scaling is the 100x error described on MINOR_UNIT_CURRENCIES.
        minor = self._minor_unit(reported_currency)
        if minor:
            return minor[0]

        if reported_currency and len(reported_currency) == 3:
            return reported_currency.upper()

        # Map of Yahoo Finance suffixes to currencies
        suffix_currency_map = {
            '.DE': 'EUR',  # Germany (Xetra)
            '.F': 'EUR',   # Frankfurt
            '.STU': 'EUR', # Stuttgart
            '.L': 'GBP',   # London
            '.AS': 'EUR',  # Amsterdam
            '.PA': 'EUR',  # Paris
            '.MC': 'EUR',  # Madrid
            '.MI': 'EUR',  # Milan
            '.SW': 'CHF',  # Swiss
            '.HK': 'HKD',  # Hong Kong
            '.T': 'JPY',   # Tokyo
            '.TO': 'CAD',  # Toronto
            '.TW': 'TWD',  # Taiwan
            '.KS': 'KRW',  # Korea — EXCHANGE_SUFFIXES maps KRX here, but this map didn't
            '.AX': 'AUD',  # Australia
        }

        # Check if ticker has a known suffix
        for suffix, currency in suffix_currency_map.items():
            if ticker.endswith(suffix):
                return currency

        # No suffix or unknown suffix - use security currency
        return security.currency

    async def _try_fetch_yahoo(
        self,
        ticker: str,
        security: Security,
        start_date: date,
        end_date: date
    ) -> Tuple[List[Dict], bool]:
        """
        Attempt to fetch prices from Yahoo Finance with a specific ticker.

        Returns:
            Tuple of (prices_list, rate_limited)
            - prices_list: List of price dicts, empty if ticker doesn't work
            - rate_limited: True if we hit a rate limit (stop trying variations)
        """
        try:
            # Random delay between 2-5 seconds to look more human
            delay = random.uniform(2.0, 5.0)
            await asyncio.sleep(delay)

            # Download data from Yahoo Finance (in thread to avoid blocking event loop)
            # Note: yfinance 1.1.0+ handles sessions and User-Agent internally
            def _fetch_history():
                yf_ticker = yf.Ticker(ticker)
                hist = yf_ticker.history(
                    start=start_date,
                    end=end_date + timedelta(days=1),  # yfinance end is exclusive
                    auto_adjust=False  # Get actual close prices, not adjusted
                )
                # Yahoo states the quote currency in the metadata of the response we
                # just received, so reading it costs no extra request. Deliberately
                # NOT the public `history_metadata` property: it re-requests at an
                # intraday interval whenever 'tradingPeriods' is missing, which a daily
                # history() never populates — that would have quietly doubled our
                # Yahoo traffic to two requests per security. Read the cached dict
                # instead, and degrade to suffix inference if yfinance moves it.
                reported = None
                try:
                    meta = getattr(yf_ticker._price_history, '_history_metadata', None)
                    reported = (meta or {}).get('currency')
                except Exception:
                    pass
                return hist, reported

            hist, reported_currency = await asyncio.to_thread(_fetch_history)

            if hist.empty:
                return [], False  # No data, but not rate limited

            # Determine the correct currency for this ticker, preferring what Yahoo
            # actually reported over a guess from the suffix. Guessing is what hid the
            # SBI@TSE error: bare `SBI` matched a US fund quoted in USD, and the "no
            # suffix, so use the security's currency" fallback stamped it CAD, so
            # nothing downstream could see the position was 61% too high.
            price_currency = self._get_currency_from_ticker(
                ticker, security, reported_currency
            )

            # Divide when Yahoo quoted a minor unit. Read off `reported_currency` rather
            # than off `price_currency`, which by then has been normalised to the major
            # code and can no longer tell pence from pounds — and skipped entirely when a
            # hand-checked override decided the currency, since an override means someone
            # looked at the listing and the reported code is not to be trusted.
            minor = (
                None if ticker in self.TICKER_CURRENCY_OVERRIDES
                else self._minor_unit(reported_currency)
            )
            minor_scale = minor[1] if minor else None
            if minor_scale:
                logger.info(
                    f"{ticker} quoted in {reported_currency} (minor units); dividing "
                    f"closes by {minor_scale} and storing as {price_currency}"
                )

            prices = []
            for date_index, row in hist.iterrows():
                price_date = date_index.date()
                close_price = row['Close']

                if close_price and not (close_price != close_price):  # Check for NaN
                    close_decimal = Decimal(str(close_price))
                    if minor_scale:
                        close_decimal /= minor_scale
                    prices.append({
                        'date': price_date,
                        'close_price': close_decimal,
                        'currency': price_currency,
                        # Each fetcher tags its own rows; the caller used to hardcode
                        # 'yahoo_finance' for both providers.
                        'source': 'yahoo_finance',
                    })

            if prices:
                # The dividends in this same response (GitHub issue #11, item a): stored
                # by `fetch_and_cache_prices` if these prices are the ones kept. Parsing
                # only — never allowed to cost the prices.
                try:
                    self._last_chart_actions = (ticker, chart_actions(hist))
                except Exception as e:
                    logger.warning(f"Could not read chart dividends for {ticker}: {e}")

            return prices, False

        except Exception as e:
            # Shared with the fundamentals, ratings and watchlist loops
            # (`yahoo_rate_limit.is_rate_limit`) so all four agree on what a rate limit
            # is. It stays deliberately narrow: this same verdict decides whether to
            # keep trying ticker *variations* below, so a broader match would abort
            # auto-discovery for every ticker Yahoo simply does not know.
            if is_rate_limit(e):
                logger.warning(f"Rate limit detected for {ticker}: {e}")
                return [], True  # Rate limited - stop trying variations

            # For other errors (invalid ticker, etc.), continue trying variations
            return [], False

    async def fetch_prices_from_alpha_vantage(
        self,
        security: Security,
        outputsize: str = "compact"
    ) -> List[Dict]:
        """
        Fetch prices from Alpha Vantage API (fallback).
        Note: Free tier primarily supports US stocks.

        Asks about a security at most once a UTC day (`alpha_vantage_asked_at`, so the
        limit survives a restart) and not at all once AV has said its cap is reached
        this pass. "full" is premium-only: on a free key AV answers it with an
        `Information` notice and no data, which is what the only caller asked for
        until 2026-10-17 (GitHub issue #10).

        Args:
            security: Security object
            outputsize: "compact" (the free tier's latest 100 days) or "full"

        Returns:
            List of dicts with date, close_price, currency
        """
        if not settings.alpha_vantage_api_key or settings.alpha_vantage_api_key == "your_api_key_here":
            return []  # Skip if no API key

        # This endpoint serves US listings and quotes them in USD, and the response
        # carries no currency to read back — unlike Yahoo, where the reported
        # currency is available and is what the row is tagged with. So a non-USD
        # security cannot be priced here honestly: stamping its own currency onto a
        # USD quote is exactly how SBI was carried 61% high for months. Refuse
        # rather than adopt, the same way a mismatched Yahoo ticker is refused.
        if (security.currency or "").upper() != "USD":
            logger.warning(
                f"Skipping Alpha Vantage for {security.symbol}: it quotes USD and this "
                f"security is {security.currency}, which cannot be verified from the response"
            )
            return []

        if self.alpha_vantage_capped:
            logger.info(f"Skipping Alpha Vantage for {security.symbol}: its cap was reached this run")
            return []
        asked = security.alpha_vantage_asked_at
        if asked is not None and asked.date() == utcnow().date():
            logger.info(f"Skipping Alpha Vantage for {security.symbol}: already asked today")
            return []
        # Stamped before the request: the free tier counts a call whatever it answers.
        security.alpha_vantage_asked_at = utcnow()

        params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": security.symbol,  # Alpha Vantage uses US symbols primarily
            "outputsize": outputsize,
            "apikey": settings.alpha_vantage_api_key
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    "https://www.alphavantage.co/query",
                    params=params
                )
                response.raise_for_status()
                data = response.json()

                if "Error Message" in data:
                    logger.error(f"Alpha Vantage error: {data['Error Message']}")
                    return []

                # `Note` is the older rate-limit notice, `Information` the current one —
                # the daily cap, or a premium-only parameter. Either way AV will answer
                # the next security the same, so stop asking it for the rest of the pass.
                for notice in ("Note", "Information"):
                    if notice in data:
                        logger.warning(
                            f"Alpha Vantage {notice} for {security.symbol}: {data[notice]} "
                            f"— not asking it again this run"
                        )
                        self.alpha_vantage_capped = True
                        return []

                if "Time Series (Daily)" not in data:
                    logger.warning(f"Alpha Vantage returned no daily series for {security.symbol}")
                    return []

                time_series = data["Time Series (Daily)"]
                prices = []

                for date_str, price_data in time_series.items():
                    price_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    close_price = price_data["4. close"]

                    prices.append({
                        'date': price_date,
                        'close_price': Decimal(str(close_price)),
                        # Safe only because of the USD guard above.
                        'currency': security.currency,
                        'source': 'alpha_vantage',
                    })

                return prices

        except Exception as e:
            logger.error(f"Error fetching Alpha Vantage data: {e}")
            return []

    async def fetch_and_cache_prices(
        self,
        security: Security,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        refresh_provisional: bool = True,
    ) -> int:
        """
        Fetch price data and cache in database.
        Only fetches missing dates to minimize API calls.

        Uses Yahoo Finance as primary source (free, good international coverage).
        Falls back to Alpha Vantage if Yahoo Finance fails (US stocks only).

        Includes polite delays between securities to avoid rate limiting.

        Args:
            security: Security object with symbol, exchange, and currency
            start_date: Start date (defaults to 1 year ago)
            end_date: End date (defaults to today)
            refresh_provisional: False skips re-fetching cached recent closes
                (`get_missing_dates`) — the weekend full sync

        Returns:
            Number of prices written — new rows plus provisional ones re-stated. It
            has counted rows *submitted* rather than rows newly inserted since
            bulk_create, so a run over a warm cache no longer reports 0.
        """
        # Yahoo has already told us to back off, so asking again for a different
        # security is the same IP making the same mistake. Ahead of the sleep as
        # The innermost gate, deliberately duplicated from `sync_securities`. This
        # function is reachable directly, so a rule enforced only at the loop leaks
        # the first time somebody calls the inner one -- and what leaks here is a
        # bare-symbol auto-mapping onto an unrelated listing, which is silent, sticky
        # and shadows the suffix logic even after the mapping is fixed.
        if not is_yahoo_eligible(security):
            logger.debug(
                f"Not fetching {security.symbol}@{security.exchange}: "
                f"price_source={security.price_source}"
            )
            return 0

        # well as the request: once a pass is abandoned there is nothing to pace.
        if self.rate_limited:
            logger.warning(
                f"Skipping {security.symbol}: Yahoo rate-limited this run"
            )
            return 0

        if not end_date:
            end_date = date.today()

        if not start_date:
            start_date = end_date - timedelta(days=365)

        # What we are missing outright, plus the trailing days whose cached row may
        # still be a mid-session price rather than a close (PROVISIONAL_PRICE_DAYS).
        missing_dates = await self.market_price_repo.get_missing_dates(
            security.id, start_date, end_date, refresh_provisional=refresh_provisional,
        )

        if not missing_dates:
            logger.debug(f"All prices already cached for {security.symbol} ({start_date} to {end_date})")
            # Add delay even when skipping to maintain consistent pacing
            await asyncio.sleep(random.uniform(2.0, 4.0))
            return 0

        # Ask only for the span that is actually missing. The 08:00 job passes a
        # 730-day window, so before this it re-downloaded two years per security
        # every morning purely because today's close had not published yet —
        # ~19.5k rows rewritten across 40 securities for a few hundred new ones.
        # A gap early in the range (a split purge, a new security) still pulls the
        # whole span, because that is where min(missing) then sits.
        fetch_start = max(start_date, min(missing_dates) - timedelta(days=PRICE_FETCH_BUFFER_DAYS))
        logger.info(
            f"Fetching {len(missing_dates)} missing/provisional prices for "
            f"{security.symbol} on {security.exchange} ({fetch_start}..{end_date})"
        )

        # Try Yahoo Finance first (primary source)
        self.yahoo_chart_actions = None
        prices_data = await self.fetch_prices_from_yahoo(
            security, fetch_start, end_date
        )
        # Read before any fallback: Alpha Vantage's prices carry no dividends.
        chart = self.yahoo_chart_actions

        # If Yahoo Finance returns nothing, try Alpha Vantage (US stocks only). Not when
        # Yahoo *refused* us: a 429 also comes back as an empty list, and falling through
        # spent one of the free tier's few daily calls and switched this security's
        # `source` column — the one every pricing diagnosis reads first — for a failure
        # that had nothing to do with the security.
        if (
            not prices_data
            and not self.rate_limited
            and security.exchange in ['NASDAQ', 'NYSE', 'ARCA', 'AMEX']
        ):
            # The free tier's `compact` reaches ~100 trading days back; an older gap is
            # beyond it, and `full` is premium-only, so the call would be spent for nothing.
            if (date.today() - min(missing_dates)).days > ALPHA_VANTAGE_COMPACT_REACH_DAYS:
                logger.info(
                    f"Not asking Alpha Vantage for {security.symbol}: the gap starts "
                    f"{min(missing_dates)}, beyond what its free tier serves"
                )
            else:
                logger.info(f"Yahoo Finance failed for {security.symbol}, trying Alpha Vantage...")
                prices_data = await self.fetch_prices_from_alpha_vantage(
                    security, outputsize="compact"
                )

        if not prices_data:
            logger.warning(f"Could not fetch any price data for {security.symbol}")
            # Every provider answered with nothing for dates we are missing: stamp it, so
            # `sync_securities` backs off instead of re-asking at every pass — a delisted
            # ticker's trailing gap never closes. Never on a rate limit: that is not an
            # answer about this security, and the next pass must resume it.
            if not self.rate_limited:
                security.price_miss_count = (security.price_miss_count or 0) + 1
                security.price_checked_at = utcnow()
                await self.db.commit()
            # Add delay before moving to next security
            await asyncio.sleep(random.uniform(3.0, 6.0))
            return 0

        # Filter to only missing dates and prepare for caching
        missing_dates_set = set(missing_dates)
        prices_to_cache = []

        for price_info in prices_data:
            if price_info['date'] in missing_dates_set:
                prices_to_cache.append({
                    'security_id': security.id,
                    'date': price_info['date'],
                    'close_price': price_info['close_price'],
                    'currency': price_info['currency'],
                    # The fetcher says where the row came from. This used to be the
                    # literal 'yahoo_finance' for both providers, with a comment
                    # conceding it was wrong when the Alpha Vantage fallback fired —
                    # so a fallback price claimed a provenance it did not have, in
                    # the one column every pricing diagnosis reads first.
                    'source': price_info.get('source', 'unknown'),
                })

        # Bulk insert
        if prices_to_cache:
            count = await self.market_price_repo.bulk_create(prices_to_cache)
            clear_price_backoff(security)
            await self.db.commit()
            logger.info(f"Cached {count} new prices for {security.symbol}")
            await self._store_chart_dividends(security, chart)

            # Add delay between securities to be polite to Yahoo Finance
            # 3-6 seconds feels human and keeps us well under rate limits
            delay = random.uniform(3.0, 6.0)
            logger.debug(f"Waiting {delay:.1f}s before next security...")
            await asyncio.sleep(delay)

            return count

        await self._store_chart_dividends(security, chart)
        return 0

    async def _store_chart_dividends(self, security: Security, chart) -> None:
        """
        Hand the kept response's dividends to `DividendService.store_chart_dividends`.

        After the prices are committed, and best effort: a dividend that cannot be stored
        here is read by the dividend pass's re-verify, so it must never cost the prices.
        A failed write is rolled back and `security` refreshed, because the callers read
        its attributes next and an expired one would raise MissingGreenlet.
        """
        if not chart:
            return
        dividends, split_dates = chart
        if not dividends and not split_dates:
            return
        from app.services.dividend_service import DividendService

        symbol = security.symbol
        try:
            stored = await DividendService(self.db).store_chart_dividends(
                security, dividends, split_dates
            )
            await self.db.commit()
            if stored:
                logger.info(f"Stored {stored} dividend(s) for {symbol} from its price response")
        except Exception as e:
            logger.warning(f"Could not store the chart dividends for {symbol}: {e}")
            await self.db.rollback()
            await self.db.refresh(security)

    # How far before the anchor date to look for the sibling's close when the anchor
    # day itself has none — a NAV struck on a Swiss holiday, or the two strike on
    # different clocks. Nearest *prior* close only: a later one would import a move the
    # NAV had not yet seen.
    SIBLING_ANCHOR_LOOKBACK_DAYS = 7

    async def sync_sibling_prices(
        self, security: Security, days_back: int = 730, refresh_provisional: bool = True,
    ) -> int:
        """
        Price a security from a sibling share class: statement NAV × sibling return.

        For a pension-only fund tranche Yahoo does not quote (`PRICE_SOURCE_SIBLING`).
        Its provider prints a NAV only on transaction rows, so the previous design
        carried the last NAV forward 45 business days and let the position go unpriced
        after that — dropping ~0.6% of the book out of the total to avoid a stale price
        error of a fraction of that. The owner's call on 2026-09-12: the share count
        never changes between uploads, and another share class of the *same fund* is
        quoted daily, so use its moves.

            price(t) = NAV(anchor) × close_sibling(t) / close_sibling(anchor)

        where the anchor is the newest `finpension_statement` row. The level comes
        from the provider, every daily move from the sibling; the two classes differ
        only by fee, ~0.1%/yr. The importer rescales the derived rows onto the newest
        NAV on every upload, and warns when a new NAV disagrees with the
        row derived for that day — the tracking check, since a 49% level gap between
        classes is exactly what this must never mistake for a price.

        Refuses whole rather than guessing: no active mapping (a sibling must be
        declared by hand — `manage_mappings set … --sibling` — never inferred from a
        suffix), no statement NAV to anchor to, a sibling quoted in another currency
        (FX would ride into the ratio), or no sibling close near the anchor date. A
        refusal is raised so the loop's `errors` names it; the 7-day staleness alarm is
        the durable one. Never writes over a statement row: those are the anchors.
        Honours the rate-limit latch like every other Yahoo caller here.
        """
        if self.rate_limited:
            logger.warning(f"Skipping {security.symbol}: Yahoo rate-limited this run")
            return 0

        mapping = await self.ticker_mapping_repo.get_mapping(security.symbol, security.exchange)
        if not mapping:
            raise ValueError(
                f"{security.symbol}@{security.exchange} is priced from a sibling share "
                f"class but has no active ticker mapping naming it. Declare one with "
                f"`manage_mappings set … --sibling`; a sibling is never inferred."
            )
        ticker = mapping.yahoo_ticker

        statement_rows = list((await self.db.execute(
            select(MarketPrice)
            .where(
                MarketPrice.security_id == security.id,
                MarketPrice.source == PRICE_SOURCE_STATEMENT,
            )
            .order_by(MarketPrice.date.asc())
        )).scalars().all())
        end_date = date.today()
        anchors = [r for r in statement_rows if r.date <= end_date]
        if not anchors:
            raise ValueError(
                f"{security.symbol}@{security.exchange} has no statement NAV on record to "
                f"anchor its sibling-class prices to. Import the provider's transaction "
                f"export first."
            )
        anchor = anchors[-1]
        statement_dates = {r.date for r in statement_rows}

        # Never derive before the first observed NAV: nothing was held, and a price
        # there would be an extrapolation the read path never needs.
        start_date = max(statement_rows[0].date, end_date - timedelta(days=days_back))
        # Only the derived rows say what the sibling has already been asked for; a
        # statement NAV is an anchor, not a fetch (see `get_missing_dates`).
        missing = [
            d for d in await self.market_price_repo.get_missing_dates(
                security.id, start_date, end_date,
                sources=[PRICE_ROW_SOURCE_SIBLING_SCALED],
                refresh_provisional=refresh_provisional,
            )
            if d not in statement_dates
        ]
        if not missing:
            logger.debug(f"All sibling-derived prices cached for {security.symbol}")
            await asyncio.sleep(random.uniform(2.0, 4.0))
            return 0

        fetch_start = min(min(missing), anchor.date) - timedelta(
            days=self.SIBLING_ANCHOR_LOOKBACK_DAYS
        )
        prices, rate_limited = await self._try_fetch_yahoo(
            ticker, security, fetch_start, end_date
        )
        if rate_limited:
            logger.warning(f"Rate limit hit on sibling {ticker}; abandoning the pass")
            self.rate_limited = True
            return 0
        if not prices:
            raise ValueError(
                f"Sibling {ticker} returned no prices for {security.symbol}@"
                f"{security.exchange} between {fetch_start} and {end_date}."
            )
        fetched_currency = prices[0]["currency"]
        if security.currency and fetched_currency != security.currency:
            raise ValueError(
                f"Sibling {ticker} is quoted in {fetched_currency} but {security.symbol} is "
                f"{security.currency}; an FX move would ride into the ratio, so refusing."
            )

        closes: Dict[date, Decimal] = {p["date"]: p["close_price"] for p in prices}
        anchor_close = closes.get(anchor.date)
        if anchor_close is None:
            prior = [d for d in closes if anchor.date - timedelta(
                days=self.SIBLING_ANCHOR_LOOKBACK_DAYS) <= d < anchor.date]
            anchor_close = closes[max(prior)] if prior else None
        if not anchor_close:
            raise ValueError(
                f"Sibling {ticker} has no close on or within "
                f"{self.SIBLING_ANCHOR_LOOKBACK_DAYS} days before the anchor NAV date "
                f"{anchor.date} for {security.symbol}; cannot scale."
            )
        factor = anchor.close_price / anchor_close
        logger.info(
            f"{security.symbol}: sibling {ticker} anchored on {anchor.date} "
            f"(NAV {anchor.close_price} / close {anchor_close} = ×{factor:.6f})"
        )

        rows = [
            {
                "security_id": security.id,
                "date": d,
                "close_price": (closes[d] * factor).quantize(Decimal("0.000001")),
                "currency": security.currency,
                "source": PRICE_ROW_SOURCE_SIBLING_SCALED,
            }
            for d in missing if d in closes
        ]
        if not rows:
            return 0
        count = await self.market_price_repo.bulk_create(rows)
        await self.db.commit()
        logger.info(f"Cached {count} sibling-derived prices for {security.symbol}")
        await asyncio.sleep(random.uniform(3.0, 6.0))
        return count

    async def sync_securities(
        self,
        securities: List[Security],
        days_back: int = 730,
        refresh_provisional: bool = True,
    ) -> Dict:
        """
        Fetch each security's missing and provisional prices, stopping on a rate limit.

        **Shared by `SchedulerService.sync_market_data` and
        `POST /api/market-data/sync` so the two cannot drift.** They were separate
        copies of this loop, and the drift was immediate and one-sided: the
        rate-limit circuit breaker was added to the scheduler's copy on 2026-08-04
        and the public route kept asking Yahoo for the remaining ~38 securities
        after being told to back off — the exact "two implementations of one job"
        failure CLAUDE.md opens with, in the half a stranger can reach.

        Returns counts plus `rate_limited_after` (the symbol we stopped on, or None).
        The caller owns the commit, the response shape and any diagnostics.

        Securities whose prices do not come from Yahoo are skipped and counted in
        `skipped_not_yahoo` rather than dropped silently -- a loop that quietly
        processes fewer than it was handed reads as coverage it does not have.

        **Scope** (GitHub issue #10), decided here so both callers inherit it:

        - A short pass (`days_back` within `SOLD_PRICE_GRACE_DAYS` — the six 7-day
          slots) asks only about securities `held(closed_within_days=...)`: an open lot,
          or one closed inside the grace. Others are counted in `skipped_sold`.
        - The long pass (18:00, 730 days) also takes every security sold before that.
          One whose held span (first open .. last close, inside the window) still has a
          gap — first seen already closed, a split invalidation, `--purge-prices` — is
          fetched up to its last close only. A short trailing run with no bar at an old
          close date is not a gap (`REPAIR_TRAILING_GRACE_WEEKDAYS`). One with a complete span is priced to
          today, for `post_sale_pct`: one request a day rather than seven.
        - A security every provider last came back empty for is skipped while
          `price_backoff_until` says so, and counted in `skipped_backoff`.

        `planned` is how many it set out to ask, which is what "N of M" means.

        `refresh_provisional=False` asks only for dates with no row at all, so a warm
        security costs no request — the weekend full sync (GitHub issue #9).
        """
        total_prices = 0
        processed = 0
        skipped_not_yahoo = 0
        skipped_sold = 0
        skipped_backoff = 0
        errors: List[str] = []
        rate_limited_after: Optional[str] = None

        logger.info(f"Syncing market data for {len(securities)} securities...")

        held_ids, recent_ids, spans = await self._price_scope(securities)
        long_pass = days_back > SOLD_PRICE_GRACE_DAYS
        window_start = date.today() - timedelta(days=days_back)
        now = utcnow()

        plan = []
        for security in securities:
            sibling = is_sibling_priced(security)
            if not sibling and not is_yahoo_eligible(security):
                skipped_not_yahoo += 1
                logger.debug(
                    f"Skipping {security.symbol}@{security.exchange}: "
                    f"price_source={security.price_source}, not a Yahoo instrument"
                )
                continue
            repair_end: Optional[date] = None
            if security.id not in recent_ids:
                if not long_pass:
                    skipped_sold += 1
                    continue
                first_open, last_close = spans.get(security.id, (None, None))
                if not sibling and last_close is not None and await self.market_price_repo.get_missing_dates(
                    security.id, max(window_start, first_open or window_start), last_close,
                    trailing_grace_weekdays=REPAIR_TRAILING_GRACE_WEEKDAYS,
                ):
                    repair_end = last_close
            until = price_backoff_until(security, security.id in held_ids)
            if until is not None and now < until:
                skipped_backoff += 1
                logger.debug(
                    f"Skipping {security.symbol}@{security.exchange}: every provider came "
                    f"back empty {security.price_miss_count}x running; next ask after {until}"
                )
                continue
            plan.append((security, sibling, repair_end))

        for security, sibling, repair_end in plan:
            try:
                logger.info(
                    f"Fetching prices for {security.symbol} ({security.exchange})"
                    + (f" up to its last close {repair_end}" if repair_end else "") + "..."
                )
                if sibling:
                    count = await self.sync_sibling_prices(
                        security, days_back=days_back,
                        refresh_provisional=refresh_provisional,
                    )
                elif repair_end is not None:
                    count = await self.sync_security_prices(
                        security, days_back=days_back, end_date=repair_end,
                        refresh_provisional=refresh_provisional,
                    )
                else:
                    count = await self.sync_security_prices(
                        security, days_back=days_back,
                        refresh_provisional=refresh_provisional,
                    )
                total_prices += count
                processed += 1
                logger.info(f"Wrote {count} price rows for {security.symbol}")
            except Exception as e:
                error_msg = f"Failed to fetch prices for {security.symbol}: {str(e)}"
                logger.error(error_msg)
                errors.append(error_msg)

            # Yahoo said back off. CLAUDE.md's recovery is "stop immediately, wait
            # 30-60 min", and every remaining security is the same IP asking again
            # seconds later. What is already written stays written; the dates we
            # never reached are simply still missing, so the next pass resumes.
            if self.rate_limited:
                rate_limited_after = security.symbol
                logger.error(
                    f"Yahoo rate-limited at {security.symbol}; abandoning the "
                    f"remaining {len(plan) - processed} securities this run"
                )
                break

        return {
            "total_prices": total_prices,
            "processed": processed,
            "planned": len(plan),
            "skipped_not_yahoo": skipped_not_yahoo,
            "skipped_sold": skipped_sold,
            "skipped_backoff": skipped_backoff,
            "errors": errors,
            "rate_limited_after": rate_limited_after,
        }

    async def _price_scope(self, securities: List[Security]):
        """
        What `sync_securities` needs about the lots, for the securities it was handed:
        the ids `held()` selects, the ids `held(closed_within_days=SOLD_PRICE_GRACE_DAYS)`
        selects, and each security's (first open date, last close date).
        """
        ids = [s.id for s in securities]

        async def _ids(criterion):
            rows = await self.db.execute(
                select(Security.id).where(Security.id.in_(ids), criterion)
            )
            return set(rows.scalars().all())

        spans = await self.db.execute(
            select(TaxLot.security_id, func.min(TaxLot.open_date), func.max(TaxLot.close_date))
            .where(TaxLot.security_id.in_(ids))
            .group_by(TaxLot.security_id)
        )
        return (
            await _ids(held()),
            await _ids(held(closed_within_days=SOLD_PRICE_GRACE_DAYS)),
            {sid: (first_open, last_close) for sid, first_open, last_close in spans.all()},
        )

    async def sync_security_prices(
        self,
        security: Security,
        days_back: int = 730,
        end_date: Optional[date] = None,
        refresh_provisional: bool = True,
    ) -> int:
        """
        Sync historical prices for a security (fetch missing dates).

        Args:
            security: Security to sync prices for
            days_back: How many days back to fetch (default: 730 = 2 years)
            end_date: Last date to fetch (default today) — a sold security's last close
                when only its held span is wanted
            refresh_provisional: see `fetch_and_cache_prices`

        Returns:
            Number of price rows written — new plus provisional ones restated.

            This used to return the number of rows *in the window*, which is what made
            a pass report `prices_fetched: 234` for ~80 actual writes, and it also cost
            a second full-range SELECT per security to compute a number nobody could
            act on. The docstring already promised writes; now it tells the truth. A
            warm cache legitimately returns 0 and no caller treats that as a failure.
        """
        start_date = date.today() - timedelta(days=days_back)

        return await self.fetch_and_cache_prices(
            security, start_date, end_date or date.today(),
            refresh_provisional=refresh_provisional,
        )
