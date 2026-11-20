"""
The Japanese pass: each Japanese company's annual securities report (有価証券報告書) from
EDINET in, `company_financials` rows out (docs/company-financials.md, *Japan*). Run by the
weekly markets job beside the other regulators and by `app/cli/fetch_company_markets.py
--source edinet`; both hold `EDINET_GATE` and record a `sync_runs` row. **Needs
`EDINET_API_KEY`** (the FSA's free subscription key); without one it makes no request.

**Scope**: every company the app names with a Japanese ISIN and no page from another source
(Toyota and MUFG have their SEC 20-F pages): Hitachi, Fujitsu, NEC, Tokyo Electron
(2026-11-19). Its EDINET `secCode` is the listing's four-digit code and a check digit `0`
(the baskets' ticker: Hitachi's 6501 is `65010`).

**Finding a report**: EDINET lists filings by day only (`documents.json?date=`), so the pass
reads day lists, each once a pass and shared by every company: from today back
`FIRST_LOOKBACK_DAYS` for a company with no report yet, around the anniversary
`HISTORY_YEARS` before its newest report for its history, and once its next report is due
(`NEXT_DUE_AFTER` past the newest filing) the days since it last looked
(`profile_checked_at`). An annual securities report is `docTypeCode` 120.

**Reading** (`summary_facts`): the report's XBRL instance (`type=1`, a zip) holds the
**summary of business results** (`…SummaryOfBusinessResults`, `jpcrp_cor`): five fiscal
years of revenue, profit attributable to owners, diluted EPS, operating cash flow, cash and
equity, in the company's standard's spelling (J-GAAP, `…IFRS…`, `…USGAAP…`), and the dividend
per share on its non-consolidated context. Each becomes its IFRS concept (`SUMMARY`), so the
one normaliser reads them as it reads an ESEF report; operating income comes from the
statements (`OPERATING`), the current and prior year. Consolidated contexts only. Two
reports, five years apart, give ten years.

**Bounded**: `PASS_REQUEST_CAP` requests a pass, `REQUEST_INTERVAL_S` apart.
"""

import asyncio
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utc_today, utcnow
from app.config import settings
from app.models.company_financials import EDINET_SCHEME, VENDOR_SCHEME, Company
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.services.company_markets import NamedCompany, named_companies, sec_isins
from app.services.company_financials_normaliser import REGULATOR_ANNUAL
from app.services.regulator_facts import normalise_reports, one_value_each

logger = logging.getLogger(__name__)

EDINET_GATE = "edinet"
SCHEDULED_SYNC_TYPE = "company_edinet_refresh"
MANUAL_SYNC_TYPE = "manual_company_edinet_fetch"
RESULT_KEY = "company_edinet_result"
SOURCE = EDINET_SCHEME
NOT_CONFIGURED = (
    "EDINET_API_KEY is not set; Japanese company financials were not refreshed."
)

BASE = "https://api.edinet-fsa.go.jp/api/v2"
ANNUAL_REPORT = "120"
FIRST_LOOKBACK_DAYS = 400
HISTORY_YEARS = 5
HISTORY_WINDOW_DAYS = 12
NEXT_DUE_AFTER = timedelta(days=330)
PASS_REQUEST_CAP = 700
REQUEST_INTERVAL_S = 0.5
READ, NONE = "read", "none"
#: A Japanese ISIN: `JP3…`.
JAPAN_ISIN = re.compile(r"^JP3[0-9A-Z]{8}\d$")
FY_DAYS = (350, 380)

#: The summary of business results' elements (`jpcrp_cor`), by standard, as the IFRS concept
#: the normaliser reads (`IFRS_TWINS`), and the kind of value.
SUMMARY: Dict[str, Tuple[str, str]] = {
    "NetSalesSummaryOfBusinessResults": ("Revenue", "money"),
    "RevenueIFRSSummaryOfBusinessResults": ("Revenue", "money"),
    "RevenuesUSGAAPSummaryOfBusinessResults": ("Revenue", "money"),
    "ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults": (
        "ProfitLossAttributableToOwnersOfParent",
        "money",
    ),
    "ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults": (
        "ProfitLossAttributableToOwnersOfParent",
        "money",
    ),
    "NetIncomeLossAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults": (
        "ProfitLossAttributableToOwnersOfParent",
        "money",
    ),
    "DilutedEarningsPerShareSummaryOfBusinessResults": (
        "DilutedEarningsLossPerShare",
        "per_share",
    ),
    "DilutedEarningsLossPerShareIFRSSummaryOfBusinessResults": (
        "DilutedEarningsLossPerShare",
        "per_share",
    ),
    "DilutedEarningsLossPerShareUSGAAPSummaryOfBusinessResults": (
        "DilutedEarningsLossPerShare",
        "per_share",
    ),
    "NetCashProvidedByUsedInOperatingActivitiesSummaryOfBusinessResults": (
        "CashFlowsFromUsedInOperatingActivities",
        "money",
    ),
    "CashFlowsFromUsedInOperatingActivitiesIFRSSummaryOfBusinessResults": (
        "CashFlowsFromUsedInOperatingActivities",
        "money",
    ),
    "CashFlowsFromUsedInOperatingActivitiesUSGAAPSummaryOfBusinessResults": (
        "CashFlowsFromUsedInOperatingActivities",
        "money",
    ),
    "CashAndCashEquivalentsSummaryOfBusinessResults": (
        "CashAndCashEquivalents",
        "money",
    ),
    "CashAndCashEquivalentsIFRSSummaryOfBusinessResults": (
        "CashAndCashEquivalents",
        "money",
    ),
    "CashAndCashEquivalentsUSGAAPSummaryOfBusinessResults": (
        "CashAndCashEquivalents",
        "money",
    ),
    "EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults": (
        "EquityAttributableToOwnersOfParent",
        "money",
    ),
    "EquityAttributableToOwnersOfParentUSGAAPSummaryOfBusinessResults": (
        "EquityAttributableToOwnersOfParent",
        "money",
    ),
    # J-GAAP's net assets include the non-controlling interests: the total equity line.
    "NetAssetsSummaryOfBusinessResults": ("Equity", "money"),
    # Declared for the fiscal year, stated on the non-consolidated context.
    "DividendPaidPerShareSummaryOfBusinessResults": (
        "DividendsRecognisedAsDistributionsToOwnersPerShare",
        "per_share",
    ),
}
#: Operating income from the statements (`jppfs_cor`, `jpigp_cor`): the summary has none.
OPERATING: Dict[str, Tuple[str, str]] = {
    "OperatingIncome": ("ProfitLossFromOperatingActivities", "money"),
    "OperatingProfitLossIFRS": ("ProfitLossFromOperatingActivities", "money"),
}
EDINET_NS = re.compile(
    r"^http://disclosure\.edinet-fsa\.go\.jp/taxonomy/(jpcrp|jppfs|jpigp)/"
)
XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
NON_CONSOLIDATED = "NonConsolidatedMember"


def is_configured() -> bool:
    return bool((settings.edinet_api_key or "").strip())


def sec_code(named: NamedCompany) -> Optional[str]:
    """EDINET's five-digit `secCode` of a Japanese listing (its four-digit code and `0`)."""
    ticker = (named.ticker or "").strip().upper()
    if re.fullmatch(r"\d{3}[0-9A-Z]", ticker) and any(
        JAPAN_ISIN.match(i) for i in named.isins
    ):
        return ticker + "0"
    return None


def summary_facts(
    instance: bytes, accn: str, filed: date
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """
    The report's summary of business results (and operating income) as facts in the shape
    `regulator_facts` reads: `{concept, unit, start, end, val, accn, filed, form}`, and the
    report's element per IFRS concept (`tagged`). Consolidated contexts only, but the
    dividend per share, which the summary states on the non-consolidated one.
    """
    contexts: Dict[str, Tuple[Optional[str], Optional[str], List[str]]] = {}
    units: Dict[str, str] = {}
    raw = []
    depth = 0
    for event, elem in ET.iterparse(io.BytesIO(instance), events=("start", "end")):
        if event == "start":
            depth += 1
            continue
        depth -= 1
        if depth != 1:
            # A context's period or member: read with the context it belongs to.
            continue
        tag = elem.tag
        if tag == XBRLI + "context":
            period = elem.find(XBRLI + "period")
            start = end = None
            if period is not None:
                start = (period.findtext(XBRLI + "startDate") or "").strip() or None
                end = (
                    (period.findtext(XBRLI + "endDate") or "").strip()
                    or (period.findtext(XBRLI + "instant") or "").strip()
                    or None
                )
            members = [
                (m.text or "").rsplit(":", 1)[-1]
                for m in elem.iter(XBRLDI + "explicitMember")
            ]
            typed = any(True for _ in elem.iter(XBRLDI + "typedMember"))
            contexts[elem.get("id") or ""] = (
                start,
                end,
                members + (["typed"] if typed else []),
            )
        elif tag == XBRLI + "unit":
            measures = [
                (m.text or "").rsplit(":", 1)[-1] for m in elem.iter(XBRLI + "measure")
            ]
            units[elem.get("id") or ""] = "/".join(measures)
        elif tag.startswith("{"):
            uri, _, name = tag[1:].partition("}")
            if EDINET_NS.match(uri) and (name in SUMMARY or name in OPERATING):
                if elem.get(XSI_NIL) != "true" and elem.text and elem.text.strip():
                    raw.append(
                        (
                            name,
                            elem.get("contextRef") or "",
                            elem.get("unitRef") or "",
                            elem.text.strip(),
                        )
                    )
        elem.clear()
    facts: List[Dict[str, Any]] = []
    tagged: Dict[str, str] = {}
    for name, ctx, unit_ref, text in raw:
        concept, kind = SUMMARY.get(name) or OPERATING[name]
        context = contexts.get(ctx)
        if context is None:
            continue
        start, end, members = context
        dividend = concept == "DividendsRecognisedAsDistributionsToOwnersPerShare"
        if members and not (dividend and members == [NON_CONSOLIDATED]):
            continue
        try:
            value = float(text)
            end_day = date.fromisoformat(end or "")
            start_day = date.fromisoformat(start) if start else None
        except ValueError:
            continue
        unit = units.get(unit_ref, "")
        currency = unit.split("/")[0]
        if not re.fullmatch(r"[A-Z]{3}", currency):
            continue
        if kind == "per_share":
            if not unit.endswith("/shares") and not unit.endswith("Shares"):
                continue
            unit_out = f"{currency}/shares"
        else:
            if "/" in unit:
                continue
            unit_out = currency
        if concept in (
            "CashAndCashEquivalents",
            "EquityAttributableToOwnersOfParent",
            "Equity",
        ):
            if start_day is not None:
                continue
        else:
            if (
                start_day is None
                or not FY_DAYS[0] <= (end_day - start_day).days + 1 <= FY_DAYS[1]
            ):
                continue
        facts.append(
            {
                "concept": concept,
                "unit": unit_out,
                "start": start_day.isoformat() if start_day else None,
                "end": end_day.isoformat(),
                "val": value,
                "accn": accn,
                "filed": filed.isoformat(),
                "form": REGULATOR_ANNUAL,
            }
        )
        tagged.setdefault(concept, f"edinet:{name}")
    return one_value_each(facts), tagged


def instance_of(zipped: bytes) -> Optional[bytes]:
    """The report's XBRL instance from EDINET's zip (`XBRL/PublicDoc/*.xbrl`), or None."""
    try:
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            names = [
                n
                for n in archive.namelist()
                if n.startswith("XBRL/PublicDoc/") and n.endswith(".xbrl")
            ]
            return archive.read(names[0]) if names else None
    except zipfile.BadZipFile:
        return None


class JapanFinancialsSync:
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
        self._days: Dict[date, List[Dict[str, Any]]] = {}

    async def scope(self) -> List[Tuple[NamedCompany, str]]:
        """Every named Japanese company with no page from another source, and its secCode."""
        answered = await sec_isins(self.db)
        others = {
            (c.isin or "").strip().upper()
            for c in await self.repo.other_companies()
            # A vendor page yields to a regulator's (`vendor_sync`).
            if c.scheme not in (EDINET_SCHEME, VENDOR_SCHEME) and c.isin
        }
        out = []
        for named in await named_companies(self.db):
            code = sec_code(named)
            if code and not any(i in answered or i in others for i in named.isins):
                out.append((named, code))
        return out

    async def refresh(
        self, *, force: bool = False, dry_run: bool = False
    ) -> Optional[Dict[str, Any]]:
        now, today = utcnow(), utc_today()
        scope = await self.scope()
        summary: Dict[str, Any] = {
            "status": "success",
            "configured": is_configured(),
            "in_scope": len(scope),
            "companies": 0,
            "reports_read": 0,
            "days_listed": 0,
            "rows_written": 0,
            "errors": 0,
            "requests": 0,
            "outcomes": [],
        }
        if not is_configured():
            # Without its key the pass records nothing, as OpenDART's does.
            return None
        if dry_run:
            summary["message"] = (
                f"Japanese companies: {len(scope)} in scope (dry run, no request made)"
            )
            return summary
        warnings: List[str] = []
        async with httpx.AsyncClient(
            transport=self._transport, timeout=httpx.Timeout(120.0)
        ) as client:
            for named, code in scope:
                company = await self.repo.ensure_other_company(
                    EDINET_SCHEME,
                    code[:4],
                    ticker=code[:4],
                    isin=next(i for i in named.isins if JAPAN_ISIN.match(i)),
                    country="JP",
                )
                company.name = named.name or company.name
                await self.db.commit()
                key = company.key
                try:
                    outcome = await self._one(
                        client, company, code, summary, now, today, force
                    )
                except (httpx.HTTPError, EdinetError) as e:
                    summary["errors"] += 1
                    logger.warning("EDINET read for %s failed: %s", key, e)
                    await self.db.rollback()
                    outcome = "error"
                summary["outcomes"].append({"key": key, "outcome": outcome})
                if self.requests >= PASS_REQUEST_CAP:
                    warnings.append(
                        f"The Japanese pass stopped at {PASS_REQUEST_CAP} requests; "
                        f"the rest waits for the next one."
                    )
                    break
        summary["requests"] = self.requests
        summary["days_listed"] = len(self._days)
        if summary["errors"]:
            warnings.append(
                f"{summary['errors']} Japanese compan"
                f"{'y' if summary['errors'] == 1 else 'ies'} could not be read from "
                f"EDINET and keep their previous figures."
            )
        summary["message"] = (
            f"Japanese companies: {summary['companies']} with reports of {len(scope)}, "
            f"{summary['reports_read']} reports read, {summary['rows_written']} rows, "
            f"{summary['requests']} requests"
        )
        if warnings:
            summary["warnings"] = warnings
        return summary

    async def _get(
        self, client: httpx.AsyncClient, path: str, **params: str
    ) -> httpx.Response:
        if self.requests:
            await self._sleep(REQUEST_INTERVAL_S)
        self.requests += 1
        response = await client.get(
            f"{BASE}/{path}",
            params={**params, "Subscription-Key": settings.edinet_api_key.strip()},
        )
        if response.status_code != 200:
            raise EdinetError(f"EDINET answered HTTP {response.status_code} for {path}")
        return response

    async def _day(self, client: httpx.AsyncClient, day: date) -> List[Dict[str, Any]]:
        """One day's filings (each day once a pass, shared by every company)."""
        if day not in self._days:
            body = (
                await self._get(
                    client, "documents.json", date=day.isoformat(), type="2"
                )
            ).json()
            status = str((body.get("metadata") or {}).get("status") or "200")
            if status != "200":
                raise EdinetError(f"EDINET's list for {day} answered status {status}")
            self._days[day] = [
                r for r in body.get("results") or [] if isinstance(r, dict)
            ]
        return self._days[day]

    async def _find(
        self, client: httpx.AsyncClient, code: str, days: List[date]
    ) -> Optional[Dict[str, Any]]:
        """The company's annual securities report filed on one of `days`, newest day first."""
        for day in sorted(days, reverse=True):
            if self.requests >= PASS_REQUEST_CAP and day not in self._days:
                return None
            for row in await self._day(client, day):
                if (
                    row.get("secCode") == code
                    and row.get("docTypeCode") == ANNUAL_REPORT
                    and str(row.get("xbrlFlag")) == "1"
                    and str(row.get("withdrawalStatus") or "0") == "0"
                ):
                    return row
        return None

    async def _one(
        self,
        client,
        company: Company,
        code: str,
        summary,
        now: datetime,
        today: date,
        force: bool,
    ) -> str:
        if company.universe_seen_at is not None:
            company.universe_seen_at = now
            await self.db.commit()
        reports = await self.repo.company_reports(company.id)
        read = sorted(
            (r for r in reports.values() if r.status == READ),
            key=lambda r: r.filed or date.min,
        )
        # (the days to look through, whether they are the next report's watch)
        wanted: List[Tuple[List[date], bool]] = []
        if not read:
            wanted.append(
                ([today - timedelta(days=d) for d in range(FIRST_LOOKBACK_DAYS)], False)
            )
        else:
            newest, oldest = read[-1], read[0]
            due = newest.filed + NEXT_DUE_AFTER
            if today >= due:
                looked = (
                    company.profile_checked_at.date()
                    if company.profile_checked_at
                    else due
                )
                start = max(due, looked)
                days = [
                    start + timedelta(days=d) for d in range((today - start).days + 1)
                ]
                wanted.append((days, True))
            if len(read) < 2:
                anchor = oldest.filed.replace(year=oldest.filed.year - HISTORY_YEARS)
                wanted.append(
                    (
                        [
                            anchor + timedelta(days=d)
                            for d in range(
                                -HISTORY_WINDOW_DAYS, HISTORY_WINDOW_DAYS + 1
                            )
                        ],
                        False,
                    )
                )
        read_any = False
        for days, watch in wanted:
            found = await self._find(client, code, days)
            if watch:
                # The days up to today are looked through: the next pass starts there.
                company.profile_checked_at = datetime.combine(
                    today, datetime.min.time()
                )
            if found is None or found["docID"] in reports:
                continue
            zipped = (
                await self._get(client, f"documents/{found['docID']}", type="1")
            ).content
            instance = instance_of(zipped)
            filed = date.fromisoformat(str(found.get("submitDateTime") or "")[:10])
            if instance is None:
                await self.repo.record_report(
                    company.id,
                    found["docID"],
                    status=NONE,
                    read_at=now,
                    accn=found["docID"],
                    filed=filed,
                )
                await self.db.commit()
                continue
            facts, tagged = summary_facts(instance, found["docID"], filed)
            end = found.get("periodEnd")
            await self.repo.record_report(
                company.id,
                found["docID"],
                status=READ,
                read_at=now,
                period_end=date.fromisoformat(end) if end else None,
                accn=found["docID"],
                filed=filed,
                facts=facts,
                tagged=tagged,
            )
            await self.db.commit()
            summary["reports_read"] += 1
            read_any = True
        await self.db.commit()
        if read_any or force or (company.fetched_at is None and read):
            return await self._write(company, summary, now)
        return "unchanged"

    async def _write(
        self, company: Company, summary: Dict[str, Any], now: datetime
    ) -> str:
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


class EdinetError(Exception):
    pass
