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
`HISTORY_YEARS` before its oldest report for its history (one more a pass, up to
`HISTORY_REPORTS` reports), and once its next report is due
(`NEXT_DUE_AFTER` past the newest filing) the days since it last looked
(`profile_checked_at`). An annual securities report is `docTypeCode` 120.

**Reading** (`summary_facts`): the report's XBRL instance (`type=1`, a zip) holds the
**summary of business results** (`…SummaryOfBusinessResults`, `jpcrp_cor`): five fiscal
years of revenue, profit attributable to owners, diluted EPS, operating cash flow, cash and
equity, in the company's standard's spelling (J-GAAP, `…IFRS…`, `…USGAAP…`), and the dividend
per share on its non-consolidated context. Each becomes its IFRS concept (`SUMMARY`), so the
one normaliser reads them as it reads an ESEF report; an IFRS filer's operating profit is in
the summary too, a J-GAAP filer's only in the statements (`OPERATING`), the current and prior
year. Consolidated contexts only.

**Splits** (`_on_latest_basis`, checked on all four reports 2026-11-21): a summary's EPS is
restated for a split only from some year on (Fujitsu's 10:1 of 2024 left FY2021's 922.97
beside FY2022's 110.54) and its dividends are as declared (Tokyo Electron's 1,711 before its
3:1, 393 after). The issued share count it states at each year end steps by the split
(`_split_factors`), so each year's per-share figures are put on the report's latest basis:
the dividend always, the EPS when its profit over EPS sits nearer the year's issued count
than the count times the split. Reports `HISTORY_YEARS` (4) apart share a year, so the
normaliser's own vote links an older report's basis to the newer one. The year of a share
consolidation has no dividend (`_consolidation_years`): its summary adds an interim on the
old basis to a final on the new (Hitachi's 8 + 50 of 2018, really 40 + 50).

**A split after the annual report** (`restated_eps`): Tokyo Electron's 5:1 of 2026-10-01 left
its June report's EPS and share count on the old basis beside post-split basket prices, a
fifth of its real P/E. The half-year report (`docTypeCode` 160) restates last year's EPS and
states the issued shares at its filing date. When both moved by the same split, that one EPS
is kept as the newest filing's fact and the normaliser's vote restates the rest.

**Bounded**: `PASS_REQUEST_CAP` requests a pass, `REQUEST_INTERVAL_S` apart.
"""

import asyncio
import io
import logging
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utc_today, utcnow
from app.config import settings
from app.models.company_financials import EDINET_SCHEME, VENDOR_SCHEME, Company
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.services.company_markets import NamedCompany, named_companies, sec_isins
from app.services.company_financials_normaliser import (
    NEIGHBOUR_RATIOS,
    NEIGHBOUR_TOLERANCE,
    REGULATOR_ANNUAL,
    _snap,
)
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
HALF_YEAR_REPORT = "160"
FIRST_LOOKBACK_DAYS = 400
#: What a read keeps: 1 until 2026-11-21 (NULL); 2 with per-share figures on the latest
#: share basis, a transition year's IFRS spelling, basic EPS for a blank diluted one, and an
#: IFRS filer's operating profit and total equity from its summary; 3 with the shares
#: outstanding at the year end (`OUTSTANDING`); 4 with no dividend for a consolidation's year.
#: A report read under an older one is read once more, by its document id (`_reread`).
READER = 4
#: Four years apart, so two reports' five-year summaries share a year (*Splits*); three
#: reports give thirteen fiscal years.
HISTORY_YEARS = 4
HISTORY_REPORTS = 3
HISTORY_WINDOW_DAYS = 12
NEXT_DUE_AFTER = timedelta(days=330)
#: The half-year report is looked for from the half's end, about this long after the fiscal
#: year's, for `HALF_YEAR_WINDOW_DAYS` (it is due within 45 days).
HALF_YEAR_AFTER_DAYS = 183
HALF_YEAR_WINDOW_DAYS = 70
#: A split proven by a half-year report: the stated EPS over the restated one is a split ratio
#: within rounding, and the issued shares at its filing date over the annual report's shares
#: outstanding are that ratio within the treasury shares the second count leaves out.
SPLIT_EPS_TOLERANCE = 0.005
SPLIT_COUNT_TOLERANCE = 0.10
PASS_REQUEST_CAP = 700
REQUEST_INTERVAL_S = 0.5
READ, NONE = "read", "none"
#: A half-year report: never an annual one (`NEXT_DUE_AFTER` counts from those), its `facts`
#: the restated EPS of a split it proves, else none.
INTERIM = "interim"
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
    "ProfitLossIFRSSummaryOfBusinessResults": ("ProfitLoss", "money"),
    "OperatingProfitIFRSSummaryOfBusinessResults": (
        "ProfitLossFromOperatingActivities",
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
    "TotalEquityIFRSSummaryOfBusinessResults": ("Equity", "money"),
    # Declared for the fiscal year, stated on the non-consolidated context.
    "DividendPaidPerShareSummaryOfBusinessResults": (
        "DividendsRecognisedAsDistributionsToOwnersPerShare",
        "per_share",
    ),
}
#: The basic EPS, read as the diluted one only for a year whose diluted EPS the summary leaves
#: blank: a Japanese report leaves it blank when no share is dilutive, or in a loss year, and
#: diluted then equals basic (NEC's FY2013-FY2016).
BASIC_EPS: Dict[str, Tuple[str, str]] = {
    name: ("DilutedEarningsLossPerShare", "per_share")
    for name in (
        "BasicEarningsLossPerShareSummaryOfBusinessResults",
        "BasicEarningsLossPerShareIFRSSummaryOfBusinessResults",
        "BasicEarningsLossPerShareUSGAAPSummaryOfBusinessResults",
    )
}
#: The issued share count at each year end, on the non-consolidated context: never stored,
#: it dates each split (`_split_factors`).
ISSUED_SHARES = "TotalNumberOfIssuedSharesSummaryOfBusinessResults"
#: The treasury shares (and those held crosswise) at the year end, from the report's status of
#: shares: the issued count less them is the shares outstanding, the market cap's count
#: (`OUTSTANDING`; Fujitsu's FY2025 1,739.8m issued, 5.3m held). Without it, no count.
TREASURY_SHARES = "TotalNumberOfSharesHeldTreasurySharesEtc"
OUTSTANDING = "NumberOfSharesOutstanding"
#: The issued shares at a report's filing date, on its whole-company context.
ISSUED_AT_FILING = "NumberOfIssuedSharesAsOfFilingDateIssuedSharesTotalNumberOfSharesEtc"
FILING_DATE_CONTEXT = "FilingDateInstant"
EPS = "DilutedEarningsLossPerShare"
DPS = "DividendsRecognisedAsDistributionsToOwnersPerShare"
#: Operating income from the statements (`jppfs_cor`, `jpigp_cor`), for a J-GAAP filer's
#: summary, which has none.
OPERATING: Dict[str, Tuple[str, str]] = {
    "OperatingIncome": ("ProfitLossFromOperatingActivities", "money"),
    "OperatingProfitLossIFRS": ("ProfitLossFromOperatingActivities", "money"),
}
#: EDINET's taxonomy, and a report's own extension of it: an IFRS filer's summary states its
#: operating profit and total equity in elements of its own namespace under the standard
#: names (Fujitsu's FY2025, `jpcrp030000/asr/001/E01766-000/…`), read by those names only.
EDINET_NS = re.compile(
    r"^http://disclosure\.edinet-fsa\.go\.jp/(taxonomy/(jpcrp|jppfs|jpigp)|jpcrp\d+/)"
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
    dividend per share and the issued shares, which the summary states on the
    non-consolidated one. Per-share figures on the report's latest share basis
    (`_on_latest_basis`).
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
            if EDINET_NS.match(uri) and (
                name in SUMMARY
                or name in OPERATING
                or name in BASIC_EPS
                or name == ISSUED_SHARES
                or name == TREASURY_SHARES
            ):
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
    candidates: List[Tuple[Tuple[bool, bool], Dict[str, Any], str]] = []
    issued: Dict[str, float] = {}
    treasury: Dict[str, float] = {}
    for name, ctx, unit_ref, text in raw:
        context = contexts.get(ctx)
        if context is None:
            continue
        start, end, members = context
        if name == ISSUED_SHARES:
            if (
                start is None
                and end
                and members in ([], [NON_CONSOLIDATED])
                and units.get(unit_ref) == "shares"
            ):
                value = _number(text)
                if value:
                    issued[end] = value
            continue
        if name == TREASURY_SHARES:
            if start is None and end and not members and units.get(unit_ref) == "shares":
                value = _number(text)
                if value is not None and value >= 0:
                    treasury[end] = value
            continue
        concept, kind = SUMMARY.get(name) or OPERATING.get(name) or BASIC_EPS[name]
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
        fact = {
            "concept": concept,
            "unit": unit_out,
            "start": start_day.isoformat() if start_day else None,
            "end": end_day.isoformat(),
            "val": value,
            "accn": accn,
            "filed": filed.isoformat(),
            "form": REGULATOR_ANNUAL,
        }
        candidates.append((_rank(name), fact, name))
    # Per period and concept, only the best-ranked spelling: the year an IFRS adopter's
    # summary states in both standards (NEC's FY2015-FY2016, 2,821bn J-GAAP beside 2,825bn
    # IFRS) is otherwise two values one period refuses.
    best: Dict[Tuple[str, Optional[str], str], Tuple[bool, bool]] = {}
    for rank, f, _ in candidates:
        key = (f["concept"], f["start"], f["end"])
        best[key] = max(best.get(key, rank), rank)
    facts: List[Dict[str, Any]] = []
    tagged: Dict[str, str] = {}
    for rank, f, name in candidates:
        if rank == best[(f["concept"], f["start"], f["end"])]:
            facts.append(f)
            tagged.setdefault(f["concept"], f"edinet:{name}")
    for end, held in treasury.items():
        if issued.get(end, 0) > held:
            facts.append(
                {
                    "concept": OUTSTANDING,
                    "unit": "shares",
                    "start": None,
                    "end": end,
                    "val": issued[end] - held,
                    "accn": accn,
                    "filed": filed.isoformat(),
                    "form": REGULATOR_ANNUAL,
                }
            )
            tagged.setdefault(OUTSTANDING, f"edinet:{ISSUED_SHARES}-{TREASURY_SHARES}")
    return _on_latest_basis(one_value_each(facts), issued), tagged


def _rank(name: str) -> Tuple[bool, bool]:
    """A diluted EPS before a basic one (`BASIC_EPS`), then the IFRS or US-GAAP spelling
    before J-GAAP's: the standard the company reports in now, as `_taxonomy` picks a 20-F
    filer's newer taxonomy."""
    return name not in BASIC_EPS, "IFRS" in name or "USGAAP" in name


def _number(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:
        return None


def _split_factors(issued: Dict[str, float]) -> Dict[str, Fraction]:
    """
    Each year end's factor to the report's latest share basis: the product of the issued
    count's steps after it that snap to a split ratio (`NEIGHBOUR_RATIOS` within
    `NEIGHBOUR_TOLERANCE`). A buyback or a cancellation snaps to no ratio and is no step
    (Fujitsu's 2,071m to 1,740m in FY2025).
    """
    ends = sorted(issued, reverse=True)
    factors: Dict[str, Fraction] = {}
    factor = Fraction(1)
    for i, end in enumerate(ends):
        if i:
            step = _snap(issued[ends[i - 1]] / issued[end], NEIGHBOUR_RATIOS, NEIGHBOUR_TOLERANCE)
            if step is not None:
                factor *= step
        factors[end] = factor
    return factors


def _consolidation_years(issued: Dict[str, float]) -> set:
    """The year ends whose issued count fell by a whole consolidation ratio from the year
    before (Hitachi's 1:5 and Fujitsu's 1:10 of October 2018)."""
    ends = sorted(issued)
    out = set()
    for before, end in zip(ends, ends[1:]):
        step = _snap(issued[end] / issued[before], NEIGHBOUR_RATIOS, NEIGHBOUR_TOLERANCE)
        if step is not None and step < 1:
            out.add(end)
    return out


def _on_latest_basis(
    facts: List[Dict[str, Any]], issued: Dict[str, float]
) -> List[Dict[str, Any]]:
    """
    The dividends and EPS of the years before a split on the report's latest basis: a
    dividend is as declared, so always divided by the split; an EPS only when its profit
    over EPS sits nearer that year's issued count than the count times the split (the
    summary restates some years and not others). An EPS whose basis cannot be told (no
    profit, no count, opposite signs) is left out, and so is the dividend of a consolidation's
    year, which adds two bases (`_consolidation_years`): an unknown is absent.
    """
    factors = _split_factors(issued)
    consolidated = _consolidation_years(issued)
    profit = {
        f["end"]: f["val"]
        for f in facts
        if f["concept"] == "ProfitLossAttributableToOwnersOfParent"
    }
    out = []
    for f in facts:
        if f["concept"] == DPS and f["end"] in consolidated:
            continue
        factor = float(factors.get(f["end"], 1))
        if factor == 1 or f["concept"] not in (EPS, DPS):
            out.append(f)
            continue
        if f["concept"] == DPS:
            out.append({**f, "val": f["val"] / factor})
            continue
        shares = profit.get(f["end"], 0) / f["val"] if f["val"] else 0
        count = issued.get(f["end"])
        if shares <= 0 or not count:
            continue
        stated = abs(math.log(shares / count))
        restated = abs(math.log(shares / (count * factor)))
        out.append({**f, "val": f["val"] / factor} if stated < restated else f)
    return out


def _issued_at_filing(instance: bytes) -> Optional[float]:
    """The issued shares a report states at its filing date, or None."""
    found = None
    for _, elem in ET.iterparse(io.BytesIO(instance)):
        tag = elem.tag
        if (
            tag.startswith("{")
            and tag.endswith("}" + ISSUED_AT_FILING)
            and EDINET_NS.match(tag[1:])
            and elem.get("contextRef") == FILING_DATE_CONTEXT
        ):
            found = _number((elem.text or "").strip())
        elem.clear()
    return found


def restated_eps(
    instance: bytes, accn: str, filed: date, annual: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    The fiscal year's EPS a half-year report restates for a split after the annual report
    (`annual`, that report's facts), as the newest filing's fact: the normaliser's split vote
    then puts every older per-share figure and share count on the new basis. Only when the
    issued shares moved by the same ratio; an EPS restated for anything else is left out.
    """
    issued = _issued_at_filing(instance)
    if not issued:
        return []
    stated = {f["end"]: f["val"] for f in annual if f["concept"] == EPS}
    counts = {f["end"]: f["val"] for f in annual if f["concept"] == OUTSTANDING}
    out = []
    for fact in summary_facts(instance, accn, filed)[0]:
        if fact["concept"] != EPS or not fact["val"]:
            continue
        old, count = stated.get(fact["end"]), counts.get(fact["end"])
        if not old or not count:
            continue
        step = _snap(old / fact["val"], NEIGHBOUR_RATIOS, SPLIT_EPS_TOLERANCE)
        if (
            step is not None
            and step != 1
            and abs(issued / count / float(step) - 1) <= SPLIT_COUNT_TOLERANCE
        ):
            out.append(fact)
    return out


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
        self,
        client: httpx.AsyncClient,
        code: str,
        days: List[date],
        doc_type: str = ANNUAL_REPORT,
    ) -> Optional[Dict[str, Any]]:
        """The company's report of `doc_type` filed on one of `days`, newest day first."""
        for day in sorted(days, reverse=True):
            if self.requests >= PASS_REQUEST_CAP and day not in self._days:
                return None
            for row in await self._day(client, day):
                if (
                    row.get("secCode") == code
                    and row.get("docTypeCode") == doc_type
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
        read_any = await self._reread(client, company, reports, summary, now)
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
            if len(read) < HISTORY_REPORTS:
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
                reader=READER,
            )
            await self.db.commit()
            summary["reports_read"] += 1
            read_any = True
        if read and await self._half_year(
            client, company, code, reports, read[-1], summary, now, today
        ):
            read_any = True
        await self.db.commit()
        if read_any or force or (company.fetched_at is None and read):
            return await self._write(company, summary, now)
        return "unchanged"

    async def _half_year(
        self, client, company: Company, code: str, reports, newest, summary, now, today
    ) -> bool:
        """
        The half-year report after the newest annual one, read once, in its window and from
        the day last looked through (`profile_checked_at`, which the annual watch starts
        from only months later). True when it proves a split (`restated_eps`).
        """
        if newest.period_end is None or any(
            r.status == INTERIM and r.filed and r.filed > newest.filed
            for r in reports.values()
        ):
            return False
        opens = newest.period_end + timedelta(days=HALF_YEAR_AFTER_DAYS)
        last = min(opens + timedelta(days=HALF_YEAR_WINDOW_DAYS), today)
        looked = company.profile_checked_at.date() if company.profile_checked_at else opens
        start = max(opens, looked)
        if start > last:
            return False
        days = [start + timedelta(days=d) for d in range((last - start).days + 1)]
        found = await self._find(client, code, days, HALF_YEAR_REPORT)
        if self.requests < PASS_REQUEST_CAP:
            company.profile_checked_at = datetime.combine(today, datetime.min.time())
        if found is None or found["docID"] in reports:
            return False
        zipped = (await self._get(client, f"documents/{found['docID']}", type="1")).content
        instance = instance_of(zipped)
        filed = date.fromisoformat(str(found.get("submitDateTime") or "")[:10])
        facts = (
            restated_eps(instance, found["docID"], filed, newest.facts or [])
            if instance is not None
            else []
        )
        await self.repo.record_report(
            company.id,
            found["docID"],
            status=INTERIM,
            read_at=now,
            accn=found["docID"],
            filed=filed,
            facts=facts,
            reader=READER,
        )
        await self.db.commit()
        summary["reports_read"] += 1
        return bool(facts)

    async def _reread(
        self, client, company: Company, reports, summary, now: datetime
    ) -> bool:
        """Each report read under an older `READER`, read once more from its zip."""
        any_read = False
        for report in list(reports.values()):
            if report.status != READ or (report.reader or 1) >= READER:
                continue
            zipped = (
                await self._get(client, f"documents/{report.report_id}", type="1")
            ).content
            instance = instance_of(zipped)
            if instance is None:
                continue
            filed = report.filed or now.date()
            facts, tagged = summary_facts(instance, report.report_id, filed)
            await self.repo.record_report(
                company.id,
                report.report_id,
                status=READ,
                read_at=now,
                period_end=report.period_end,
                accn=report.report_id,
                filed=filed,
                facts=facts,
                tagged=tagged,
                reader=READER,
            )
            await self.db.commit()
            summary["reports_read"] += 1
            any_read = True
        return any_read

    async def _write(
        self, company: Company, summary: Dict[str, Any], now: datetime
    ) -> str:
        reports = await self.repo.company_reports(company.id)
        facts: List[Dict[str, Any]] = []
        tagged: Dict[str, str] = {}
        for report in sorted(
            (r for r in reports.values() if r.status in (READ, INTERIM)),
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
