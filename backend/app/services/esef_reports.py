"""
An ESEF annual report's xBRL-JSON as facts the one normaliser reads (docs/company-financials.md,
*Europe*). Pure.

**Undimensioned facts only**, as companyfacts holds: a fact with any dimension beyond its
concept, entity, period and unit (a segment, a component of equity, a restatement axis) is
not read. Of those, the `ifrs-full` concepts the normaliser reads (`CONCEPT_KIND`) become
facts; a number tagged with the company's own extension concept instead is skipped and
counted (`non_standard`), as OpenDART's non-standard lines are. Another entity's facts (the
LEI is in each) are not read.

**Annual only**: an ESEF filing is the annual financial report, so a duration is read only
when it spans a fiscal year (`YEAR_DAYS`) and every fact is filed as `REGULATOR_ANNUAL`; the
service then takes the fiscal year as the trailing twelve months, as for a 20-F filer.

**Dates**: xBRL-JSON writes a period's end as the instant after it (`2025-01-01T00:00:00` is
the end of 31 December 2024), so a date at midnight is moved back one day; a start is its own
day. **Units**: `iso4217:EUR` monetary, `iso4217:EUR/xbrli:shares` per share, `xbrli:shares`
a count — read only where the concept's kind agrees (`regulator_facts.unit_for`). The values
are already scaled (xBRL-JSON's canonical values), so `decimals` is not applied again.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.services.company_financials_normaliser import (
    MONETARY,
    PER_SHARE,
    REGULATOR_ANNUAL,
    SHARES,
)
from app.services.regulator_facts import CONCEPT_KIND, one_value_each, unit_for

#: A duration read as a fiscal year: 52/53-week years included.
YEAR_DAYS = (350, 380)
CORE_DIMENSIONS = frozenset({"concept", "entity", "period", "unit"})
IFRS = "ifrs-full"


@dataclass
class Converted:
    facts: List[Dict[str, Any]] = field(default_factory=list)
    #: Numbers tagged with the company's own extension concept, skipped.
    non_standard: int = 0
    #: IFRS concept -> its name in the report (`ifrs-full:Revenue`).
    tagged: Dict[str, str] = field(default_factory=dict)


def _moment(text: str, is_end: bool) -> Optional[date]:
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    day = moment.date()
    if is_end and moment.time() == datetime.min.time() and "T" in text:
        day -= timedelta(days=1)
    return day


def period(text: Any) -> Optional[Tuple[Optional[date], date]]:
    """(start, end) of a duration, (None, date) of an instant, or None."""
    if not isinstance(text, str) or not text:
        return None
    if "/" in text:
        start_text, end_text = text.split("/", 1)
        start, end = _moment(start_text, False), _moment(end_text, True)
        if start is None or end is None or end < start:
            return None
        return start, end
    end = _moment(text, True)
    return None if end is None else (None, end)


def unit_of(text: Any) -> Tuple[str, Optional[str]]:
    """(currency, the normaliser's kind) of an xBRL-JSON unit: `iso4217:EUR` -> ("EUR",
    MONETARY), `iso4217:EUR/xbrli:shares` -> ("EUR", PER_SHARE), `xbrli:shares` -> ("",
    SHARES); anything else ("", None)."""
    if not isinstance(text, str):
        return "", None
    if text == "xbrli:shares":
        return "", SHARES
    if text.startswith("iso4217:"):
        rest = text[len("iso4217:") :]
        if rest.endswith("/xbrli:shares"):
            rest, kind = rest[: -len("/xbrli:shares")], PER_SHARE
        else:
            kind = MONETARY
        if len(rest) == 3 and rest.isalpha():
            return rest.upper(), kind
    return "", None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def convert(
    document: Dict[str, Any], lei: str, accn: str, filed: Optional[date]
) -> Converted:
    """One filing's facts (`regulator_facts`): its undimensioned annual IFRS figures."""
    out = Converted()
    if filed is None:
        return out
    entity = f"scheme:{lei}".upper()
    facts: List[Dict[str, Any]] = []
    raw = document.get("facts")
    for fact in raw.values() if isinstance(raw, dict) else []:
        if not isinstance(fact, dict):
            continue
        dims = fact.get("dimensions")
        if not isinstance(dims, dict) or set(dims) - CORE_DIMENSIONS:
            continue
        if str(dims.get("entity") or "").upper() != entity:
            continue
        value = _number(fact.get("value"))
        prefix, _, concept = str(dims.get("concept") or "").partition(":")
        if value is None or not concept:
            continue
        if prefix != IFRS:
            if "unit" in dims:
                out.non_standard += 1
            continue
        currency, kind = unit_of(dims.get("unit"))
        if kind is None or CONCEPT_KIND.get(concept) != kind:
            continue
        unit = unit_for(concept, currency)
        span = period(dims.get("period"))
        if span is None:
            continue
        start, end = span
        if start is not None:
            days = (end - start).days + 1
            if not YEAR_DAYS[0] <= days <= YEAR_DAYS[1]:
                continue
        facts.append(
            {
                "concept": concept,
                "unit": unit,
                "start": None if start is None else start.isoformat(),
                "end": end.isoformat(),
                "val": value,
                "accn": accn,
                "filed": filed.isoformat(),
                "form": REGULATOR_ANNUAL,
            }
        )
        out.tagged.setdefault(concept, f"{IFRS}:{concept}")
    out.facts = one_value_each(facts)
    return out


__all__ = ["CONCEPT_KIND", "Converted", "YEAR_DAYS", "convert", "period", "unit_of"]
