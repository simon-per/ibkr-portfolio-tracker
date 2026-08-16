"""
The four US issuer adapters, and the traps their real files sprang on 2026-08-16.

Separate from `test_etf_basket_import.py` because these four share a problem the first three
do not have: **none of them publishes an ISIN on every row**, and the identifier column they
publish instead holds three different things. Everything here was measured on the live file
named in each test rather than imagined, but the bodies are synthesised rather than committed —
an issuer's basket is its own regulatory disclosure and this repository is public, so the
fixtures carry the *shapes* plus a handful of public identifiers and nothing more.

Offline: no network, no database.
"""
import io
import json
import zipfile
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.etf_basket_parsers import (
    BasketParseError,
    parse_defiance,
    parse_first_trust,
    parse_invesco,
    parse_vaneck,
)

SOXQ = "US46138G6153"      # its own CUSIP is 46138G615
GRID = "US33737A1088"
QTUM = "US26922A4206"
SMH = "IE00BMC38736"

FETCHED = date(2026, 8, 16)


# --------------------------------------------------------------------------------- Invesco

def invesco_json(holdings, cusip="46138G615", business="2026-08-14", effective="2026-08-15",
                 declared=None) -> bytes:
    payload = {
        "cusip": cusip,
        "effectiveDate": effective,
        "effectiveBusinessDate": business,
        "totalNumberOfHoldings": len(holdings) if declared is None else declared,
        "holdings": holdings,
    }
    return json.dumps(payload).encode()


def invesco_row(ticker, name, weight, cusip, kind="Common Stock"):
    return {
        "ticker": ticker, "issuerName": name, "percentageOfTotalNetAssets": weight,
        "cusip": cusip, "securityTypeName": kind, "units": 1, "currency": "USD",
    }


def test_invesco_derives_isins_from_cusips_and_prefers_the_business_date():
    basket = parse_invesco(invesco_json([
        invesco_row("NVDA", "NVIDIA Corp", 60.0, "67066G104"),
        invesco_row("AVGO", "Broadcom Inc", 40.0, "11135F101"),
    ]), SOXQ)

    assert [r.isin for r in basket.rows] == ["US67066G1040", "US11135F1012"]
    # `effectiveDate` is a day later than `effectiveBusinessDate` on every real payload, and
    # the business date is the one the holdings describe. Taking the later would make every
    # basket look a day fresher than it is.
    assert basket.as_of_date == date(2026, 8, 14)
    assert basket.as_of_is_issuer_stated is True


def test_invesco_leaves_a_cins_unconverted_and_carries_it_forward():
    """
    ASML's line in SOXQ is `N07059210` — a CINS, not a CUSIP. Prefixing it with `US` happens to
    give the right answer for a NY-registry ADR and the wrong one for Eaton or Schneider, and
    nothing in the file says which case it is. So it is kept raw for OpenFIGI's `ID_CUSIP`.
    """
    basket = parse_invesco(invesco_json([
        invesco_row("NVDA", "NVIDIA Corp", 60.0, "67066G104"),
        invesco_row("ASML", "ASML Holding NV", 40.0, "N07059210",
                    kind="American Depository Receipt - NY"),
    ]), SOXQ)

    asml = basket.rows[1]
    assert asml.isin is None
    assert asml.identifier == "N07059210"
    assert asml.asset_class == "Equity"
    # The ISIN it would have had is not invented, so it does not count toward coverage.
    assert basket.identifier_coverage_pct == Decimal("60")


def test_invesco_routes_a_money_market_holding_to_the_nested_fund_bucket():
    """
    `Money Market Fund, Taxable` is deliberately absent from `INVESCO_ASSET_CLASSES`. Coercing
    an unmapped value to "Equity" would make AGPXX a top-50 *company*; passing it through lets
    `counts_as_invested`'s "fund" marker catch it, which is the bucket it belongs in.
    """
    basket = parse_invesco(invesco_json([
        invesco_row("NVDA", "NVIDIA Corp", 99.0, "67066G104"),
        invesco_row("AGPXX", "Invesco Government &amp; Agency Portfolio", 1.0, "825252885",
                    kind="Money Market Fund, Taxable"),
    ]), SOXQ)

    assert basket.rows[1].asset_class == "Money Market Fund, Taxable"
    # Still invested — it is a holding in a security — so it is not swept into the residual.
    assert basket.equity_weight_pct == Decimal("100")
    # And the issuer HTML-escapes its names, which no other source here does.
    assert basket.rows[1].name == "Invesco Government & Agency Portfolio"


def test_invesco_skips_a_null_weight_row_and_counts_it():
    """`USD Pending Dividends` really does arrive with a null weight and no CUSIP."""
    basket = parse_invesco(invesco_json([
        invesco_row("NVDA", "NVIDIA Corp", 60.0, "67066G104"),
        invesco_row("MU", "Micron Technology Inc", 40.0, "595112103"),
        invesco_row("USDPDV", "USD Pending Dividends", None, None, kind="Currency"),
    ]), SOXQ)

    assert len(basket.rows) == 2
    assert basket.skipped_rows == 1
    assert basket.source_rows == 3


def test_invesco_refuses_a_response_for_another_fund():
    with pytest.raises(BasketParseError, match="served a different fund"):
        parse_invesco(invesco_json([
            invesco_row("NVDA", "NVIDIA Corp", 100.0, "67066G104"),
        ], cusip="46138G102"), SOXQ)


def test_invesco_refuses_a_truncated_payload_whose_weights_still_sum_plausibly():
    with pytest.raises(BasketParseError, match="declares 34 holdings"):
        parse_invesco(invesco_json([
            invesco_row("NVDA", "NVIDIA Corp", 60.0, "67066G104"),
            invesco_row("MU", "Micron Technology Inc", 40.0, "595112103"),
        ], declared=34), SOXQ)


def test_invesco_does_not_ask_a_provider_about_a_cash_placeholder():
    """
    `CASHUSD00` has the exact shape of a CINS and is not an identifier at all. Shape alone
    cannot tell them apart, so the parser uses what the issuer already stated about the row.
    """
    basket = parse_invesco(invesco_json([
        invesco_row("NVDA", "NVIDIA Corp", 100.0, "67066G104"),
        invesco_row("USD", "CASH &amp; EQUIVALENTS", 0.0, "CASHUSD00", kind="Currency"),
        invesco_row(None, "SECURITIES LENDING - BNYM", 0.0, "BNYMLEND",
                    kind="Uninvestible Cash"),
    ]), SOXQ)

    assert [r.identifier for r in basket.rows] == [None, None, None]
    assert [r.isin for r in basket.rows] == ["US67066G1040", None, None]


# ----------------------------------------------------------------------------- First Trust

FIRST_TRUST_HEADER = ("Security Name", "Identifier", "CUSIP", "Classification",
                      "Shares / Quantity", "Market Value", "Weighting")


def first_trust_html(rows, ticker="GRID", as_of="8/14/2026", header=FIRST_TRUST_HEADER) -> bytes:
    def cells(values, tag="td"):
        return "".join(f"<{tag}>{v}</{tag}>" for v in values)

    body = "".join(f"<tr>{cells(r)}</tr>" for r in rows)
    # The real page wraps the holdings table in layout tables and puts a *different* table
    # first, which is why `_pick_table` matches on headers rather than taking table[0].
    return f"""<!DOCTYPE html><html><head>
      <title>First Trust Something Index Fund ({ticker})</title></head><body>
      <table><tr><th>Fund Summary</th><th>Holdings</th></tr>
        <tr><td>nav</td><td>bar</td></tr></table>
      <table><tr><td>Holdings as of {as_of}</td></tr>
        <table><tr>{cells(header, 'th')}</tr>{body}</table>
      </table>
    </body></html>""".encode()


def test_first_trust_reads_the_holdings_table_rather_than_the_first_one():
    basket = parse_first_trust(first_trust_html([
        ("Eaton Corporation Plc", "ETN", "G29183103", "Diversified Industrials",
         "2,462,353", "$1,111,777,003.03", "60.00%"),
        ("Nvidia Corp", "NVDA", "67066G104", "Semiconductors",
         "1,000", "$1,000.00", "40.00%"),
    ]), GRID, "GRID")

    assert len(basket.rows) == 2
    assert basket.as_of_date == date(2026, 8, 14)
    assert [r.name for r in basket.rows] == ["Eaton Corporation Plc", "Nvidia Corp"]


def test_first_trust_keeps_a_cins_raw_and_derives_only_the_digit_leading_one():
    """
    77 of GRID's 128 rows are CINS, including its three largest holdings — Eaton, Schneider and
    Johnson Controls, all Irish or French issuers listed in New York. `US`-prefixing the column
    wholesale would fabricate an identifier for 60% of the fund, each one check-digit-valid.
    """
    basket = parse_first_trust(first_trust_html([
        ("Eaton Corporation Plc", "ETN", "G29183103", "Diversified Industrials",
         "1", "$1", "50.00%"),
        ("Schneider Electric SE", "SU.FP", "F86921107", "Electrical Components",
         "1", "$1", "30.00%"),
        ("Nvidia Corp", "NVDA", "67066G104", "Semiconductors", "1", "$1", "20.00%"),
    ]), GRID, "GRID")

    assert [r.isin for r in basket.rows] == [None, None, "US67066G1040"]
    assert [r.identifier for r in basket.rows] == ["G29183103", "F86921107", None]
    assert basket.identifier_coverage_pct == Decimal("20")


def test_first_trust_reads_its_currency_rows_off_the_dollar_prefixed_ticker():
    """
    `Classification` looks like an asset-class column and holds an industry. The nine currency
    lines are marked only by a `$`-prefixed ticker, which no real ticker uses.
    """
    basket = parse_first_trust(first_trust_html([
        ("Nvidia Corp", "NVDA", "67066G104", "Semiconductors", "1", "$1", "99.00%"),
        ("US Dollar", "$USD", "", "Other", "251,682", "$340,627.03", "0.74%"),
        ("South Korean Won", "$KRW", "", "Other", "-540,574,937", "($381,524.86)", "0.26%"),
    ]), GRID, "GRID")

    assert [r.asset_class for r in basket.rows] == [None, "Cash", "Cash"]
    assert basket.equity_weight_pct == Decimal("99.00")
    # The filter genuinely cannot run on an industry column, and the API says so — the cash
    # rows are excluded by the convention above, not by a class the issuer published.
    assert basket.asset_class_available is False


def test_first_trust_does_not_store_other_as_a_sector():
    basket = parse_first_trust(first_trust_html([
        ("Nvidia Corp", "NVDA", "67066G104", "Semiconductors", "1", "$1", "99.00%"),
        ("US Dollar", "$USD", "", "Other", "1", "$1", "1.00%"),
    ]), GRID, "GRID")

    assert [r.sector for r in basket.rows] == ["Semiconductors", None]


def test_first_trust_refuses_a_page_for_another_fund():
    body = first_trust_html([
        ("Nvidia Corp", "NVDA", "67066G104", "Semiconductors", "1", "$1", "100.00%"),
    ], ticker="FTXL")
    with pytest.raises(BasketParseError, match="does not name GRID"):
        parse_first_trust(body, GRID, "GRID")


def test_first_trust_refuses_a_page_whose_holdings_table_is_gone():
    body = first_trust_html(
        [("Nvidia Corp", "NVDA", "67066G104", "Semiconductors", "1", "$1", "100.00%")],
        header=("Security Name", "Identifier", "CUSIP", "Sector", "Shares", "Value", "Weight"),
    )
    with pytest.raises(BasketParseError, match="no table on the page"):
        parse_first_trust(body, GRID, "GRID")


# -------------------------------------------------------------------------------- Defiance

def defiance_html(rows, ticker="QTUM", as_of="08/17/2026") -> bytes:
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    # The maturity date inside a holding's own name is the reason the as-of search is anchored.
    return f"""<!DOCTYPE html><html><head>
      <title>{ticker} Full Holdings - Defiance ETFs</title></head><body>
      <p>Something copyrighted 17/02/2022.</p>
      <table>
        <tr><th>Ticker</th><th>Name</th><th>CUSIP</th><th>ETF Weight</th><th>Shares</th></tr>
        {body}
      </table>
      <p>Data as of {as_of}. Fund holdings are subject to change.</p>
    </body></html>""".encode()


def test_defiance_tells_a_cusip_a_cins_and_a_sedol_apart_in_one_column():
    """
    Its column is headed "CUSIP" and holds all three. Measured on QTUM's 89 rows: 68
    nine-character identifiers, 20 SEDOLs and one literal `Cash&Other`. Sending a SEDOL to
    OpenFIGI as `ID_CUSIP` resolves nothing, so the kind has to be read off the shape.
    """
    basket = parse_defiance(defiance_html([
        ("NET", "Cloudflare Inc", "18915M107", "40.00%", "296,018"),
        ("ARQQ", "Arqit Quantum Inc", "G0567U127", "30.00%", "4,967,421"),
        ("3443 TT", "Global Unichip Corp", "B056381", "20.00%", "519,991"),
        ("Cash&Other", "Cash & Other", "Cash&Other", "10.00%", "3,495,084"),
    ]), QTUM, "QTUM", FETCHED)

    assert [r.isin for r in basket.rows] == ["US18915M1071", None, None, None]
    assert [r.identifier for r in basket.rows] == [None, "G0567U127", "B056381", None]
    assert [r.asset_class for r in basket.rows] == [None, None, None, "Cash"]


def test_defiance_clamps_its_forward_dated_as_of_to_the_fetch_date():
    """
    `Data as of 08/17/2026` on a file fetched on the 16th describing Friday the 14th's close:
    the same T+1 effective-date convention Invesco makes explicit. A future as-of would make a
    stale basket look fresh, and staleness must only ever err old.
    """
    basket = parse_defiance(defiance_html([
        ("NET", "Cloudflare Inc", "18915M107", "60.00%", "1"),
        ("HON", "Honeywell Aerospace Inc", "43849R105", "40.00%", "1"),
    ]), QTUM, "QTUM", FETCHED)

    assert basket.as_of_date == FETCHED
    assert basket.as_of_is_issuer_stated is False


def test_defiance_keeps_a_stated_date_that_is_already_in_the_past():
    basket = parse_defiance(defiance_html([
        ("NET", "Cloudflare Inc", "18915M107", "60.00%", "1"),
        ("HON", "Honeywell Aerospace Inc", "43849R105", "40.00%", "1"),
    ], as_of="08/14/2026"), QTUM, "QTUM", FETCHED)

    assert basket.as_of_date == date(2026, 8, 14)
    assert basket.as_of_is_issuer_stated is True


def test_defiance_does_not_date_the_basket_from_a_bond_maturity_in_a_holding_name():
    """
    QTUM's page carries five date-shaped strings — the as-of, `12/01/2031` inside a money-market
    fund's own name, a `17/02/2022` that is not even month-first, and two impossible years. An
    unanchored search took the `17/02/2022` and refused; the maturity would have parsed cleanly
    and dated the basket to 2031, which is the outcome worth pinning against.
    """
    basket = parse_defiance(defiance_html([
        ("FGXXX", "First American Government Obligations Fund 12/01/2031", "31846V336",
         "50.00%", "16,476,452"),
        ("NET", "Cloudflare Inc", "18915M107", "50.00%", "1"),
    ], as_of="08/14/2026"), QTUM, "QTUM", FETCHED)

    assert basket.as_of_date == date(2026, 8, 14)


def test_defiance_refuses_a_client_rendered_page_with_no_table():
    body = b"""<!DOCTYPE html><html><head><title>QTUM - Defiance ETFs</title></head>
      <body><div id="holdings-app"></div><p>Data as of 08/14/2026.</p></body></html>"""
    with pytest.raises(BasketParseError, match="no table on the page"):
        parse_defiance(body, QTUM, "QTUM", FETCHED)


# ---------------------------------------------------------------------------------- VanEck

def vaneck_xlsx(rows, as_of="08/14/2026", header=("Number", "Holding Name", "Ticker", "ISIN",
                                                  "Shares", "Market Value", "% of Net Assets")):
    """
    A minimal but real XLSX: shared strings plus one sheet, exactly the shape VanEck ships.

    Every cell is written as a shared string because that is what the real workbook does, and
    it is what makes the `t="s"` index path the one under test.
    """
    grid = [[f"All Holdings  {as_of}"], [""], list(header)]
    grid.extend([str(c) for c in row] for row in rows)

    strings, index = [], {}
    for row in grid:
        for cell in row:
            if cell not in index:
                index[cell] = len(strings)
                strings.append(cell)

    def letter(i):
        out = ""
        while True:
            out = chr(ord("A") + i % 26) + out
            i = i // 26 - 1
            if i < 0:
                return out

    sheet_rows = "".join(
        f'<row r="{n}">' + "".join(
            f'<c r="{letter(i)}{n}" t="s"><v>{index[c]}</v></c>'
            for i, c in enumerate(row)
        ) + "</row>"
        for n, row in enumerate(grid, start=1)
    )
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {ns}><sheetData>{sheet_rows}</sheetData></worksheet>",
        )
    return buffer.getvalue()


def test_vaneck_reads_published_isins_and_the_as_of_above_the_header():
    """
    The only source here that publishes real ISINs on every row, so every constituent folds
    without a provider round-trip — including ASML's NY registry line, whose CINS the other
    three adapters have to hand to OpenFIGI.
    """
    basket = parse_vaneck(vaneck_xlsx([
        ("1", "Advanced Micro Devices Inc", "AMD US", "US0079031078", "1,931,589",
         "$ 993,590,066.00", "60.00%"),
        ("2", "Asml Holding Nv", "ASML US", "USN070592100", "501,081",
         "$ 924,033,450.00", "40.00%"),
    ]), SMH)

    assert [r.isin for r in basket.rows] == ["US0079031078", "USN070592100"]
    assert basket.identifier_coverage_pct == Decimal("100")
    # Row 1 carries the date and row 3 the header, so neither is found by position.
    assert basket.as_of_date == date(2026, 8, 14)
    # `AMD US` is a Bloomberg ticker; the exchange half is not part of the ticker.
    assert [r.ticker for r in basket.rows] == ["AMD", "ASML"]


def test_vaneck_reads_the_placeholder_row_as_cash_rather_than_a_company():
    basket = parse_vaneck(vaneck_xlsx([
        ("1", "Nvidia Corp", "NVDA US", "US67066G1040", "1", "$ 1.00", "99.00%"),
        ("26", "Other/Cash", " -- ", " -- ", " -- ", "$ 709,142.00", "1.00%"),
    ]), SMH)

    assert [r.asset_class for r in basket.rows] == [None, "Cash"]
    assert basket.rows[1].isin is None
    assert basket.rows[1].identifier is None
    assert basket.equity_weight_pct == Decimal("99.00")


def test_vaneck_does_not_count_the_trailing_blank_row_as_a_parse_failure():
    body = vaneck_xlsx([
        ("1", "Nvidia Corp", "NVDA US", "US67066G1040", "1", "$ 1.00", "60.00%"),
        ("2", "Intel Corp", "INTC US", "US4581401001", "1", "$ 1.00", "40.00%"),
        ("", "", "", "", "", "", ""),
    ])
    basket = parse_vaneck(body, SMH)

    assert len(basket.rows) == 2
    assert basket.skipped_rows == 0
    assert basket.source_rows == 2


def test_vaneck_refuses_a_consent_page_by_its_magic_bytes():
    """
    A cookieless GET loops the geo/consent redirects and eventually hands back HTML. Without
    this the failure is an obscure zip error rather than a named one, and `_refuse_if_not_data`
    cannot help — an XLSX is binary, so its HTML sniff never runs on this adapter.
    """
    with pytest.raises(BasketParseError, match="not a zip archive"):
        parse_vaneck(b"<!DOCTYPE html><html><body>Accept cookies</body></html>", SMH)


def test_vaneck_refuses_a_workbook_whose_columns_moved():
    body = vaneck_xlsx(
        [("1", "Nvidia Corp", "NVDA US", "US67066G1040", "1", "$ 1.00", "100.00%")],
        header=("Number", "Holding Name", "Ticker", "Sedol", "Shares", "Market Value",
                "% of Net Assets"),
    )
    with pytest.raises(BasketParseError, match="no row carries the columns"):
        parse_vaneck(body, SMH)


# ------------------------------------------------------------------- shared across adapters

@pytest.mark.parametrize("adapter,call", [
    ("first_trust", lambda b: parse_first_trust(b, GRID, "GRID")),
    ("defiance", lambda b: parse_defiance(b, QTUM, "QTUM", FETCHED)),
])
def test_an_empty_body_is_refused_rather_than_parsed_as_an_empty_basket(adapter, call):
    with pytest.raises(BasketParseError, match="empty response body"):
        call(b"")


def test_every_new_adapter_declares_a_staleness_expectation():
    """
    A basket whose adapter is missing from `ADAPTER_STALE_DAYS` silently falls back to the
    45-day default, which for a daily-republished feed means the badge cannot fire until six
    weeks after the fetch stopped working — a warning that arrives long after it is useful.
    """
    from app.services.lookthrough_service import ADAPTER_STALE_DAYS, stale_after_days

    for adapter in ("invesco", "first_trust", "defiance", "vaneck"):
        assert adapter in ADAPTER_STALE_DAYS
        assert stale_after_days(adapter) == 7


def test_a_basket_dated_a_week_out_is_stale_for_a_daily_issuer():
    basket = parse_invesco(invesco_json([
        invesco_row("NVDA", "NVIDIA Corp", 100.0, "67066G104"),
        invesco_row("MU", "Micron Technology Inc", 0.0, "595112103"),
    ], business=str(date.today() - timedelta(days=8))), SOXQ)

    from app.services.lookthrough_service import stale_after_days

    assert (date.today() - basket.as_of_date).days > stale_after_days(basket.adapter)
