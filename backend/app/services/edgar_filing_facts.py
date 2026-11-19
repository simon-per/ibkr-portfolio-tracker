"""
The figures companyfacts leaves out, read from the filings themselves
(docs/company-financials.md, *Filing instances*).

**companyfacts carries only facts tagged without a dimension.** Many companies tag their
dividend per share only on a dimensioned context: a column of the statement of equity
(`StatementEquityComponentsAxis` — retained earnings for PCAR, MDLZ and INTC, common stock
for MA and XEL) or a share class (`StatementClassOfStockAxis` — BKR's Class A). For years at
a time the document then holds no dividend per share (MA from 2019 to mid-2023), and the
Company tab's TTM had a hole wherever one quarter was missing (seen on MA, 2026-11-07). The
figure is in the filing's XBRL instance, which EDGAR serves beside the filing.

So when a quarter shows dividends paid but no dividend per share, the pass reads the
instance of the filing that reports that quarter (a 10-Q, or the 10-K for a fourth quarter)
and keeps its dividend-per-share facts on a whitelisted dimension member (`MEMBERS`). The
diluted EPS the same way (2026-11-08): BKR has tagged it only on its Class A since 2023, so
companyfacts held no quarter's EPS after 2022 and the P/E stopped there; a quarter with net
income and no EPS reads its filing for the EPS on a share class (`EPS_MEMBERS`). Each
filing is read once for what the reader looks for — a filing never changes — and what it
held is stored, nothing included (`sec_filing_reads`, with the `READER` that read it). The
normaliser takes those facts beside companyfacts' and lets them fill a period only where
companyfacts has no fact of that concept (`company_financials_normaliser.normalise`).

Pure: the requests are `EdgarClient.filing_index` / `filing_document`, the loop is
`CompanyFinancialsSync`.
"""

import io
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.company_financials_normaliser import (
    IFRS_TWINS,
    METRICS,
    SEGMENT_DEBT,
    SEGMENT_WITNESSES,
)
from app.services.edgar_client import ANNUAL_FORM_PREFIXES
# A filing on EDGAR: the SEC's periodic forms only (the normaliser's `FORMS` also takes the
# other regulators' reports).
from app.services.edgar_client import PERIODIC_FORMS as FORMS

#: The concepts read from a filing on a dimension member: the dividend-per-share chain on
#: `MEMBERS`, the diluted EPS chain on `EPS_MEMBERS`, nothing else.
DIVIDEND_CONCEPTS = frozenset(METRICS["dividends_per_share"].concepts)
EPS_CONCEPTS = frozenset(METRICS["eps_diluted"].concepts)
CONCEPTS = DIVIDEND_CONCEPTS | EPS_CONCEPTS
#: The concepts read undimensioned from a filing companyfacts has not taken in yet
#: (`parse_instance(whole=True)`, review item CO5): every chain's.
CHAIN_CONCEPTS = frozenset(c for spec in METRICS.values() for c in spec.concepts)

#: The (axis, member) pairs whose dividend per share is the company's own per common share.
#: Whitelisted, never blacklisted: a member not named here (a second share class, a
#: preferred series, a special dividend, a subsequent event, a company's own extension
#: member such as AMAT's per-quarter ones) is never read. The equity statement's columns
#: all carry the one rate the company declared; a share class only when it is the class the
#: company is quoted by (BKR's Class A; GE held the Class B); COST tags what it declared on
#: the dividends axis. Surveyed over every gap filing of the 102 companies, 2026-11-07.
MEMBERS = frozenset(
    {
        ("StatementEquityComponentsAxis", "RetainedEarningsMember"),
        ("StatementEquityComponentsAxis", "CommonStockMember"),
        ("StatementClassOfStockAxis", "CommonStockMember"),
        ("StatementClassOfStockAxis", "CommonClassAMember"),
        ("DividendsAxis", "DividendDeclaredMember"),
    }
)

CLASS_AXIS = "StatementClassOfStockAxis"
#: The share classes whose diluted EPS is read. A period is kept only when every class the
#: filing tags its EPS on gives one value — the classes not read count too: Alphabet tags
#: its three classes alike, while a company whose classes earn different amounts per share
#: (Berkshire's A and B) has no one EPS to read, whichever class it is quoted by. **Unless
#: one class alone is listed** (2026-11-19): the cover page names a trading symbol on that
#: class only (`dei:TradingSymbol`), so it is the class every price is of — Visa's Class A
#: (`V`), beside Class B and C shares that convert into it and trade nowhere; Visa had no
#: EPS at all. Berkshire lists both classes and stays without one.
EPS_MEMBERS = frozenset({(CLASS_AXIS, "CommonStockMember"), (CLASS_AXIS, "CommonClassAMember")})
#: The cover page's trading symbol (`dei:TradingSymbol`), by local name in any dei year.
DEI = re.compile(r"^\{http://xbrl\.sec\.gov/dei/[^}]+\}TradingSymbol$")

#: A company's own concepts (`nvda:PurchasesOfPropertyAndEquipmentAndIntangibleAssets`),
#: which companyfacts never carries, named by their standard label in the filing's label
#: linkbase (2026-11-16, the source waterfall's third layer): per metric, the label it
#: must have and what it must not say. Capex and debt only — the figures the measurement
#: found under extension concepts (NVDA's capex FY2013-FY2021, CDNS's carrying value of
#: debt). A label is a name, not a proof: the normaliser takes the concept only where it
#: agrees with the chain's figure on every period both report (`_extend`). Lower case.
EXTENSION_LABELS: Dict[str, Tuple[str, str]] = {
    "capex": (
        r"^(purchases?|payments?|acquisitions?|additions?|expenditures?) (of|for|to|related"
        r" to|to acquire|on) (property|premises)(,? plant)?(,| and| &)+ equipment"
        r"|^capital expenditures?$",
        r"business|proceeds|sale|accrued|unpaid|not yet paid|incurred|lease|held for"
        r"|deposit|financ|payable|investment|software|leasehold|vehicles|rental",
    ),
    "debt_total": (
        r"^(total )?((carrying|book) (value|amount) of )?(total )?(long-term )?debt"
        r"( outstanding)?(, net)?(, including current (portion|maturities))?$",
        r"fair|principal|face|maturit|interest|issuance|cost|discount|lease|securit"
        r"|repay|proceeds|percent|ratio|related part",
    ),
    "debt_current": (
        r"^((current portion|current maturities) of (long-term )?(debt|notes|borrowings)"
        r"|short-term (debt|borrowings)|(notes payable|long-term debt|debt),? current)$",
        r"lease|fair|related part",
    ),
    "debt_noncurrent": (
        r"^(long-term (debt|notes|borrowings)|notes payable),? (net of|less|excluding)"
        r" current (portion|maturities)$|^(long-term (debt|notes|borrowings)),? noncurrent$",
        r"lease|fair|related part",
    ),
}
#: The timing each metric's concept must have: a flow is a duration, a balance an instant.
EXTENSION_FLOWS = frozenset({"capex"})
EXTENSION_FAMILIES = {
    "extension:capex": frozenset({"capex"}),
    "extension:debt": frozenset({"debt_total", "debt_current", "debt_noncurrent"}),
}

#: Segment members summed where no total exists (2026-11-16, the waterfall's fourth layer):
#: BRK and PCAR state their debt per segment, which companyfacts leaves out, so their debt
#: was unknown. What is read is the normaliser's (`SEGMENT_DEBT`, `SEGMENT_WITNESSES`).
#: A borrowing under any concept on a member's column (the normaliser's `DEBT_LIKE`, less
#: what is held rather than owed): a member showing one that `SEGMENT_DEBT` does not
#: state cannot count as owing nothing.
SEGMENT_DEBT_LIKE = re.compile(r"Debt|Notes|Borrowing|CommercialPaper|LoansPayable|LineOfCredit")
SEGMENT_NOT_DEBT = re.compile(
    r"AvailableForSale|Securities|Receivable|Investment|Asset|Capacity|Commitment|Percent"
    r"|Rate|Covenant|Fee|FairValue|Face|Maturit|Repayment|Proceeds|Payments|Interest"
    r"|DeferredTax|Discount|Premium|Issuance"
)
SEGMENT_FAMILY = "segments:debt"

#: The concepts each gap's read must hold to fill it, by the metric missing; an extension
#: gap is held by a fact a label named for one of its family's metrics (`covers`).
GAP_CONCEPTS: Dict[str, Any] = {
    "dividends_per_share": DIVIDEND_CONCEPTS,
    "eps_diluted": EPS_CONCEPTS,
    **EXTENSION_FAMILIES,
    SEGMENT_FAMILY: frozenset(SEGMENT_DEBT),
}
#: What a stored read looked for (`sec_filing_reads.reader`): 1 the dividend per share
#: alone (2026-11-07), 2 the diluted EPS too (2026-11-08), 3 the company's own concepts
#: by their labels too (2026-11-16), 4 the EPS of the one listed share class (2026-11-19).
#: A filing read before a gap's kind was looked for is read once more for that gap, and
#: never again after.
READER = 4
READ_SINCE = {
    "dividends_per_share": 1,
    "eps_diluted": 4,
    **{family: 3 for family in EXTENSION_FAMILIES},
    SEGMENT_FAMILY: 3,
}

#: A gap and a filing's period are one period when their ends are this close (the
#: normaliser's `_canonical_ends` folds ends within a week the same way).
SAME_PERIOD_DAYS = 7

US_GAAP = "http://fasb.org/us-gaap/"
#: The IFRS Foundation's taxonomy, `https://xbrl.ifrs.org/taxonomy/<date>/ifrs-full` (or
#: `http:`): a 20-F filer's (NU, TSMC) statements, read under their US-GAAP twins.
_IFRS_FULL = re.compile(r"https?://xbrl\.ifrs\.org/taxonomy/[^/]+/ifrs-full$")
XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
#: EDGAR's name for a filing's own instance, `<prefix>-<YYYYMMDD>.xml`: not a linkbase
#: (`_cal.xml`), a rendered report (`R12.xml`, 2009-2013) or `FilingSummary.xml`.
_INSTANCE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*-\d{8}\.xml", re.IGNORECASE)
#: Inline XBRL's extracted instance; a plain file name, never a path.
_EXTRACTED = re.compile(r"[a-z0-9][a-z0-9._-]*_htm\.xml", re.IGNORECASE)


@dataclass(frozen=True)
class Filing:
    """One 10-Q or 10-K companyfacts names, and the period it reports."""

    accn: str
    form: str
    filed: date
    period_end: date


def _date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def filings(companyfacts: Dict[str, Any]) -> Dict[str, Filing]:
    """
    Every 10-Q/10-K/20-F (and amendment) the document's us-gaap or ifrs-full facts come
    from, by accession number. Its period is the newest end carrying more than half as many
    duration facts as the commonest: a few carry a later date (a dividend declared after the quarter, as a
    subsequent event), while the commonest alone can be last year's comparatives — BKR's
    Q2 2026 10-Q has 102 facts ending in June 2025 and 101 in June 2026, so the quarter it
    reports had no filing and its EPS and dividend were never read (2026-11-09).
    """
    facts = (companyfacts or {}).get("facts") or {}
    # An IFRS filer's 20-F (NU, TSMC) is a filing like any other.
    gaap = {**(facts.get("ifrs-full") or {}), **(facts.get("us-gaap") or {})}
    ends: Dict[str, Counter] = defaultdict(Counter)
    meta: Dict[str, Tuple[str, date]] = {}
    for body in gaap.values():
        for rows in ((body or {}).get("units") or {}).values():
            for row in rows or ():
                form, accn = row.get("form"), row.get("accn")
                start, end = _date(row.get("start")), _date(row.get("end"))
                filed = _date(row.get("filed"))
                if form not in FORMS or not accn or None in (start, end, filed):
                    continue
                ends[accn][end] += 1
                meta.setdefault(accn, (form, filed))
    return {
        accn: Filing(accn, meta[accn][0], meta[accn][1], _own_period(c))
        for accn, c in ends.items()
    }


def _own_period(ends: Counter) -> date:
    """The newest end with more than half the facts of the commonest one (`filings`)."""
    top = max(ends.values())
    return max(end for end, n in ends.items() if 2 * n > top)


#: How far back a filing companyfacts skipped is looked for (STATUS 7a): the last eight
#: quarters, what the tab's newest TTM points and the pass's gap count read.
SKIPPED_LOOKBACK = timedelta(days=730)
#: A quarter's span at most (the normaliser's `QUARTER_DAYS`): two stored quarter ends
#: further apart than this have a quarter between them that no filing gave.
MAX_QUARTER_DAYS = 120
#: Newest figures older than this mean a filing is due that companyfacts has not served:
#: a 10-Q is due 40-45 days after its quarter, a 10-K 60-90 days after the year
#: (`company_financials_sync.FIGURES_STALE_AFTER`), and an annual-only filer's 20-F a year
#: and four months after its last one.
STALE_QUARTERLY = timedelta(days=150)
STALE_ANNUAL = timedelta(days=365 + 150)


def companyfacts_accns(companyfacts: Dict[str, Any]) -> set:
    """Every accession number companyfacts carries a statement fact of (us-gaap or
    ifrs-full). Not the cover page's (`dei`, `srt`): TSMC's FY2025 20-F was in companyfacts
    with two cover facts and none of its figures (2026-11-16)."""
    out = set()
    facts = (companyfacts or {}).get("facts") or {}
    for block in (facts.get("us-gaap"), facts.get("ifrs-full")):
        for body in (block or {}).values():
            for rows in ((body or {}).get("units") or {}).values():
                out.update(r.get("accn") for r in rows or () if r.get("accn"))
    return out


def read_period(facts: Sequence[Dict[str, Any]]) -> Optional[date]:
    """The period a filing read whole reports (`_own_period` over its duration facts),
    or None when it held none."""
    ends: Counter = Counter()
    for f in facts:
        start, end = _date(f.get("start")), _date(f.get("end"))
        if start is not None and end is not None:
            ends[end] += 1
    return _own_period(ends) if ends else None


def skipped(
    rows: Sequence[Dict[str, Any]],
    companyfacts: Dict[str, Any],
    read_periods: Iterable[date],
    today: date,
) -> List[date]:
    """
    The signs that companyfacts skipped a filing the SEC holds (STATUS 7a), newest first:
    a recent quarter end (within `SKIPPED_LOOKBACK`) that the figures carry — derived from
    a later filing's year-to-date ladder — but no filing companyfacts names (or a filing
    already read) reports as its own period, as AEP's and EXC's Q2 2026; a quarter missing
    between two recent ones; and figures stale enough that the next report is overdue (NU's
    and TSMC's FY2025 20-F). An annual-only filer is judged by its years. Pure; the pass
    then asks the filing list (`company_financials_sync._read_skipped`).
    """
    own = [f.period_end for f in filings(companyfacts).values()]
    own += [p for p in read_periods if p is not None]
    quarters = sorted({r["period_end"] for r in rows if r["fiscal_period"] != "FY"})
    years = sorted({r["period_end"] for r in rows if r["fiscal_period"] == "FY"})
    ends = quarters or years
    if not ends:
        return []
    signs = set()
    if today - ends[-1] > (STALE_QUARTERLY if quarters else STALE_ANNUAL):
        signs.add(today)
    recent = [e for e in ends if today - e <= SKIPPED_LOOKBACK]
    for end in recent:
        if not any(abs((p - end).days) <= SAME_PERIOD_DAYS for p in own):
            signs.add(end)
    if quarters:
        for earlier, later in zip(recent, recent[1:]):
            if (later - earlier).days > MAX_QUARTER_DAYS:
                signs.add(earlier + timedelta(days=(later - earlier).days // 2))
    return sorted(signs, reverse=True)


def dividend_gaps(rows: Iterable[Dict[str, Any]]) -> List[date]:
    """
    Quarter ends with dividends paid but no dividend per share, newest first: the periods
    a filing may hold the figure for. A company paying only preferred dividends shows here
    too; its filings are read once and hold nothing.
    """
    paid, have = set(), set()
    for row in rows:
        if row["fiscal_period"] == "FY":
            continue
        if row["metric"] == "dividends_paid" and row["value"] > 0:
            paid.add(row["period_end"])
        elif row["metric"] == "dividends_per_share":
            have.add(row["period_end"])
    return sorted(paid - have, reverse=True)


def eps_gaps(rows: Iterable[Dict[str, Any]]) -> List[date]:
    """
    Quarter ends with net income but no diluted EPS, newest first: BKR's quarters from 2023
    (its EPS on the Class A alone). A quarter whose diluted count was tagged has its EPS
    already (net income over it, the normaliser); a filing that tags none on a class is
    read once and holds nothing.
    """
    income, have = set(), set()
    for row in rows:
        if row["fiscal_period"] == "FY":
            continue
        if row["metric"] == "net_income":
            income.add(row["period_end"])
        elif row["metric"] == "eps_diluted":
            have.add(row["period_end"])
    return sorted(income - have, reverse=True)


def gaps(rows: Sequence[Dict[str, Any]]) -> List[Tuple[date, str]]:
    """Every quarter a filing may fill, as (quarter end, the metric missing), newest
    first: the dividend gaps and the EPS gaps."""
    found = [(d, "dividends_per_share") for d in dividend_gaps(rows)]
    found += [(d, "eps_diluted") for d in eps_gaps(rows)]
    return sorted(found, key=lambda gap: gap[0], reverse=True)


def ttm_dividend_gap(rows: Sequence[Dict[str, Any]]) -> bool:
    """Whether a dividend gap falls in the company's newest four quarters: its TTM dividend
    per share is absent now."""
    newest = sorted({r["period_end"] for r in rows if r["fiscal_period"] != "FY"})[-4:]
    return any(gap in newest for gap in dividend_gaps(rows))


def reporting(by_accn: Dict[str, Filing], period_end: date) -> Optional[Filing]:
    """The filing that reports the quarter ending on `period_end` — the original before an
    amendment, which may restate one note and tag nothing else."""
    candidates = [
        f
        for f in by_accn.values()
        if abs((f.period_end - period_end).days) <= SAME_PERIOD_DAYS
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda f: (f.form.endswith("/A"), f.filed, f.accn))


def sources(by_accn: Dict[str, Filing], period_end: date) -> List[Filing]:
    """
    The filings that may hold the quarter ending on `period_end`, in the order to ask them:
    the one reporting it, then the one reporting the same quarter a year later (its
    comparative), and for a fiscal year end the 10-K two years later as well (the statement
    of equity shows three years). BKR's 2018 filings tag the dividend on two dimension
    members, which are not read; its 2019 10-Qs and FY2020 10-K repeat those periods on one.
    """
    own = reporting(by_accn, period_end)
    out = [own] if own is not None else []
    years = 2 if own is not None and own.form.startswith(ANNUAL_FORM_PREFIXES) else 1
    for n in range(1, years + 1):
        later = reporting(by_accn, period_end + timedelta(days=365 * n))
        if later is not None and later not in out:
            out.append(later)
    return out


def covers(
    facts: Sequence[Dict[str, Any]], period_end: date, concepts: frozenset = CONCEPTS
) -> bool:
    """Whether facts of `concepts` read from a filing end on this period (a comparative
    quarter, a year-to-date rung or a year), so its own filing need not be read in the
    same pass. An extension family's `concepts` are metrics: a fact a label named for one
    of them (`metric`)."""
    return any(
        (f.get("metric") in concepts if "metric" in f else f.get("concept") in concepts)
        and (end := _date(f.get("end"))) is not None
        and abs((end - period_end).days) <= SAME_PERIOD_DAYS
        for f in facts
    )


#: How far back a debt companyfacts cannot show is looked for in segment columns, or a
#: quarter's own 10-Q for the company's own debt concept: the tab's default ten-year range.
SEGMENT_LOOKBACK = timedelta(days=3653)


#: The years from which a fiscal year's missing capex or debt sends the pass to its 10-K
#: for the company's own concepts: XBRL's first years (2009) on.
EXTENSION_SINCE = date(2009, 1, 1)


#: The provenance tags the app itself writes before a `:` in `source_concept` (a segment sum,
#: the vendor fill, the other regulators, the standard taxonomies). Any other prefix is the
#: company's own concept, read by its label (`cdns:CarryingValueOfDebtOutstanding`).
APP_SOURCE_PREFIXES = frozenset(
    {"segments", "alpha_vantage", "dart", "esef", "us-gaap", "ifrs-full", "dei", "srt"}
)
DEBT_METRICS = ("debt_total", "debt_current", "debt_noncurrent")


def own_concept(source: Any) -> bool:
    """Whether a row's `source_concept` is the company's own concept."""
    prefix, sep, _ = str(source or "").partition(":")
    return bool(sep) and prefix not in APP_SOURCE_PREFIXES


def extension_gaps(
    rows: Sequence[Dict[str, Any]], today: Optional[date] = None
) -> List[Tuple[date, str]]:
    """
    Fiscal year ends missing capex, or debt (neither the total nor both halves), newest
    first, as (year end, family): the years whose 10-K may state the figure under the
    company's own concept (`EXTENSION_LABELS`). One 10-K holds a year's flows and its
    balance sheet. **A quarter's debt** is asked of its 10-Q only for a company whose debt
    some year already came from its own concept (CDNS: `cdns:CarryingValueOfDebtOutstanding`
    in every 10-K, its quarters blank), within `SEGMENT_LOOKBACK` of `today`: elsewhere a
    10-Q holds nothing a 10-K did not show, and 511 quarters at 61 companies would be read
    for nothing (measured 2026-11-19).
    """
    have: Dict[date, set] = defaultdict(set)
    quarters: Dict[date, set] = defaultdict(set)
    for row in rows:
        if row["fiscal_period"] == "FY":
            have[row["period_end"]].add(row["metric"])
        else:
            quarters[row["period_end"]].add(row["metric"])
    out = []

    def no_debt(metrics: set) -> bool:
        return "debt_total" not in metrics and not (
            {"debt_current", "debt_noncurrent"} <= metrics
        )

    for end, metrics in have.items():
        if end < EXTENSION_SINCE:
            continue
        if "capex" not in metrics:
            out.append((end, "extension:capex"))
        if no_debt(metrics):
            out.append((end, "extension:debt"))
    if today is not None and any(
        r["metric"] in DEBT_METRICS and own_concept(r.get("source_concept")) for r in rows
    ):
        for end, metrics in quarters.items():
            if (
                end not in have
                and end >= EXTENSION_SINCE
                and today - end <= SEGMENT_LOOKBACK
                and no_debt(metrics)
            ):
                out.append((end, "extension:debt"))
    return sorted(out, key=lambda gap: gap[0], reverse=True)



def debt_blind(rows: Sequence[Dict[str, Any]]) -> bool:
    """A company no source but segment columns gives a debt figure for (BRK, PCAR): no
    debt row at all, a segment sum's aside. A debt-free balance sheet's zeros count as
    figures (`_no_debt_at_all`)."""
    return not any(
        r["metric"] in ("debt_total", "debt_noncurrent", "debt_current")
        and not str(r.get("source_concept", "")).startswith("segments:")
        for r in rows
    )


def segment_gaps(rows: Sequence[Dict[str, Any]], today: date) -> List[Tuple[date, str]]:
    """The period ends within `SEGMENT_LOOKBACK` with no debt figure, newest first, as
    (end, `SEGMENT_FAMILY`): the balance sheets whose own filing may state it per segment."""
    debt = {
        r["period_end"] for r in rows
        if r["metric"] in ("debt_total", "debt_noncurrent", "debt_current")
    }
    ends = {r["period_end"] for r in rows if today - r["period_end"] <= SEGMENT_LOOKBACK}
    return [(end, SEGMENT_FAMILY) for end in sorted(ends - debt, reverse=True)]


def label_linkbase(index: Any) -> Optional[str]:
    """The filing's label linkbase (`<prefix>-<YYYYMMDD>_lab.xml`) among its files, or
    None when there is not exactly one."""
    items = ((index or {}).get("directory") or {}).get("item") or []
    names = [
        i["name"]
        for i in items
        if isinstance(i, dict)
        and isinstance(i.get("name"), str)
        and re.fullmatch(r"[a-z0-9][a-z0-9._-]*_lab\.xml", i["name"], re.IGNORECASE)
    ]
    return names[0] if len(names) == 1 else None


_XLINK = "{http://www.w3.org/1999/xlink}"
_STANDARD_LABEL = "http://www.xbrl.org/2003/role/label"


def extension_metrics(body: bytes) -> Dict[str, Tuple[str, str]]:
    """
    The company's own concepts a label linkbase names like a metric: `{concept id ->
    (metric, label)}`, the id as the schema spells it (`nvda_PurchasesOf...`), read by its
    standard label (`EXTENSION_LABELS`). A concept whose label fits two metrics is none.
    Raises `ET.ParseError` on a document that is not XML.
    """
    locs: Dict[str, str] = {}
    arcs: List[Tuple[str, str]] = []
    labels: Dict[str, str] = {}
    for _, elem in ET.iterparse(io.BytesIO(body), events=("end",)):
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "loc":
            href = elem.get(_XLINK + "href") or ""
            locs[elem.get(_XLINK + "label") or ""] = href.rpartition("#")[2]
        elif tag == "labelArc":
            arcs.append((elem.get(_XLINK + "from") or "", elem.get(_XLINK + "to") or ""))
        elif tag == "label" and elem.get(_XLINK + "role") == _STANDARD_LABEL:
            labels[elem.get(_XLINK + "label") or ""] = " ".join((elem.text or "").split())
    out: Dict[str, Tuple[str, str]] = {}
    for source, target in arcs:
        concept, label = locs.get(source), labels.get(target)
        if not concept or not label or concept.startswith(("us-gaap_", "dei_", "srt_")):
            continue
        text = label.lower()
        named = [
            metric
            for metric, (named_like, not_named) in EXTENSION_LABELS.items()
            if re.search(named_like, text) and not re.search(not_named, text)
        ]
        if len(named) == 1:
            out[concept] = (named[0], label)
    return out


#: Namespaces that are no company's own: the standard taxonomies.
_STANDARD_NS = re.compile(r"fasb\.org|xbrl\.org|sec\.gov|ifrs\.org|w3\.org")


def instance_name(index: Any) -> Optional[str]:
    """
    The XBRL instance among a filing's files (its `index.json`), or None when there is no
    single one. An inline-XBRL filing (2019 on) has EDGAR's extracted `<name>_htm.xml`; an
    older one its own `<prefix>-<YYYYMMDD>.xml` beside the schema's linkbases and, until
    about 2013, the rendered reports (`R1.xml`, ...).
    """
    items = ((index or {}).get("directory") or {}).get("item") or []
    names = [
        i["name"]
        for i in items
        if isinstance(i, dict) and isinstance(i.get("name"), str)
    ]
    extracted = [n for n in names if _EXTRACTED.fullmatch(n)]
    if extracted:
        return extracted[0] if len(extracted) == 1 else None
    plain = [n for n in names if _INSTANCE.fullmatch(n)]
    return plain[0] if len(plain) == 1 else None


def _local(qname: str) -> str:
    return qname.rsplit(":", 1)[-1].strip()


def _gaap_name(qname: str, prefixes: Dict[str, str]) -> Optional[str]:
    """The local name of a `prefix:Name` QName in the us-gaap namespace, else None: a
    company's own extension member of the same name is not the standard one."""
    prefix, _, name = qname.strip().rpartition(":")
    return name if prefixes.get(prefix, "").startswith(US_GAAP) else None


def parse_instance(
    body: bytes,
    whole: bool = False,
    extensions: Optional[Dict[str, Tuple[str, str]]] = None,
    segments: bool = False,
) -> List[Dict[str, Any]]:
    """
    The dividend-per-share facts of one XBRL instance that sit on exactly one whitelisted
    dimension member (`MEMBERS`), and the diluted EPS facts on one share class
    (`EPS_MEMBERS`): `{concept, unit, start, end, val}`, dates as ISO strings (`start` None
    for a balance-sheet instant), the unit spelled as companyfacts spells it (`USD`,
    `shares`, `USD/shares`). A period two accepted facts give different values for (two
    share classes paid differently) is left out whole, and an EPS period any other share
    class gives another value for too. Raises `ET.ParseError` on a document that is not XML.

    Undimensioned facts are returned only `whole`: companyfacts has them, except for a
    filing it has not taken in yet (review item CO5, 2026-11-08: six companies sat on
    their Q1 for 221 days while their Q2 10-Qs were filed). Then every chain concept's
    undimensioned facts come too, and win over a member's value for the same period —
    they are what companyfacts would carry. An IFRS filing's (`ifrs-full`) lines come
    under their US-GAAP twins (`IFRS_TWINS`), only when it has no us-gaap chain fact.

    `extensions` (`extension_metrics` of the filing's label linkbase) adds the company's
    own concepts its labels name like a metric, undimensioned and in one currency, a flow
    as a duration and a balance as an instant: `{concept: "nvda:Name", metric, label, ...}`.

    `segments` adds, at each balance-sheet date, the `SEGMENT_DEBT` and `SEGMENT_WITNESSES`
    facts on exactly one explicit member (`{..., axis, member}`) and the witnesses'
    undimensioned totals (`axis` ""), and names every debt-like concept a member shows
    (`{concept, end, axis, member, debt_like: True}`, no value) — what the normaliser's
    `_segments` needs to prove a partition and sum it.
    """
    contexts: Dict[str, Tuple[Any, ...]] = {}
    units: Dict[str, str] = {}
    candidates: List[Tuple[str, str, Optional[str], str]] = []
    ifrs: List[Tuple[str, str, Optional[str], str]] = []
    own: List[Tuple[str, str, Optional[str], str, str]] = []
    segmented: List[Tuple[str, str, Optional[str], str]] = []
    # The contexts the cover page names a trading symbol on (`DEI`).
    symbol_contexts: List[str] = []
    prefixes: Dict[str, str] = {}
    depth = 0
    events = ("start-ns", "start", "end")
    for event, elem in ET.iterparse(io.BytesIO(body), events=events):
        if event == "start-ns":
            prefix, uri = elem
            prefixes.setdefault(prefix, uri)
            continue
        if event == "start":
            depth += 1
            continue
        depth -= 1
        if depth != 1:
            continue
        tag = elem.tag
        if tag == XBRLI + "context":
            period = elem.find(XBRLI + "period")
            start = end = None
            if period is not None:
                start = (period.findtext(XBRLI + "startDate") or "").strip() or None
                end = (period.findtext(XBRLI + "endDate") or "").strip() or None
                if end is None and (whole or extensions or segments):
                    end = (period.findtext(XBRLI + "instant") or "").strip() or None
            dims = [
                (
                    _gaap_name(m.get("dimension") or "", prefixes),
                    _gaap_name(m.text or "", prefixes),
                )
                for m in elem.iter(XBRLDI + "explicitMember")
            ]
            typed = any(True for _ in elem.iter(XBRLDI + "typedMember"))
            # Every explicit member by its local name, whatever its namespace: a segment's
            # axis is often `srt:` and its member the company's own (`pcar:`, `brka:`).
            raw = [
                (_local(m.get("dimension") or ""), _local(m.text or ""))
                for m in elem.iter(XBRLDI + "explicitMember")
            ]
            contexts[elem.get("id")] = (start, end, dims, typed, raw)
        elif tag == XBRLI + "unit":
            measures = [_local(m.text or "") for m in elem.iter(XBRLI + "measure")]
            if len(measures) == 2 and measures[1] == "shares":
                units[elem.get("id")] = f"{measures[0]}/shares"
            elif len(measures) == 1 and (whole or extensions or segments):
                units[elem.get("id")] = measures[0]
        elif tag.startswith("{" + US_GAAP) and tag.split("}", 1)[1] in (
            CHAIN_CONCEPTS if whole else CONCEPTS
        ):
            if elem.get(XSI_NIL) != "true" and elem.text is not None:
                candidates.append(
                    (
                        tag.split("}", 1)[1],
                        elem.get("contextRef") or "",
                        elem.get("unitRef"),
                        elem.text.strip(),
                    )
                )
        elif segments and tag.startswith("{" + US_GAAP) and (
            (name := tag.split("}", 1)[1]) in SEGMENT_DEBT
            or name in SEGMENT_WITNESSES
            or (SEGMENT_DEBT_LIKE.search(name) and not SEGMENT_NOT_DEBT.search(name))
        ):
            if elem.get(XSI_NIL) != "true" and elem.text is not None:
                segmented.append(
                    (name, elem.get("contextRef") or "", elem.get("unitRef"), elem.text.strip())
                )
        elif DEI.match(tag):
            if (elem.text or "").strip():
                symbol_contexts.append(elem.get("contextRef") or "")
        elif extensions and tag.startswith("{") and not _STANDARD_NS.search(tag):
            # The company's own concept: by the id its schema gives it, `<prefix>_<Name>`.
            uri, _, name = tag[1:].partition("}")
            prefix = next((p for p, u in prefixes.items() if u == uri and p), None)
            named = extensions.get(f"{prefix}_{name}") if prefix else None
            if named and elem.get(XSI_NIL) != "true" and elem.text is not None:
                own.append(
                    (
                        f"{prefix}:{name}",
                        elem.get("contextRef") or "",
                        elem.get("unitRef"),
                        elem.text.strip(),
                        named[0],
                    )
                )
        elif whole and tag.startswith("{"):
            # An IFRS filer's statement line, under its US-GAAP twin (STATUS 7b: NU's and
            # TSMC's FY2025 20-F sat outside companyfacts for months, and this read us-gaap
            # only). Undimensioned only, like every whole read.
            uri, _, name = tag[1:].partition("}")
            if (
                name in IFRS_TWINS
                and _IFRS_FULL.match(uri)
                and elem.get(XSI_NIL) != "true"
                and elem.text is not None
            ):
                ifrs.append(
                    (
                        IFRS_TWINS[name],
                        elem.get("contextRef") or "",
                        elem.get("unitRef"),
                        elem.text.strip(),
                    )
                )
        elem.clear()
    # One taxonomy, as the normaliser reads companyfacts (`_taxonomy`): a filing with any
    # us-gaap chain fact is read in us-gaap alone, never both.
    if not any(c in CHAIN_CONCEPTS for c, _, _, _ in candidates):
        candidates.extend(ifrs)

    by_period: Dict[Tuple[str, str, Optional[str], str], set] = defaultdict(set)
    plain: Dict[Tuple[str, str, Optional[str], str], set] = defaultdict(set)
    # An EPS period's values on every share class, read or not, and on the listed one.
    classes: Dict[Tuple[str, str, Optional[str], str], set] = defaultdict(set)
    on_listed: Dict[Tuple[str, str, Optional[str], str], set] = defaultdict(set)
    listed = _listed_class(symbol_contexts, contexts)
    for concept, ctx_ref, unit_ref, text in candidates:
        context, unit = contexts.get(ctx_ref), units.get(unit_ref)
        if context is None or unit is None:
            continue
        start, end, dims, typed, _ = context
        if typed:
            continue
        on_class = concept in EPS_CONCEPTS and len(dims) == 1 and dims[0][0] == CLASS_AXIS
        target: Optional[Dict] = None
        if not dims and whole:
            target = plain
        elif concept in DIVIDEND_CONCEPTS and len(dims) == 1 and dims[0] in MEMBERS:
            target = by_period
        elif on_class and dims[0] in EPS_MEMBERS:
            target = by_period
        elif not on_class:
            continue
        if _date(end) is None or (start is not None and _date(start) is None):
            continue
        if start is None and target is not plain:
            continue
        try:
            val = float(text)
        except ValueError:
            continue
        if not math.isfinite(val):
            continue
        if on_class:
            classes[(concept, unit, start, end)].add(val)
            if dims[0] == listed:
                on_listed[(concept, unit, start, end)].add(val)
        if target is not None:
            target[(concept, unit, start, end)].add(val)
    for key in by_period.keys() & classes.keys():
        by_period[key] = classes[key]  # another class's value leaves the period out
    for key, vals in on_listed.items():
        if len(classes[key]) > 1 and len(vals) == 1:
            by_period[key] = vals  # unless one class alone is listed: its EPS
    merged = {k: v for k, v in by_period.items() if k not in plain}
    merged.update(plain)
    out = [
        {"concept": c, "unit": u, "start": s, "end": e, "val": next(iter(vals))}
        for (c, u, s, e), vals in sorted(
            merged.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "", kv[0][3])
        )
        if len(vals) == 1
    ]
    return (
        out
        + _own_facts(own, contexts, units, extensions or {})
        + _segment_facts(segmented, contexts, units)
    )


def _listed_class(symbol_contexts: Sequence[str], contexts: Dict[str, Tuple[Any, ...]]):
    """The one share class (`(CLASS_AXIS, member)`) the cover page names a trading symbol
    on, or None: none named on a class (one class, its symbol undimensioned), or several
    (Berkshire's A and B)."""
    named = set()
    for ref in symbol_contexts:
        context = contexts.get(ref)
        if context is None:
            continue
        dims = context[2]
        if len(dims) == 1 and dims[0][0] == CLASS_AXIS and dims[0][1]:
            named.add(dims[0])
        elif not dims:
            return None
    return next(iter(named)) if len(named) == 1 else None


def _segment_facts(segmented, contexts, units) -> List[Dict[str, Any]]:
    """The segment facts `parse_instance` kept (see its `segments`): instants only, a
    value on one explicit member or none (a witness's total), and debt-like names."""
    values: Dict[Tuple[str, str, str, str, str], set] = defaultdict(set)
    named: set = set()
    for concept, ctx_ref, unit_ref, text in segmented:
        context, unit = contexts.get(ctx_ref), units.get(unit_ref)
        if context is None or unit is None or "/" in unit:
            continue
        start, end, _, typed, dims = context
        if start is not None or typed or _date(end) is None or len(dims) > 1:
            continue
        axis, member = dims[0] if dims else ("", "")
        valued = concept in SEGMENT_DEBT or concept in SEGMENT_WITNESSES
        if not dims and concept not in SEGMENT_WITNESSES:
            continue
        if dims and not valued:
            named.add((concept, end, axis, member))
            continue
        try:
            val = float(text)
        except ValueError:
            continue
        if math.isfinite(val):
            values[(concept, unit, end, axis, member)].add(val)
    out = [
        {"concept": c, "unit": u, "start": None, "end": e, "val": next(iter(vals)),
         "axis": a, "member": m}
        for (c, u, e, a, m), vals in sorted(values.items())
        if len(vals) == 1
    ]
    out += [
        {"concept": c, "end": e, "axis": a, "member": m, "debt_like": True}
        for c, e, a, m in sorted(named)
    ]
    return out


def _own_facts(own, contexts, units, extensions) -> List[Dict[str, Any]]:
    """The company's own concepts' facts `parse_instance` kept: undimensioned, in one
    currency, timed as their metric is; a period given two values is left out."""
    found: Dict[Tuple[str, str, Optional[str], str, str], set] = defaultdict(set)
    for concept, ctx_ref, unit_ref, text, metric in own:
        context, unit = contexts.get(ctx_ref), units.get(unit_ref)
        if context is None or unit is None or "/" in unit:
            continue
        start, end, dims, typed, _ = context
        if dims or typed or _date(end) is None:
            continue
        if (start is not None) != (metric in EXTENSION_FLOWS):
            continue
        if start is not None and _date(start) is None:
            continue
        try:
            val = float(text)
        except ValueError:
            continue
        if math.isfinite(val):
            found[(concept, unit, start, end, metric)].add(val)
    labels = {f"{k.split('_', 1)[0]}:{k.split('_', 1)[1]}": v[1] for k, v in extensions.items() if "_" in k}
    return [
        {"concept": c, "unit": u, "start": s, "end": e, "val": next(iter(vals)),
         "metric": m, "label": labels.get(c)}
        for (c, u, s, e, m), vals in sorted(
            found.items(), key=lambda kv: (kv[0][0], kv[0][2] or "", kv[0][3])
        )
        if len(vals) == 1
    ]
