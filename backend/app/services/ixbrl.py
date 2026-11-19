"""
An Inline XBRL document (XHTML with `ix:nonFraction` facts) read into the xBRL-JSON shape the
ESEF converter already reads (`esef_reports.convert`), so a filing published as Inline XBRL
rather than through filings.xbrl.org needs no second converter (docs/company-financials.md,
*Taiwan*). Taiwan's MOPS serves each listed company's quarterly report this way (2026-11-19).

**Read**: every `ix:nonFraction` with its context (entity, period, explicit members) and unit
from `ix:resources`; the shown number in its `format` (`ixt:numdotdecimal`,
`ixt:num-dot-decimal`, the comma-decimal forms, and the zero dash), times `10**scale`,
negated where `sign="-"`. A fact whose number cannot be read, or whose context or unit is
missing, is left out; nothing is guessed. Concepts and measures are spelled by their
namespace, not the document's prefixes (`ifrs-full:Revenue`, `iso4217:TWD/xbrli:shares`).

**Shape** (xBRL-JSON, as filings.xbrl.org serves it): a duration's end and an instant are
written as the midnight after the day, which `esef_reports.period` reads back.

Pure: bytes in, a dict out. Raises `ET.ParseError` on a document that is not XML.
"""

import io
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any, Dict, Optional, Tuple

IX = re.compile(r"^http://www\.xbrl\.org/20\d\d/inlineXBRL$")
XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
IFRS_FULL = re.compile(r"^https?://xbrl\.ifrs\.org/taxonomy/[^/]+/ifrs-full$")
ISO4217 = "http://www.xbrl.org/2003/iso4217"
#: The prefixes xBRL-JSON spells these namespaces with, whatever the document calls them.
KNOWN = {ISO4217: "iso4217", XBRLI: "xbrli"}
#: Number formats by the local name of `format`: dot as the decimal mark, comma as it, a
#: dash for zero. Any other format is not read.
DOT_DECIMAL = {"numdotdecimal", "num-dot-decimal", "numcommadot", "num-comma-dot"}
COMMA_DECIMAL = {"numcommadecimal", "num-comma-decimal", "numdotcomma", "num-dot-comma"}
ZERO = {"zerodash", "fixed-zero", "fixedzero", "nocontent", "zero-dash"}


def _local(qname: str) -> str:
    return qname.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _spell(qname: str, prefixes: Dict[str, str]) -> Optional[str]:
    """`prefix:Name` as `ifrs-full:Name` / `iso4217:TWD` by the prefix's namespace."""
    prefix, sep, name = qname.strip().partition(":")
    if not sep:
        return None
    uri = prefixes.get(prefix, "")
    if IFRS_FULL.match(uri):
        return f"ifrs-full:{name}"
    return f"{KNOWN.get(uri, prefix)}:{name}"


def number(text: str, fmt: Optional[str]) -> Optional[float]:
    """The shown number in its `format`, or None."""
    shown = (text or "").strip()
    kind = _local(fmt or "numdotdecimal").lower()
    if kind in ZERO or (
        shown in {"-", "—", "–"} and kind in DOT_DECIMAL | COMMA_DECIMAL
    ):
        return 0.0
    if kind in DOT_DECIMAL:
        digits = shown.replace(",", "").replace(" ", "").replace(" ", "")
    elif kind in COMMA_DECIMAL:
        digits = (
            shown.replace(".", "").replace(" ", "").replace(" ", "").replace(",", ".")
        )
    else:
        return None
    if not re.fullmatch(r"\d+(\.\d+)?", digits):
        return None
    return float(digits)


def _moment(day: str) -> Optional[str]:
    try:
        return (
            date.fromisoformat(day.strip()[:10]) + timedelta(days=1)
        ).isoformat() + "T00:00:00"
    except ValueError:
        return None


def parse(body: bytes) -> Dict[str, Any]:
    """The document's numeric facts as `{"facts": {id: {"value", "dimensions"}}}`."""
    prefixes: Dict[str, str] = {}
    contexts: Dict[str, Tuple[str, str, Dict[str, str]]] = {}
    units: Dict[str, str] = {}
    raw = []
    for event, elem in ET.iterparse(io.BytesIO(body), events=("start-ns", "end")):
        if event == "start-ns":
            prefix, uri = elem
            prefixes.setdefault(prefix, uri)
            continue
        uri, _, tag = (
            elem.tag[1:].partition("}")
            if elem.tag.startswith("{")
            else ("", "", elem.tag)
        )
        if uri == XBRLI and tag == "context":
            entity = elem.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
            period = elem.find(f"{{{XBRLI}}}period")
            span = None
            if period is not None:
                start = period.findtext(f"{{{XBRLI}}}startDate")
                end = period.findtext(f"{{{XBRLI}}}endDate")
                instant = period.findtext(f"{{{XBRLI}}}instant")
                if start and end and _moment(end):
                    span = f"{start.strip()[:10]}T00:00:00/{_moment(end)}"
                elif instant:
                    span = _moment(instant)
            dims = {
                _spell(m.get("dimension") or "", prefixes) or "": _spell(
                    m.text or "", prefixes
                )
                or ""
                for m in elem.iter(f"{{{XBRLDI}}}explicitMember")
            }
            typed = any(True for _ in elem.iter(f"{{{XBRLDI}}}typedMember"))
            if entity is not None and span and not typed:
                contexts[elem.get("id") or ""] = (
                    f"scheme:{(entity.text or '').strip()}",
                    span,
                    dims,
                )
        elif uri == XBRLI and tag == "unit":
            measures = [
                _spell(m.text or "", prefixes) for m in elem.iter(f"{{{XBRLI}}}measure")
            ]
            if all(measures) and len(measures) in (1, 2):
                units[elem.get("id") or ""] = "/".join(measures)
        elif IX.match(uri) and tag == "nonFraction":
            if elem.get(XSI_NIL) != "true":
                raw.append(
                    (
                        elem.get("name") or "",
                        elem.get("contextRef") or "",
                        elem.get("unitRef") or "",
                        "".join(elem.itertext()),
                        elem.get("format"),
                        elem.get("scale") or "0",
                        elem.get("sign"),
                    )
                )
    facts: Dict[str, Any] = {}
    for i, (name, ctx, unit_ref, text, fmt, scale, sign) in enumerate(raw):
        concept, context, unit = (
            _spell(name, prefixes),
            contexts.get(ctx),
            units.get(unit_ref),
        )
        value = number(text, fmt)
        if concept is None or context is None or unit is None or value is None:
            continue
        try:
            value *= 10 ** int(scale)
        except ValueError:
            continue
        if sign == "-":
            value = -value
        entity, span, dims = context
        facts[f"f{i}"] = {
            "value": value,
            "dimensions": {
                "concept": concept,
                "entity": entity,
                "period": span,
                "unit": unit,
                **dims,
            },
        }
    return {"facts": facts}


__all__ = ["number", "parse"]
