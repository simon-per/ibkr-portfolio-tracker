"""
Currency Service
Handles currency conversion to EUR using Frankfurter API.
Caches exchange rates in the database to minimize API calls.
"""
from typing import Dict, Iterable, List, Optional, Set, Tuple
from datetime import date, timedelta
from decimal import Decimal
import httpx
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utc_today, utcnow
from app.models.exchange_rate import ExchangeRate
from app.services.fx_policy import (
    CARRIED_SUFFIX, FX_MAX_CARRY_DAYS, _get_most_recent_rate, published_rows,
    within_carry_bound,
)

logger = logging.getLogger(__name__)

Pair = Tuple[str, str]

# Codes Frankfurter answered with a 4xx when asked for on their own, each with the UTC day
# it did. Until that day ends a code is left out of every request and its pairs go
# straight to the fallback, so a code that drifted out of the provider's list costs one
# batch rejection and one request of its own per day — not N+1 requests at every slot.
# In-process, like `benchmark_service._last_upstream_attempt` (one uvicorn worker): a
# restart re-learns it once, which bounds the same way without a table to keep.
_frankfurter_rejected: Dict[str, date] = {}

# A 4xx that is about the asking, not about what was asked for: answered like an outage
# (no per-code retry, nothing remembered).
_NOT_A_REJECTION = {408, 429}


def reset_frankfurter_rejections() -> None:
    """Clear the rejection memo. For tests — the memo is process-lifetime state."""
    _frankfurter_rejected.clear()


def _is_rejection(status: Optional[int]) -> bool:
    return status is not None and 400 <= status < 500 and status not in _NOT_A_REJECTION


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday — the anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — the algorithm's own name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def is_target_closing_day(day: date) -> bool:
    """A weekday the ECB publishes no reference rate: the TARGET2 closing days."""
    if (day.month, day.day) in CurrencyService.ECB_FIXED_CLOSING_DAYS:
        return True
    easter = easter_sunday(day.year)
    return day in (easter - timedelta(days=2), easter + timedelta(days=1))


class CurrencyService:
    """Service for currency conversion and exchange rate caching"""

    FRANKFURTER_API_URL = "https://api.frankfurter.dev/v1"
    BASE_CURRENCY = "EUR"

    # Currencies Frankfurter serves. It republishes the ECB reference rates, so the
    # list is the ECB's set + EUR and does not grow — anything outside it
    # (TWD, RUB, QAR, SAR, ...) has to come from FALLBACK_API_URL below.
    #
    # BGN stays although Bulgaria adopted the euro on 2026-01-01 and the ECB publishes
    # no rate for it after 2025: its history before then is still an ECB series, which
    # only Frankfurter can supply for an old lot (the fallback is latest-only). For a
    # date after it, a reply without BGN is a per-pair miss and a 4xx costs only BGN's
    # own pair (`_fetch_ecb_range`), so the entry cannot take the other pairs down.
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

    # The tag a carried copy wears; owned by `fx_policy`, which the preload path shares.
    CARRIED_SUFFIX = CARRIED_SUFFIX

    # The scale of `exchange_rates.rate` (Numeric(18, 8)). A rate derived from the ECB
    # table is rounded to it here, so the value held in the session is the one a later
    # read returns.
    RATE_SCALE = Decimal("1e-8")

    # The ECB publishes once per TARGET working day, around 16:00 CET (14:00/15:00 UTC),
    # and Frankfurter carries it shortly after. Before this UTC hour today does not count
    # as published, so the warm-up does not ask for a day that is not out yet.
    ECB_PUBLISHED_BY_UTC_HOUR = 16
    # The fixed-date TARGET closing days. The two moving ones, Good Friday and Easter
    # Monday, come from `easter_sunday` (`is_target_closing_day`).
    ECB_FIXED_CLOSING_DAYS = {(1, 1), (5, 1), (12, 25), (12, 26)}
    # A pair's history reaches the start of a warm-up window when its oldest published
    # rate is within this many days of it: a window opening on a weekend or over Easter
    # has no rate on its first day, and without the slack every pass would re-ask for it.
    WARM_REACH_SLACK_DAYS = 7

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
        cached = await self._get_cached_row(from_currency, target_date, to_currency)
        if cached is not None and cached.rate and not cached.source.endswith(self.CARRIED_SUFFIX):
            return cached.rate
        if cached is not None:
            # A carried copy caches a verdict, not a rate: re-derived from the published
            # rate it stands in for, without a fetch (it is what keeps a weekend read off
            # the network), so it can neither outlive the bound nor go on carrying a
            # stand-in's value after the quote replaced it. Refused, it is gone and the
            # date is resolved like any other miss.
            carried = await self._carry_forward(
                from_currency, target_date, to_currency, existing=cached
            )
            if carried is not None:
                return carried

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

    async def _carry_forward(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str,
        existing: Optional[ExchangeRate] = None,
    ) -> Optional[Decimal]:
        """
        Reuse the newest *published* rate on or before ``target_date`` (weekends,
        holidays), refusing once it is older than `fx_policy.FX_MAX_CARRY_DAYS` — the
        bound the preload path applies too, so a date's verdict does not depend on which
        path asked first. A carried rate is an approximation that ages: a stale rate
        stamped onto a new lot's *persisted* cost_basis_eur is a permanent mis-valuation,
        while a refusal lets the caller's skip-with-warning path take over and self-heal
        when providers recover.

        Caches it under the requested date keeping the provider tag of the row it came
        from, so the audit trail survives the copy, plus `CARRIED_SUFFIX`, so the copy can
        be replaced once that day's real quote is published. ``existing`` is a copy
        already stored for the date: refreshed from the anchor, or deleted on a refusal.
        """
        recent = await _get_most_recent_rate(
            self.session, from_currency, to_currency, target_date
        )
        age = (target_date - recent.date).days if recent else None
        if recent is None or not within_carry_bound(recent.date, target_date):
            if recent is not None:
                logger.warning(
                    f"Refusing to carry a {age}-day-old {from_currency}/{to_currency} rate "
                    f"({recent.date}) onto {target_date} — beyond {FX_MAX_CARRY_DAYS} days"
                )
            if existing is not None:
                await self.session.delete(existing)
                await self.session.flush()
            return None
        rate, source, _ = recent
        source = f"{source}{self.CARRIED_SUFFIX}"
        if existing is None:
            await self._cache_rate(from_currency, to_currency, target_date, rate, source=source)
        elif existing.rate != rate or existing.source != source:
            existing.rate = rate
            existing.source = source
            await self.session.flush()
        return rate

    async def _get_cached_rate(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str
    ) -> Optional[Decimal]:
        """Get exchange rate from database cache"""
        exchange_rate = await self._get_cached_row(from_currency, target_date, to_currency)
        return exchange_rate.rate if exchange_rate else None

    async def _get_cached_row(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str
    ) -> Optional[ExchangeRate]:
        """The row cached for exactly ``target_date``, of any source."""
        result = await self.session.execute(
            select(ExchangeRate).where(
                ExchangeRate.date == target_date,
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency
            )
        )
        return result.scalar_one_or_none()

    async def _batch_fetch_rates(
        self,
        from_currency: str,
        target_date: date,
        to_currency: str,
        days_back: int = 30
    ) -> bool:
        """
        Fetch and cache one pair's rates over ``target_date - days_back .. target_date``
        in one request (`_fetch_ecb_range`).

        Returns:
            True if rates came back and were cached; False if the provider errored or
            answered with nothing. Callers that report a count need this: swallowing the
            error *and* counting the currency as done reported "14 currencies synced" on
            a run where one fetch had actually failed.
        """
        start_date = target_date - timedelta(days=days_back)
        answered, _ = await self._fetch_ecb_range(
            [(from_currency, to_currency)], start_date, target_date
        )
        return bool(answered)

    def _rejected_by_frankfurter(self, pair: Pair) -> bool:
        """True while a code of ``pair`` stands rejected for today (`_frankfurter_rejected`)."""
        today = utcnow().date()
        return any(_frankfurter_rejected.get(code) == today for code in pair)

    async def _fetch_ecb_range(
        self,
        pairs: List[Pair],
        start_date: date,
        end_date: date,
    ) -> Tuple[Set[Pair], int]:
        """
        One Frankfurter range request (`/start..end`) for every pair in ``pairs``, each
        cached in its own direction. Returns the pairs a reply carried a rate for and the
        number of requests made. Never raises.

        Always asked as EUR -> each currency — the ECB's own figures — and every pair is
        derived from that one table (``table[to] / table[from]``, EUR implied at 1), so
        `X -> EUR` is stored as the inverse and `EUR -> CHF` as quoted. That is what lets
        one request serve the whole warm-up, and it is more exact than asking with ``X``
        as the base: v1 rounds such a reply to five decimals, so KRW->EUR came back as
        0.00067 against the ECB's 1/1499.01 = 0.00066711 (checked 2026-10-17).

        Riding one request means one code the provider rejects would fail every pair. So
        a 4xx for a request naming several codes is retried once per code, and a code
        rejected on its own is remembered for the rest of the UTC day
        (`_frankfurter_rejected`) and left out of every later request: its pairs come
        back unserved, the rest are stored. A 5xx, a 408/429 or a transport error is an
        outage and fails the whole request, as before — retrying it per code would only
        multiply the requests into a provider that is not answering.
        """
        pairs = [pair for pair in pairs if not self._rejected_by_frankfurter(pair)]
        quotes = sorted({c for pair in pairs for c in pair} - {self.BASE_CURRENCY})
        if not quotes:
            return set(), 0
        span = f"{', '.join(f'{f}/{t}' for f, t in pairs)} over {start_date}..{end_date}"

        async with httpx.AsyncClient() as client:
            status, rates_by_date = await self._get_ecb_range(client, quotes, start_date, end_date)
            requests = 1
            if _is_rejection(status) and len(quotes) == 1:
                self._remember_rejection(quotes[0], status)
            elif _is_rejection(status):
                logger.warning(
                    f"Range fetch for {span} rejected (status {status}); asking for each "
                    f"of {', '.join(quotes)} on its own"
                )
                rates_by_date = {}
                for quote in quotes:
                    status, alone = await self._get_ecb_range(
                        client, [quote], start_date, end_date
                    )
                    requests += 1
                    if _is_rejection(status):
                        self._remember_rejection(quote, status)
                    elif alone is None:
                        break  # an outage now: the codes not yet asked stay unserved
                    else:
                        for date_str, quoted in alone.items():
                            rates_by_date.setdefault(date_str, {}).update(quoted)
            elif rates_by_date is None:
                logger.warning(f"Range fetch for {span} failed (status {status})")

        if not rates_by_date:
            return set(), requests

        answered: Set[Pair] = set()
        for date_str, quoted in rates_by_date.items():
            table = {code: Decimal(str(value)) for code, value in quoted.items() if value}
            table[self.BASE_CURRENCY] = Decimal(1)
            rate_date = date.fromisoformat(date_str)
            for from_currency, to_currency in pairs:
                if from_currency not in table or to_currency not in table:
                    continue
                rate = (table[to_currency] / table[from_currency]).quantize(self.RATE_SCALE)
                # A published ECB quote replaces a stand-in for the same day. Before it is
                # published a date is filled by a carry-forward copy or the fallback's
                # *latest* rate, and keeping that row once the real one exists made the
                # stand-in permanent: found 2026-10-01, a fallback CHF->EUR of 1.05918 on
                # 09-28 against the ECB's 1.0566, carried onto 09-30 and never corrected,
                # put a CHF 4,500 withdrawal in at CHF 4,517.50.
                await self._store_published_rate(from_currency, to_currency, rate_date, rate)
                answered.add((from_currency, to_currency))

        # A pair missing from a *successful* reply is a miss, not a success — the provider
        # drifting out of step with SUPPORTED_CURRENCIES looks exactly like this — so the
        # caller falls through instead of counting a currency it never actually got a
        # rate for.
        logger.debug(f"Range fetch for {span}: {len(rates_by_date)} day(s)")
        return answered, requests

    def _remember_rejection(self, code: str, status: int) -> None:
        logger.warning(
            f"Frankfurter rejected {code} (status {status}); leaving it out of every "
            f"request until the end of the UTC day"
        )
        _frankfurter_rejected[code] = utcnow().date()

    async def _get_ecb_range(
        self, client: httpx.AsyncClient, quotes: List[str], start_date: date, end_date: date,
    ) -> Tuple[Optional[int], Optional[Dict[str, Dict[str, float]]]]:
        """
        One GET of `/start..end?from=EUR&to=<quotes>`. Returns the status (None on a
        transport error) and the reply's ``rates`` by date (None unless it succeeded).
        Never raises.
        """
        url = f"{self.FRANKFURTER_API_URL}/{start_date.isoformat()}..{end_date.isoformat()}"
        params = {"from": self.BASE_CURRENCY, "to": ",".join(quotes)}
        try:
            logger.debug(f"Range fetching {url} with params {params}")
            response = await client.get(url, params=params, timeout=10.0)
            if response.status_code >= 400:
                return response.status_code, None
            # {"base": "EUR", "rates": {"2026-10-16": {"CHF": 0.9272, "USD": 1.1253}, ...}}
            return response.status_code, response.json().get('rates', {})
        except Exception as e:
            # httpx transport errors routinely stringify to '', which made this line read
            # "Batch fetch error: " and say nothing about what broke or where. Don't raise:
            # get_exchange_rate() decides what an empty cache means, and can reach the
            # fallback provider instead of giving up.
            logger.error(
                f"Range fetch error for {', '.join(quotes)} over {start_date}..{end_date}: "
                f"{type(e).__name__}: {e}"
            )
            return None, None

    def _last_ecb_publication_day(self, target_date: date) -> date:
        """The newest day on or before ``target_date`` whose ECB rates should be out."""
        now = utcnow()
        day = min(target_date, now.date())
        if day == now.date() and now.hour < self.ECB_PUBLISHED_BY_UTC_HOUR:
            day -= timedelta(days=1)
        while day.weekday() >= 5 or is_target_closing_day(day):
            day -= timedelta(days=1)
        return day

    async def _published_span(
        self, from_currency: str, to_currency: str, on_or_before: date,
        fallback_counts: bool = False,
    ) -> Tuple[Optional[date], Optional[date]]:
        """
        The oldest and newest date holding a *published* rate for the pair. A stand-in
        (fallback or carried) does not count: it is what a later fetch must still replace.

        ``fallback_counts`` is for a pair Frankfurter cannot serve, where the fallback's
        latest-only row is the only publication there will ever be.
        """
        criteria = [
            ExchangeRate.from_currency == from_currency,
            ExchangeRate.to_currency == to_currency,
            ExchangeRate.date <= on_or_before,
            published_rows(),
        ]
        if not fallback_counts:
            criteria.append(ExchangeRate.source != self.FALLBACK_SOURCE)
        oldest, newest = (await self.session.execute(
            select(func.min(ExchangeRate.date), func.max(ExchangeRate.date)).where(*criteria)
        )).one()
        return oldest, newest

    async def stale_pairs(
        self, pairs: Iterable[Tuple[str, str]], publication_days: int,
        as_of: Optional[date] = None,
    ) -> List[Tuple[Tuple[str, str], Optional[date]]]:
        """
        The pairs whose newest published rate has missed ``publication_days`` ECB
        publication days or more, with that date (None: no published rate at all).

        Database only. Counted in publication days, so a weekend or a TARGET holiday is
        not an outage, and a pair the next slot brings current never appears. A pair
        Frankfurter cannot serve is judged on its fallback rows.
        """
        as_of = as_of or utc_today()
        cutoff = self._last_ecb_publication_day(as_of)
        for _ in range(publication_days - 1):
            cutoff = self._last_ecb_publication_day(cutoff - timedelta(days=1))
        stale = []
        for pair in pairs:
            ecb = set(pair) <= self.SUPPORTED_CURRENCIES
            _, newest = await self._published_span(*pair, as_of, fallback_counts=not ecb)
            if newest is None or newest < cutoff:
                stale.append((pair, newest))
        return stale

    async def _refresh_ecb_pairs(
        self, pairs: List[Tuple[str, str]], target_date: date, days_back: int
    ) -> Tuple[List[Tuple[str, str]], int]:
        """
        Bring every pair up to the ECB's last publication day in **one** request that
        spans only what some pair is missing — and in none when every pair is current.

        A pair needs the whole window when it has no published rate or its history does
        not reach the window's start (a first warm-up, a backfill); otherwise only from
        its newest published day. Returns the pairs the provider did not serve and the
        number of requests made: 0 or 1, plus one per code when the provider rejects the
        batch (`_fetch_ecb_range`). A pair with a code rejected earlier today is unserved
        without being asked, and does not widen the span.
        """
        last_published = self._last_ecb_publication_day(target_date)
        window_start = target_date - timedelta(days=days_back)
        reach = window_start + timedelta(days=self.WARM_REACH_SLACK_DAYS)

        starts: Dict[Tuple[str, str], date] = {}
        for pair in pairs:
            oldest, newest = await self._published_span(*pair, target_date)
            if newest is None or oldest > reach:
                starts[pair] = window_start
            elif newest < last_published:
                # From the newest day held, not the day after: re-asking one known day
                # keeps the reply non-empty while the next one is not out yet (Frankfurter
                # lagging the ECB), so a pair missing from it still means the
                # provider has nothing for that currency.
                starts[pair] = max(newest, window_start)
        asked = {p: s for p, s in starts.items() if not self._rejected_by_frankfurter(p)}
        answered, requests = (
            await self._fetch_ecb_range(list(asked), min(asked.values()), target_date)
            if asked else (set(), 0)
        )
        return [pair for pair in starts if pair not in answered], requests

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
        age = abs((utc_today() - target_date).days)
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
        extra_pairs: Iterable[Tuple[str, str]] = (),
    ) -> Dict:
        """
        Make sure ``currencies`` have a usable rate for ``target_date``.

        Called at every sync so a currency is never first looked up on the day a position
        in it arrives. ECB currencies — and ``extra_pairs``, such as the `EUR -> CHF/USD`
        rows the base-currency switch reads — are brought up to the ECB's last
        publication day by **one** Frankfurter request covering only the missing span,
        and by none once they are current (`_refresh_ecb_pairs`). Every other currency is
        satisfied from a **single** fallback-table request, which matters because for
        those there is no historical endpoint — the only history that will ever exist is
        the one these daily calls accumulate.

        Never raises: a provider being down degrades the warm-up, it must not fail the
        sync that called it.
        """
        target_date = target_date or utc_today()
        wanted = {c.strip().upper() for c in currencies if c and c.strip()}
        wanted.discard(to_currency)

        primary = sorted(c for c in wanted if c in self.SUPPORTED_CURRENCIES)
        secondary = sorted(wanted - set(primary))

        primary_pairs = [(c, to_currency) for c in primary]
        pairs = primary_pairs + [
            (f, t) for f, t in extra_pairs
            if f != t and {f, t} <= self.SUPPORTED_CURRENCIES and (f, t) not in primary_pairs
        ]
        unserved: List[Tuple[str, str]] = []
        requests = 0
        if pairs:
            try:
                unserved, requests = await self._refresh_ecb_pairs(
                    pairs, target_date, days_back
                )
            except Exception as e:  # the fetch already swallows, belt and braces
                logger.error(f"Warm-up of {len(pairs)} ECB pair(s) failed: {e}")
                unserved = pairs
        warmed_primary = len(pairs) - len(unserved)
        failed = [f for f, t in unserved if (f, t) in primary_pairs]
        failed_extra = [f"{f}->{t}" for f, t in unserved if (f, t) not in primary_pairs]
        if failed_extra:
            logger.warning(f"Frankfurter did not answer for {', '.join(failed_extra)}")

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
            "frankfurter_requests": requests,
            "fallback": warmed_secondary,
            "fallback_available": table is not None if secondary else None,
            "currencies": sorted(wanted),
        }
        if failed or failed_extra:
            # Named, not just counted: a currency that failed both providers is the
            # precondition for a position quietly disappearing, so it belongs in the
            # sync record rather than only in the container log.
            summary["frankfurter_failed"] = failed + failed_extra
        return summary

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
