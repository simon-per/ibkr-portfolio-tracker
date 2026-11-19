"""
filings.xbrl.org, XBRL International's index of the European Single Electronic Format's annual
reports (docs/company-financials.md, *Europe*): the Company tab's European figures. A JSON:API
with no key and no published limit; two calls, one door:

- `/api/entities/<LEI>/filings` — a company's ESEF filings by its LEI: each one's period end,
  its package id (`fxo_id`), when it was added and where its xBRL-JSON lies (`json_url`). An
  LEI the index does not hold answers an empty list. Paged (`links.next`); at most
  `MAX_PAGES` pages are followed.
- the filing's xBRL-JSON (`json_url`): its facts, each with its concept, period, unit and any
  further dimension. Read in full only up to `MAX_REPORT_BYTES` (most are 2-5 MB; a large
  bank's can pass 60 MB, Santander's 2024 report did); a larger one is `TooLarge`, never
  half-read.

Paced at `MIN_INTERVAL_S`; HTTP 429/5xx and transport failures are retried twice, then
`EsefError` for that company. Nothing here is a secret.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.services.paced_http import PacedGet

BASE_URL = "https://filings.xbrl.org"
FILINGS = BASE_URL + "/api/entities/{lei}/filings"

REQUEST_TIMEOUT_S = 120.0
MIN_INTERVAL_S = 1.0
RETRY_DELAYS_S = (5.0, 30.0)
PAGE_SIZE = 100
MAX_PAGES = 5
# Santander's reports run 57-71 MB, mostly text blocks (1,953 facts in its 70.6 MB FY2022):
# parsed, 133 MB held and 206 MB at the peak beside the raw bytes (measured 2026-11-19), on
# a VPS with 6 GB free. A filing refused at a smaller cap is read once more at this one
# (`esef_sync.retry_larger`).
MAX_REPORT_BYTES = 150_000_000
#: A filings page (about 1 kB a filing).
MAX_LIST_BYTES = 5_000_000
USER_AGENT = (
    "ibkr-portfolio-tracker (+https://github.com/simon-per/ibkr-portfolio-tracker)"
)


class EsefError(Exception):
    """A transient failure: the company's reads stop for this pass, nothing is stamped."""


class TooLarge(EsefError):
    """A filing's xBRL-JSON over `MAX_REPORT_BYTES`: never read, whatever the pass."""


class NotFound(Exception):
    """HTTP 404: the index holds no such entity (an LEI it never received a filing for)."""


@dataclass(frozen=True)
class Filing:
    """One ESEF filing as the index lists it."""

    fxo_id: str
    period_end: date
    added: Optional[date]
    json_url: Optional[str]
    country: Optional[str]


def _date(text: Any) -> Optional[date]:
    try:
        return datetime.fromisoformat(str(text).strip()[:10]).date()
    except (TypeError, ValueError):
        return None


def parse_filings(body: Any) -> List[Filing]:
    """A filings page's filings; one without an id or a period end is left out."""
    out: List[Filing] = []
    data = body.get("data") if isinstance(body, dict) else None
    for item in data if isinstance(data, list) else []:
        attrs = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(attrs, dict):
            continue
        fxo = str(attrs.get("fxo_id") or "").strip()
        end = _date(attrs.get("period_end"))
        if not fxo or end is None:
            continue
        url = str(attrs.get("json_url") or "").strip() or None
        out.append(
            Filing(
                fxo,
                end,
                _date(attrs.get("date_added")),
                url,
                str(attrs.get("country") or "").strip().upper() or None,
            )
        )
    return out


class EsefClient(PacedGet):
    """One pass's filings.xbrl.org calls. Use as an async context manager."""

    def __init__(
        self,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = MIN_INTERVAL_S,
    ):
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._min_interval_s = min_interval_s
        self._last_request_at: Optional[float] = None
        self._client: Optional[httpx.AsyncClient] = None
        #: Requests made, for the run's record.
        self.requests = 0

    async def __aenter__(self) -> "EsefClient":
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S, transport=self._transport, follow_redirects=True
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(
        self, url: str, params: Optional[Dict[str, Any]], limit: int
    ) -> bytes:
        last = ""
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            if attempt:
                await self._sleep(RETRY_DELAYS_S[attempt - 1])
            self.requests += 1
            try:
                async with self._paced():
                    async with self._client.stream(
                        "GET",
                        url,
                        params=params,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": USER_AGENT,
                        },
                    ) as response:
                        if response.status_code in (429, 500, 502, 503, 504):
                            last = f"{url}: HTTP {response.status_code}"
                            continue
                        if response.status_code == 404:
                            raise NotFound(url)
                        if response.status_code != 200:
                            raise EsefError(f"{url}: HTTP {response.status_code}")
                        chunks: List[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise TooLarge(f"{url}: larger than {limit} bytes")
                            chunks.append(chunk)
                        return b"".join(chunks)
            except httpx.HTTPError as e:
                last = f"{url}: {type(e).__name__}: {e}"
        raise EsefError(last or f"{url}: no answer")

    async def _json(
        self, url: str, params: Optional[Dict[str, Any]], limit: int = MAX_LIST_BYTES
    ) -> Any:
        data = await self._get(url, params, limit)
        try:
            return json.loads(data)
        except ValueError as e:
            raise EsefError(f"{url}: the response was not JSON") from e

    async def filings(self, lei: str) -> List[Filing]:
        """Every ESEF filing the index holds for the LEI: none (an empty list) also when it
        knows no such entity (HTTP 404)."""
        url: Optional[str] = FILINGS.format(lei=lei)
        params: Optional[Dict[str, Any]] = {"page[size]": PAGE_SIZE}
        out: List[Filing] = []
        for _ in range(MAX_PAGES):
            if url is None:
                break
            try:
                body = await self._json(url, params)
            except NotFound:
                if out:
                    raise EsefError(f"{url}: HTTP 404 on a later page") from None
                return []
            if not isinstance(body, dict):
                raise EsefError(f"{url}: the response was not an object")
            out.extend(parse_filings(body))
            nxt = (body.get("links") or {}).get("next")
            url = str(nxt) if nxt and str(nxt).startswith(BASE_URL) else None
            params = None
        return out

    async def report(self, json_url: str) -> Dict[str, Any]:
        """A filing's xBRL-JSON document."""
        url = (
            json_url
            if json_url.startswith(BASE_URL)
            else BASE_URL + "/" + json_url.lstrip("/")
        )
        try:
            body = await self._json(url, None, MAX_REPORT_BYTES)
        except NotFound:
            raise EsefError(f"{url}: HTTP 404") from None
        if not isinstance(body, dict) or not isinstance(body.get("facts"), dict):
            raise EsefError(f"{url}: not an xBRL-JSON document")
        return body
