"""
The European pass: ESEF annual reports from filings.xbrl.org in, `company_financials` rows out
(docs/company-financials.md, *Europe*). Run by the weekly scheduled job and by
`app/cli/fetch_company_markets.py`; both hold `ESEF_GATE` and record a `sync_runs` row.

**Scope**: every company the app names (`company_markets.named_companies`: the look-through's
heaviest 200 and the held listings) with a European ISIN (`region_taxonomy.region_of_isin`,
and the Crown Dependencies' JE/GG/IM, which file with the FCA), an LEI (the identity store's,
from GLEIF) and no SEC page. A company with no LEI cannot be asked and is counted.

**Filings**: the index's filings for the LEI, one per period end (the newest added, a
correction over the first), each read **once** (`company_reports`, by its `fxo_id`). The
list is asked until one pass has handled every filing in it, then once its newest year is due a
successor (`NEXT_DUE_AFTER` past its end), at most every `LIST_RECHECK_AFTER`. An LEI the
index holds no filing for backs off by the shared miss back-off (`miss_backoff`), and is not
listed on the tab until it has one (`universe_seen_at`). A filing with no xBRL-JSON (the
index failed to process it) is skipped and counted, asked again with the list; one larger
than the client reads (`esef_client.TooLarge`) is stamped `skipped`, never asked again, and
named in a warning.

**Bounded**: `FILINGS_PER_COMPANY` reads a company a pass, `PASS_REQUEST_CAP` requests a pass.
No key; touches neither Yahoo nor IBKR nor EDGAR.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utc_today, utcnow
from app.models.company_financials import LEI_SCHEME, Company
from app.repositories.company_financials_repository import (
    CompanyFinancialsRepository,
    facts_backoff_until,
)
from app.services.company_markets import NamedCompany, named_companies, sec_isins
from app.services.esef_client import EsefClient, EsefError, Filing, TooLarge
from app.services.esef_reports import convert
from app.services.region_taxonomy import EUROPE, region_of_isin
from app.services.regulator_facts import normalise_reports

logger = logging.getLogger(__name__)

ESEF_GATE = "esef"
SCHEDULED_SYNC_TYPE = "company_esef_refresh"
MANUAL_SYNC_TYPE = "manual_company_esef_fetch"
RESULT_KEY = "company_esef_result"
SOURCE = "esef"

#: Jersey, Guernsey and the Isle of Man: offshore to the region map, but their London-listed
#: companies file annual reports with the FCA.
CROWN_DEPENDENCIES = frozenset({"JE", "GG", "IM"})
#: A year's report is looked for once its period ended this long ago (ESEF issuers have four
#: months to publish; the index adds a filing within weeks of that).
NEXT_DUE_AFTER = timedelta(days=365 + 90)
LIST_RECHECK_AFTER = timedelta(days=6)
FILINGS_PER_COMPANY = 12
PASS_REQUEST_CAP = 1_000

READ = "read"
#: A filing never read (over the client's size cap): stamped so it is not asked again.
SKIPPED = "skipped"
DONE = (READ, SKIPPED)


def is_european(isin: str) -> bool:
    isin = (isin or "").strip().upper()
    return region_of_isin(isin) == EUROPE or isin[:2] in CROWN_DEPENDENCIES


def newest_per_period(filings: List[Filing]) -> Dict[date, Filing]:
    """One filing per period end: the newest added (then the highest package id), with an
    xBRL-JSON or not."""
    out: Dict[date, Filing] = {}
    for f in sorted(filings, key=lambda f: (f.added or date.min, f.fxo_id)):
        out[f.period_end] = f
    return out


class EuropeFinancialsSync:
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

    async def scope(self) -> List[NamedCompany]:
        """Every named European company with no SEC page (with an LEI or not)."""
        answered = await sec_isins(self.db)
        return [
            c
            for c in await named_companies(self.db)
            if not any(i in answered for i in c.isins)
            and any(is_european(i) for i in c.isins)
        ]

    async def refresh(
        self, *, force: bool = False, dry_run: bool = False
    ) -> Dict[str, Any]:
        """One pass; its summary for a `sync_runs` row. `force` asks every in-scope LEI's
        list now (never re-reads a filing already read); `dry_run` asks nothing."""
        now, today = utcnow(), utc_today()
        named = await self.scope()
        with_lei = [c for c in named if c.lei]
        summary: Dict[str, Any] = {
            "status": "success",
            "in_scope": len(named),
            "no_lei": len(named) - len(with_lei),
            "lists_asked": 0,
            "without_filings": 0,
            "companies": 0,
            "filings_read": 0,
            "filings_unprocessed": 0,
            "too_large": [],
            "non_standard_facts": 0,
            "rows_written": 0,
            "errors": 0,
            "requests": 0,
            "outcomes": [],
        }
        warnings: List[str] = []
        if dry_run:
            summary["message"] = (
                f"European companies: {len(named)} in scope, {len(with_lei)} with an LEI "
                f"(dry run, no request made)"
            )
            return summary

        async with EsefClient(transport=self._transport, sleep=self._sleep) as client:
            for named_company in with_lei:
                if client.requests >= PASS_REQUEST_CAP:
                    warnings.append(
                        f"The European pass stopped at {PASS_REQUEST_CAP} requests; the rest "
                        f"waits for the next one."
                    )
                    break
                company = await self._company(named_company)
                key = company.key  # a failed read rolls back, expiring the row
                outcome = await self._one(client, company, summary, now, today, force)
                summary["outcomes"].append({"key": key, "outcome": outcome})
            summary["requests"] = client.requests

        for fxo in summary["too_large"]:
            warnings.append(
                f"{fxo}: its xBRL-JSON is larger than the reader takes; that year stays blank."
            )
        if summary["errors"]:
            warnings.append(
                f"{summary['errors']} European compan"
                f"{'y' if summary['errors'] == 1 else 'ies'} could not be read from "
                f"filings.xbrl.org and keep their previous figures."
            )
        summary["message"] = (
            f"European companies: {summary['companies']} with filings of {len(with_lei)} "
            f"with an LEI, {summary['filings_read']} filings read, {summary['rows_written']} "
            f"rows, {summary['requests']} requests"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _company(self, named: NamedCompany) -> Company:
        isin = next(i for i in named.isins if is_european(i))
        company = await self.repo.ensure_other_company(
            LEI_SCHEME, named.lei, ticker=named.ticker, isin=isin, country=isin[:2]
        )
        if company.name is None:
            company.name = named.name
        await self.db.commit()
        return company

    @staticmethod
    def _list_due(
        company: Company,
        reports: Dict[str, Any],
        now: datetime,
        today: date,
        force: bool,
    ) -> bool:
        if force:
            return True
        if (
            facts_backoff_until(company) is not None
            and facts_backoff_until(company) > now
        ):
            return False
        checked = company.profile_checked_at
        if checked is None:
            # Never a list handled to its end (a pass stopped part-way): ask it again.
            return True
        if checked > now - LIST_RECHECK_AFTER:
            return False
        ends = [
            r.period_end for r in reports.values() if r.status in DONE and r.period_end
        ]
        return not ends or today >= max(ends) + NEXT_DUE_AFTER

    async def _one(
        self,
        client: EsefClient,
        company: Company,
        summary: Dict[str, Any],
        now: datetime,
        today: date,
        force: bool,
    ) -> str:
        """One company: its filings list when due, every filing not read yet, then its rows
        rewritten from every filing stored. Returns the outcome for the record."""
        reports = await self.repo.company_reports(company.id)
        read_any = False
        if company.universe_seen_at is not None:
            # A company the index answered for stays listed, whatever this pass asks.
            company.universe_seen_at = now
            await self.db.commit()
        try:
            if self._list_due(company, reports, now, today, force):
                summary["lists_asked"] += 1
                filings = newest_per_period(await client.filings(company.native_id))
                if not filings:
                    company.profile_checked_at = now
                    summary["without_filings"] += 1
                    await self.repo.record_other_miss(company, now)
                    await self.db.commit()
                    return "no filings"
                company.universe_seen_at = now
                newest = max(filings)
                company.fiscal_year_end = newest.strftime("%m%d")
                company.facts_checked_at = None
                company.facts_miss_count = None
                await self.db.commit()
                asked = 0
                for end, filing in sorted(filings.items(), reverse=True):
                    stamp = reports.get(filing.fxo_id)
                    if stamp is not None and stamp.status in DONE:
                        continue
                    if filing.json_url is None:
                        summary["filings_unprocessed"] += 1
                        continue
                    if (
                        asked >= FILINGS_PER_COMPANY
                        or client.requests >= PASS_REQUEST_CAP
                    ):
                        break
                    asked += 1
                    try:
                        document = await client.report(filing.json_url)
                    except TooLarge:
                        await self.repo.record_report(
                            company.id,
                            filing.fxo_id,
                            status=SKIPPED,
                            read_at=now,
                            period_end=end,
                            accn=filing.fxo_id,
                            filed=filing.added,
                        )
                        await self.db.commit()
                        summary["too_large"].append(filing.fxo_id)
                        continue
                    converted = convert(
                        document, company.native_id, filing.fxo_id, filing.added
                    )
                    await self.repo.record_report(
                        company.id,
                        filing.fxo_id,
                        status=READ,
                        read_at=now,
                        period_end=end,
                        accn=filing.fxo_id,
                        filed=filing.added,
                        facts=converted.facts,
                        tagged=converted.tagged,
                        non_standard=converted.non_standard,
                    )
                    await self.db.commit()
                    summary["filings_read"] += 1
                    summary["non_standard_facts"] += converted.non_standard
                    read_any = True
                else:
                    # Every filing of the list handled: the list is not asked again until
                    # `_list_due` says so.
                    company.profile_checked_at = now
                    await self.db.commit()
        except EsefError as e:
            summary["errors"] += 1
            logger.warning("filings.xbrl.org read for %s failed: %s", company.key, e)
            await self.db.rollback()
            return "error"
        if company.universe_seen_at == now:
            summary["companies"] += 1
        if (
            read_any
            or force
            or (company.fetched_at is None and company.universe_seen_at)
        ):
            return await self._write(company, summary, now)
        return "unchanged" if company.universe_seen_at else "backing off"

    async def _write(
        self, company: Company, summary: Dict[str, Any], now: datetime
    ) -> str:
        """The company's rows from its newest filing of each period, through the one
        normaliser; refused whole — nothing written — when they give no figure."""
        reports = await self.repo.company_reports(company.id)
        newest: Dict[date, Any] = {}
        for report in sorted(
            (r for r in reports.values() if r.status == READ and r.period_end),
            key=lambda r: (r.filed or date.min, r.report_id),
        ):
            newest[report.period_end] = report
        facts: List[Dict[str, Any]] = []
        tagged: Dict[str, str] = {}
        for report in newest.values():
            facts.extend(report.facts or [])
            for concept, name in (report.tagged or {}).items():
                tagged.setdefault(concept, name)
        result = normalise_reports(company.name, facts, SOURCE, tagged)
        if not result.rows:
            await self.db.commit()
            return "no figures"
        await self.repo.replace_company_rows(company, result.rows, now)
        await self.db.commit()
        summary["rows_written"] += len(result.rows)
        return "written"
