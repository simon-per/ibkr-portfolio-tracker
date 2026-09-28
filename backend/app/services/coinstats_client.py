"""
The CoinStats public API — the crypto view's only upstream (docs/crypto.md).

**Reached through a share token, not a portfolio id.** The owner connected Binance and a
Phantom wallet *in the CoinStats app*. The API's `portfolioId` only covers portfolios
connected through the API itself (`POST /portfolio/wallet|exchange`), so the one way to
read an app-connected portfolio is the token behind a share link. Anyone holding that
token can read the portfolio, so it is a secret exactly like the key.

**Secrets travel in headers, never the URL.** `httpx` error strings carry the request URL,
and those strings end up in `sync_runs.message`. With nothing secret in the URL a
stringified error cannot leak anything; `app/redact.py` masks both values anyway, as the
second line of defence.

**Credits, not requests, are the budget.** Every call costs a documented number of
credits against a monthly allowance (20,000 on the free plan), so the costs are named
here and `tests/test_crypto_budget.py` multiplies them by the schedule. The rate limit is
documented as 2 requests/second on the free plan, and **the real limit is tighter**: measured
2026-09-28, the third call of a pass — about a second in, 0.6 s after the previous one had
*started* — came back 429 "Rate limit exceeded". So the gap is now `MIN_REQUEST_INTERVAL_S`
measured from the previous response's *completion*; a pass is three to five calls, so a generous
gap costs seconds.

**No retries inside a run.** A 429 means either the rate limit or "Insufficient credits"
(CoinStats uses the one status for both and sends no Retry-After), and retrying either
spends what the run exists to conserve. Every failure raises a typed error and the next
scheduled slot is the retry — the bounded re-ask every other upstream here gets.
"""
import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coinstats.app/v1"
REQUEST_TIMEOUT_S = 20.0

# Seconds between the end of one call and the start of the next. Not the documented
# 2 requests/second: 0.6 s from the previous call's start was refused on 2026-09-28.
MIN_REQUEST_INTERVAL_S = 2.0

# Credits per call, from the CoinStats documentation (checked 2026-09-27). `coins` is per
# page. `usage` is free, which is what lets every run check its balance before spending.
CREDIT_COST = {
    "usage": 0,
    "value": 10,
    "coins": 8,
    "chart": 10,
    "pl_history": 25,
}

# `/portfolio/coins` pages. The probe (app/cli/coinstats_probe.py) measures how many a
# real portfolio needs; the cap bounds a wallet full of airdropped spam tokens, which would
# otherwise cost 8 credits for every hundred of them on every sync.
COINS_PAGE_LIMIT = 100
MAX_COIN_PAGES = 5


class CoinStatsError(Exception):
    """A CoinStats call failed. `reason` is a stable word recorded on the sync run."""

    reason = "error"


class CoinStatsAuthError(CoinStatsError):
    """401/403: the key or share token was rejected, or the plan lacks the endpoint."""

    reason = "auth"


class CoinStatsNotSynced(CoinStatsError):
    """409: CoinStats is still syncing the portfolio's transactions. Not a fault — the
    next slot asks again."""

    reason = "not_synced"


class CoinStatsQuotaError(CoinStatsError):
    """429: the rate limit or the month's credits. Abandon the pass; never retry."""

    reason = "quota"


class CoinStatsTransientError(CoinStatsError):
    """5xx, a transport failure, or a body that is not the JSON the endpoint documents."""

    reason = "transient"


class CoinStatsTooManyPages(CoinStatsError):
    """More coin pages than `MAX_COIN_PAGES`. Refused whole rather than stored partly."""

    reason = "too_many_pages"


def is_configured() -> bool:
    """True when both the key and the share token are set. Read at call time, so a
    test's monkeypatch (and conftest's blanking) is honoured."""
    return bool(
        (settings.coin_stats_api_key or "").strip()
        and (settings.coin_stats_share_token or "").strip()
    )


def _error_detail(response: httpx.Response) -> str:
    """CoinStats' own message for a failed call, if it sent one, kept short. It is
    CoinStats' wording ("Insufficient credits for this request"), which says more than
    the status alone — and it never contains our credentials, which are in headers."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or ""
        if isinstance(message, str):
            return message.strip()[:200]
    return ""


class CoinStatsClient:
    """
    One pass's worth of CoinStats calls. Use as an async context manager.

    `transport` is injectable so tests drive the client with `httpx.MockTransport` and
    no request ever leaves the machine; `sleep` and `clock` are injectable so pacing is
    testable without waiting for it.
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
        # Credits this client has spent, by the documented costs. Recorded on the run so
        # the budget is observable per sync rather than only on the CoinStats dashboard.
        self.credits_spent = 0
        # Pages the last `portfolio_coins()` fetched: 8 credits each, so it is the number
        # that says whether a spam-heavy wallet is inflating every sync's cost.
        self.coin_pages = 0

    async def __aenter__(self) -> "CoinStatsClient":
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

    # ---------------------------------------------------------------- plumbing

    @staticmethod
    def _headers(portfolio: bool) -> Dict[str, str]:
        headers = {"X-API-KEY": (settings.coin_stats_api_key or "").strip()}
        # The share token only goes where it is needed: `/usage/credits` is account-level
        # and has no business seeing the portfolio's credential.
        if portfolio:
            headers["sharetoken"] = (settings.coin_stats_share_token or "").strip()
            passcode = (settings.coin_stats_share_passcode or "").strip()
            if passcode:
                headers["passcode"] = passcode
        return headers

    async def _pace(self) -> None:
        # `_last_request_at` is when the previous call *finished* (see `_get`): a slow
        # portfolio call must not let the next one follow on its heels.
        if self._last_request_at is not None:
            wait = self._min_interval_s - (self._clock() - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)

    async def _get(
        self, path: str, cost_key: str, params: Optional[Dict[str, Any]] = None,
        portfolio: bool = True,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("CoinStatsClient must be used as an async context manager")
        await self._pace()
        try:
            response = await self._client.get(
                path, params=params, headers=self._headers(portfolio)
            )
        except httpx.HTTPError as e:
            # The type and message only; the URL in it carries no secret by construction.
            raise CoinStatsTransientError(f"{path}: {type(e).__name__}: {e}") from e
        finally:
            self._last_request_at = self._clock()

        status = response.status_code
        # A refused call is not charged (CoinStats refunds 503s explicitly, and a 4xx does
        # no work), so only a 2xx counts toward `credits_spent`.
        if status in (401, 403):
            raise CoinStatsAuthError(
                f"{path}: HTTP {status} — CoinStats rejected the API key or share token "
                f"(or the plan lacks this endpoint). {_error_detail(response)}".strip()
            )
        if status == 409:
            raise CoinStatsNotSynced(
                f"{path}: HTTP 409 — CoinStats is still syncing this portfolio. "
                f"{_error_detail(response)}".strip()
            )
        if status == 429:
            raise CoinStatsQuotaError(
                f"{path}: HTTP 429 — rate limit or monthly credits exhausted. "
                f"{_error_detail(response)}".strip()
            )
        if status >= 400:
            raise CoinStatsTransientError(
                f"{path}: HTTP {status}. {_error_detail(response)}".strip()
            )

        self.credits_spent += CREDIT_COST[cost_key]
        try:
            return response.json()
        except ValueError as e:
            raise CoinStatsTransientError(f"{path}: the response was not JSON") from e

    # ---------------------------------------------------------------- endpoints

    async def credits(self) -> Dict[str, Any]:
        """`GET /usage/credits` — free: totalCredits, usedCredits, remainingCredits,
        subscription."""
        body = await self._get("/usage/credits", "usage", portfolio=False)
        if not isinstance(body, dict):
            raise CoinStatsTransientError("/usage/credits: expected an object")
        return body

    async def portfolio_value(self) -> Dict[str, Any]:
        """`GET /portfolio/value` in USD: totalValue, defiValue, totalCost and the P&L
        figures CoinStats computes."""
        body = await self._get("/portfolio/value", "value", params={"currency": "USD"})
        if not isinstance(body, dict):
            raise CoinStatsTransientError("/portfolio/value: expected an object")
        return body

    async def portfolio_coins(self) -> List[Dict[str, Any]]:
        """
        Every holding, across as many pages as there are — or none.

        Refuses whole: a failed page raises (so a partial list can never be stored as
        the portfolio), and more than `MAX_COIN_PAGES` full pages raises rather than
        silently truncating. A page shorter than the limit is the last.
        """
        items: List[Dict[str, Any]] = []
        for page in range(1, MAX_COIN_PAGES + 1):
            body = await self._get(
                "/portfolio/coins", "coins",
                params={"page": page, "limit": COINS_PAGE_LIMIT},
            )
            result = body.get("result") if isinstance(body, dict) else None
            if not isinstance(result, list):
                raise CoinStatsTransientError("/portfolio/coins: expected a `result` list")
            items.extend(r for r in result if isinstance(r, dict))
            self.coin_pages = page
            if len(result) < COINS_PAGE_LIMIT:
                return items
        raise CoinStatsTooManyPages(
            f"/portfolio/coins: more than {MAX_COIN_PAGES} pages of {COINS_PAGE_LIMIT} "
            f"holdings — refusing rather than storing a truncated portfolio. Hiding spam "
            f"tokens in CoinStats, or raising MAX_COIN_PAGES, clears it."
        )

    async def portfolio_chart(self) -> List[List[Any]]:
        """`GET /portfolio/chart?type=all` in USD: rows of [timestamp, usd, btc, eth]."""
        body = await self._get(
            "/portfolio/chart", "chart", params={"type": "all", "currency": "USD"}
        )
        result = body.get("result") if isinstance(body, dict) else body
        if not isinstance(result, list):
            raise CoinStatsTransientError("/portfolio/chart: expected a `result` list")
        return result

    async def portfolio_pl_history(self) -> Dict[str, Any]:
        """`GET /portfolio/pl/history?interval=daily&range=all` in USD: CoinStats'
        cash-flow-adjusted P&L, so a deposit is not drawn as a gain. Returns the whole
        body (`result` points plus `meta`)."""
        body = await self._get(
            "/portfolio/pl/history", "pl_history",
            params={"interval": "daily", "range": "all", "currency": "USD"},
        )
        if not isinstance(body, dict) or not isinstance(body.get("result"), list):
            raise CoinStatsTransientError("/portfolio/pl/history: expected a `result` list")
        return body
