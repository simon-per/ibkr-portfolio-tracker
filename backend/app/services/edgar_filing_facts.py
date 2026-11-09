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

from app.services.company_financials_normaliser import FORMS, METRICS
from app.services.edgar_client import ANNUAL_FORM_PREFIXES

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
#: (Berkshire's A and B) has no one EPS to read, whichever class it is quoted by.
EPS_MEMBERS = frozenset({(CLASS_AXIS, "CommonStockMember"), (CLASS_AXIS, "CommonClassAMember")})

#: The concepts each gap's read must hold to fill it, by the metric missing.
GAP_CONCEPTS = {"dividends_per_share": DIVIDEND_CONCEPTS, "eps_diluted": EPS_CONCEPTS}
#: What a stored read looked for (`sec_filing_reads.reader`): 1 the dividend per share
#: alone (2026-11-07), 2 the diluted EPS too (2026-11-08). A filing read before a metric
#: was looked for is read once more for that metric's gap, and never again after.
READER = 2
READ_SINCE = {"dividends_per_share": 1, "eps_diluted": 2}

#: A gap and a filing's period are one period when their ends are this close (the
#: normaliser's `_canonical_ends` folds ends within a week the same way).
SAME_PERIOD_DAYS = 7

US_GAAP = "http://fasb.org/us-gaap/"
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
    Every 10-Q/10-K (and amendment) the document's us-gaap facts come from, by accession
    number. Its period is the newest end carrying more than half as many duration facts as
    the commonest: a few carry a later date (a dividend declared after the quarter, as a
    subsequent event), while the commonest alone can be last year's comparatives — BKR's
    Q2 2026 10-Q has 102 facts ending in June 2025 and 101 in June 2026, so the quarter it
    reports had no filing and its EPS and dividend were never read (2026-11-09).
    """
    gaap = ((companyfacts or {}).get("facts") or {}).get("us-gaap") or {}
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
    same pass."""
    return any(
        f.get("concept") in concepts
        and (end := _date(f.get("end"))) is not None
        and abs((end - period_end).days) <= SAME_PERIOD_DAYS
        for f in facts
    )


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


def parse_instance(body: bytes, whole: bool = False) -> List[Dict[str, Any]]:
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
    they are what companyfacts would carry.
    """
    contexts: Dict[
        str, Tuple[Optional[str], Optional[str], List[Tuple[str, str]], bool]
    ] = {}
    units: Dict[str, str] = {}
    candidates: List[Tuple[str, str, Optional[str], str]] = []
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
                if end is None and whole:
                    end = (period.findtext(XBRLI + "instant") or "").strip() or None
            dims = [
                (
                    _gaap_name(m.get("dimension") or "", prefixes),
                    _gaap_name(m.text or "", prefixes),
                )
                for m in elem.iter(XBRLDI + "explicitMember")
            ]
            typed = any(True for _ in elem.iter(XBRLDI + "typedMember"))
            contexts[elem.get("id")] = (start, end, dims, typed)
        elif tag == XBRLI + "unit":
            measures = [_local(m.text or "") for m in elem.iter(XBRLI + "measure")]
            if len(measures) == 2 and measures[1] == "shares":
                units[elem.get("id")] = f"{measures[0]}/shares"
            elif len(measures) == 1 and whole:
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
        elem.clear()

    by_period: Dict[Tuple[str, str, Optional[str], str], set] = defaultdict(set)
    plain: Dict[Tuple[str, str, Optional[str], str], set] = defaultdict(set)
    # An EPS period's values on every share class, read or not.
    classes: Dict[Tuple[str, str, Optional[str], str], set] = defaultdict(set)
    for concept, ctx_ref, unit_ref, text in candidates:
        context, unit = contexts.get(ctx_ref), units.get(unit_ref)
        if context is None or unit is None:
            continue
        start, end, dims, typed = context
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
        if target is not None:
            target[(concept, unit, start, end)].add(val)
    for key in by_period.keys() & classes.keys():
        by_period[key] = classes[key]  # another class's value leaves the period out
    merged = {k: v for k, v in by_period.items() if k not in plain}
    merged.update(plain)
    return [
        {"concept": c, "unit": u, "start": s, "end": e, "val": next(iter(vals))}
        for (c, u, s, e), vals in sorted(
            merged.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "", kv[0][3])
        )
        if len(vals) == 1
    ]
