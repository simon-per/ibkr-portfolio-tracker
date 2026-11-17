"""
The Korean pass: OpenDART's periodic reports in, `company_financials` rows out
(docs/company-financials.md, *South Korea*). Run by the weekly scheduled job and by
`app/cli/fetch_company_markets.py`; both hold `DART_GATE` and record a `sync_runs` row.

**Scope**: every company the app names (`company_markets.named_companies`: the look-through's
heaviest 200 and the held listings) with a Korean listing's ISIN and no SEC page. The KRX
ISIN standard carries the issuer's five-digit code and a share-class digit (`KR7` + `00593` +
`0` common, `1` a preferred share + …), so every class of an issuer leads to its common
share's stock code (`005930`), which finds OpenDART's `corp_code` in `corpCode.xml` — asked at
most once a pass and only for a listing with no answer on record (`dart_corp_codes`). So
Samsung's preferred share (KR7005931001, traded as 005935) is the same company, 00126380.

**Reports**: every `fnlttSinglAcntAll` report (`dart_reports.REPORTS`) from `FIRST_YEAR`,
consolidated, **once per report**: what it held is kept (`company_reports`), and a report
that held nothing is asked again only while its period is recent (`NONE_FINAL_AFTER`), at most
weekly. The profile (`company.json`: KSIC industry code, fiscal year end, English name) at
most monthly. A company whose fiscal year does not end in December is refused whole.

**Bounded**: `REPORTS_PER_COMPANY` reads a company a pass, `PASS_REQUEST_CAP` requests a pass
— OpenDART allows 20,000 a day. OpenDART refusing the key, the IP or the day's budget stops the
pass (`OpenDartRefused`); a company whose read fails keeps its figures and resumes next pass.
**Without `OPENDART_API_KEY`** the pass makes no request and records nothing, as the Alpha
Vantage fill does without its key. Touches neither Yahoo nor IBKR nor EDGAR.
"""

import asyncio
import calendar
import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utc_today, utcnow
from app.config import settings
from app.models.company_financials import DART_SCHEME, Company
from app.repositories.company_financials_repository import (
    CompanyFinancialsRepository,
    facts_backoff_until,
)
from app.services.company_classification import ksic_name
from app.services.company_markets import named_companies, sec_isins
from app.services.dart_reports import (
    REPORTS,
    UnsupportedYear,
    convert,
    due,
    filed_on,
    period_end,
)
from app.services.opendart_client import (
    OpenDartClient,
    OpenDartError,
    OpenDartRefused,
)
from app.services.regulator_facts import normalise_reports

logger = logging.getLogger(__name__)

DART_GATE = "opendart"
SCHEDULED_SYNC_TYPE = "company_dart_refresh"
MANUAL_SYNC_TYPE = "manual_company_dart_fetch"
RESULT_KEY = "company_dart_result"

#: OpenDART's statements API begins with the 2015 reports.
FIRST_YEAR = 2015
#: A report is asked once its period ended this long ago: Korean filers have 45 days for a
#: quarter and 90 for the year; the quickest file within weeks.
DUE_AFTER_DAYS = 14
#: A report that held nothing is final once its period is this old; younger, it is asked
#: again at most every `NONE_RECHECK_AFTER` (not filed yet).
NONE_FINAL_AFTER = timedelta(days=400)
NONE_RECHECK_AFTER = timedelta(days=6)
PROFILE_RECHECK_AFTER = timedelta(days=30)
CORP_CODES_RECHECK_AFTER = timedelta(days=30)
#: Reads of one company in one pass; the first fill is about 45 (four a year since 2015).
REPORTS_PER_COMPANY = 60
#: Requests in one pass, far under OpenDART's 20,000 a day.
PASS_REQUEST_CAP = 2_000

READ, NONE = "read", "none"
NOT_CONFIGURED = "OPENDART_API_KEY is not set; the Korean pass makes no request."


def is_configured() -> bool:
    return bool((settings.opendart_api_key or "").strip())


def common_code(isin: str) -> Optional[str]:
    """
    The stock code of the common share of a Korean listing's issuer, from its ISIN (the KRX
    standard: `KR7`, the issuer's five digits, a share-class digit, …): KR7005930003 and the
    preferred KR7005931001 are both 005930. None for anything else (a code with letters, a
    bond, a foreign ISIN).
    """
    isin = (isin or "").strip().upper()
    if len(isin) == 12 and isin.startswith("KR7") and isin[3:9].isdigit():
        return isin[3:8] + "0"
    return None


def is_common(isin: str) -> bool:
    """The share-class digit of a KRX ISIN is 0 for the common share."""
    return len(isin) == 12 and isin[8] == "0"


def fiscal_year_end(month: str) -> Optional[str]:
    """OpenDART's fiscal year-end month (`acc_mt`, "12") as `MMDD` ("1231"), or None."""
    if not month.isdigit() or not 1 <= int(month) <= 12:
        return None
    m = int(month)
    return f"{m:02d}{calendar.monthrange(2001, m)[1]:02d}"


def report_id(year: int, report: str) -> str:
    return f"{year}-{report}"


class KoreaFinancialsSync:
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

    async def scope(self) -> Dict[str, str]:
        """ISIN -> its issuer's common stock code, for every named Korean listing with no
        SEC page."""
        answered = await sec_isins(self.db)
        out: Dict[str, str] = {}
        for company in await named_companies(self.db):
            if any(i in answered for i in company.isins):
                continue
            for isin in company.isins:
                code = common_code(isin)
                if code is not None:
                    out.setdefault(isin, code)
        return out

    async def refresh(
        self, *, force: bool = False, dry_run: bool = False
    ) -> Optional[Dict[str, Any]]:
        """One pass; its summary for a `sync_runs` row, or None without a key (nothing is
        recorded). `force` re-asks the reports that held nothing and the profiles, never a
        report already read; `dry_run` lists what is due and asks nothing."""
        if not is_configured():
            return None
        now, today = utcnow(), utc_today()
        codes = await self.scope()
        stored = await self.repo.dart_codes()
        need = sorted(
            isin
            for isin in codes
            if isin not in stored
            or (
                stored[isin].corp_code is None
                and (force or stored[isin].checked_at <= now - CORP_CODES_RECHECK_AFTER)
            )
        )
        summary: Dict[str, Any] = {
            "status": "success",
            "in_scope": len(codes),
            "corp_code_lookups": len(need),
            "companies": 0,
            "profiles_read": 0,
            "reports_due": 0,
            "reports_read": 0,
            "reports_empty": 0,
            "non_standard_lines": 0,
            "rows_written": 0,
            "refused_year": [],
            "errors": 0,
            "requests": 0,
            "stopped": None,
            "outcomes": [],
        }
        warnings: List[str] = []
        if dry_run:
            summary["message"] = (
                f"Korean companies: {len(codes)} listings in scope, {len(need)} stock codes "
                f"to look up (dry run, no request made)"
            )
            return summary

        async with OpenDartClient(
            settings.opendart_api_key.strip(),
            transport=self._transport,
            sleep=self._sleep,
        ) as client:
            try:
                if need:
                    listed = await client.corp_codes()
                    for isin in need:
                        found = listed.get(codes[isin])
                        await self.repo.record_dart_code(
                            isin, codes[isin], found.corp_code if found else None, now
                        )
                    await self.db.commit()
                    stored = await self.repo.dart_codes()
                companies = await self._companies(codes, stored, now)
                summary["companies"] = len(companies)
                for company in companies:
                    if client.requests >= PASS_REQUEST_CAP:
                        warnings.append(
                            f"The Korean pass stopped at {PASS_REQUEST_CAP} requests; the rest "
                            f"waits for the next one."
                        )
                        break
                    # A failed read before rolls back, expiring every row: load it again.
                    await self.db.refresh(company)
                    key = company.key
                    outcome = await self._one(
                        client, company, summary, now, today, force
                    )
                    summary["outcomes"].append({"key": key, "outcome": outcome})
            except OpenDartRefused as refused:
                summary["stopped"] = refused.status
                warnings.append(
                    f"OpenDART refused the pass (status {refused.status}); the rest waits for "
                    f"the next one. Check OPENDART_API_KEY and the day's budget."
                )
            except OpenDartError as e:
                summary["errors"] += 1
                warnings.append(f"OpenDART's company list could not be read ({e}).")
            summary["requests"] = client.requests

        if summary["errors"]:
            warnings.append(
                f"{summary['errors']} Korean compan{'y' if summary['errors'] == 1 else 'ies'} "
                f"could not be read from OpenDART and keep their previous figures."
            )
        for key in summary["refused_year"]:
            warnings.append(
                f"{key}: its fiscal year does not end in December; its reports are not read."
            )
        summary["message"] = (
            f"Korean companies: {summary['companies']} in scope, {summary['reports_read']} "
            f"reports read, {summary['rows_written']} rows, {summary['requests']} requests"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _companies(
        self, codes: Dict[str, str], stored: Dict[str, Any], now: datetime
    ) -> List[Company]:
        """One `companies` row per corp_code in scope, stamped as found by this pass; named by
        its common share's code and ISIN."""
        by_corp: Dict[str, List[Tuple[str, str]]] = {}
        for isin, code in codes.items():
            row = stored.get(isin)
            if row is not None and row.corp_code:
                by_corp.setdefault(row.corp_code, []).append((isin, code))
        out: List[Company] = []
        for corp, listings in sorted(by_corp.items()):
            listings.sort(key=lambda ic: (not is_common(ic[0]), ic[0]))
            isin, code = listings[0]
            company = await self.repo.ensure_other_company(
                DART_SCHEME, corp, ticker=code, isin=isin, country="KR"
            )
            company.universe_seen_at = now
            out.append(company)
        await self.db.commit()
        return out

    async def _one(
        self,
        client: OpenDartClient,
        company: Company,
        summary: Dict[str, Any],
        now: datetime,
        today: date,
        force: bool,
    ) -> str:
        """One company: its profile when due, every due report not read yet, then its rows
        rewritten from every report stored. Returns the outcome for the record."""
        try:
            if (
                force
                or company.profile_checked_at is None
                or (company.profile_checked_at <= now - PROFILE_RECHECK_AFTER)
            ):
                profile = await client.company(company.native_id)
                summary["profiles_read"] += 1
                if profile:
                    code = (profile.get("induty_code") or "").strip() or None
                    company.industry_code = code
                    company.industry = ksic_name(code) if code else None
                    month = (profile.get("acc_mt") or "").strip()
                    company.fiscal_year_end = fiscal_year_end(month)
                    name = (profile.get("corp_name_eng") or "").strip()
                    if name:
                        company.name = name
                company.profile_checked_at = now
                await self.db.commit()
            if (
                company.fiscal_year_end is not None
                and not company.fiscal_year_end.startswith("12")
            ):
                summary["refused_year"].append(company.key)
                return "refused: fiscal year"
            reports = await self.repo.company_reports(company.id)
            asked = 0
            read_any = False
            for year, report in self._due(reports, now, today, force):
                if asked >= REPORTS_PER_COMPANY or client.requests >= PASS_REQUEST_CAP:
                    break
                summary["reports_due"] += 1
                asked += 1
                rows = await client.statements(company.native_id, year, report)
                end = period_end(year, report)
                if not rows:
                    await self.repo.record_report(
                        company.id,
                        report_id(year, report),
                        status=NONE,
                        read_at=now,
                        period_end=end,
                    )
                    summary["reports_empty"] += 1
                else:
                    receipt = str(rows[0].get("rcept_no") or "")
                    converted = convert(rows, year, report, receipt)
                    await self.repo.record_report(
                        company.id,
                        report_id(year, report),
                        status=READ,
                        read_at=now,
                        period_end=end,
                        accn=receipt,
                        filed=filed_on(receipt),
                        facts=converted.facts,
                        tagged=converted.tagged,
                        non_standard=converted.non_standard,
                    )
                    summary["reports_read"] += 1
                    summary["non_standard_lines"] += converted.non_standard
                    read_any = True
                await self.db.commit()
        except UnsupportedYear:
            summary["refused_year"].append(company.key)
            return "refused: fiscal year"
        except OpenDartError as e:
            summary["errors"] += 1
            logger.warning("OpenDART read for %s failed: %s", company.key, e)
            await self.db.rollback()
            return "error"
        if read_any or force or company.fetched_at is None:
            return await self._write(company, summary, now)
        return "unchanged"

    @staticmethod
    def _due(reports: Dict[str, Any], now: datetime, today: date, force: bool):
        """(year, report) due an ask, newest first: its period ended `DUE_AFTER_DAYS` ago,
        never read; one that held nothing only while recent, at most weekly."""
        out = []
        for year in range(today.year, FIRST_YEAR - 1, -1):
            for report, _ in reversed(REPORTS):
                if not due(year, report, today, DUE_AFTER_DAYS):
                    continue
                stamp = reports.get(report_id(year, report))
                if stamp is not None and stamp.status == READ:
                    continue
                if stamp is not None and stamp.status == NONE and not force:
                    end = period_end(year, report)
                    if today - end > NONE_FINAL_AFTER:
                        continue
                    if stamp.read_at > now - NONE_RECHECK_AFTER:
                        continue
                out.append((year, report))
        return out

    async def _write(
        self, company: Company, summary: Dict[str, Any], now: datetime
    ) -> str:
        """The company's rows from every report stored, through the one normaliser; refused
        whole — nothing written — when they give no figure."""
        reports = await self.repo.company_reports(company.id)
        facts: List[Dict[str, Any]] = []
        tagged: Dict[str, str] = {}
        for report in sorted(reports.values(), key=lambda r: r.report_id):
            if report.status != READ:
                continue
            facts.extend(report.facts or [])
            for concept, name in (report.tagged or {}).items():
                tagged.setdefault(concept, name)
        result = normalise_reports(company.name, facts, DART_SCHEME, tagged)
        if not result.rows:
            if facts_backoff_until(company) is None:
                await self.repo.record_other_miss(company, now)
            await self.db.commit()
            return "no figures"
        await self.repo.replace_company_rows(company, result.rows, now)
        await self.db.commit()
        summary["rows_written"] += len(result.rows)
        return "written"
