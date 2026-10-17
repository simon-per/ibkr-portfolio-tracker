"""
Holding logos from logo.dev's API, stored in `security_logos` (docs/logos.md).

**Server-side only, and the secret key never leaves this module's request headers.** The
browser never talks to logo.dev: it reads the stored bytes from `/api/logos/{id}`, so the
CSP stays `img-src 'self'`. logo.dev learns the ISINs of every security ever held — the
owner accepted that on 2026-10-17.

**Scope:** every security with an ISIN, sold ones included — the Activity tab lists every
trade ever made, and a sold holding's row is as much a row as a held one's — held ones
asked first, so a new purchase never waits behind the back catalogue. `fetch_logos
--basket` can also fill the constituents of a stored fund basket ahead of any purchase:
logos are keyed by ISIN, so a stock bought later shows its logo at once.

**Bounded, because every found logo is billed** (about $0.02; the free plan carries $5 of
credits a month): an ISIN is asked about only when it has no row or its row is older than
`RECHECK_AFTER`; a pass makes at most `MAX_LOOKUPS_PER_PASS` lookups, paced
`MIN_INTERVAL_S` apart; 401/402/403/429 stop the pass; a 202 (`not_indexed`) leaves the
ISIN for the next pass and never sleeps on `Retry-After`. "Nothing there" and a refused
image are remembered (`checked_at`) so they are not re-bought every pass; a failed request
is not, so it is retried next pass. A manual row (`app/cli/import_logo.py`) is never
asked about again.

**Refuse whole** (`validate_logo`): PNG, JPEG or WebP only — never SVG, which served from
our own origin could carry script — at most `MAX_LOGO_BYTES`, and the magic bytes must be
the type the server named. A refused image stores nothing.

Touches neither Yahoo nor IBKR. The Yahoo ticker used for the ticker fallback comes from
the price path's own resolver, which is a database lookup.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.models.etf_basket import EtfHolding
from app.models.security import Security
from app.models.security_logo import LOGO_SOURCE_LOGO_DEV
from app.repositories.security_logo_repository import SecurityLogoRepository
from app.repositories.security_repository import held
from app.services.paced_http import PacedGet

logger = logging.getLogger(__name__)

API_URL = "https://api.logo.dev/v2/brands/logo"
# Asked of the signed download URL: a 128 px square mark (the avatar is 32–36 px, so 2x+
# for high-DPI screens), PNG for a transparent background.
DOWNLOAD_PARAMS = {"size": "128", "format": "png"}
REQUEST_TIMEOUT_S = 20.0

RECHECK_AFTER = timedelta(days=90)
MAX_LOOKUPS_PER_PASS = 30
MIN_INTERVAL_S = 1.0
MAX_LOGO_BYTES = 100 * 1024

# HTTP statuses after which no further request is made this pass: the key is wrong (401,
# 403), the credits are spent (402), or the rate limit is reached (429).
STOP_STATUSES = frozenset({401, 402, 403, 429})

# Whitelist, by magic bytes. WebP is RIFF....WEBP, so its check reads two places.
_MAGIC: Dict[str, Callable[[bytes], bool]] = {
    "image/png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda b: b.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda b: len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP",
}
ALLOWED_CONTENT_TYPES = frozenset(_MAGIC)


class LogoRefused(ValueError):
    """The image (or the answer carrying it) is not one we store. Nothing is written."""


class LogoDevStop(Exception):
    """logo.dev refused the pass as a whole (`STOP_STATUSES`)."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class LogoDevError(Exception):
    """One request failed (transport, 5xx, an unexpected body). Retried next pass."""


def is_configured() -> bool:
    """True when the secret key is set. Read at call time so a test's monkeypatch counts."""
    return bool((settings.logo_dev_secret_key or "").strip())


def sniff_image_type(data: bytes) -> Optional[str]:
    """The whitelisted content type `data` starts like, or None."""
    for content_type, matches in _MAGIC.items():
        if matches(data):
            return content_type
    return None


def validate_logo(data: bytes, declared_type: Optional[str]) -> str:
    """
    The content type to store `data` under, or `LogoRefused`.

    `declared_type` is the server's Content-Type; None (a file on disk) means the magic
    bytes alone decide. The one implementation both the automatic pass and the manual
    import run, so the two cannot accept different things.
    """
    if not data:
        raise LogoRefused("empty file")
    if len(data) > MAX_LOGO_BYTES:
        raise LogoRefused(f"{len(data)} bytes, over the {MAX_LOGO_BYTES}-byte limit")
    sniffed = sniff_image_type(data)
    if declared_type is not None:
        declared = declared_type.split(";")[0].strip().lower()
        if declared not in ALLOWED_CONTENT_TYPES:
            raise LogoRefused(f"content type {declared or '(none)'} is not PNG, JPEG or WebP")
        if sniffed != declared:
            raise LogoRefused(f"declared {declared} but the bytes are {sniffed or 'not an image we accept'}")
    if sniffed is None:
        raise LogoRefused("not a PNG, JPEG or WebP image")
    return sniffed


class LogoDevClient(PacedGet):
    """
    One pass's worth of logo.dev calls. Use as an async context manager.

    Paced by the shared `paced_http.PacedGet` (one request at a time, spaced from when the
    previous one finished; `tests/test_crypto_http.py` fails a second `_pace`). `transport`, `sleep` and
    `clock` are injectable so tests drive it with `httpx.MockTransport`, never leave the
    machine, and measure pacing without waiting. The key travels in the `Authorization`
    header to `API_URL` only — never to the signed download URL, which is another host and
    needs none.
    """

    def __init__(
        self,
        key: str,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        min_interval_s: float = MIN_INTERVAL_S,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._key = key
        self._transport = transport
        self._min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: Optional[float] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "LogoDevClient":
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_S, transport=self._transport, follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def lookup(self, field: str, value: str) -> Tuple[str, Optional[str]]:
        """
        Ask for one brand by `isin` or `ticker`. Returns `("found", signed_url)`,
        `("not_found", None)` or `("not_indexed", None)`; raises `LogoDevStop` or
        `LogoDevError`.
        """
        response = await self._send(
            API_URL, params={field: value},
            headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
            transient=LogoDevError,
        )
        status = response.status_code
        if status in STOP_STATUSES:
            raise LogoDevStop(status)
        if status == 404:
            return "not_found", None
        if status == 202:
            return "not_indexed", None
        if status != 200:
            raise LogoDevError(f"lookup answered HTTP {status}")
        try:
            body = response.json()
        except ValueError as e:
            raise LogoDevError("lookup answered with a body that is not JSON") from e
        # The image sits inside the `{data, metadata}` envelope: `data.url` is the signed
        # download URL and `data.status` says whether it is ready (logo.dev's v2 reference,
        # read 2026-10-17 — the first live pass found no top-level `url`).
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise LogoDevError("lookup answered without a data object")
        if data.get("status") not in (None, "ready"):
            return "not_indexed", None
        url = data.get("url")
        if not isinstance(url, str) or not url:
            raise LogoDevError("lookup answered without a url")
        return "found", url

    async def download(self, url: str) -> Tuple[bytes, Optional[str]]:
        """
        Fetch the signed URL with `DOWNLOAD_PARAMS`. Reads at most one byte past
        `MAX_LOGO_BYTES`, so an oversized answer is refused without being held in full.
        """
        target = httpx.URL(url)
        if target.scheme != "https":
            raise LogoRefused("the download URL is not https")
        target = target.copy_merge_params(DOWNLOAD_PARAMS)
        await self._pace()
        try:
            async with self._client.stream("GET", target) as response:
                status = response.status_code
                if status in STOP_STATUSES:
                    raise LogoDevStop(status)
                if status != 200:
                    raise LogoDevError(f"download answered HTTP {status}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_LOGO_BYTES:
                        break
                return bytes(body), response.headers.get("content-type")
        except httpx.HTTPError as e:
            # Never str(e): it would carry the signed URL and its signature.
            raise LogoDevError(f"download failed: {type(e).__name__}") from e
        finally:
            self._last_request_at = self._clock()


@dataclass
class LogoTarget:
    """
    One ISIN the pass may ask about. `security` is None for a basket constituent nobody
    holds; its ticker fallback then uses the issuer's published ticker.
    """
    symbol: str
    security: Optional[Security] = None
    ticker: Optional[str] = None
    held: bool = False


class LogoService:
    """The logo pass: which ISINs are due, asking logo.dev, storing what passes."""

    def __init__(self, db: AsyncSession, transport: Optional[httpx.AsyncBaseTransport] = None,
                 sleep: Callable[[float], Any] = asyncio.sleep):
        self.db = db
        self.repo = SecurityLogoRepository(db)
        self._transport = transport
        self._sleep = sleep

    async def _targets(self, isin: Optional[str]) -> Dict[str, LogoTarget]:
        """ISIN -> one security carrying it: every security, or the one ISIN asked for."""
        query = select(Security).order_by(Security.id)
        if isin is not None:
            query = query.where(Security.isin == isin.strip().upper())
        held_ids = set((await self.db.execute(select(Security.id).where(held()))).scalars())
        out: Dict[str, LogoTarget] = {}
        for security in (await self.db.execute(query)).scalars().all():
            if not security.isin:
                continue
            target = out.setdefault(
                security.isin.strip().upper(), LogoTarget(security.symbol, security)
            )
            target.held = target.held or security.id in held_ids
        return out

    async def _basket_targets(
        self, funds: Sequence[str], top: Optional[int]
    ) -> Tuple[Dict[str, LogoTarget], List[str]]:
        """
        The constituents of each stored fund basket — a fund ISIN, or the symbol of a
        security that is one — heaviest first, the `top` largest of each when given.
        Returns the targets and the funds with no stored basket.
        """
        out: Dict[str, LogoTarget] = {}
        unknown: List[str] = []
        for ref in funds:
            ref = ref.strip().upper()
            fund_isins = {ref} | set((await self.db.execute(
                select(Security.isin).where(Security.symbol == ref, Security.isin.is_not(None))
            )).scalars())
            query = (
                select(EtfHolding)
                .where(EtfHolding.fund_isin.in_(fund_isins),
                       EtfHolding.constituent_isin.is_not(None))
                .order_by(EtfHolding.weight_pct.desc(), EtfHolding.line_no)
            )
            if top is not None:
                query = query.limit(top)
            rows = (await self.db.execute(query)).scalars().all()
            if not rows:
                unknown.append(ref)
            for row in rows:
                out.setdefault(row.constituent_isin.strip().upper(), LogoTarget(
                    (row.constituent_ticker or row.constituent_name)[:10],
                    ticker=row.constituent_ticker,
                ))
        return out, unknown

    async def _ticker(self, security: Security) -> str:
        # The price path's resolver, so the fallback asks about the listing we price.
        from app.services.market_data_service import MarketDataService
        return await MarketDataService(self.db)._get_yahoo_ticker(security)

    async def refresh(
        self,
        *,
        dry_run: bool = False,
        force: bool = False,
        isin: Optional[str] = None,
        baskets: Optional[Sequence[str]] = None,
        top: Optional[int] = None,
        limit: int = MAX_LOOKUPS_PER_PASS,
    ) -> Dict[str, Any]:
        """
        One pass. Returns a summary for a `sync_runs` row; writes are committed as they
        happen, so a crash mid-pass keeps what was already paid for.

        Without `LOGO_DEV_SECRET_KEY` a real pass is `skipped` with no warning and no
        query (a warning that is always present teaches the reader to skip it). A dry run
        makes no request and writes nothing, key or not.
        """
        configured = is_configured()
        if not dry_run and not configured:
            return {"status": "skipped", "message": "skipped: not configured"}

        now = utcnow()
        unknown_baskets: List[str] = []
        if baskets:
            targets, unknown_baskets = await self._basket_targets(baskets, top)
        else:
            targets = await self._targets(isin)
        due = await self.repo.due(targets, now, None if force else RECHECK_AFTER)
        # Held first (a stable sort keeps ISIN order within each half): a pass's budget goes
        # to a new purchase before the back catalogue of sold securities.
        due.sort(key=lambda i: not targets[i].held)
        summary: Dict[str, Any] = {
            "status": "success", "configured": configured, "targets": len(targets),
            "unknown_baskets": unknown_baskets,
            "due": len(due), "lookups": 0, "stored": 0, "not_found": 0,
            "not_indexed": 0, "refused": 0, "errors": 0, "deferred": 0, "stopped": None,
            "outcomes": [],
        }
        if dry_run:
            summary["outcomes"] = [
                {"isin": i, "symbol": targets[i].symbol, "outcome": "due"} for i in due
            ]
            summary["message"] = (
                f"Logos: {len(due)} of {len(targets)} due, at most {limit} lookups a pass "
                f"(dry run, no request made)"
            )
            return summary
        if not due:
            summary["message"] = "Logos: nothing due"
            return summary

        warnings: List[str] = []
        async with LogoDevClient(settings.logo_dev_secret_key.strip(),
                                 transport=self._transport, sleep=self._sleep) as client:
            for position, target_isin in enumerate(due):
                target = targets[target_isin]
                if summary["lookups"] >= limit:
                    summary["deferred"] += len(due) - position
                    break
                try:
                    outcome = await self._one(client, target_isin, target, summary, limit)
                except LogoDevStop as stop:
                    summary["stopped"] = stop.status
                    summary["deferred"] += len(due) - position
                    warnings.append(
                        f"logo.dev answered HTTP {stop.status}; the logo pass stopped and "
                        f"the rest waits for the next one."
                    )
                    break
                except LogoRefused as refused:
                    summary["refused"] += 1
                    await self.repo.mark_checked(target_isin, LOGO_SOURCE_LOGO_DEV, utcnow())
                    await self.db.commit()
                    outcome = f"refused: {refused}"
                    warnings.append(f"{target.symbol} ({target_isin}): logo refused — {refused}.")
                except LogoDevError as error:
                    summary["errors"] += 1
                    outcome = f"error: {error}"
                summary["outcomes"].append(
                    {"isin": target_isin, "symbol": target.symbol, "outcome": outcome}
                )

        if summary["errors"]:
            n = summary["errors"]
            warnings.append(
                f"{n} logo lookup{'' if n == 1 else 's'} failed and will be asked again "
                f"next pass."
            )
        summary["message"] = (
            f"Logos: {summary['stored']} stored, {summary['not_found']} not found, "
            f"{summary['not_indexed']} not indexed yet, {summary['refused']} refused, "
            f"{summary['errors']} failed, {summary['deferred']} left for the next pass "
            f"({summary['lookups']} lookups)"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _one(self, client: LogoDevClient, isin: str, target: LogoTarget,
                   summary: Dict[str, Any], limit: int) -> str:
        """Ask about one ISIN, then its ticker if logo.dev has no such ISIN."""
        summary["lookups"] += 1
        state, url = await client.lookup("isin", isin)
        via = "isin"
        ticker = None
        if state == "not_found":
            ticker = (await self._ticker(target.security) if target.security is not None
                      else target.ticker)
        if state == "not_found" and ticker:
            if summary["lookups"] >= limit:
                # The fallback would overspend the pass; nothing is stamped, so the
                # next pass asks again from the start.
                summary["deferred"] += 1
                return "deferred: ticker lookup over this pass's budget"
            summary["lookups"] += 1
            state, url = await client.lookup("ticker", ticker)
            via = f"ticker {ticker}"
        if state == "not_indexed":
            summary["not_indexed"] += 1
            return "not indexed yet"
        if state == "not_found":
            summary["not_found"] += 1
            await self.repo.mark_checked(isin, LOGO_SOURCE_LOGO_DEV, utcnow())
            await self.db.commit()
            return "not found"

        data, content_type = await client.download(url)
        stored_type = validate_logo(data, content_type)
        written = await self.repo.store(isin, data, stored_type, LOGO_SOURCE_LOGO_DEV, utcnow())
        await self.db.commit()
        if not written:
            return "kept the manual logo"
        summary["stored"] += 1
        return f"stored via {via}"
