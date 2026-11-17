"""
Another regulator's figures into the one normaliser (docs/company-financials.md, *Markets
beyond the SEC*): OpenDART's statement lines (`dart_reports`) and an ESEF report's xBRL-JSON
facts (`esef_reports`) are each turned into facts shaped like companyfacts' — an `ifrs-full`
concept, a unit, a real start and end, the report's id as the accession number, its filing
date, and a form (`REGULATOR_ANNUAL`, `REGULATOR_INTERIM`) — and read by
`company_financials_normaliser.normalise` under `IFRS_TWINS`, every chain, ladder and vote
unchanged. **No second parser of figures**: this module only moves values into the shape the
normaliser reads, and names each row's source again afterwards (`relabel`).
"""

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.services.company_financials_normaliser import (
    IFRS_TWINS,
    METRICS,
    MONETARY,
    PER_SHARE,
    SHARES,
    Normalised,
    normalise,
)

#: The IFRS concepts the normaliser reads, by their twin's kind (`METRICS`).
_KIND_OF_TWIN = {c: spec.kind for spec in METRICS.values() for c in spec.concepts}
CONCEPT_KIND: Dict[str, str] = {
    name: _KIND_OF_TWIN[twin] for name, twin in IFRS_TWINS.items() if twin in _KIND_OF_TWIN
}


def unit_for(concept: str, currency: str) -> Optional[str]:
    """The companyfacts unit of an IFRS concept the normaliser reads, or None if it reads none."""
    kind = CONCEPT_KIND.get(concept)
    if kind == MONETARY:
        return currency
    if kind == PER_SHARE:
        return f"{currency}/shares"
    if kind == SHARES:
        return "shares"
    return None


def one_value_each(facts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Within one report, one value per (concept, start, end): a repeat of the same value is
    kept once; two different values leave the period out whole (two share classes' EPS, as
    Berkshire's classes are left out), never one picked.
    """
    by_key: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        by_key[(fact["accn"], fact["concept"], fact["start"], fact["end"])].append(fact)
    out = []
    for group in by_key.values():
        if len({f["val"] for f in group}) == 1:
            out.append(group[0])
    return out


def companyfacts(entity_name: Optional[str], facts: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Facts (`concept, unit, start, end, val, accn, filed, form`) as a companyfacts document
    with an `ifrs-full` block."""
    block: Dict[str, Any] = {}
    for f in facts:
        units = block.setdefault(f["concept"], {"units": {}})["units"]
        units.setdefault(f["unit"], []).append(
            {k: f[k] for k in ("start", "end", "val", "accn", "filed", "form") if f.get(k) is not None}
        )
    return {"entityName": entity_name, "facts": {"ifrs-full": block}}


def relabel(rows: Sequence[Dict[str, Any]], source: str, names: Dict[str, str]) -> None:
    """
    Every row's `source_concept` in the source's own words: the US-GAAP twin the normaliser
    read a concept under becomes the concept the report tagged (`names`, twin -> that
    concept), prefixed by the source (`dart:ifrs-full_Revenue`, `esef:ifrs-full_Revenue`).
    Derived sources keep their shape (`... less Q1-Q3`, `a/b`).
    """
    if not names:
        pattern = None
    else:
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(t) for t in sorted(names, key=len, reverse=True)) + r")\b"
        )
    for row in rows:
        text = row["source_concept"]
        if pattern is not None:
            text = pattern.sub(lambda m: names[m.group(1)], text)
        row["source_concept"] = f"{source}:{text}"[:128]


def normalise_reports(
    entity_name: Optional[str],
    facts: Iterable[Dict[str, Any]],
    source: str,
    tagged: Dict[str, str],
) -> Normalised:
    """
    One company's facts from every stored report, through the one normaliser; each row's
    source named (`relabel`). `tagged` maps each IFRS concept to the name the report gave it
    (`ifrs-full_Revenue`, OpenDART's `dart_OperatingIncomeLoss`).
    """
    result = normalise(companyfacts(entity_name, facts))
    twins = {IFRS_TWINS[name]: tag for name, tag in tagged.items() if name in IFRS_TWINS}
    relabel(result.rows, source, twins)
    return result
