"""
CoinGecko's Demo API — the crypto book's price source (docs/crypto.md).

CoinStats stays the source of *what is held*; CoinGecko says what each coin was worth on
each day, so the value and P&L series are computed here from quantities × prices instead
of mirrored from CoinStats' history (which counted transfers from an untracked exchange
as profit).

**The key travels in a header, never the URL.** `httpx` error strings carry the request
URL, and those strings can reach `sync_runs.message`; `app/redact.py` masks the key anyway
as the second line of defence.

**The Demo plan's limits, named once here:** about 30 calls a minute, 10,000 a month, and
history reaching back **365 days** (`HISTORY_LIMIT_DAYS`). The client waits
`MIN_REQUEST_INTERVAL_S` after each response *completes* — the CoinStats lesson: a limit
measured from the previous call's start is the one that gets refused.

**No retries inside a run.** A 429 abandons the pass (detected by status code — the Yahoo
family test forbids marker lists in services); the next slot is the retry. Every failure
is a typed error carrying a stable `reason`.
"""
import asyncio
import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"
REQUEST_TIMEOUT_S = 30.0
KEY_HEADER = "x-cg-demo-api-key"

# Seconds between the end of one call and the start of the next: 30 calls a minute is one
# every two seconds, and half a second of slack keeps a slow response from crowding it.
MIN_REQUEST_INTERVAL_S = 2.5

# The Demo plan serves at most this many days of history; older days are out of reach
# (rows already stored persist).
HISTORY_LIMIT_DAYS = 365

# `/simple/price` takes many ids per call; chunked so a long list cannot outgrow a URL.
SIMPLE_PRICE_CHUNK = 50

# The Demo plan's monthly allowance, which `tests/test_crypto_budget.py` multiplies against.
MONTHLY_CALL_ALLOWANCE = 10_000


class CoinGeckoError(Exception):
    """A CoinGecko call failed. `reason` is a stable word for the run's warning."""

    reason = "error"


class CoinGeckoAuthError(CoinGeckoError):
    """401/403: the key was rejected."""

    reason = "auth"


class CoinGeckoQuotaError(CoinGeckoError):
    """429: the rate limit or the month's calls. Abandon the pass; never retry."""

    reason = "quota"


class CoinGeckoRequestError(CoinGeckoError):
    """Another 4xx — typically a range beyond the plan's history limit."""

    reason = "bad_request"


class CoinGeckoTransientError(CoinGeckoError):
    """5xx, a transport failure, or a body that is not the JSON the endpoint documents."""

    reason = "transient"


def is_configured() -> bool:
    """True when the Demo key is set. Read at call time so a test's monkeypatch counts."""
    return bool((settings.coingecko_api_key or "").strip())


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        status = body.get("status") if isinstance(body.get("status"), dict) else {}
        message = body.get("error") or status.get("error_message") or ""
        if isinstance(message, str):
            return message.strip()[:200]
    return ""


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


class CoinGeckoClient:
    """
    One pass's worth of CoinGecko calls. Use as an async context manager.

    `transport`, `sleep` and `clock` are injectable so tests drive it with
    `httpx.MockTransport`, never leave the machine, and measure pacing without waiting.
    """

    def __init__(
        self,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._transport = transport
        self._min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: Optional[float] = None
        self._client: Optional[httpx.AsyncClient] = None
        # Calls that reached CoinGecko and came back 2xx — the Demo plan's unit.
        self.calls = 0

    async def __aenter__(self) -> "CoinGeckoClient":
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=REQUEST_TIMEOUT_S,
            transport=self._transport,
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _pace(self) -> None:
        if self._last_request_at is not None:
            wait = self._min_interval_s - (self._clock() - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if self._client is None:
            raise RuntimeError("CoinGeckoClient must be used as an async context manager")
        await self._pace()
        try:
            response = await self._client.get(
                path, params=params,
                headers={KEY_HEADER: (settings.coingecko_api_key or "").strip()},
            )
        except httpx.HTTPError as e:
            raise CoinGeckoTransientError(f"{path}: {type(e).__name__}: {e}") from e
        finally:
            self._last_request_at = self._clock()

        status = response.status_code
        if status in (401, 403):
            raise CoinGeckoAuthError(
                f"{path}: HTTP {status} — CoinGecko rejected the API key. "
                f"{_error_detail(response)}".strip()
            )
        if status == 429:
            raise CoinGeckoQuotaError(
                f"{path}: HTTP 429 — rate limit or monthly calls exhausted. "
                f"{_error_detail(response)}".strip()
            )
        if 400 <= status < 500:
            raise CoinGeckoRequestError(
                f"{path}: HTTP {status}. {_error_detail(response)}".strip()
            )
        if status >= 500:
            raise CoinGeckoTransientError(
                f"{path}: HTTP {status}. {_error_detail(response)}".strip()
            )
        self.calls += 1
        try:
            return response.json()
        except ValueError as e:
            raise CoinGeckoTransientError(f"{path}: the response was not JSON") from e

    # ---------------------------------------------------------------- endpoints

    async def coins_list(self) -> List[Dict[str, str]]:
        """`/coins/list`: every coin's `{id, symbol, name}` — one call, for the id mapping."""
        body = await self._get("/coins/list")
        if not isinstance(body, list):
            raise CoinGeckoTransientError("/coins/list: expected a list")
        out = []
        for item in body:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                out.append({
                    "id": item["id"],
                    "symbol": str(item.get("symbol") or ""),
                    "name": str(item.get("name") or ""),
                })
        return out

    async def simple_price(self, ids: Iterable[str]) -> Dict[str, float]:
        """`/simple/price` in USD: `{coingecko_id: price}` for each id CoinGecko priced.
        An id it did not price is absent from the result, never 0."""
        wanted = sorted(set(ids))
        prices: Dict[str, float] = {}
        for start in range(0, len(wanted), SIMPLE_PRICE_CHUNK):
            chunk = wanted[start:start + SIMPLE_PRICE_CHUNK]
            body = await self._get(
                "/simple/price", params={"ids": ",".join(chunk), "vs_currencies": "usd"}
            )
            if not isinstance(body, dict):
                raise CoinGeckoTransientError("/simple/price: expected an object")
            for coin_id, block in body.items():
                price = _finite(block.get("usd")) if isinstance(block, dict) else None
                if price is not None and price > 0:
                    prices[coin_id] = price
        return prices

    async def market_chart_range(
        self, coin_id: str, from_ts: int, to_ts: int
    ) -> List[Tuple[int, float]]:
        """`/coins/{id}/market_chart/range` in USD: `[(epoch_ms, price)]`. CoinGecko picks
        the granularity from the span (hourly inside 90 days, daily beyond); the caller
        reduces it to one close per UTC day."""
        body = await self._get(
            f"/coins/{coin_id}/market_chart/range",
            params={"vs_currency": "usd", "from": int(from_ts), "to": int(to_ts)},
        )
        points = body.get("prices") if isinstance(body, dict) else None
        if not isinstance(points, list):
            raise CoinGeckoTransientError(
                f"/coins/{coin_id}/market_chart/range: expected a `prices` list"
            )
        out = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            stamp, price = _finite(point[0]), _finite(point[1])
            if stamp is None or price is None or price <= 0:
                continue
            out.append((int(stamp), price))
        return out
