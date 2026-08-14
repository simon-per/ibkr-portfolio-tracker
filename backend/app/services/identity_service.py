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
from app.repositories.isin_identity_repository import IsinIdentityRepository

logger = logging.getLogger(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"

# Documented no-key limits: 25 requests/minute at 10 mapping jobs each (an 11th job is a
# hard HTTP 413, verified). 2.6s between requests keeps us under 25/min with headroom.
OPENFIGI_BATCH = 10
OPENFIGI_MIN_INTERVAL_S = 2.6

# GLEIF publishes no rate limit and returns no rate-limit headers, so this is a
# self-imposed courtesy rather than a measured ceiling. One ISIN per request — the API has
# no batch form for an ISIN filter.
GLEIF_MIN_INTERVAL_S = 1.1

REQUEST_TIMEOUT_S = 20.0


def _user_agent() -> str:
    """
    Identify the client honestly, with a contact address when one is configured.

    The address comes from settings rather than a literal because this repository is
    public, and an operator's email does not belong in it.
    """
    contact = (settings.lookthrough_contact_email or "").strip()
    suffix = f" (+{contact})" if contact else ""
    return f"ibkr-portfolio-tracker/{settings.app_version}{suffix}"


class IdentityService:
    """Fetches and caches company identity for ISINs."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = IsinIdentityRepository(db)

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
        headers = {"Content-Type": "application/json", "User-Agent": _user_agent()}
        async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT_S) as client:
            for index in range(0, len(isins), OPENFIGI_BATCH):
                batch = isins[index:index + OPENFIGI_BATCH]
                if index:
                    await asyncio.sleep(OPENFIGI_MIN_INTERVAL_S)
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
            "User-Agent": _user_agent(),
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
