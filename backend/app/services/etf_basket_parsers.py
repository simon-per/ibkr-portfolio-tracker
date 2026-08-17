"""
Turn one issuer's holdings file into a basket we can store. Pure: no network, no DB.

Split from the fetching half deliberately (`app/cli/fetch_etf_baskets.py` writes a response
body to disk, `app/cli/import_etf_basket.py` parses and stores it), for the same reason
`ingest_flex_xml.py` is separate from the Flex client: it makes every scraper's failure mode
a committable fixture, and it is the only way to test the trap below at all.

**The trap, and it is measured rather than imagined.** The formerly-documented iShares
holdings URL returns **HTTP 200, `Content-Type: text/csv`, `Content-Disposition: attachment`
— and an HTML body.** Neither the status code nor the content type can be trusted, so every
parser validates on content. And the consequence of getting that wrong is worse than a failed
import: an import *succeeds*, replaces a real 1,338-row basket with three junk rows, and
every look-through figure silently shrinks.

So the rule is `import_prices.py`'s, sharpened: **reject the whole file, never skip rows.** A
partially-parsed basket is indistinguishable afterwards from a complete one, and the
plausibility gate in `EtfBasketRepository.replace_basket` keeps the previous basket when this
module refuses.

Seven issuers, seven sets of hazards, every one observed on a live file rather than imagined
(the first three on 2026-08-14, the four US feeds on 2026-08-16):

- **Xtrackers/DWS** — semicolon-delimited CSV, weights are **fractions summing to 1.0**, no
  asset-class column at all, and no as-of date anywhere (not in the body, not in a
  `Last-Modified` header). It echoes `ShareClass ISIN` on every row, which is the cheapest
  possible guard against a redirect serving another fund's basket.
- **iShares/BlackRock** — JSON of **index-aligned parallel arrays**. A partial response
  misaligns ISINs against weights, which is the worst corruption this feature can suffer, so
  the lengths are asserted equal. Note the payload also contains *unrelated* short arrays
  (length 4 alongside 1,338), so "all arrays are the same length" is the wrong check; only
  the datapoints actually read are compared.
- **Vanguard US** — paginated JSON, 500 rows a page, month-end as-of that lags ~6 weeks.
  Its stock endpoint returns equities only, so what is missing from Σ is genuinely the
  non-equity remainder.
- **Invesco** — keyless JSON keyed by the fund's own CUSIP. Names arrive HTML-escaped, one row
  carries a null weight, and `effectiveDate` is a day *later* than the `effectiveBusinessDate`
  the holdings actually describe.
- **First Trust** — a server-rendered HTML table nested inside layout tables, whose
  `Classification` column looks like an asset class and holds an industry. Its currency rows
  are marked only by a `$`-prefixed ticker.
- **Defiance** — HTML again, at `/{ticker}-full-holdings/` (the plain product page renders its
  table client-side and carries none of it). Its as-of is stamped with the *next* business day.
- **VanEck** — an XLSX, read with `zipfile` + `ElementTree` rather than a new dependency. The
  only source here that publishes an ISIN on every row.

**The three US HTML/JSON feeds share one hazard the others do not have: their nine-character
identifier column is not all CUSIPs.** It mixes CUSIPs, CINS (foreign issuers, letter-leading)
and — on Defiance — SEDOLs. `app/services/security_identifiers.py` tells them apart by shape
and check digit, converts only what can be converted, and hands the rest to `ConstituentRow
.identifier` for OpenFIGI. Prefixing a CINS with `US` yields a check-digit-valid ISIN belonging
to nothing, which is the silent-fabrication direction this codebase refuses everywhere.
"""
import csv
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
from typing import Dict, List, Optional, Sequence, Tuple

from app.services.security_identifiers import (
    derive_north_american_isin,
    identifier_kind,
)

logger = logging.getLogger(__name__)

# Rows representing a holding in a *security* — a company, or another fund — as opposed to
# cash, FX forwards, futures and margin.
#
# A WHITELIST, never a blacklist: the reasoning behind `CashFlowRepository.get_deposits()`
# selecting DEPOSITWITHDRAW by name so no new transfer-ish type can leak in. A blacklist
# admits whatever label an issuer invents next ('Rights', 'Warrant') as a company.
#
# **`counts_as_invested` is shared with `lookthrough_service` on purpose.** The parser uses it
# to compute the `equity_weight_pct` that the read path's plausibility floor keys on, and the
# service uses it to decide which rows to attribute. Two copies would let a stored figure and
# the value derived from the same rows disagree — this file's whole subject.
INVESTED_ASSET_CLASSES = frozenset({"equity"})
FUND_CLASS_MARKER = "fund"

# A basket of one row is a parse failure, not a fund.
MIN_ROWS = 2
# Rounding puts real files a hair over 100 (IWDA measures 100.01). Anything beyond this is a
# unit error or a notional-weighted file, and is refused rather than clamped.
MAX_TOTAL_WEIGHT_PCT = Decimal("101")


class BasketParseError(Exception):
    """A problem with the file — reported, never partially applied."""


def counts_as_invested(asset_class: Optional[str], asset_class_available: bool) -> bool:
    """
    Is this row a holding in a security rather than cash or a derivative?

    **A stated class always decides, whether or not the issuer publishes a class column.** That
    matters because an adapter can know things the file does not say in a column: Xtrackers has
    no asset-class field at all, but marks its cash and futures lines with an unmistakable
    identifier convention (`_CURRENCYUSD`, `___ADI34XYM5`), and `parse_dws` turns that into a
    stated class. Before this rule those rows were attributed as *companies*, so XNAS's basket
    produced company rows called "US DOLLAR" and "NASDAQ 100 E-MINI SEP26".

    A row with **nothing** stated counts only when the issuer publishes no class column at all —
    there the filter genuinely cannot run, which `asset_class_available=False` records so the
    API can say so. Never inferred from "the row has an ISIN", which a bond also has.
    """
    value = (asset_class or "").strip().lower()
    if value:
        return value in INVESTED_ASSET_CLASSES or FUND_CLASS_MARKER in value
    return not asset_class_available


@dataclass(frozen=True)
class ConstituentRow:
    line_no: int
    name: str
    weight_pct: Decimal
    isin: Optional[str] = None
    ticker: Optional[str] = None
    asset_class: Optional[str] = None
    sector: Optional[str] = None
    country: Optional[str] = None

    # The identifier the issuer published, kept verbatim when it is NOT an ISIN — a CINS or a
    # SEDOL that `app/services/security_identifiers.py` refuses to convert locally. It exists
    # so identity resolution has something to send OpenFIGI: on GRID that is 77 of 128 rows,
    # including its three largest holdings, so discarding it would leave most of the fund
    # permanently unfoldable. Empty whenever `isin` is set, so there is one answer per row.
    identifier: Optional[str] = None


@dataclass
class ParsedBasket:
    fund_isin: str
    as_of_date: date
    source: str
    adapter: str
    rows: List[ConstituentRow]
    source_rows: int
    skipped_rows: int = 0
    as_of_is_issuer_stated: bool = True
    asset_class_available: bool = True

    @property
    def total_weight_pct(self) -> Decimal:
        return sum((r.weight_pct for r in self.rows), Decimal("0"))

    @property
    def equity_weight_pct(self) -> Decimal:
        """Share of the fund attributable to securities — companies plus nested funds."""
        return sum(
            (
                r.weight_pct
                for r in self.rows
                if counts_as_invested(r.asset_class, self.asset_class_available)
            ),
            Decimal("0"),
        )

    @property
    def identifier_coverage_pct(self) -> Decimal:
        """
        Share of the invested weight carrying an ISIN.

        A row with no identifier can never be folded with a direct holding, so this bounds how
        much of a fund's look-through can consolidate at all.
        """
        invested = self.equity_weight_pct
        if not invested:
            return Decimal("0")
        with_isin = sum(
            (
                r.weight_pct
                for r in self.rows
                if r.isin
                and counts_as_invested(r.asset_class, self.asset_class_available)
            ),
            Decimal("0"),
        )
        return with_isin / invested * Decimal("100")

    def validate(self) -> None:
        """
        Refuse the whole basket rather than store a doubtful one.

        Each of these was chosen against a real failure: too few rows is a truncated or
        HTML-bodied response; a negative weight is a short futures line on a
        notional-weighted file, and would *subtract* a company's exposure and could cancel a
        real direct holding; a total beyond 101 is a unit error.
        """
        if len(self.rows) < MIN_ROWS:
            raise BasketParseError(
                f"only {len(self.rows)} constituent row(s) parsed, which is a truncated or "
                f"non-holdings response rather than a fund"
            )
        for row in self.rows:
            invested = counts_as_invested(row.asset_class, self.asset_class_available)
            # **A negative weight is refused only on an INVESTED row**, and that distinction
            # came from real data: EMIM publishes five negative cash lines (THB -0.01,
            # TWD -0.01, CNH -0.01, HKD -0.02, KRW -0.10), which are ordinary overdrawn
            # currency balances. Refusing a 4,042-row basket over -0.10% of cash costs the
            # whole fund's look-through to protect against nothing. A negative *security*
            # weight is the real hazard — a short that would subtract a company's exposure
            # and could cancel a real direct holding — so that still refuses the file.
            #
            # Note this deliberately still refuses ANY negative row from an issuer that
            # publishes no asset class (Xtrackers): with no way to tell cash from equity,
            # the conservative reading is the only available one.
            if row.weight_pct < 0 and invested:
                raise BasketParseError(
                    f"line {row.line_no} ({row.name!r}, asset class "
                    f"{row.asset_class or 'unstated'}) has weight {row.weight_pct}: a "
                    f"negative weight on a security would subtract exposure and could cancel "
                    f"a real holding. Refusing the file rather than clamping it"
                )
            if row.weight_pct > 100:
                raise BasketParseError(
                    f"line {row.line_no} ({row.name!r}) has weight {row.weight_pct}%, which "
                    f"is more than the whole fund — almost certainly a unit error"
                )
            # An unlabelled row is fine as long as something identifies it — `_label` will
            # have substituted its ISIN or ticker. Nameless AND unidentifiable is not a
            # holding we can attribute, or even show, so that refuses.
            if not row.name.strip():
                raise BasketParseError(
                    f"line {row.line_no} has neither a name nor any identifier, so it cannot "
                    f"be attributed to anything"
                )
        total = self.total_weight_pct
        if total > MAX_TOTAL_WEIGHT_PCT:
            raise BasketParseError(
                f"weights sum to {total}%, beyond the {MAX_TOTAL_WEIGHT_PCT}% tolerance — a "
                f"fraction/percent mix-up or a notional-weighted file"
            )


# --------------------------------------------------------------------------- shared bits

_HTML_SNIFF = re.compile(rb"^\s*(<!doctype|<html|<\?xml|\{\"errors)", re.I)


def _refuse_if_empty(body: bytes, adapter: str) -> None:
    if not (body or b"").strip():
        raise BasketParseError(f"{adapter}: empty response body")


def _refuse_if_not_data(body: bytes, adapter: str) -> None:
    """
    The lying-content-type guard.

    The retired iShares holdings URL still answers 200 with `Content-Type: text/csv` and an
    HTML body, so this is checked on the bytes rather than on any header.

    **Not for the adapters whose payload really is a web page** — First Trust and Defiance
    publish their baskets as HTML tables, so for those the equivalent guard is finding the
    holdings table at all (`_pick_table`), which a consent page or an error page fails.
    """
    if _HTML_SNIFF.match(body or b""):
        raise BasketParseError(
            f"{adapter}: the response body is a web page, not a holdings file — the endpoint "
            f"has probably moved. Status codes and content types are not evidence here"
        )
    _refuse_if_empty(body, adapter)


def _decimal(raw, what: str) -> Decimal:
    """
    Parse a number written by any of these issuers.

    Handles a thousands separator and a decimal comma: with both separators present the comma
    is thousands (`73,640,313.10`), with only a comma it is the decimal point
    (`0,0571624952`, the German DWS export). Getting that backwards turns a 5.7% weight into
    571%, which `validate()` would catch — but catching it here names the field.
    """
    text = str(raw).strip().replace("%", "").replace(" ", "").replace("\xa0", "")
    if not text or text.lower() in ("-", "n/a", "null", "none"):
        raise BasketParseError(f"{what}: no usable number in {raw!r}")
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise BasketParseError(f"{what}: {raw!r} is not a number")


def _clean(value) -> Optional[str]:
    text = ("" if value is None else str(value)).strip()
    return text or None


def _label(name, isin: Optional[str], ticker: Optional[str]) -> str:
    """
    The best available label for a constituent row.

    Issuers do ship nameless rows: XNAS's export carries IE00BYQNZ507 with an empty name at
    0.008% of the fund. Refusing a whole basket over a blank string would be absurd, and the
    ISIN is what actually folds the row anyway — the displayed name comes from the resolved
    identity when there is one, and this is only the last resort. Empty here means the row has
    no name AND no identifier, which `validate()` then refuses.
    """
    return (_clean(name) or isin or _clean(ticker) or "")


def _isin(value) -> Optional[str]:
    text = (_clean(value) or "").upper()
    # 12 alphanumerics, two-letter country prefix. A blank, a '-' or a CUSIP is left as None
    # rather than stored as a broken identifier that would fold two companies together.
    return text if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", text) else None


def _resolve_identifier(
    raw, is_security: bool = True
) -> Tuple[Optional[str], Optional[str]]:
    """
    Split a published identifier into `(isin, unresolved_identifier)`.

    One rule for the three US issuers that publish nine-character identifiers, because they
    make the same two mistakes available: a CINS prefixed `US` is a *syntactically valid* ISIN
    belonging to nothing, and a SEDOL sitting in a column headed "CUSIP" is neither. Whatever
    cannot be converted here is carried forward verbatim for OpenFIGI rather than dropped —
    see `ConstituentRow.identifier`.

    `is_security=False` for a row the issuer has already called cash: `CASHUSD00` has the shape
    of a CINS and asking a provider about it would be noise.
    """
    text = (_clean(raw) or "").upper()
    if not text or not is_security:
        return None, None
    kind = identifier_kind(text)
    if kind == "ISIN":
        return text, None
    if kind == "CUSIP":
        # Digit-leading, so derivable. `derive_north_american_isin` assumes the US form; the
        # Canadian one exists in the same numeric space and only one of the two is real.
        return derive_north_american_isin(text), None
    if kind in ("CINS", "SEDOL"):
        return None, text
    return None, None


class _TableCollector(HTMLParser):
    """
    Every `<table>` on a page, as a list of rows of plain-text cells.

    `html.parser` rather than a regex because both issuer pages nest tables inside layout
    tables — First Trust's holdings table is the sixth of eight — and `<table>.*?</table>` finds
    the wrong closing tag the moment that happens. A stack keeps each cell with its innermost
    table. Deliberately tolerant of unclosed tags, which is what real markup looks like.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []
        self._stack: List[List[List[str]]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if self._row:
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            # Innermost first, so a nested table is a table in its own right and the outer one
            # simply does not contain its cells.
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def close(self):  # pragma: no cover - only reached on malformed markup
        super().close()
        while self._stack:
            self.tables.append(self._stack.pop())


def _html_tables(body: bytes, adapter: str) -> List[List[List[str]]]:
    _refuse_if_empty(body, adapter)
    collector = _TableCollector()
    collector.feed(body.decode("utf-8", errors="replace"))
    collector.close()
    return collector.tables


def _pick_table(
    tables: Sequence[Sequence[Sequence[str]]], required: Sequence[str], adapter: str
) -> List[List[str]]:
    """
    The first table whose header row carries every required column, matched case-insensitively.

    Positional selection ("the sixth table") would break the first time the issuer adds a
    banner, and silently: the sixth table would still parse, just as something else. Matching on
    the headers means a layout change fails loudly with the headers it did find.
    """
    wanted = [w.strip().lower() for w in required]
    for table in tables:
        if not table:
            continue
        header = [c.strip().lower() for c in table[0]]
        if all(w in header for w in wanted) and len(table) > 1:
            return [list(row) for row in table]
    seen = [t[0] for t in tables if t]
    raise BasketParseError(
        f"{adapter}: no table on the page carries the columns {list(required)} — the layout "
        f"changed, or the response is a consent/error page. Headers seen: {seen[:8]}"
    )


def _column_index(header: Sequence[str], name: str, adapter: str) -> int:
    lowered = [c.strip().lower() for c in header]
    try:
        return lowered.index(name.strip().lower())
    except ValueError:  # pragma: no cover - _pick_table has already required the column
        raise BasketParseError(f"{adapter}: column {name!r} vanished from the header {header}")


def _us_date_after(text: str, anchor: str, adapter: str) -> date:
    """
    The `M/D/YYYY` that follows a named phrase — never merely the first one on the page.

    The anchor is the whole point. QTUM's page carries **five** date-shaped strings: the as-of,
    a bond maturity inside a holding's own name (`... Obligations Fund 12/01/2031`), a
    `17/02/2022` that is not even month-first, and two with impossible years. An unanchored
    search took the `17/02/2022` and refused the file — which was the lucky outcome; the
    maturity date would have parsed cleanly and dated the basket to 2031.

    Refused rather than defaulted when absent: every issuer here publishes one, so its absence
    means the page is not the page we think it is.
    """
    pattern = re.escape(anchor) + r"[^0-9]{0,20}(\d{1,2})/(\d{1,2})/(\d{4})"
    match = re.search(pattern, text or "", re.I)
    if not match:
        raise BasketParseError(f"{adapter}: no date follows {anchor!r} on the page")
    month, day, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise BasketParseError(
            f"{adapter}: {month}/{day}/{year} after {anchor!r} is not a date"
        )


def _require_ticker(body: bytes, ticker: str, adapter: str) -> None:
    """
    Refuse a page whose `<title>` does not name the fund we asked for.

    Both routes are keyed by ticker in a query string or a path segment, so a redirect — a
    retired ticker, a typo, a locale bounce — serves another fund's holdings under a 200. This
    is the HTML equivalent of the `ShareClass ISIN` echo `parse_dws` checks on every row.
    """
    text = body.decode("utf-8", errors="replace")
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    title = unescape(match.group(1)).strip() if match else ""
    if ticker.upper() not in title.upper():
        raise BasketParseError(
            f"{adapter}: the page is titled {title[:80]!r}, which does not name {ticker} — it "
            f"is another fund's holdings, or a consent page"
        )


# ------------------------------------------------------------------------ Xtrackers / DWS

DWS_FUND_ISIN_COL = "ShareClass ISIN"
DWS_COLUMNS = {
    "isin": "Constituent ISIN",
    "name": "Constituent Name",
    "country": "Constituent Country",
    "weight": "Constituent Weighting",
    "sector": "Constituent Industry Classification Name",
}


def _dws_asset_class(raw_identifier: Optional[str]) -> Optional[str]:
    """
    Read Xtrackers' non-security rows off their identifier, since the export has no class column.

    Observed on the live XNAS basket: `_CURRENCYUSD` / `_CURRENCYEUR` for cash and
    `___ADI34XYM5` ("NASDAQ 100 E-MINI SEP26") for a futures line. Both are unmistakable — a
    real ISIN is two letters then ten alphanumerics and can never begin with an underscore — so
    this reads a fact the issuer stated rather than guessing.

    **Only the negatives are derived.** Everything else returns None rather than "Equity",
    because asserting that would be an inference about instruments we have not looked at, and
    for a bond or multi-asset Xtrackers fund it would be wrong. A None row still counts as
    invested here (there is no class column), so the effect is confined to excluding the rows
    the issuer itself flagged.
    """
    text = (raw_identifier or "").strip()
    if not text.startswith("_"):
        return None
    return "Cash" if text.upper().startswith("_CURRENCY") else "Futures"


def parse_dws(body: bytes, fund_isin: str, fetched_on: date) -> ParsedBasket:
    """
    Xtrackers' constituent export: semicolon-delimited, weights as fractions of 1.

    Two things are load-bearing. The weights are **fractions** and are scaled here, at the
    adapter boundary — a fraction reaching `etf_holdings.weight_pct` would make the fund
    contribute a hundredth of its value and read as 99% cash, which is a plausible figure.
    And `ShareClass ISIN` is checked against the fund we asked for on every row, because the
    URL is keyed by ISIN and a redirect would otherwise hand us another fund's basket.

    There is no as-of date to read, so the fetch date stands in and
    `as_of_is_issuer_stated=False` says so.
    """
    _refuse_if_not_data(body, "dws")
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")

    missing = [c for c in (DWS_FUND_ISIN_COL, *DWS_COLUMNS.values()) if c not in (reader.fieldnames or [])]
    if missing:
        raise BasketParseError(
            f"dws: expected column(s) {missing} absent; header was {reader.fieldnames}"
        )

    wanted = fund_isin.strip().upper()
    rows: List[ConstituentRow] = []
    source_rows = 0
    for index, record in enumerate(reader, start=1):
        if not any((v or "").strip() for v in record.values()):
            continue
        source_rows += 1
        declared = (record.get(DWS_FUND_ISIN_COL) or "").strip().upper()
        if declared != wanted:
            raise BasketParseError(
                f"dws: row {index} belongs to {declared or '(blank)'}, not to the requested "
                f"{wanted} — the export served a different fund"
            )
        fraction = _decimal(record[DWS_COLUMNS["weight"]], f"dws row {index} weight")
        raw_id = _clean(record[DWS_COLUMNS["isin"]])
        row_isin = _isin(record[DWS_COLUMNS["isin"]])
        rows.append(ConstituentRow(
            line_no=index,
            name=_label(record[DWS_COLUMNS["name"]], row_isin, raw_id),
            weight_pct=fraction * Decimal("100"),
            isin=row_isin,
            # No asset-class *column* exists in this export, but the issuer's own identifier
            # convention states the class for exactly the rows that are not securities, so the
            # adapter reads it rather than leaving the filter blind. See `_dws_asset_class`.
            asset_class=_dws_asset_class(raw_id),
            # 'unknown' is what the export writes when it has no classification; storing it
            # verbatim would put an "unknown" sector on a row that simply was not classified.
            sector=(lambda s: None if (s or "").lower() == "unknown" else s)(
                _clean(record[DWS_COLUMNS["sector"]])
            ),
            country=_clean(record[DWS_COLUMNS["country"]]),
        ))

    basket = ParsedBasket(
        fund_isin=wanted, as_of_date=fetched_on, source="dws", adapter="dws",
        rows=rows, source_rows=source_rows,
        as_of_is_issuer_stated=False, asset_class_available=False,
    )
    basket.validate()
    return basket


# -------------------------------------------------------------------- iShares / BlackRock

ISHARES_FIELDS = ("isin", "ticker", "issueName", "holdingPercent", "assetClass",
                  "sectorName", "countryOfRisk")


def parse_ishares(body: bytes, fund_isin: str) -> ParsedBasket:
    """
    BlackRock's product-data JSON: fifteen index-aligned parallel arrays.

    The alignment is the hazard. A truncated or partial response leaves one array shorter than
    the others and every subsequent ISIN is attached to the wrong weight — silent, plausible
    and catastrophic. So the arrays actually read are compared for equal length and a mismatch
    refuses the file.

    Note the payload also carries unrelated short arrays (a four-element `dateList` beside a
    1,338-element `isin`), so a blanket "every array is the same length" check would refuse
    every valid response. Only the fields below are compared.
    """
    _refuse_if_not_data(body, "ishares")
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise BasketParseError(f"ishares: response is not JSON: {e}")

    try:
        points = (
            payload["componentsByNameMap"]["holdings"]
            ["containersByNameMap"]["all"]["dataPointsByNameMap"]
        )
    except (KeyError, TypeError):
        raise BasketParseError(
            "ishares: no holdings container in the response — the product id may be wrong, "
            "or the API shape changed"
        )

    columns = {}
    for name in ISHARES_FIELDS:
        value = (points.get(name) or {}).get("formattedValue")
        columns[name] = value if isinstance(value, list) else None

    if columns["holdingPercent"] is None or columns["issueName"] is None:
        raise BasketParseError(
            "ishares: the response carries no weight or name array, so it is not a holdings "
            "payload"
        )

    lengths = {name: len(col) for name, col in columns.items() if col is not None}
    if len(set(lengths.values())) != 1:
        raise BasketParseError(
            f"ishares: parallel arrays disagree in length ({lengths}) — zipping them would "
            f"attach constituents to the wrong weights. Refusing the whole file"
        )
    count = next(iter(lengths.values()))

    def at(name: str, index: int):
        column = columns[name]
        return column[index] if column is not None else None

    rows: List[ConstituentRow] = []
    for index in range(count):
        row_isin = _isin(at("isin", index))
        row_ticker = _clean(at("ticker", index))
        rows.append(ConstituentRow(
            line_no=index + 1,
            name=_label(at("issueName", index), row_isin, row_ticker),
            weight_pct=_decimal(at("holdingPercent", index), f"ishares row {index + 1} weight"),
            isin=row_isin,
            ticker=row_ticker,
            asset_class=_clean(at("assetClass", index)),
            sector=_clean(at("sectorName", index)),
            country=_clean(at("countryOfRisk", index)),
        ))

    basket = ParsedBasket(
        fund_isin=fund_isin.strip().upper(),
        as_of_date=_ishares_as_of(points),
        source="blackrock", adapter="blackrock",
        rows=rows, source_rows=count,
        asset_class_available=columns["assetClass"] is not None,
    )
    basket.validate()
    return basket


def _ishares_as_of(points) -> date:
    """
    iShares stamps its own as-of, in `13/Aug/2026` form.

    Refused rather than defaulted when absent: this issuer *does* publish a date, so its
    absence means the payload is not what we think it is, and standing the fetch date in would
    report a basket of unknown vintage as today's.
    """
    raw = (points.get("asOfDate") or {}).get("formattedValue")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    text = _clean(raw)
    if not text:
        raise BasketParseError("ishares: the response carries no asOfDate")
    for pattern in ("%d/%b/%Y", "%Y-%m-%d", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise BasketParseError(f"ishares: cannot read asOfDate {text!r}")


# ------------------------------------------------------------------------- Vanguard (US)

def parse_vanguard_us(bodies: Sequence[bytes], fund_isin: str) -> ParsedBasket:
    """
    Vanguard's US profile API, one body per 500-row page.

    Pages are concatenated in the order given, and the count the API declares is checked
    against the rows actually assembled — a missing page is exactly the shape that would make
    a fund look like it holds only its largest names, and the weights would still sum
    plausibly.

    **The pages must also agree with each OTHER about that count, and on 2026-08-17 they did
    not.** Vanguard serves this endpoint from a cluster whose nodes can hold different
    snapshots, so a 21-request walk can be answered partly from one and partly from another:
    VT's pages came back 13 saying `size` 10,055 and 8 saying 10,032, twice in a row. The
    damage is far worse than the 23-row difference implies, because ~8,000 of VT's holdings
    have a weight of 0.00% and therefore no stable sort order between snapshots — any page
    boundary that crosses one duplicates a chunk and drops another. Measured: 10,032 rows
    assembled carrying only **9,114 distinct holdings**, against 10,025 in the basket already
    stored. Roughly 900 companies would have silently vanished from a fund that is ~11% of
    this account once VWCE's proxy is counted, and the weights still summed to 91.60%.

    So a `size` that varies across pages is refused as its own fault, named as what it is.
    That check is exact rather than a threshold, and it is the one that matters: the
    row-count check above caught this only because the two snapshots happened to disagree on
    the total. Two snapshots with equal totals and different membership would still get
    through, which is recorded here rather than guarded, because nothing has been observed to
    calibrate a duplicate-rate rule against — the legitimate duplicate rate is 7 rows in
    10,032 (dual listings such as BAM and BEPC), the torn one was 918.

    Pagination is unavoidable: `count` above 500 is not merely capped, it returns a non-JSON
    body, so the whole basket cannot be taken in one request. Retrying is the operator's move
    and it is not automatic — a cluster that stays split would spend 21 requests per attempt
    forever, and the previous basket is kept meanwhile.

    Its as-of is a month end and normally lags ~6 weeks. That is Vanguard's publishing
    cadence, not staleness, and must not be treated as a fault.
    """
    if not bodies:
        raise BasketParseError("vanguard_us: no pages supplied")

    declared_sizes: Dict[int, List[int]] = {}
    as_of: Optional[date] = None
    rows: List[ConstituentRow] = []

    for page_index, body in enumerate(bodies, start=1):
        _refuse_if_not_data(body, "vanguard_us")
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise BasketParseError(f"vanguard_us: page {page_index} is not JSON: {e}")

        if isinstance(payload.get("size"), int):
            declared_sizes.setdefault(payload["size"], []).append(page_index)
        if as_of is None:
            as_of = _vanguard_as_of(payload)

        entities = ((payload.get("fund") or {}).get("entity")) or []
        if not isinstance(entities, list):
            raise BasketParseError(f"vanguard_us: page {page_index} has no holdings array")
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            row_isin = _isin(entity.get("isin"))
            row_ticker = _clean(entity.get("ticker"))
            rows.append(ConstituentRow(
                line_no=len(rows) + 1,
                name=_label(
                    entity.get("longName") or entity.get("shortName"), row_isin, row_ticker
                ),
                weight_pct=_decimal(
                    entity.get("percentWeight"), f"vanguard_us row {len(rows) + 1} weight"
                ),
                isin=row_isin,
                ticker=row_ticker,
                # The endpoint returns stock holdings only, so there is no asset class to
                # read and the remainder of the fund is genuinely non-equity.
                asset_class=None,
            ))

    if as_of is None:
        raise BasketParseError("vanguard_us: no asOfDate in any page")
    if len(declared_sizes) > 1:
        spread = ", ".join(
            f"{size} on {len(pages)} page(s)"
            for size, pages in sorted(declared_sizes.items())
        )
        raise BasketParseError(
            f"vanguard_us: the pages disagree about how many holdings the fund has "
            f"({spread}), so they are not one snapshot — Vanguard answered this walk from "
            f"nodes holding different data, and offsets that shift between them duplicate "
            f"some holdings while dropping others. Re-fetch; the stored basket is kept."
        )
    declared_size = next(iter(declared_sizes), None)
    if declared_size is not None and declared_size != len(rows):
        raise BasketParseError(
            f"vanguard_us: the API declares {declared_size} holdings but {len(rows)} were "
            f"assembled — a page is missing, and the weights would still look plausible"
        )

    basket = ParsedBasket(
        fund_isin=fund_isin.strip().upper(), as_of_date=as_of,
        source="vanguard_us", adapter="vanguard_us",
        rows=rows, source_rows=len(rows), asset_class_available=False,
    )
    basket.validate()
    return basket


def _vanguard_as_of(payload) -> Optional[date]:
    raw = _clean(payload.get("asOfDate"))
    if not raw:
        return None
    # '2026-06-30T00:00:00-04:00'
    head = raw.split("T")[0]
    try:
        return date.fromisoformat(head)
    except ValueError:
        raise BasketParseError(f"vanguard_us: cannot read asOfDate {raw!r}")


# ------------------------------------------------------------------------------ Invesco

# What Invesco's `securityTypeName` means in the vocabulary `counts_as_invested` speaks.
# Translated at the adapter boundary rather than by widening `INVESTED_ASSET_CLASSES`: the
# whitelist there is the shared rule, and every issuer that invents a new label would otherwise
# have to be admitted to it. Measured on the live SOXQ basket, which carries six distinct values.
INVESCO_ASSET_CLASSES = {
    "common stock": "Equity",
    "american depository receipt": "Equity",
    "american depository receipt - ny": "Equity",
    "preferred stock": "Preferred",
    "currency": "Cash",
    "uninvestible cash": "Cash",
    "cash": "Cash",
}


def _invesco_asset_class(raw: Optional[str]) -> Optional[str]:
    """
    Invesco's own type, mapped to the shared vocabulary — and deliberately not exhaustive.

    A value nobody has mapped is returned verbatim, which matters for the one that must keep
    working without an entry: `Money Market Fund, Taxable` contains "fund", so
    `counts_as_invested` routes it to the nested-fund bucket on its own. Coercing unmapped
    values to "Equity" would turn that into a top-50 company.
    """
    text = (raw or "").strip()
    return INVESCO_ASSET_CLASSES.get(text.lower(), text or None)


def parse_invesco(body: bytes, fund_isin: str) -> ParsedBasket:
    """
    Invesco's keyless holdings JSON, keyed by the fund's own CUSIP.

    Two properties make this the least fragile adapter here: the endpoint takes an identifier
    derivable from the fund ISIN, so there is no hand-discovered id to keep in step the way
    BlackRock's `portfolio_id` needs, and it echoes that CUSIP back so a redirect serving
    another fund is caught on the response rather than assumed away.

    Constituents carry a CUSIP and no ISIN, so the ISIN is derived where the identifier is
    North American and left absent where it is a CINS — see `app/services/cusip.py` for why
    prefixing a CINS with `US` is the dangerous option.
    """
    _refuse_if_not_data(body, "invesco")
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise BasketParseError(f"invesco: response is not JSON ({exc})")

    holdings = payload.get("holdings")
    if not isinstance(holdings, list):
        raise BasketParseError("invesco: payload carries no 'holdings' list")

    wanted = fund_isin.strip().upper()
    echoed = (payload.get("cusip") or "").strip().upper()
    if echoed and derive_north_american_isin(echoed) not in (None, wanted):
        raise BasketParseError(
            f"invesco: the response is for CUSIP {echoed}, not for the requested {wanted} — "
            f"the endpoint served a different fund"
        )

    declared = payload.get("totalNumberOfHoldings")
    if isinstance(declared, int) and declared != len(holdings):
        raise BasketParseError(
            f"invesco: the payload declares {declared} holdings but carries {len(holdings)} — "
            f"a truncated response, whose weights would still sum plausibly"
        )

    as_of = _invesco_as_of(payload)
    rows: List[ConstituentRow] = []
    skipped = 0
    for index, holding in enumerate(holdings, start=1):
        raw_weight = holding.get("percentageOfTotalNetAssets")
        if raw_weight is None:
            # Measured: `USD Pending Dividends` carries a null weight and no CUSIP. A row the
            # issuer gave no weight contributes nothing and is counted rather than guessed at.
            skipped += 1
            continue
        asset_class = _invesco_asset_class(holding.get("securityTypeName"))
        row_isin, unresolved = _resolve_identifier(
            holding.get("cusip"), is_security=counts_as_invested(asset_class, True)
        )
        # `issuerName` arrives HTML-escaped ("Invesco Government &amp; Agency Portfolio"),
        # which is the only place in these four sources that happens.
        rows.append(ConstituentRow(
            line_no=index,
            name=_label(unescape(_clean(holding.get("issuerName")) or ""), row_isin,
                        holding.get("ticker")),
            weight_pct=_decimal(raw_weight, f"invesco row {index} weight"),
            isin=row_isin,
            identifier=unresolved,
            ticker=_clean(holding.get("ticker")),
            asset_class=asset_class,
        ))

    basket = ParsedBasket(
        fund_isin=wanted, as_of_date=as_of, source="invesco", adapter="invesco",
        rows=rows, source_rows=len(holdings), skipped_rows=skipped,
        as_of_is_issuer_stated=True, asset_class_available=True,
    )
    basket.validate()
    return basket


def _invesco_as_of(payload) -> date:
    """
    `effectiveBusinessDate`, not `effectiveDate`.

    They differ — measured 2026-08-14 against 2026-08-15 — and the business date is the one the
    holdings describe. Taking the later of the two would make every basket look a day fresher
    than it is, in the direction a staleness alarm must never err.
    """
    for key in ("effectiveBusinessDate", "effectiveDate"):
        raw = (payload.get(key) or "").strip()
        if raw:
            try:
                return datetime.strptime(raw[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
    raise BasketParseError("invesco: no usable effective date on the payload")


# -------------------------------------------------------------------------- First Trust

FIRST_TRUST_COLUMNS = {
    "name": "Security Name",
    "ticker": "Identifier",
    "cusip": "CUSIP",
    "sector": "Classification",
    "weight": "Weighting",
}


def _first_trust_asset_class(identifier: Optional[str]) -> Optional[str]:
    """
    Read First Trust's cash rows off its ticker convention, since the page states no class.

    `Classification` looks like an asset-class column and is not: it holds an *industry*
    (`Diversified Industrials`, `Electrical Components`). The nine currency lines are marked
    only by a `$`-prefixed ticker — `$GBP`, `$KRW`, `$CHF` — which is unmistakable, since no
    real ticker begins with one. Same shape as `_dws_asset_class`: only the negatives are
    derived, because asserting "Equity" for everything else would be wrong for a bond fund.
    """
    text = (identifier or "").strip()
    return "Cash" if text.startswith("$") else None


def parse_first_trust(body: bytes, fund_isin: str, ticker: str) -> ParsedBasket:
    """
    First Trust's holdings page: a server-rendered HTML table, one row per constituent.

    The identifiers are the reason this adapter is careful. 77 of GRID's 128 rows carry a
    **CINS** rather than a CUSIP — including its three largest holdings, Eaton, Schneider and
    Johnson Controls, all Irish or French issuers — so `US`-prefixing the column wholesale
    would fabricate an identifier for 60% of the fund. Those rows keep the raw value for
    OpenFIGI instead; see `_resolve_identifier`.
    """
    # Emptiness first, so a truncated response is reported as empty rather than as a
    # page with an unhelpful title.
    _refuse_if_empty(body, "first_trust")
    _require_ticker(body, ticker, "first_trust")
    tables = _html_tables(body, "first_trust")
    table = _pick_table(tables, list(FIRST_TRUST_COLUMNS.values()), "first_trust")
    header = table[0]
    at = {key: _column_index(header, name, "first_trust")
          for key, name in FIRST_TRUST_COLUMNS.items()}

    rows: List[ConstituentRow] = []
    skipped = 0
    for index, record in enumerate(table[1:], start=1):
        if len(record) <= max(at.values()):
            skipped += 1
            continue
        raw_weight = _clean(record[at["weight"]])
        if not raw_weight:
            skipped += 1
            continue
        row_ticker = _clean(record[at["ticker"]])
        asset_class = _first_trust_asset_class(row_ticker)
        row_isin, unresolved = _resolve_identifier(
            record[at["cusip"]], is_security=counts_as_invested(asset_class, False)
        )
        sector = _clean(record[at["sector"]])
        rows.append(ConstituentRow(
            line_no=index,
            name=_label(record[at["name"]], row_isin, row_ticker),
            weight_pct=_decimal(raw_weight, f"first_trust row {index} weight"),
            isin=row_isin,
            identifier=unresolved,
            ticker=row_ticker,
            asset_class=asset_class,
            # `Other` is what the page writes for a row it has not classified, so storing it
            # would put a sector on a row that simply has none — the rule `parse_dws` applies
            # to its own literal 'unknown'.
            sector=None if (sector or "").lower() == "other" else sector,
        ))

    basket = ParsedBasket(
        fund_isin=fund_isin.strip().upper(),
        as_of_date=_us_date_after(
            body.decode("utf-8", errors="replace"), "as of", "first_trust"
        ),
        source="first_trust", adapter="first_trust",
        rows=rows, source_rows=len(table) - 1, skipped_rows=skipped,
        # The industry column is not an asset class, so the equity filter genuinely cannot run
        # and the API says so. The cash rows are still excluded, by the convention above.
        asset_class_available=False,
    )
    basket.validate()
    return basket


# ----------------------------------------------------------------------------- Defiance

DEFIANCE_COLUMNS = {
    "ticker": "Ticker",
    "name": "Name",
    "cusip": "CUSIP",
    "weight": "ETF Weight",
}


def _defiance_asset_class(identifier: Optional[str]) -> Optional[str]:
    """Defiance's cash lines are the ones whose identifier column starts with `CASH`."""
    return "Cash" if (identifier or "").strip().upper().startswith("CASH") else None


def parse_defiance(body: bytes, fund_isin: str, ticker: str, fetched_on: date) -> ParsedBasket:
    """
    Defiance's full-holdings page — note `/{ticker}-full-holdings/`, not `/{ticker}/`, whose
    table is rendered client-side and absent from the response.

    **Its column headed "CUSIP" holds three different things.** Measured on QTUM's 89 rows:
    68 nine-character CUSIPs and CINS, **20 SEDOLs** (`B056381` Global Unichip, `6640400` NEC),
    and the literal `Cash&Other`. `_resolve_identifier` tells them apart by shape and check
    digit rather than by the heading.

    **And its stated date is an effective date, not a portfolio date.** `Data as of 08/17/2026`
    on a file fetched on the 16th, describing Friday the 14th's close — the same T+1 convention
    Invesco makes explicit by publishing `effectiveDate` beside `effectiveBusinessDate`. A
    future as-of would make a stale basket look fresh, and staleness must only ever err old, so
    it is clamped to the fetch date and `as_of_is_issuer_stated` says the date is ours.
    """
    # Emptiness first, so a truncated response is reported as empty rather than as a
    # page with an unhelpful title.
    _refuse_if_empty(body, "defiance")
    _require_ticker(body, ticker, "defiance")
    tables = _html_tables(body, "defiance")
    table = _pick_table(tables, list(DEFIANCE_COLUMNS.values()), "defiance")
    header = table[0]
    at = {key: _column_index(header, name, "defiance")
          for key, name in DEFIANCE_COLUMNS.items()}

    rows: List[ConstituentRow] = []
    skipped = 0
    for index, record in enumerate(table[1:], start=1):
        if len(record) <= max(at.values()):
            skipped += 1
            continue
        raw_weight = _clean(record[at["weight"]])
        if not raw_weight:
            skipped += 1
            continue
        raw_id = _clean(record[at["cusip"]])
        asset_class = _defiance_asset_class(raw_id)
        row_isin, unresolved = _resolve_identifier(
            raw_id, is_security=counts_as_invested(asset_class, False)
        )
        rows.append(ConstituentRow(
            line_no=index,
            name=_label(record[at["name"]], row_isin, record[at["ticker"]]),
            weight_pct=_decimal(raw_weight, f"defiance row {index} weight"),
            isin=row_isin,
            identifier=unresolved,
            ticker=_clean(record[at["ticker"]]),
            asset_class=asset_class,
        ))

    stated = _us_date_after(
        body.decode("utf-8", errors="replace"), "Data as of", "defiance"
    )
    basket = ParsedBasket(
        fund_isin=fund_isin.strip().upper(),
        as_of_date=min(stated, fetched_on),
        source="defiance", adapter="defiance",
        rows=rows, source_rows=len(table) - 1, skipped_rows=skipped,
        as_of_is_issuer_stated=stated <= fetched_on,
        asset_class_available=False,
    )
    basket.validate()
    return basket


# ------------------------------------------------------------------------------- VanEck

XLSX_MAGIC = b"PK\x03\x04"
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

VANECK_COLUMNS = {
    "name": "Holding Name",
    "ticker": "Ticker",
    "isin": "ISIN",
    "weight": "% of Net Assets",
}
# What VanEck writes in a cell that has no value — on the `Other/Cash` line, in every column.
VANECK_BLANK = "--"


def _xlsx_rows(body: bytes) -> List[List[str]]:
    """
    An XLSX's first worksheet as rows of strings, using only the standard library.

    `zipfile` + `ElementTree` rather than `openpyxl`, which would be a new dependency in the
    deploy path for one 5 kB file a week. Cells are placed by their `r` reference rather than
    by document order, because a sheet may omit an empty cell entirely and appending in order
    would then shift every later column of that row into the wrong heading.
    """
    if not body.startswith(XLSX_MAGIC):
        raise BasketParseError(
            "vaneck: the response is not a zip archive, so it is not an XLSX — the download "
            "most likely returned the consent page instead of the file"
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
        shared = [
            "".join(node.text or "" for node in item.iter(_XLSX_NS + "t"))
            for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))
        ] if "xl/sharedStrings.xml" in archive.namelist() else []
        sheets = sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/sheet"))
        if not sheets:
            raise BasketParseError("vaneck: the workbook has no worksheet")
        sheet = ET.fromstring(archive.read(sheets[0]))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise BasketParseError(f"vaneck: cannot read the workbook ({exc})")

    out: List[List[str]] = []
    for row in sheet.iter(_XLSX_NS + "row"):
        cells: Dict[int, str] = {}
        for cell in row.findall(_XLSX_NS + "c"):
            index = _xlsx_column_index(cell.get("r") or "")
            kind = cell.get("t")
            if kind == "inlineStr":
                node = cell.find(_XLSX_NS + "is")
                text = "".join(t.text or "" for t in node.iter(_XLSX_NS + "t")) if node is not None else ""
            else:
                value = cell.find(_XLSX_NS + "v")
                text = value.text or "" if value is not None else ""
                if kind == "s" and text:
                    try:
                        text = shared[int(text)]
                    except (ValueError, IndexError):
                        raise BasketParseError(
                            f"vaneck: cell {cell.get('r')} references shared string {text}, "
                            f"which the workbook does not contain"
                        )
            cells[index] = text.strip()
        values = [cells.get(i, "") for i in range(max(cells) + 1)] if cells else []
        # A wholly-blank row is layout, not data — the workbook ends with one, and counting it
        # as `skipped_rows` would report a spacer as a row we failed to parse.
        if any(v for v in values):
            out.append(values)
    return out


def _xlsx_column_index(ref: str) -> int:
    """`D4` -> 3. Zero-based, so it indexes a list directly."""
    letters = "".join(c for c in ref if c.isalpha()).upper()
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return max(index - 1, 0)


def parse_vaneck(body: bytes, fund_isin: str) -> ParsedBasket:
    """
    VanEck's holdings workbook — the only one of these sources that publishes real ISINs.

    That makes it the least interesting adapter and the most valuable basket: every row folds
    against a direct holding without a provider round-trip, including the ASML NY registry line
    (`USN070592100`) whose CINS the other three would have had to send to OpenFIGI.

    The header is not on the first row — row 1 carries `All Holdings  08/14/2026`, which is also
    where the as-of comes from — so both are found by scanning rather than by position.
    """
    rows_in = _xlsx_rows(body)
    header_at = next(
        (i for i, row in enumerate(rows_in)
         if all(any(c.strip().lower() == want.lower() for c in row)
                for want in VANECK_COLUMNS.values())),
        None,
    )
    if header_at is None:
        raise BasketParseError(
            f"vaneck: no row carries the columns {list(VANECK_COLUMNS.values())} — the "
            f"workbook layout changed"
        )
    header = rows_in[header_at]
    at = {key: _column_index(header, name, "vaneck") for key, name in VANECK_COLUMNS.items()}

    as_of = _us_date_after(
        " ".join(" ".join(r) for r in rows_in[:header_at]), "All Holdings", "vaneck"
    )

    rows: List[ConstituentRow] = []
    skipped = 0
    for index, record in enumerate(rows_in[header_at + 1:], start=1):
        if len(record) <= max(at.values()):
            skipped += 1
            continue
        raw_weight = _clean(record[at["weight"]])
        if not raw_weight or raw_weight == VANECK_BLANK:
            skipped += 1
            continue
        raw_ticker = _clean(record[at["ticker"]])
        # The `Other/Cash` line writes `--` in every identifier column, which is the workbook's
        # own way of saying the row is not a security. Nothing else in the file does that.
        is_security = raw_ticker != VANECK_BLANK
        row_isin, unresolved = _resolve_identifier(record[at["isin"]], is_security=is_security)
        rows.append(ConstituentRow(
            line_no=index,
            name=_label(record[at["name"]], row_isin, raw_ticker),
            weight_pct=_decimal(raw_weight, f"vaneck row {index} weight"),
            isin=row_isin,
            identifier=unresolved,
            # `AMD US` is a Bloomberg ticker; the exchange half is dropped so it reads like
            # every other ticker in this table.
            ticker=(raw_ticker.split()[0] if raw_ticker and is_security else None),
            asset_class=None if is_security else "Cash",
        ))

    basket = ParsedBasket(
        fund_isin=fund_isin.strip().upper(), as_of_date=as_of,
        source="vaneck", adapter="vaneck",
        rows=rows, source_rows=max(len(rows_in) - header_at - 1, 0), skipped_rows=skipped,
        asset_class_available=False,
    )
    basket.validate()
    return basket


PARSERS = {
    "dws": parse_dws,
    "blackrock": parse_ishares,
    "vanguard_us": parse_vanguard_us,
    "invesco": parse_invesco,
    "first_trust": parse_first_trust,
    "defiance": parse_defiance,
    "vaneck": parse_vaneck,
}
