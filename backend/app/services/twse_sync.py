"""
The Taiwan pass: each Taiwan-listed company's annual report from MOPS (the TWSE's Market
Observation Post System) in, `company_financials` rows out (docs/company-financials.md,
*Taiwan*). Run by the weekly scheduled job beside the Korean and European passes, and by
`app/cli/fetch_company_markets.py --source twse`; both hold `TWSE_GATE` and record a
`sync_runs` row.

**Scope**: every company the app names (`company_markets.named_companies`) with a Taiwan
ISIN (`TW000<code>…`) and no page from another source (TSMC has its SEC page through its
20-F). Hon Hai and MediaTek, 2026-11-19.

**Reports**: MOPS serves each listed company's quarterly report as Inline XBRL
(`tifrs-<form>-<code>-<year>Q<n>.html`, since 2019; read by `ixbrl.parse` into the shape the
ESEF converter reads, `esef_reports.convert`). The fourth quarter's is the annual report:
its fiscal-year figures and the year before as comparatives. Each is read **once**
(`company_reports`, by `<year>Q4`), from `FIRST_YEAR` to the newest year due
(`DUE_AFTER_YEAR_END` past its end). The report's form (`ci` for a general company, a bank's
or an insurer's otherwise, `m1`/`m2` by its statement layout) is not known in advance:
`FORMS` are tried in turn and the one found kept for the company's next years. A year none
answers is stamped `none` once the next year's is out, so it is not asked again; the newest
is asked again next week.

**Bounded**: `PASS_REQUEST_CAP` requests a pass, paced `REQUEST_INTERVAL_S` apart; no key;
touches neither Yahoo, IBKR nor EDGAR.
"""

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utc_today, utcnow
from app.models.company_financials import TWSE_SCHEME, VENDOR_SCHEME, Company
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.services.company_markets import NamedCompany, named_companies, sec_isins
from app.services.esef_reports import convert
from app.services.ixbrl import parse
from app.services.regulator_facts import normalise_reports

logger = logging.getLogger(__name__)

TWSE_GATE = "twse"
SCHEDULED_SYNC_TYPE = "company_twse_refresh"
MANUAL_SYNC_TYPE = "manual_company_twse_fetch"
RESULT_KEY = "company_twse_result"
SOURCE = TWSE_SCHEME

URL = (
    "https://mopsov.twse.com.tw/server-java/FileDownLoad?step=9"
    "&fileName=tifrs-{form}-{code}-{year}Q4.html&filePath=/home/html/nas/ifrs/{year}/"
)
#: The first year MOPS serves as Inline XBRL (2019; earlier years are XBRL instances in
#: another layout). Its report carries 2018 as comparatives.
FIRST_YEAR = 2019
#: A year's annual report is due by 31 March; looked for from April.
DUE_AFTER_YEAR_END = timedelta(days=91)
#: The report forms tried, most common first: a consolidated (`cr`) report of a general
#: company (`ci`), a bank (`bd`), a financial holding (`fh`), an insurer (`ins`), each in
#: either statement layout (`m1`, `m2`).
FORMS = tuple(
    f"fr1-{layout}-{industry}-cr"
    for industry in ("ci", "fh", "bd", "ins")
    for layout in ("m1", "m2")
)
READ, NONE = "read", "none"
PASS_REQUEST_CAP = 150
REQUEST_INTERVAL_S = 1.5
#: A Taiwan ISIN carries the listing's four-digit stock code: TW0002317005 is 2317.
TAIWAN_ISIN = re.compile(r"^TW000(\d{4})\d{3}$")


def stock_code(isin: str) -> Optional[str]:
    match = TAIWAN_ISIN.match((isin or "").strip().upper())
    return match.group(1) if match else None


def is_report(body: bytes) -> bool:
    """Whether MOPS answered with a report: an XHTML document with Inline XBRL in it. A
    missing file is a short HTML page saying so."""
    head = body[:4000]
    return head.lstrip().startswith(b"<?xml") and b"inlineXBRL" in head


class TaiwanFinancialsSync:
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
        self.requests = 0

    async def scope(self) -> List[NamedCompany]:
        """Every named Taiwan company with no page from another source."""
        answered = await sec_isins(self.db)
        others = {
            isin
            for scheme_company in await self.repo.other_companies()
            # A vendor page yields to a regulator's (`vendor_sync`).
            if scheme_company.scheme not in (TWSE_SCHEME, VENDOR_SCHEME)
            and scheme_company.isin
            for isin in [scheme_company.isin.strip().upper()]
        }
        return [
            c
            for c in await named_companies(self.db)
            if any(stock_code(i) for i in c.isins)
            and not any(i in answered or i in others for i in c.isins)
        ]

    async def refresh(
        self, *, force: bool = False, dry_run: bool = False
    ) -> Dict[str, Any]:
        """One pass; its summary for a `sync_runs` row. `force` rewrites every company's rows
        from its stored reports (never re-reads one); `dry_run` asks nothing."""
        now, today = utcnow(), utc_today()
        named = await self.scope()
        summary: Dict[str, Any] = {
            "status": "success",
            "in_scope": len(named),
            "companies": 0,
            "reports_read": 0,
            "reports_missing": 0,
            "rows_written": 0,
            "errors": 0,
            "requests": 0,
            "outcomes": [],
        }
        warnings: List[str] = []
        newest_due = (today - DUE_AFTER_YEAR_END).year - 1
        if dry_run:
            summary["message"] = (
                f"Taiwan companies: {len(named)} in scope (dry run, no request made)"
            )
            return summary
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(60.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (portfolio tracker)"},
        ) as client:
            for named_company in named:
                if self.requests >= PASS_REQUEST_CAP:
                    warnings.append(
                        f"The Taiwan pass stopped at {PASS_REQUEST_CAP} requests; the rest "
                        f"waits for the next one."
                    )
                    break
                company = await self._company(named_company)
                key = company.key
                try:
                    outcome = await self._one(
                        client, company, summary, now, newest_due, force
                    )
                except httpx.HTTPError as e:
                    summary["errors"] += 1
                    logger.warning("MOPS read for %s failed: %s", key, e)
                    await self.db.rollback()
                    outcome = "error"
                summary["outcomes"].append({"key": key, "outcome": outcome})
        summary["requests"] = self.requests
        if summary["errors"]:
            warnings.append(
                f"{summary['errors']} Taiwan compan"
                f"{'y' if summary['errors'] == 1 else 'ies'} could not be read from MOPS "
                f"and keep their previous figures."
            )
        summary["message"] = (
            f"Taiwan companies: {summary['companies']} with reports of {len(named)}, "
            f"{summary['reports_read']} reports read, {summary['rows_written']} rows, "
            f"{summary['requests']} requests"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _company(self, named: NamedCompany) -> Company:
        isin = next(i for i in named.isins if stock_code(i))
        company = await self.repo.ensure_other_company(
            TWSE_SCHEME,
            stock_code(isin),
            ticker=stock_code(isin),
            isin=isin,
            country="TW",
        )
        company.name = named.name or company.name
        await self.db.commit()
        return company

    async def _get(self, client: httpx.AsyncClient, url: str) -> bytes:
        if self.requests:
            await self._sleep(REQUEST_INTERVAL_S)
        self.requests += 1
        response = await client.get(url)
        response.raise_for_status()
        return response.content

    async def _one(
        self,
        client: httpx.AsyncClient,
        company: Company,
        summary: Dict[str, Any],
        now: datetime,
        newest_due: int,
        force: bool,
    ) -> str:
        if company.universe_seen_at is not None:
            # A company MOPS answered for stays listed, whatever this pass reads.
            company.universe_seen_at = now
            await self.db.commit()
        reports = await self.repo.company_reports(company.id)
        known = [r.accn for r in reports.values() if r.status == READ and r.accn]
        forms = list(dict.fromkeys(known[-1:] + list(FORMS)))
        read_any = False
        for year in range(newest_due, FIRST_YEAR - 1, -1):
            report_id = f"{year}Q4"
            if report_id in reports:
                continue
            if self.requests >= PASS_REQUEST_CAP:
                break
            found = None
            for form in forms:
                body = await self._get(
                    client, URL.format(form=form, code=company.native_id, year=year)
                )
                if is_report(body):
                    found = (form, body)
                    break
            filed = date(year + 1, 3, 31)
            if found is None:
                summary["reports_missing"] += 1
                if year < newest_due:
                    # The next year's is out, so this one never will be: not asked again.
                    await self.repo.record_report(
                        company.id,
                        report_id,
                        status=NONE,
                        read_at=now,
                        period_end=date(year, 12, 31),
                    )
                    await self.db.commit()
                continue
            form, body = found
            forms = [form] + [f for f in forms if f != form]
            converted = convert(parse(body), company.native_id, report_id, filed)
            await self.repo.record_report(
                company.id,
                report_id,
                status=READ,
                read_at=now,
                period_end=date(year, 12, 31),
                accn=form,
                filed=filed,
                facts=converted.facts,
                tagged=converted.tagged,
                non_standard=converted.non_standard,
            )
            await self.db.commit()
            summary["reports_read"] += 1
            read_any = True
        if read_any or force or (company.fetched_at is None and reports):
            return await self._write(company, summary, now)
        return "unchanged"

    async def _write(
        self, company: Company, summary: Dict[str, Any], now: datetime
    ) -> str:
        """The company's rows from its reports, newest filing winning, through the one
        normaliser; refused whole — nothing written — when they give no figure."""
        reports = await self.repo.company_reports(company.id)
        facts: List[Dict[str, Any]] = []
        tagged: Dict[str, str] = {}
        for report in sorted(
            (r for r in reports.values() if r.status == READ),
            key=lambda r: (r.filed or date.min, r.report_id),
        ):
            facts.extend(report.facts or [])
            for concept, name in (report.tagged or {}).items():
                tagged.setdefault(concept, name)
        result = normalise_reports(company.name, facts, SOURCE, tagged)
        if not result.rows:
            await self.db.commit()
            return "no figures"
        company.universe_seen_at = now
        await self.repo.replace_company_rows(company, result.rows, now)
        await self.db.commit()
        summary["companies"] += 1
        summary["rows_written"] += len(result.rows)
        return "written"
