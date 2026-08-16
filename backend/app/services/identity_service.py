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

Never raises into its caller: an unresolvable ISIN degrades to grouping on the ISIN alone,
which is correct and merely less complete.
"""
import asyncio
import logging
from typing import Dict, Iterable, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.etf_sources import user_agent
from app.repositories.etf_basket_repository import EtfBasketRepository
from app.repositories.isin_identity_repository import IsinIdentityRepository
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


class IdentityService:
    """Fetches and caches company identity for ISINs."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = IsinIdentityRepository(db)

    @property
    def _openfigi_pacing(self) -> Tuple[Dict[str, str], int, float]:
        """`(extra headers, batch size, interval)` — a key changes all three together."""
        key = (settings.openfigi_api_key or "").strip()
        if key:
            return ({"X-OPENFIGI-APIKEY": key},
                    OPENFIGI_BATCH_KEYED, OPENFIGI_MIN_INTERVAL_KEYED_S)
        return ({}, OPENFIGI_BATCH, OPENFIGI_MIN_INTERVAL_S)

    async def resolve(
        self, isins: Iterable[str], limit: Optional[int] = None
    ) -> Dict:
        """
        Ask each provider about the ISINs it has never been asked about.

        Returns a summary suitable for a `sync_runs.details` payload. Writes are flushed
        but not committed — the caller owns the transaction, as everywhere else here.
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

        rows: Dict[str, Dict] = {}

        if need_figi:
            await self._resolve_openfigi(need_figi, rows, summary)
        if need_lei:
            await self._resolve_gleif(need_lei, rows, summary)

        if rows:
            await self.repo.upsert_many(list(rows.values()))
        summary["rows_written"] = len(rows)
        return summary

    # ----------------------------------------------------------------------- OpenFIGI

    async def _resolve_openfigi(
        self, isins: List[str], rows: Dict[str, Dict], summary: Dict
    ) -> None:
        extra, batch_size, interval = self._openfigi_pacing
        headers = {"Content-Type": "application/json", "User-Agent": user_agent(), **extra}
        async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT_S) as client:
            for index in range(0, len(isins), batch_size):
                batch = isins[index:index + batch_size]
                if index:
                    await asyncio.sleep(interval)
                try:
                    response = await client.post(
                        OPENFIGI_URL,
                        json=[{"idType": "ID_ISIN", "idValue": i} for i in batch],
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception as e:
                    # No checked_at stamp: the question was never answered, so it must be
                    # asked again rather than remembered as "nothing found".
                    summary["openfigi_errors"] += len(batch)
                    logger.warning(f"OpenFIGI batch of {len(batch)} failed: {e}")
                    continue

                if not isinstance(payload, list) or len(payload) != len(batch):
                    # The response is POSITIONALLY aligned with the request, so a length
                    # mismatch would attach one ISIN's FIGI to another. Refuse the batch.
                    summary["openfigi_errors"] += len(batch)
                    logger.warning(
                        f"OpenFIGI returned {len(payload) if isinstance(payload, list) else '?'} "
                        f"results for {len(batch)} jobs; refusing the batch rather than "
                        f"risking a positional mismatch"
                    )
                    continue

                stamp = utcnow()
                for isin, block in zip(batch, payload):
                    scfigi, composite, name = self._pick_figi(block)
                    row = rows.setdefault(isin, {"isin": isin})
                    row.update(
                        share_class_figi=scfigi,
                        composite_figi=composite,
                        figi_name=name,
                        figi_source="openfigi_api",
                        figi_checked_at=stamp,
                    )
                    if scfigi:
                        summary["openfigi_resolved"] += 1
                    else:
                        summary["openfigi_not_found"] += 1

    # ------------------------------------------------------- CINS / SEDOL -> ISIN

    async def resolve_constituent_identifiers(self, limit: Optional[int] = None) -> Dict:
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

        A row that resolves to nothing is re-asked on the next run rather than remembered, which
        the codebase normally refuses (`isin_identities`' `*_checked_at` split exists to stop
        exactly that loop). It is right here only because the pending set is a hundred or so
        identifiers across three funds — two requests with a key — and this is a manual CLI step,
        not a scheduled one. If that set ever grows past a few hundred, cache the misses.
        """
        repo = EtfBasketRepository(self.db)
        pending = await repo.unresolved_identifiers()
        if limit is not None:
            pending = pending[:limit]

        summary = {
            "identifiers_pending": len(pending),
            "identifiers_resolved": 0,
            "identifiers_not_found": 0,
            "identifiers_errors": 0,
            "holdings_updated": 0,
        }
        if not pending:
            return summary

        by_kind: Dict[str, List[str]] = {}
        for value in pending:
            id_type = OPENFIGI_ID_TYPES.get(identifier_kind(value) or "")
            if id_type:
                by_kind.setdefault(id_type, []).append(value)
            # Anything else was never an identifier; it is left alone rather than guessed at.

        found: Dict[str, str] = {}
        extra, batch_size, interval = self._openfigi_pacing
        headers = {"Content-Type": "application/json", "User-Agent": user_agent(), **extra}
        async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT_S) as client:
            first = True
            for id_type, values in sorted(by_kind.items()):
                for index in range(0, len(values), batch_size):
                    batch = values[index:index + batch_size]
                    if not first:
                        await asyncio.sleep(interval)
                    first = False
                    try:
                        response = await client.post(
                            OPENFIGI_URL,
                            json=[{"idType": id_type, "idValue": v} for v in batch],
                        )
                        response.raise_for_status()
                        payload = response.json()
                    except Exception as e:
                        summary["identifiers_errors"] += len(batch)
                        logger.warning(f"OpenFIGI {id_type} batch of {len(batch)} failed: {e}")
                        continue

                    if not isinstance(payload, list) or len(payload) != len(batch):
                        # Positionally aligned, exactly as the ISIN path is, so a length
                        # mismatch would attach one company's ISIN to another's row — the one
                        # error this feature cannot tolerate.
                        summary["identifiers_errors"] += len(batch)
                        logger.warning(
                            f"OpenFIGI returned a differently-sized result for {len(batch)} "
                            f"{id_type} jobs; refusing the batch"
                        )
                        continue

                    for value, block in zip(batch, payload):
                        scfigi, _, _ = self._pick_figi(block)
                        if scfigi:
                            found[value] = scfigi
                            summary["identifiers_resolved"] += 1
                        else:
                            summary["identifiers_not_found"] += 1

        if found:
            summary["holdings_updated"] = await repo.apply_resolved_identifiers(found)
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
        self, isins: List[str], rows: Dict[str, Dict], summary: Dict
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
                row = rows.setdefault(isin, {"isin": isin})
                row.update(
                    lei=lei,
                    issuer_name=name,
                    lei_source="gleif_api" if lei else None,
                    lei_checked_at=utcnow(),
                )
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
