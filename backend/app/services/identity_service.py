"""
Resolve ISIN -> company identity from OpenFIGI and GLEIF, and cache both answers.

**Neither provider is optional, because they fail in opposite directions.** Measured
against this account's own ISINs on 2026-08-14:

    GLEIF        folds share classes           both Alphabet ISINs -> one LEI,
                                               both ASML ISINs -> one LEI
                 has NO ISIN record for        TSMC, Samsung, SK Hynix, Credo
    OpenFIGI     folds one share class         GOOGL@US + ABEA@Xetra -> one shareClassFIGI
                 across venues
                 resolves                      all four ISINs GLEIF misses

So a single-provider design would either lose every Asian ordinary this account holds
directly, or lose the Alphabet/ASML folds that are the whole visible point of the feature.

**Neither Yahoo nor IBKR is involved, so neither rule at the top of CLAUDE.md applies** —
but these are still third-party services used as a guest rather than a customer, so: a
descriptive User-Agent carrying a contact address, one request at a time, generous pacing
below the documented ceilings, and no retry storm. FIGI identifiers are public-domain;
GLEIF publishes openly. Nothing here redistributes either.

**A definitive "no" is cached and a failed request is not.** `*_checked_at` is stamped only
when a provider answered with a parseable body — so a 500 or a timeout is retried next run,
while "GLEIF has no record for this ISIN" is remembered. Without that split every run
re-asks the four permanently-absent ISINs forever, which is the loop the holiday rule in
`get_missing_dates()` and `UPSTREAM_RETRY_COOLDOWN_SECONDS` in `benchmark_service` both
exist to stop.

**A 429 from OpenFIGI ends every OpenFIGI request of the run** (issue #66). Both passes —
ISINs and CINS/SEDOL — ask through one `_openfigi_batches` on the service's one paced
`OpenFigiClient`, so the second pass's first request is spaced from the first pass's last,
and a refusal in either latches `openfigi_rate_limited` for both. Each used to carry its
own copy of the loop and its own pacer, and `continue` past a 429 to the next batch.

Never raises into its caller: an unresolvable ISIN degrades to grouping on the ISIN alone,
which is correct and merely less complete.
"""
import asyncio
import logging
import time
from contextlib import aclosing, asynccontextmanager
from datetime import timedelta
from typing import (
    Any, AsyncIterator, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple,
)

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.etf_sources import user_agent
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.repositories.isin_identity_repository import IsinIdentityRepository
from app.services.paced_http import PacedGet
from app.services.security_identifiers import identifier_kind

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"

# Documented no-key limits: 25 requests/minute at 10 mapping jobs each (an 11th job is a
# hard HTTP 413, verified). 2.6s between requests keeps us under 25/min with headroom.
OPENFIGI_BATCH = 10
OPENFIGI_MIN_INTERVAL_S = 2.6

# With a free `X-OPENFIGI-APIKEY`: 100 jobs per request and 250 requests/minute. Both numbers
# move together and neither is worth having alone — a 10x batch at the keyless rate would
# still take an hour for a fund's tail. 0.3s keeps us under 250/min with the same headroom
# ratio as the keyless pacing above.
OPENFIGI_BATCH_KEYED = 100
OPENFIGI_MIN_INTERVAL_KEYED_S = 0.3

# Which `idType` each shape of identifier must be asked as. **Not interchangeable, and the
# obvious substitution fails silently**: asked as `ID_CUSIP`, the CINS `G29183103` (Eaton)
# returns zero rows, while as `ID_CINS` it returns 104 venue lines sharing one shareClassFIGI.
# Verified against the live API on 2026-08-16, which is the only way to learn this — a wrong
# idType is not an error, it is an empty answer.
OPENFIGI_ID_TYPES = {
    "CUSIP": "ID_CUSIP",
    "CINS": "ID_CINS",
    "SEDOL": "ID_SEDOL",
    "ISIN": "ID_ISIN",
}

# GLEIF publishes no rate limit and returns no rate-limit headers, so this is a
# self-imposed courtesy rather than a measured ceiling. One ISIN per request — the API has
# no batch form for an ISIN filter.
GLEIF_MIN_INTERVAL_S = 1.1

REQUEST_TIMEOUT_S = 20.0

# How long OpenFIGI's "no match" for a CINS/SEDOL is believed before it is asked again. Not
# forever, unlike an ISIN miss: a newly listed security's identifier can be missing from
# OpenFIGI for a while and then appear.
IDENTIFIER_MISS_RECHECK_DAYS = 30


def rotating_window(items: List[str], limit: int, turn: int) -> List[str]:
    """
    `limit` items starting `turn * limit` along `items`, wrapping around.

    Consecutive turns cover every item within `ceil(len(items) / limit)` turns whatever the
    items are, so no prefix of the list can hold the budget for good.
    """
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    start = (turn * limit) % len(items)
    return (items[start:] + items[:start])[:limit]


class OpenFigiRateLimited(Exception):
    """OpenFIGI answered 429: no further OpenFIGI request this run, from either pass."""


class OpenFigiClient(PacedGet):
    """
    The OpenFIGI mapping endpoint, paced by the shared `paced_http.PacedGet`.

    One per `IdentityService`, outliving each pass's HTTP session (`session`), so its
    `_last_request_at` spaces the CINS/SEDOL pass's first request from the ISIN pass's
    last. A POST, so `PacedGet._send` (a GET) is not used; `_paced` is.
    """

    def __init__(
        self,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._min_interval_s = OPENFIGI_MIN_INTERVAL_S
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: Optional[float] = None
        self._client = None

    @asynccontextmanager
    async def session(self, headers: Dict[str, str], interval: float) -> AsyncIterator[None]:
        """One pass's HTTP client; the pacing state stays on this object across passes."""
        self._min_interval_s = interval
        async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT_S) as client:
            self._client = client
            try:
                yield
            finally:
                self._client = None

    async def map(self, jobs: List[Tuple[str, str]]) -> Any:
        """POST one batch of `(idType, idValue)` jobs; the decoded body, or raises."""
        async with self._paced():
            response = await self._client.post(
                OPENFIGI_URL, json=[{"idType": t, "idValue": v} for t, v in jobs],
            )
        if response.status_code == 429:
            raise OpenFigiRateLimited()
        response.raise_for_status()
        return response.json()


class IdentityService:
    """Fetches and caches company identity for ISINs."""

    # Latched by the first OpenFIGI 429 and read by both passes (`_openfigi_batches`). On
    # the class as well, so a service built through `__new__` in a test can read it.
    openfigi_rate_limited = False

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = IsinIdentityRepository(db)
        self.openfigi_rate_limited = False
        self._openfigi = OpenFigiClient()

    @property
    def _openfigi_pacing(self) -> Tuple[Dict[str, str], int, float]:
        """`(extra headers, batch size, interval)` — a key changes all three together."""
        key = (settings.openfigi_api_key or "").strip()
        if key:
            return ({"X-OPENFIGI-APIKEY": key},
                    OPENFIGI_BATCH_KEYED, OPENFIGI_MIN_INTERVAL_KEYED_S)
        return ({}, OPENFIGI_BATCH, OPENFIGI_MIN_INTERVAL_S)

    async def _openfigi_batches(
        self, jobs: List[Tuple[str, str]]
    ) -> AsyncIterator[Tuple[List[str], Optional[list]]]:
        """
        Ask OpenFIGI about `(idType, idValue)` jobs in batches; yield each batch's values
        with its answer, positionally aligned — or None when the request failed or the
        answer was not one block per job, which the caller counts and stamps nothing for.

        The one OpenFIGI loop, for both passes, on the service's one `OpenFigiClient`. A 429
        latches `openfigi_rate_limited` and ends this and every later call on the service:
        what was not asked is simply still unasked, and the next run resumes it.
        """
        if self.openfigi_rate_limited or not jobs:
            return
        extra, batch_size, interval = self._openfigi_pacing
        headers = {"Content-Type": "application/json", "User-Agent": user_agent(), **extra}
        async with self._openfigi.session(headers, interval):
            for index in range(0, len(jobs), batch_size):
                batch = jobs[index:index + batch_size]
                values = [value for _, value in batch]
                try:
                    payload = await self._openfigi.map(batch)
                except OpenFigiRateLimited:
                    self.openfigi_rate_limited = True
                    logger.warning(
                        f"OpenFIGI answered HTTP 429; no further OpenFIGI request this run "
                        f"({len(jobs) - index} of {len(jobs)} jobs left unasked)"
                    )
                    return
                except Exception as e:
                    logger.warning(f"OpenFIGI batch of {len(batch)} failed: {e}")
                    yield values, None
                    continue
                if not isinstance(payload, list) or len(payload) != len(batch):
                    # The response is POSITIONALLY aligned with the request, so a length
                    # mismatch would attach one identifier's FIGI to another. Refuse it.
                    logger.warning(
                        f"OpenFIGI returned "
                        f"{len(payload) if isinstance(payload, list) else '?'} results for "
                        f"{len(batch)} jobs; refusing the batch rather than risking a "
                        f"positional mismatch"
                    )
                    yield values, None
                    continue
                yield values, payload

    async def resolve(
        self,
        isins: Iterable[str],
        limit: Optional[int] = None,
        on_batch: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Dict:
        """
        Ask each provider about the ISINs it has never been asked about.

        Returns a summary suitable for a `sync_runs.details` payload. Each provider batch is
        upserted as soon as it is answered — one OpenFIGI request, one GLEIF ISIN — and
        `on_batch` runs after each: `resolve_identities` passes `db.commit`, so a GLEIF run
        killed forty minutes in keeps every answer it already had instead of re-asking them
        all. Without it writes are flushed but not committed — the caller owns the
        transaction, as everywhere else here.
        """
        wanted = sorted({i.strip().upper() for i in isins if i and i.strip()})
        need_figi = await self.repo.unchecked(wanted, "openfigi")
        need_lei = await self.repo.unchecked(wanted, "gleif")
        if limit is not None:
            need_figi = need_figi[:limit]
            need_lei = need_lei[:limit]

        summary = {
            "requested": len(wanted),
            "openfigi_asked": len(need_figi),
            "gleif_asked": len(need_lei),
            "openfigi_resolved": 0,
            "openfigi_not_found": 0,
            "openfigi_errors": 0,
            "gleif_resolved": 0,
            "gleif_not_found": 0,
            "gleif_ambiguous": 0,
            "gleif_errors": 0,
        }

        written: Set[str] = set()

        if need_figi:
            await self._resolve_openfigi(need_figi, summary, written, on_batch)
        if need_lei:
            await self._resolve_gleif(need_lei, summary, written, on_batch)

        summary["rows_written"] = len(written)
        summary["openfigi_rate_limited"] = self.openfigi_rate_limited
        return summary

    async def _persist(self, rows: List[Dict], written: Set[str], on_batch) -> None:
        """Upsert one batch's answers. Each provider writes only its own columns."""
        if not rows:
            return
        await self.repo.upsert_many(rows)
        written.update(row["isin"] for row in rows)
        if on_batch is not None:
            await on_batch()

    # ----------------------------------------------------------------------- OpenFIGI

    async def _resolve_openfigi(
        self, isins: List[str], summary: Dict, written: Set[str], on_batch=None
    ) -> None:
        jobs = [("ID_ISIN", isin) for isin in isins]
        async with aclosing(self._openfigi_batches(jobs)) as batches:
            async for batch, payload in batches:
                if payload is None:
                    # No checked_at stamp: the question was never answered, so it must be
                    # asked again rather than remembered as "nothing found".
                    summary["openfigi_errors"] += len(batch)
                    continue

                stamp = utcnow()
                rows: List[Dict] = []
                for isin, block in zip(batch, payload):
                    scfigi, composite, name = self._pick_figi(block)
                    rows.append({
                        "isin": isin,
                        "share_class_figi": scfigi,
                        "composite_figi": composite,
                        "figi_name": name,
                        "figi_source": "openfigi_api",
                        "figi_checked_at": stamp,
                    })
                    if scfigi:
                        summary["openfigi_resolved"] += 1
                    else:
                        summary["openfigi_not_found"] += 1
                await self._persist(rows, written, on_batch)

    # ------------------------------------------------------- CINS / SEDOL -> ISIN

    async def pending_constituent_identifiers(self) -> List[str]:
        """
        Identifiers with no ISIN and no FIGI, less those OpenFIGI answered "no match" for
        within `IDENTIFIER_MISS_RECHECK_DAYS`. The one definition of what this pass would
        ask, shared with the CLI's count.
        """
        repo = EtfBasketRepository(self.db)
        unresolved = await repo.unresolved_identifiers()
        known_misses = await repo.identifier_misses_since(
            utcnow() - timedelta(days=IDENTIFIER_MISS_RECHECK_DAYS)
        )
        return [i for i in unresolved if i not in known_misses]

    async def resolve_constituent_identifiers(
        self,
        limit: Optional[int] = None,
        on_batch: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Dict:
        """
        Give a shareClassFIGI to fund constituents whose issuer published no ISIN.

        Three of the four US adapters publish nine-character identifiers instead, and most of
        those cannot be converted locally: 77 of GRID's 128 rows are CINS (Eaton, Schneider and
        Johnson Controls — its three largest holdings), and 20 of QTUM's 89 are SEDOLs. Without
        this pass each of those companies stands alone as its own look-through row, so one
        genuinely held twice — Eaton sits in both GRID and MSCI World — shows up as two.

        **It writes a FIGI, not an ISIN, because OpenFIGI has no ISIN to give.** Measured
        2026-08-16: `ID_CINS G29183103` returns 104 venue rows, every field a FIGI, ticker or
        exchange code. `IdentityMember` already unions on `share_class_figi`, so the FIGI folds
        the row exactly as well — and it is a real identifier rather than one we made up.

        **The idType is read off the identifier's shape, and the obvious choices are wrong.**
        `ID_CUSIP` with a CINS returns **zero rows** — resolving nothing, silently — so a CINS
        must go as `ID_CINS`, and a SEDOL as `ID_SEDOL`. Both verified live before this was
        written.

        **A miss is remembered** (`constituent_identifier_misses`, since 2026-10-17) and asked
        again only after `IDENTIFIER_MISS_RECHECK_DAYS` — the same `*_checked_at` split
        `isin_identities` makes, and for the same reason: until then a permanent miss was
        re-asked on every pass, which the daily basket refresh would have made every evening.
        A failed request stamps nothing, so it is asked again. Scheduled callers still pass
        `limit` (`etf_basket_refresh` passes `SCHEDULED_IDENTITY_LIMIT`), which bounds one
        evening's pass the way the ISIN pass is bounded.

        **The bounded window rotates by day** (`rotating_window`). It was `pending[:limit]` of
        a sorted list until 2026-10-15, so the same alphabetically-first identifiers were asked
        every evening and everything after them never: ASML's CINS `N07059210` sat unasked in
        SOXQ and QQQM while OpenFIGI resolves it at once, and ASML read as three companies.

        Answers are written per OpenFIGI batch and `on_batch` runs after each, as in `resolve`.
        """
        repo = EtfBasketRepository(self.db)
        pending = await self.pending_constituent_identifiers()
        if limit is not None:
            pending = rotating_window(pending, limit, utcnow().date().toordinal())

        summary = {
            "identifiers_pending": len(pending),
            "identifiers_resolved": 0,
            "identifiers_not_found": 0,
            "identifiers_errors": 0,
            "holdings_updated": 0,
            "openfigi_rate_limited": self.openfigi_rate_limited,
        }
        if not pending:
            return summary

        jobs: List[Tuple[str, str]] = []
        for value in pending:
            id_type = OPENFIGI_ID_TYPES.get(identifier_kind(value) or "")
            if id_type:
                jobs.append((id_type, value))
            # Anything else was never an identifier; it is left alone rather than guessed at.
        # Grouped by idType, as the batches were when each kind had its own loop. Each job
        # names its own idType, so a batch spanning two kinds is still one valid request.
        jobs.sort(key=lambda job: job[0])

        async with aclosing(self._openfigi_batches(jobs)) as batches:
            async for batch, payload in batches:
                if payload is None:
                    # Unanswered, so nothing is stamped and it is asked again next run.
                    summary["identifiers_errors"] += len(batch)
                    continue

                found: Dict[str, str] = {}
                missed: List[str] = []
                for value, block in zip(batch, payload):
                    scfigi, _, _ = self._pick_figi(block)
                    if scfigi:
                        found[value] = scfigi
                        summary["identifiers_resolved"] += 1
                    else:
                        missed.append(value)
                        summary["identifiers_not_found"] += 1

                if found:
                    summary["holdings_updated"] += await repo.apply_resolved_identifiers(found)
                    await repo.forget_identifier_misses(found)
                if missed:
                    await repo.record_identifier_misses(missed, utcnow())
                if on_batch is not None:
                    await on_batch()
        summary["openfigi_rate_limited"] = self.openfigi_rate_limited
        return summary

    @staticmethod
    def _pick_figi(block) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Reduce one ISIN's result to a single shareClassFIGI, compositeFIGI and name.

        An ISIN returns *every venue line* — 231 rows for Alphabet class A — which all
        share one shareClassFIGI except for a handful of synthetic currency lines that
        carry `null`. So the first non-null wins rather than the first row, or those
        synthetic lines would occasionally decide the answer.
        """
        if not isinstance(block, dict):
            return None, None, None
        data = block.get("data")
        if not isinstance(data, list) or not data:
            return None, None, None

        def first(field: str) -> Optional[str]:
            for row in data:
                if isinstance(row, dict):
                    value = row.get(field)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            return None

        return first("shareClassFIGI"), first("compositeFIGI"), first("name")

    # -------------------------------------------------------------------------- GLEIF

    async def _resolve_gleif(
        self, isins: List[str], summary: Dict, written: Set[str], on_batch=None
    ) -> None:
        headers = {
            "Accept": "application/vnd.api+json",
            "User-Agent": user_agent(),
        }
        async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT_S) as client:
            for index, isin in enumerate(isins):
                if index:
                    await asyncio.sleep(GLEIF_MIN_INTERVAL_S)
                try:
                    response = await client.get(GLEIF_URL, params={"filter[isin]": isin})
                    response.raise_for_status()
                    payload = response.json()
                except Exception as e:
                    summary["gleif_errors"] += 1
                    logger.warning(f"GLEIF lookup for {isin} failed: {e}")
                    continue

                lei, name, ambiguous = self._pick_lei(payload)
                await self._persist([{
                    "isin": isin,
                    "lei": lei,
                    "issuer_name": name,
                    "lei_source": "gleif_api" if lei else None,
                    "lei_checked_at": utcnow(),
                }], written, on_batch)
                if ambiguous:
                    summary["gleif_ambiguous"] += 1
                    logger.warning(
                        f"GLEIF returned several LEI records for {isin}; recording none, "
                        f"so the ISIN groups on its shareClassFIGI or on itself"
                    )
                elif lei:
                    summary["gleif_resolved"] += 1
                else:
                    summary["gleif_not_found"] += 1

    @staticmethod
    def _pick_lei(payload) -> Tuple[Optional[str], Optional[str], bool]:
        """
        (LEI, legal name, ambiguous) from a GLEIF JSON:API response.

        **More than one record refuses rather than picking.** An arbitrary choice could
        fold one company's ISIN into another's group, which is the one direction of error
        this feature cannot tolerate — while refusing merely leaves the ISIN grouped on a
        weaker identifier. Same judgement `import_prices._resolve_security` makes about an
        ambiguous symbol.
        """
        if not isinstance(payload, dict):
            return None, None, False
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None, None, False
        if len(data) > 1:
            return None, None, True

        record = data[0]
        lei = record.get("id") if isinstance(record, dict) else None
        name = None
        try:
            name = record["attributes"]["entity"]["legalName"]["name"]
        except (KeyError, TypeError):
            pass
        return (lei or None), (name or None), False


async def held_isins(db: AsyncSession, funds: Optional[bool] = False) -> List[str]:
    """
    Distinct ISINs (stripped, upper-cased, sorted) of securities with at least one open
    tax lot — `security_repository.held()` — split by `is_known_etf_isin`.

    ``funds=False`` (the default) is the companies only: GLEIF returns no record for a fund
    ISIN (measured on IE00B4L5Y983), a fund is never a row in the company table, and the
    requests buy nothing. ``funds=True`` is the funds only — the basket refresh's list.
    ``funds=None`` is both, because a fund missing from `ETF_ALLOCATIONS` is treated as an
    ordinary holding, and resolving it then gives it a proper name.

    Shared by `app/cli/resolve_identities.py` and both scheduled passes in
    `etf_basket_refresh`, so none of them can disagree about what "held" means.
    """
    from app.etf_mappings import is_known_etf_isin
    from app.models.security import Security
    from app.repositories.security_repository import held

    rows = (
        await db.execute(select(Security.isin).where(held()).distinct())
    ).scalars().all()
    isins = sorted({r.strip().upper() for r in rows if r and r.strip()})
    if funds is None:
        return isins
    return [i for i in isins if is_known_etf_isin(i) == funds]
