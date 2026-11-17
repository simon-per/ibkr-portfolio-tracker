"""
OpenDART, Korea's Financial Supervisory Service's disclosure API (docs/company-financials.md,
*South Korea*): the Company tab's Korean figures. Three endpoints, one door:

- `corpCode.xml` — a zip of every filer: stock code -> `corp_code` (the company's id here).
- `company.json` — one company's profile: its industry code (`induty_code`, KSIC), the month
  its fiscal year ends (`acc_mt`), its English name.
- `fnlttSinglAcntAll.json` — one periodic report's full consolidated statements (`fs_div=CFS`),
  every line with its `account_id` (`ifrs-full_*`, `dart_*`, or none: non-standard).

**The key travels in the query string** (`crtfc_key`): it is never put in a message, a log
line or an exception (`_scrub`), and `app/redact.py` masks it besides. OpenDART answers HTTP
200 with its own `status` in the body: `000` an answer, `013` no data, `020` the daily limit
(20,000 requests), `010`/`011`/`012`/`901` a key or IP it refuses — `OpenDartRefused`, which
stops the pass; `800` maintenance and transport failures are retried twice, then
`OpenDartError` for that company. Paced at `MIN_INTERVAL_S`, well under the published 1,000
a minute.
"""

import asyncio
import io
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.services.paced_http import PacedGet

BASE_URL = "https://opendart.fss.or.kr/api"
CORP_CODES = f"{BASE_URL}/corpCode.xml"
COMPANY = f"{BASE_URL}/company.json"
STATEMENTS = f"{BASE_URL}/fnlttSinglAcntAll.json"

REQUEST_TIMEOUT_S = 60.0
MIN_INTERVAL_S = 0.5
RETRY_DELAYS_S = (2.0, 10.0)
#: The largest corpCode.xml this reads (about 3.6 MB zipped, 30 MB unzipped in 2026-11).
MAX_ZIP_BYTES = 20_000_000

OK = "000"
NO_DATA = "013"
#: The key, the IP, the daily limit: asking the next company would be refused the same way.
REFUSED = frozenset({"010", "011", "012", "020", "021", "901"})
RETRYABLE = frozenset({"800"})


class OpenDartError(Exception):
    """A transient failure: the company's reads stop for this pass, nothing is stamped."""


class OpenDartRefused(Exception):
    """OpenDART refused the key, the IP or the day's budget: the pass stops."""

    def __init__(self, status: str, message: str = ""):
        super().__init__(f"OpenDART status {status}{': ' + message if message else ''}")
        self.status = status


@dataclass(frozen=True)
class CorpCode:
    corp_code: str
    name: Optional[str]
    name_en: Optional[str]


class OpenDartClient(PacedGet):
    """One pass's OpenDART calls. Use as an async context manager."""

    def __init__(
        self,
        api_key: str,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = MIN_INTERVAL_S,
    ):
        self._key = api_key
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._min_interval_s = min_interval_s
        self._last_request_at: Optional[float] = None
        self._client: Optional[httpx.AsyncClient] = None
        #: Requests made, for the run's record and the daily budget.
        self.requests = 0

    async def __aenter__(self) -> "OpenDartClient":
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S, transport=self._transport, follow_redirects=True
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    async def _get(self, url: str, params: Dict[str, str]) -> httpx.Response:
        last = ""
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            if attempt:
                await self._sleep(RETRY_DELAYS_S[attempt - 1])
            self.requests += 1
            try:
                response = await self._send(
                    url,
                    params={"crtfc_key": self._key, **params},
                    headers={"Accept": "*/*"},
                    transient=OpenDartError,
                )
            except OpenDartError as e:
                last = self._scrub(str(e))
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                last = f"{url}: HTTP {response.status_code}"
                continue
            if response.status_code != 200:
                raise OpenDartError(f"{url}: HTTP {response.status_code}")
            return response
        raise OpenDartError(last or f"{url}: no answer")

    async def _json(self, url: str, params: Dict[str, str]) -> Dict[str, Any]:
        """The body of an answer whose `status` is `000` or `013` (no data)."""
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            if attempt:
                await self._sleep(RETRY_DELAYS_S[attempt - 1])
            response = await self._get(url, params)
            try:
                body = response.json()
            except ValueError as e:
                raise OpenDartError(f"{url}: the response was not JSON") from e
            if not isinstance(body, dict):
                raise OpenDartError(f"{url}: the response was not an object")
            status = str(body.get("status") or "")
            if status in REFUSED:
                raise OpenDartRefused(
                    status, self._scrub(str(body.get("message") or ""))
                )
            if status in RETRYABLE:
                continue
            if status not in (OK, NO_DATA):
                raise OpenDartError(f"{url}: status {status}")
            return body
        raise OpenDartError(f"{url}: still under maintenance")

    async def corp_codes(self) -> Dict[str, CorpCode]:
        """Every listed filer: stock code -> its corp_code and names."""
        response = await self._get(CORP_CODES, {})
        data = response.content
        if len(data) > MAX_ZIP_BYTES:
            raise OpenDartError("corpCode.xml is larger than expected")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if len(names) != 1:
                    raise OpenDartError("corpCode.xml: not one file in the zip")
                xml = archive.read(names[0])
        except zipfile.BadZipFile:
            # A refusal arrives as a JSON or XML body, not a zip.
            try:
                body = response.json()
            except ValueError:
                raise OpenDartError("corpCode.xml: not a zip") from None
            status = str((body or {}).get("status") or "")
            if status in REFUSED:
                raise OpenDartRefused(status) from None
            raise OpenDartError(f"corpCode.xml: status {status}") from None
        return parse_corp_codes(xml)

    async def company(self, corp_code: str) -> Optional[Dict[str, Any]]:
        """The company's profile, or None when OpenDART has none."""
        body = await self._json(COMPANY, {"corp_code": corp_code})
        return body if body.get("status") == OK else None

    async def statements(
        self, corp_code: str, year: int, report_code: str
    ) -> Optional[List[Dict[str, Any]]]:
        """One report's consolidated statement lines, or None when it holds none (`013`)."""
        body = await self._json(
            STATEMENTS,
            {
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": report_code,
                "fs_div": "CFS",
            },
        )
        if body.get("status") != OK:
            return None
        rows = body.get("list")
        return (
            [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        )


def parse_corp_codes(xml: bytes) -> Dict[str, CorpCode]:
    """corpCode.xml's listed filers (a stock code), stock code -> `CorpCode`."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise OpenDartError("corpCode.xml is not XML") from e
    out: Dict[str, CorpCode] = {}
    for item in root.iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        corp = (item.findtext("corp_code") or "").strip()
        if len(stock) != 6 or not corp:
            continue
        out[stock] = CorpCode(
            corp,
            (item.findtext("corp_name") or "").strip() or None,
            (item.findtext("corp_eng_name") or "").strip() or None,
        )
    return out
