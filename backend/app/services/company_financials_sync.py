"""
The EDGAR pass: held SEC filers' companyfacts in, `company_financials` rows out
(docs/company-financials.md). Run by the weekly scheduled job and by
`app/cli/fetch_company_financials.py`; both hold `EDGAR_GATE` and record a `sync_runs` row.

**Scope** (`in_scope`): held securities (`security_repository.held`) listed on a US
exchange — the exchanges the price path maps to a bare ticker — that are not a known fund.
Their ticker, spelled the SEC's way, finds the CIK; a non-US listing, or a ticker the SEC
does not list, never reaches companyfacts. A foreign filer (ASML's and NU's 20-F) has a CIK
but files IFRS, so its document yields no row: a miss, backed off like any other.
**One company, one request**: listings sharing a CIK (GOOG, GOOGL) are fetched once and
the rows written under each.

**Bounded** (`STALE_AFTER`, `CIK_RECHECK_AFTER`, `miss_backoff`): a company is fetched at
most once in `STALE_AFTER`; a ticker the SEC does not list is looked up again after
`CIK_RECHECK_AFTER`; a CIK whose document has nothing this app reads (a 404, a 20-F) is
asked again on the shared doubling schedule. A failed request (transport, 429, 5xx after
the client's retries) stamps nothing and is asked next pass; a 401/403 stops the pass.

**Without `EDGAR_USER_AGENT`** the pass makes no request and says so in its warnings.
Touches neither Yahoo nor IBKR.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.etf_mappings import is_known_etf_isin
from app.models.security import Security
from app.repositories.company_financials_repository import (
    CompanyFinancialsRepository,
    facts_backoff_until,
)
from app.repositories.security_repository import held
from app.services.company_financials_normaliser import normalise
from app.services.edgar_client import (
    EdgarClient,
    EdgarError,
    EdgarRefused,
    is_configured,
    sec_ticker,
)
from app.services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)

#: The gate both entry points hold. Not `SYNC_PIPELINE`: EDGAR shares no upstream with
#: Yahoo or IBKR, and entering that gate would put the stock Sync buttons on cooldown.
EDGAR_GATE = "edgar"
SCHEDULED_SYNC_TYPE = "company_financials_refresh"
MANUAL_SYNC_TYPE = "manual_company_financials_fetch"

#: Under a week, so the weekly pass refetches every company it fetched the week before.
STALE_AFTER = timedelta(days=6)
CIK_RECHECK_AFTER = timedelta(days=30)

NOT_CONFIGURED_WARNING = (
    "EDGAR_USER_AGENT is not set; company financials were not refreshed."
)


def us_exchanges() -> frozenset:
    """The exchanges whose listings the price path quotes under the bare ticker."""
    return frozenset(
        exchange
        for exchange, suffix in MarketDataService.EXCHANGE_SUFFIXES.items()
        if suffix == ""
    )


class CompanyFinancialsSync:
    def __init__(
        self,
        db: AsyncSession,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.db = db
        self.repo = CompanyFinancialsRepository(db)
        self._transport = transport
        self._sleep = sleep

    async def in_scope(self, symbols: Optional[Sequence[str]] = None) -> List[Security]:
        """Held, US-listed, not a known fund; optionally only these symbols."""
        result = await self.db.execute(
            select(Security)
            .where(held(), Security.exchange.in_(sorted(us_exchanges())))
            .order_by(Security.symbol, Security.id)
        )
        wanted = {s.upper() for s in symbols} if symbols else None
        return [
            s
            for s in result.scalars()
            if not is_known_etf_isin(s.isin)
            and (wanted is None or s.symbol.upper() in wanted)
        ]

    async def refresh(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        symbols: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """
        One pass. Returns a summary for a `sync_runs` row; each company is committed as
        it is written, so a crash mid-pass keeps what was already fetched. `force` asks
        again regardless of staleness and back-off; `dry_run` lists what is due and makes
        no request and no write.
        """
        configured = is_configured()
        if not configured and not dry_run:
            return {
                "status": "skipped",
                "message": "Company financials: skipped, EDGAR_USER_AGENT is not set",
                "warnings": [NOT_CONFIGURED_WARNING],
            }

        now = utcnow()
        securities = await self.in_scope(symbols)
        stamps = await self.repo.cik_rows(s.id for s in securities)
        need_cik = [
            s
            for s in securities
            if s.id not in stamps
            or (
                stamps[s.id].cik is None
                and (force or stamps[s.id].cik_checked_at <= now - CIK_RECHECK_AFTER)
            )
        ]
        summary: Dict[str, Any] = {
            "status": "success",
            "configured": configured,
            "in_scope": len(securities),
            "cik_lookups": len(need_cik),
            "companies_due": 0,
            "fetched": 0,
            "rows_written": 0,
            "no_data": 0,
            "errors": 0,
            "stopped": None,
            "outcomes": [],
        }
        warnings: List[str] = []

        if dry_run:
            due = self._due(securities, stamps, now, force)
            summary["companies_due"] = len(due)
            summary["outcomes"] = [
                {"cik": cik, "symbols": [s.symbol for s in members], "outcome": "due"}
                for cik, members in due.items()
            ]
            summary["message"] = (
                f"Company financials: {len(due)} companies due, {len(need_cik)} CIK "
                f"lookups (dry run, no request made)"
            )
            if not configured:
                summary["warnings"] = [NOT_CONFIGURED_WARNING]
            return summary

        async with EdgarClient(
            settings.edgar_user_agent.strip(),
            transport=self._transport,
            sleep=self._sleep,
        ) as client:
            try:
                if need_cik:
                    try:
                        await self._resolve_ciks(client, need_cik, now)
                    except EdgarError as e:
                        summary["errors"] += 1
                        warnings.append(
                            f"The SEC ticker list could not be read ({e}); "
                            f"{len(need_cik)} securit{'y' if len(need_cik) == 1 else 'ies'} "
                            f"without a CIK wait for the next pass."
                        )
                    stamps = await self.repo.cik_rows(s.id for s in securities)
                due = self._due(securities, stamps, now, force)
                summary["companies_due"] = len(due)
                for cik, members in due.items():
                    outcome = await self._one(client, cik, members, summary)
                    summary["outcomes"].append(
                        {
                            "cik": cik,
                            "symbols": [s.symbol for s in members],
                            "outcome": outcome,
                        }
                    )
            except EdgarRefused as refused:
                summary["stopped"] = refused.status
                hint = "" if refused.status == 429 else " Check EDGAR_USER_AGENT."
                warnings.append(
                    f"The SEC answered HTTP {refused.status}; the company financials "
                    f"pass stopped and the rest waits for the next one.{hint}"
                )

        failed = summary["errors"]
        n = sum(1 for o in summary["outcomes"] if o["outcome"].startswith("error"))
        if n:
            warnings.append(
                f"{n} compan{'y' if n == 1 else 'ies'} could not be fetched from EDGAR "
                f"and keep their previous figures."
            )
        summary["message"] = (
            f"Company financials: {summary['fetched']} of {summary['companies_due']} "
            f"companies fetched, {summary['rows_written']} rows, "
            f"{summary['no_data']} without 10-Q/10-K figures, {failed} failed"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _resolve_ciks(
        self, client: EdgarClient, securities: List[Security], now: datetime
    ) -> None:
        tickers = await client.company_tickers()
        for security in securities:
            ticker = sec_ticker(security.symbol)
            await self.repo.record_cik(security.id, tickers.get(ticker), ticker, now)
        await self.db.commit()

    def _due(
        self, securities, stamps, now: datetime, force: bool
    ) -> Dict[int, List[Security]]:
        """CIK -> the held listings to write, for every company due a fetch."""
        by_cik: Dict[int, List[Security]] = defaultdict(list)
        for security in securities:
            row = stamps.get(security.id)
            if row is not None and row.cik is not None:
                by_cik[row.cik].append(security)
        due: Dict[int, List[Security]] = {}
        for cik, members in by_cik.items():
            rows = [stamps[s.id] for s in members]
            if not force:
                waits = [facts_backoff_until(r) for r in rows]
                if any(w is not None and w > now for w in waits):
                    continue
                if all(
                    r.fetched_at is not None and r.fetched_at > now - STALE_AFTER
                    for r in rows
                ):
                    continue
            due[cik] = members
        return due

    async def _one(
        self,
        client: EdgarClient,
        cik: int,
        members: List[Security],
        summary: Dict[str, Any],
    ) -> str:
        ids = [s.id for s in members]
        try:
            facts = await client.company_facts(cik)
        except EdgarError as e:
            summary["errors"] += 1
            logger.warning(f"companyfacts for CIK {cik} failed: {e}")
            return f"error: {e}"
        result = normalise(facts) if facts is not None else None
        if result is None or not result.has_data:
            await self.repo.record_miss(ids, utcnow())
            await self.db.commit()
            summary["no_data"] += 1
            return "no 10-Q/10-K figures" if facts is not None else "not found"
        fetched_at = utcnow()
        for security_id in ids:
            await self.repo.replace_rows(security_id, result.rows, fetched_at)
        await self.db.commit()
        summary["fetched"] += 1
        summary["rows_written"] += len(result.rows) * len(ids)
        return f"{len(result.rows)} rows"
