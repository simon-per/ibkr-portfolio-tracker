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

Three shapes, three sets of hazards, all observed on 2026-08-14:

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
"""
import csv
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Sequence

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


def _refuse_if_not_data(body: bytes, adapter: str) -> None:
    """
    The lying-content-type guard.

    The retired iShares holdings URL still answers 200 with `Content-Type: text/csv` and an
    HTML body, so this is checked on the bytes rather than on any header.
    """
    if _HTML_SNIFF.match(body or b""):
        raise BasketParseError(
            f"{adapter}: the response body is a web page, not a holdings file — the endpoint "
            f"has probably moved. Status codes and content types are not evidence here"
        )
    if not (body or b"").strip():
        raise BasketParseError(f"{adapter}: empty response body")


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

    Its as-of is a month end and normally lags ~6 weeks. That is Vanguard's publishing
    cadence, not staleness, and must not be treated as a fault.
    """
    if not bodies:
        raise BasketParseError("vanguard_us: no pages supplied")

    declared_size: Optional[int] = None
    as_of: Optional[date] = None
    rows: List[ConstituentRow] = []

    for page_index, body in enumerate(bodies, start=1):
        _refuse_if_not_data(body, "vanguard_us")
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise BasketParseError(f"vanguard_us: page {page_index} is not JSON: {e}")

        if declared_size is None and isinstance(payload.get("size"), int):
            declared_size = payload["size"]
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


PARSERS = {"dws": parse_dws, "blackrock": parse_ishares, "vanguard_us": parse_vanguard_us}
