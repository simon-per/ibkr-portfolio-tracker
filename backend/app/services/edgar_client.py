"""
SEC EDGAR's two JSON endpoints the Company tab reads (docs/company-financials.md).

- `https://www.sec.gov/files/company_tickers.json`: every ticker the SEC lists, with its
  CIK. One request a pass, and only when a held security has no CIK on record yet.
- `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`: every XBRL fact a
  company has filed, every period and every filing, in one document.

**The SEC requires a User-Agent naming the client and a contact address** and answers 403
without one. It comes from `EDGAR_USER_AGENT` (app/config.py), never from the code — the
repository is public — and it travels only in the request header: never logged, never in
an error message (`paced_http` stringifies the URL, which carries nothing), and masked by
app/redact.py should it surface anyway.

**Paced at 5 requests a second**, half the SEC's published fair-access limit, through the
shared `paced_http.PacedGet`. **Retries are bounded**: a 429, a 5xx or a transport failure
is retried after `RETRY_DELAYS_S` and then given up as `EdgarError` (the pass goes on with
the next company and asks this one again next week). A 401/403 is `EdgarRefused`: the
User-Agent is missing or refused, so no further request is made this pass. A 404 is an
answer — no such document — and returns None.

Touches neither Yahoo nor IBKR.
"""

import asyncio
import time
from typing import Any, Callable, Dict, Optional

import httpx

from app.config import settings
from app.services.paced_http import PacedGet, json_body

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

REQUEST_TIMEOUT_S = 60.0
MIN_INTERVAL_S = 0.2
RETRY_DELAYS_S = (2.0, 10.0)
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
REFUSED_STATUSES = frozenset({401, 403})


class EdgarError(Exception):
    """One request failed after its retries. Not the company's answer: asked again later."""


class EdgarRefused(Exception):
    """The SEC refused the client (401/403) or kept rate-limiting it (429 after the
    retries): the pass stops."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


def is_configured() -> bool:
    """True when `EDGAR_USER_AGENT` is set. Read at call time so a monkeypatch counts."""
    return bool((settings.edgar_user_agent or "").strip())


def sec_ticker(symbol: str) -> str:
    """A symbol as the SEC's list spells it: IBKR's "BRK B" and Yahoo's "BRK.B" are "BRK-B"."""
    return symbol.strip().upper().replace(" ", "-").replace(".", "-")


class EdgarClient(PacedGet):
    """
    One pass's EDGAR calls. Use as an async context manager. `transport`, `sleep` and
    `clock` are injectable so tests drive it with `httpx.MockTransport` and never leave
    the machine.
    """

    def __init__(
        self,
        user_agent: str,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        min_interval_s: float = MIN_INTERVAL_S,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._transport = transport
        self._min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: Optional[float] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "EdgarClient":
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S,
            transport=self._transport,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, url: str) -> Optional[Any]:
        """The decoded body, None on a 404; `EdgarRefused` or `EdgarError` otherwise."""
        last: Optional[str] = None
        last_status: Optional[int] = None
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            if attempt:
                await self._sleep(RETRY_DELAYS_S[attempt - 1])
            try:
                response = await self._send(
                    url,
                    params=None,
                    headers=self._headers,
                    transient=EdgarError,
                )
            except EdgarError as e:
                last, last_status = str(e), None
                continue
            status = response.status_code
            if status in REFUSED_STATUSES:
                raise EdgarRefused(status)
            if status == 404:
                return None
            if status in RETRYABLE_STATUSES:
                last, last_status = f"{url}: HTTP {status}", status
                continue
            if status != 200:
                raise EdgarError(f"{url}: HTTP {status}")
            return json_body(url, response, EdgarError)
        if last_status == 429:
            # Still rate-limited after the back-off: asking the next company would only
            # spend more of the same limit.
            raise EdgarRefused(429)
        raise EdgarError(f"{last} (after {len(RETRY_DELAYS_S) + 1} attempts)")

    async def company_tickers(self) -> Dict[str, int]:
        """SEC ticker -> CIK. An answer that is not the expected shape is `EdgarError`."""
        body = await self._get_json(TICKERS_URL)
        if not isinstance(body, dict):
            raise EdgarError("company_tickers.json answered without a ticker table")
        out: Dict[str, int] = {}
        for row in body.values():
            if not isinstance(row, dict):
                continue
            ticker, cik = row.get("ticker"), row.get("cik_str")
            if (
                isinstance(ticker, str)
                and isinstance(cik, int)
                and not isinstance(cik, bool)
            ):
                out.setdefault(ticker.upper(), cik)
        if not out:
            raise EdgarError("company_tickers.json listed no ticker")
        return out

    async def company_facts(self, cik: int) -> Optional[Dict[str, Any]]:
        """The company's companyfacts document, or None when the SEC has none (404)."""
        body = await self._get_json(FACTS_URL.format(cik=cik))
        if body is not None and not isinstance(body, dict):
            raise EdgarError(f"companyfacts for CIK {cik} is not an object")
        return body
