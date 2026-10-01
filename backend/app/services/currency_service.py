"""
Currency Service
Handles currency conversion to EUR using Frankfurter API.
Caches exchange rates in the database to minimize API calls.
"""
from typing import Dict, Iterable, Optional, Tuple
from datetime import date
from decimal import Decimal
import httpx
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exchange_rate import ExchangeRate

logger = logging.getLogger(__name__)


class CurrencyService:
    """Service for currency conversion and exchange rate caching"""

    FRANKFURTER_API_URL = "https://api.frankfurter.dev/v1"
    BASE_CURRENCY = "EUR"

    # Currencies Frankfurter serves. It republishes the ECB reference rates, so the
    # list is exactly the ECB's 30 + EUR and does not grow — anything outside it
    # (TWD, RUB, QAR, SAR, ...) has to come from FALLBACK_API_URL below.
    SUPPORTED_CURRENCIES = {
        'AUD', 'BGN', 'BRL', 'CAD', 'CHF', 'CNY', 'CZK', 'DKK',
        'EUR', 'GBP', 'HKD', 'HUF', 'IDR', 'ILS', 'INR', 'ISK',
        'JPY', 'KRW', 'MXN', 'MYR', 'NOK', 'NZD', 'PHP', 'PLN',
        'RON', 'SEK', 'SGD', 'THB', 'TRY', 'USD', 'ZAR'
    }

    # Secondary provider. Primarily for currencies Frankfurter doesn't carry — before it
    # existed a TWD position (TSMC on TWSE) could not be converted at all, so
    # reconcile_taxlots() skipped the lot and the holding silently vanished from the
    # portfolio and the tax report — but also the last resort for a currency that *is*
    # in the set below when Frankfurter answers with nothing, so neither that hardcoded
    # list nor a single third party can decide on its own whether a position exists.
    #
    # The free tier serves the *latest* rates only — there is no historical endpoint
    # without a paid key. So this can never reconstruct an old rate, and we refuse to
    # pretend otherwise: it is used only for dates within FALLBACK_MAX_AGE_DAYS of
    # today, and the row is tagged `er-api-latest` so a today's-rate-applied-to-a-recent-
    # date approximation is never mistaken for a real historical quote. Older dates keep
    # raising, which preserves the existing skip-with-warning behaviour.
    #
    # In practice the window is enough: rates are cached per date as syncs run, so
    # history accumulates forward from the first day a currency appears.
    FALLBACK_API_URL = "https://open.er-api.com/v6/latest/EUR"
    FALLBACK_SOURCE = "er-api-latest"
    FALLBACK_MAX_AGE_DAYS = 7

    # Appended to the origin's tag when a rate is copied onto a later date. A copy is a
    # stand-in exactly like a fallback row — the day's real quote may simply not have been
    # published yet — and `_store_published_rate` has to be able to tell it apart from a
    # quote for that day, or the copy outlives the quote it was standing in for.
    CARRIED_SUFFIX = "+carried"

    # Currencies we keep a daily rate for even while nothing is held in them.
    #
    # Only the ones Frankfurter *cannot* serve are listed: for an ECB currency the
    # history is a request away whenever it's first needed, but for these there is no
    # historical endpoint at all, so the only history that will ever exist is the one we
    # accumulate from today forward. Warming them daily turns "you must sync within
    # FALLBACK_MAX_AGE_DAYS of the buy, or the lot is skipped forever" into "whenever
    # the statement arrives, the rate is already there".
    #
    # Chosen for markets an IBKR account can actually reach; all verified present in
    # the provider's table. Extending it is one edit and costs no extra request — the
    # whole set is satisfied by the single response that already returns all 166.
    WARM_CURRENCIES = {
        'TWD',  # Taiwan — TSMC, held since 2026-07-27
        'CNH',  # offshore yuan, which is what IBKR quotes (onshore CNY is an ECB rate)
        'AED', 'SAR', 'QAR', 'KWD',  # Gulf exchanges
        'RUB',  # the Flex parser already has to repair RUS -> RUB
        'CLP', 'COP',  # Latin America beyond BRL/MXN, which the ECB does cover
    }

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_exchange_rate(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str = "EUR"
    ) -> Decimal:
        """
        Get exchange rate for a specific date.
        Checks cache first, then fetches from API if needed.
        For weekends/holidays without data, uses the most recent available rate (carry forward).

        Args:
            from_currency: Source currency code (e.g., 'USD')
            target_date: Date for the exchange rate
            to_currency: Target currency (always EUR for this app)

        Returns:
            Exchange rate as Decimal

        Raises:
            ValueError: If no rate can be obtained for the currency on that date
        """
        # If currencies are the same, return 1.0
        if from_currency == to_currency:
            return Decimal("1.0")

        # Check database cache first. This runs before the provider split so a rate
        # already cached from either provider is reused without another request.
        cached_rate = await self._get_cached_rate(from_currency, target_date, to_currency)
        if cached_rate:
            return cached_rate

        if from_currency in self.SUPPORTED_CURRENCIES:
            # If not in cache, try to fetch a range of recent rates (batch fetch)
            # This is more efficient than fetching one date at a time
            await self._batch_fetch_rates(from_currency, target_date, to_currency)

            cached_rate = await self._get_cached_rate(from_currency, target_date, to_currency)
            if cached_rate:
                return cached_rate

            # Weekend or holiday: the ECB simply doesn't publish, and Friday's real rate
            # is a better answer than today's approximation — so carry forward *before*
            # considering the fallback, not after.
            carried = await self._carry_forward(from_currency, target_date, to_currency)
            if carried is not None:
                return carried

        # Either the currency is outside the ECB set, or Frankfurter had nothing at all
        # for it: a provider outage, or SUPPORTED_CURRENCIES having drifted out of step
        # with what the ECB actually publishes. Both used to raise here, which means a
        # hardcoded list — and a single third party — decided whether a position existed.
        # Trying the fallback last keeps that list advisory rather than load-bearing.
        await self._fetch_fallback_rate(from_currency, target_date, to_currency)

        cached_rate = await self._get_cached_rate(from_currency, target_date, to_currency)
        if cached_rate:
            return cached_rate

        carried = await self._carry_forward(from_currency, target_date, to_currency)
        if carried is not None:
            return carried

        raise ValueError(f"No exchange rate available for {from_currency} on or before {target_date}")

    # A carried rate is an approximation that ages. Friday's rate over a weekend
    # (or a holiday cluster) is the designed case; a month-old rate silently
    # stamped onto a new lot's *persisted* cost_basis_eur is not — the fallback
    # provider bounds itself at FALLBACK_MAX_AGE_DAYS while this path had no bound
    # at all. Past this, refuse and let get_exchange_rate() raise: the caller's
    # skip-with-warning path takes over and self-heals when providers recover.
    CARRY_FORWARD_MAX_AGE_DAYS = 30

    async def _carry_forward(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str
    ) -> Optional[Decimal]:
        """
        Reuse the most recent rate on or before ``target_date`` (weekends, holidays),
        refusing once the newest available rate is older than
        CARRY_FORWARD_MAX_AGE_DAYS.

        Caches it under the requested date keeping the provider tag of the row it came
        from, so the audit trail survives the copy, plus `CARRIED_SUFFIX`, so the copy can
        be replaced once that day's real quote is published.
        """
        recent = await self._get_most_recent_rate(from_currency, target_date, to_currency)
        if not recent:
            return None
        rate, source, rate_date = recent
        age = (target_date - rate_date).days
        if age > self.CARRY_FORWARD_MAX_AGE_DAYS:
            logger.warning(
                f"Refusing to carry a {age}-day-old {from_currency}/{to_currency} rate "
                f"({rate_date}) onto {target_date} — beyond {self.CARRY_FORWARD_MAX_AGE_DAYS} days"
            )
            return None
        if not source.endswith(self.CARRIED_SUFFIX):
            source = f"{source}{self.CARRIED_SUFFIX}"
        await self._cache_rate(from_currency, to_currency, target_date, rate, source=source)
        return rate

    async def _get_cached_rate(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str
    ) -> Optional[Decimal]:
        """Get exchange rate from database cache"""
        result = await self.session.execute(
            select(ExchangeRate).where(
                ExchangeRate.date == target_date,
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency
            )
        )
        exchange_rate = result.scalar_one_or_none()
        return exchange_rate.rate if exchange_rate else None

    async def _get_most_recent_rate(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str
    ) -> Optional[Tuple[Decimal, str, date]]:
        """
        Get the most recent exchange rate on or before the target date, with the
        provider that produced it and the date it belongs to. Used for
        weekends/holidays when the specific date isn't available.

        The `date <= target_date` bound is what stops a currency we only learned
        about recently from being projected backwards onto older lots: a rate first
        cached today is invisible to a request for last year, which still raises and
        so keeps the caller's skip-with-warning path.
        """
        result = await self.session.execute(
            select(ExchangeRate)
            .where(
                ExchangeRate.date <= target_date,
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency
            )
            .order_by(ExchangeRate.date.desc())
            .limit(1)
        )
        exchange_rate = result.scalar_one_or_none()
        if not exchange_rate:
            return None
        return exchange_rate.rate, exchange_rate.source, exchange_rate.date

    async def _batch_fetch_rates(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str,
        days_back: int = 30
    ) -> bool:
        """
        Batch fetch exchange rates for a date range.
        Uses Frankfurter's range endpoint: /start_date..end_date

        Args:
            from_currency: Source currency
            target_date: Target date (end of range)
            to_currency: Target currency (EUR)
            days_back: How many days before target_date to fetch (default: 30)

        Returns:
            True if rates came back and were cached; False if the provider errored or
            answered with nothing. Callers that report a count need this: swallowing the
            error *and* counting the currency as done reported "14 currencies synced" on
            a run where one fetch had actually failed.
        """
        from datetime import timedelta

        start_date = target_date - timedelta(days=days_back)
        end_date = target_date

        # Use Frankfurter's date range endpoint
        url = f"{self.FRANKFURTER_API_URL}/{start_date.isoformat()}..{end_date.isoformat()}"
        params = {
            "from": from_currency,
            "to": to_currency
        }

        async with httpx.AsyncClient() as client:
            try:
                logger.debug(f"Batch fetching {url} with params {params}")
                response = await client.get(url, params=params, timeout=10.0)
                logger.debug(f"Batch response status: {response.status_code}")

                if response.status_code >= 400:
                    logger.warning(
                        f"Batch fetch for {from_currency}/{to_currency} failed "
                        f"(status {response.status_code})"
                    )
                    return False

                data = response.json()

                # Response format: {"amount": 1, "base": "USD", "start_date": "...", "end_date": "...", "rates": {"2024-01-01": {"EUR": 0.9}, ...}}
                rates_by_date = data.get('rates', {})
                logger.debug(f"Received {len(rates_by_date)} rates")

                # Cache all rates
                for date_str, rate_data in rates_by_date.items():
                    rate_value = rate_data.get(to_currency)
                    if rate_value:
                        rate_date = date.fromisoformat(date_str)
                        rate_decimal = Decimal(str(rate_value))

                        # A published ECB quote replaces a stand-in for the same day. Before
                        # it is published a date is filled by a carry-forward copy or the
                        # fallback's *latest* rate, and keeping that row once the real
                        # one exists made the stand-in permanent: found 2026-10-01, a
                        # fallback CHF->EUR of 1.05918 on 09-28 against the ECB's 1.0566,
                        # carried onto 09-30 and never corrected, put a CHF 4,500
                        # withdrawal in at CHF 4,517.50.
                        await self._store_published_rate(
                            from_currency, to_currency, rate_date, rate_decimal
                        )

                logger.debug(f"Cached {len(rates_by_date)} rates")
                # An empty `rates` is a *successful* request that carried no data — the
                # provider drifting out of step with SUPPORTED_CURRENCIES looks exactly
                # like this. Report it as a miss so the caller falls through instead of
                # counting a currency it never actually got a rate for.
                return bool(rates_by_date)

            except Exception as e:
                # httpx transport errors routinely stringify to '', which made this line
                # read "Batch fetch error: " and say nothing about what broke or where.
                logger.error(
                    f"Batch fetch error for {from_currency}/{to_currency} "
                    f"over {start_date}..{end_date}: {type(e).__name__}: {e}"
                )
                # Don't raise: get_exchange_rate() decides what an empty cache means,
                # and can now reach the fallback provider instead of giving up.
                return False

    async def _fetch_fallback_table(self) -> Optional[Dict[str, Decimal]]:
        """
        Fetch the fallback provider's whole rate table in one request.

        The endpoint is EUR-based and returns all ~166 currencies at once, so warming N
        of them must cost one request, not N. Never raises: returns None and lets the
        caller decide what an unavailable provider means.
        """
        async with httpx.AsyncClient() as client:
            try:
                logger.debug(f"Fetching fallback FX table from {self.FALLBACK_API_URL}")
                response = await client.get(self.FALLBACK_API_URL, timeout=10.0)

                if response.status_code >= 400:
                    logger.warning(f"Fallback FX provider returned {response.status_code}")
                    return None

                data = response.json()
                if data.get("result") != "success":
                    logger.warning(f"Fallback FX provider reported failure: {data.get('result')}")
                    return None

                rates = data.get("rates") or {}
                if not rates:
                    logger.warning("Fallback FX provider returned an empty rate table")
                    return None

                table = {code: Decimal(str(value)) for code, value in rates.items()}
                # The provider quotes "X per 1 EUR" and omits the base itself.
                table.setdefault(self.BASE_CURRENCY, Decimal("1"))
                return table

            except Exception as e:
                logger.error(f"Fallback FX table fetch error: {e}")
                return None

    async def _fetch_fallback_rate(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str,
        table: Optional[Dict[str, Decimal]] = None
    ) -> None:
        """
        Fetch a rate the primary provider couldn't supply, and cache it.

        ``table`` lets a caller that already holds a fetched table (see ``warm_rates``)
        reuse it instead of issuing another request. The provider is EUR-based and
        latest-only, so:

        - the rate for an arbitrary pair is derived as rates[to] / rates[from],
          with EUR itself implied at 1;
        - a target date older than FALLBACK_MAX_AGE_DAYS is refused outright rather
          than answered with today's rate. Silently backdating would put an invented
          historical rate on an old tax lot, which the tax report would then present
          as fact. Leaving it unfetched keeps the caller's existing behaviour of
          skipping the lot and reporting it in `warnings[]`.

        Never raises: like _batch_fetch_rates it simply leaves the cache unpopulated,
        and get_exchange_rate() decides what that means.
        """
        age = abs((date.today() - target_date).days)
        if age > self.FALLBACK_MAX_AGE_DAYS:
            logger.warning(
                f"No historical rate available for {from_currency} on {target_date}: "
                f"Frankfurter does not carry it and the fallback provider serves only "
                f"current rates ({age} days old > {self.FALLBACK_MAX_AGE_DAYS})"
            )
            return

        if table is None:
            table = await self._fetch_fallback_table()
        if not table:
            return

        from_rate = table.get(from_currency)
        to_rate = table.get(to_currency)
        if not from_rate or to_rate is None:
            logger.warning(
                f"Fallback FX provider has no rate for {from_currency}/{to_currency}"
            )
            return

        # Provider is EUR-based: table[X] is "X per 1 EUR".
        rate = to_rate / from_rate

        existing = await self._get_cached_rate(from_currency, target_date, to_currency)
        if not existing:
            await self._cache_rate(
                from_currency, to_currency, target_date, rate,
                source=self.FALLBACK_SOURCE
            )
        logger.info(
            f"Cached fallback rate {from_currency}/{to_currency}={rate} for {target_date}"
        )

    async def warm_rates(
        self,
        currencies: Iterable[str],
        target_date: Optional[date] = None,
        days_back: int = 30,
        to_currency: str = "EUR",
    ) -> Dict:
        """
        Make sure ``currencies`` have a usable rate for ``target_date``.

        Called daily so a currency is never first looked up on the day a position in it
        arrives. ECB currencies go through Frankfurter's range endpoint (one request
        each, and their history is retrievable at any time anyway); every other currency
        is satisfied from a **single** fallback-table request, which matters because for
        those there is no historical endpoint — the only history that will ever exist is
        the one these daily calls accumulate.

        Never raises: a provider being down degrades the warm-up, it must not fail the
        sync that called it.
        """
        target_date = target_date or date.today()
        wanted = {c.strip().upper() for c in currencies if c and c.strip()}
        wanted.discard(to_currency)

        primary = sorted(c for c in wanted if c in self.SUPPORTED_CURRENCIES)
        secondary = sorted(wanted - set(primary))

        warmed_primary = 0
        failed = []
        for currency in primary:
            try:
                if await self._batch_fetch_rates(
                    currency, target_date, to_currency, days_back=days_back
                ):
                    warmed_primary += 1
                else:
                    failed.append(currency)
            except Exception as e:  # _batch_fetch_rates already swallows, belt and braces
                logger.error(f"Warm-up failed for {currency}: {e}")
                failed.append(currency)

        # A currency Frankfurter couldn't serve falls through to the fallback here for the
        # same reason get_exchange_rate() does it: an outage should cost accuracy, not a
        # day's worth of history for a currency that only accumulates forward.
        if failed:
            logger.warning(
                f"Frankfurter did not answer for {', '.join(failed)} — trying the fallback"
            )
            secondary = sorted(set(secondary) | set(failed))

        # The fallback is latest-only, so re-fetching a currency already cached for this
        # date would return the same number. Skipping those makes the warm-up a no-op
        # after the day's first run instead of one request on each of the day's jobs.
        outstanding = []
        for currency in secondary:
            if await self._get_cached_rate(currency, target_date, to_currency) is None:
                outstanding.append(currency)
        secondary = outstanding

        warmed_secondary = 0
        table = await self._fetch_fallback_table() if secondary else None
        if secondary and not table:
            logger.warning(
                f"Fallback FX provider unavailable; {len(secondary)} currency(ies) not "
                f"warmed for {target_date}: {', '.join(secondary)}"
            )
        elif table:
            missing = [c for c in secondary if c not in table]
            if missing:
                # A currency neither provider carries can never be valued, so say so
                # once here rather than leaving it to be discovered by a vanished
                # position months from now.
                logger.warning(
                    f"Neither FX provider carries: {', '.join(missing)} — a position in "
                    f"one of these would be skipped by reconciliation"
                )
            for currency in (c for c in secondary if c in table):
                try:
                    await self._fetch_fallback_rate(
                        currency, target_date, to_currency, table=table
                    )
                    warmed_secondary += 1
                except Exception as e:
                    logger.error(f"Fallback warm-up failed for {currency}: {e}")

        summary = {
            "frankfurter": warmed_primary,
            "fallback": warmed_secondary,
            "fallback_available": table is not None if secondary else None,
            "currencies": sorted(wanted),
        }
        if failed:
            # Named, not just counted: a currency that failed both providers is the
            # precondition for a position quietly disappearing, so it belongs in the
            # sync record rather than only in the container log.
            summary["frankfurter_failed"] = failed
        return summary

    async def _fetch_from_api(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str
    ) -> Decimal:
        """
        Fetch exchange rate from Frankfurter API.

        API docs: https://www.frankfurter.app/docs/

        Raises:
            ValueError: If currency is not supported by Frankfurter API
        """
        url = f"{self.FRANKFURTER_API_URL}/{target_date.isoformat()}"
        params = {
            "from": from_currency,
            "to": to_currency
        }

        async with httpx.AsyncClient() as client:
            try:
                logger.debug(f"Fetching {url} with params {params}")
                response = await client.get(url, params=params, timeout=10.0)
                logger.debug(f"Response status: {response.status_code}")

                # If 404, the date might not be available yet (too recent or weekend)
                # Fall back to latest available rate
                if response.status_code == 404:
                    logger.debug(f"Date {target_date} not available, using latest rate")
                    url_latest = f"{self.FRANKFURTER_API_URL}/latest"
                    response = await client.get(url_latest, params=params, timeout=10.0)
                    logger.debug(f"Latest rate response status: {response.status_code}")

                # Check for unsupported currency error (400 status)
                if response.status_code >= 400:
                    try:
                        error_data = response.json()
                        error_msg = str(error_data)
                    except:
                        error_msg = response.text

                    # Check if it's a currency support issue
                    if 'Unknown currency' in error_msg or 'not supported' in error_msg.lower():
                        raise ValueError(f"Currency {from_currency} is not supported by Frankfurter API")

                response.raise_for_status()
                data = response.json()

                # Extract rate from response
                rate = data.get('rates', {}).get(to_currency)
                if rate is None:
                    raise ValueError(f"No exchange rate found for {from_currency}/{to_currency} on {target_date}")

                return Decimal(str(rate))

            except httpx.HTTPStatusError as e:
                # Handle HTTP errors (400, 404 indicate unsupported currency)
                if e.response.status_code in (400, 404):
                    raise ValueError(f"Currency {from_currency} is not supported by Frankfurter API")
                raise ValueError(f"Failed to fetch exchange rate: {str(e)}")
            except httpx.RequestError as e:
                raise ValueError(f"Network error while fetching exchange rate: {str(e)}")

    async def _store_published_rate(
        self, from_currency: str, to_currency: str, rate_date: date, rate: Decimal
    ) -> None:
        """
        Cache a Frankfurter quote, overwriting a **stand-in** stored for the same day — a
        fallback row or a carried copy — and nothing else. A row from any other source
        (a hand-loaded rate, a test fixture, an earlier quote) is left alone: replacing
        only what announced itself as provisional keeps this from rewriting history.
        """
        existing = (await self.session.execute(
            select(ExchangeRate).where(
                ExchangeRate.date == rate_date,
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency,
            )
        )).scalar_one_or_none()
        if existing is None:
            await self._cache_rate(from_currency, to_currency, rate_date, rate)
        elif self._is_stand_in(existing.source):
            logger.info(
                f"Replacing {from_currency}/{to_currency} {existing.rate} "
                f"({existing.source}) on {rate_date} with the published {rate}"
            )
            existing.rate = rate
            existing.source = "frankfurter"
            await self.session.flush()

    def _is_stand_in(self, source: Optional[str]) -> bool:
        source = source or ""
        return source == self.FALLBACK_SOURCE or source.endswith(self.CARRIED_SUFFIX)

    async def _cache_rate(
        self,
        from_currency: str,
        to_currency: str,
        target_date: date,
        rate: Decimal,
        source: str = "frankfurter"
    ) -> None:
        """Cache exchange rate in database"""
        exchange_rate = ExchangeRate(
            date=target_date,
            from_currency=from_currency,
            to_currency=to_currency,
            rate=rate,
            source=source
        )
        self.session.add(exchange_rate)
        await self.session.flush()

    async def convert_to_eur(
        self,
        amount: Decimal,
        from_currency: str,
        target_date: date
    ) -> Decimal:
        """
        Convert an amount to EUR.

        Args:
            amount: Amount to convert
            from_currency: Source currency
            target_date: Date for exchange rate

        Returns:
            Amount in EUR
        """
        if from_currency == self.BASE_CURRENCY:
            return amount

        rate = await self.get_exchange_rate(from_currency, target_date)
        return amount * rate

    async def convert(
        self,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        target_date: date,
    ) -> Decimal:
        """
        Convert an amount between arbitrary supported currencies on a given date.

        Frankfurter is EUR-based but supports arbitrary from/to pairs, and the
        exchange-rate cache is keyed by (from, to), so this works for e.g.
        EUR->CHF, EUR->USD as well as the usual X->EUR.
        """
        if from_currency == to_currency:
            return amount

        rate = await self.get_exchange_rate(from_currency, target_date, to_currency)
        return amount * rate
