"""
An IFRS filer's 20-F read from its own instance (STATUS 7b, 2026-11-16).

NU's and TSMC's FY2025 20-F were filed months before companyfacts carried them, and the
filing's instance could not fill the year: `parse_instance` read the us-gaap namespace
only. Now a whole read takes the `ifrs-full` lines under their US-GAAP twins
(`IFRS_TWINS`), only when the filing has no us-gaap chain fact, and the normaliser takes a
filing's figures only in the currency the company reports in — TSMC translates its latest
year into USD beside the TWD every year is stated in. Synthetic documents, offline.
"""

from app.services.company_financials_normaliser import normalise
from app.services.edgar_filing_facts import parse_instance

FY_START, FY_END = "2025-01-01", "2025-12-31"


def _instance(*facts, gaap=""):
    return "".join([
        '<?xml version="1.0" encoding="utf-8"?>',
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" ',
        'xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full" ',
        'xmlns:us-gaap="http://fasb.org/us-gaap/2025" ',
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217">',
        '<xbrli:context id="fy"><xbrli:entity><xbrli:identifier scheme="x">1'
        f"</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>{FY_START}"
        f"</xbrli:startDate><xbrli:endDate>{FY_END}</xbrli:endDate></xbrli:period>"
        "</xbrli:context>",
        '<xbrli:context id="end"><xbrli:entity><xbrli:identifier scheme="x">1'
        f"</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>{FY_END}"
        "</xbrli:instant></xbrli:period></xbrli:context>",
        '<xbrli:unit id="twd"><xbrli:measure>iso4217:TWD</xbrli:measure></xbrli:unit>',
        '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>',
        *facts,
        gaap,
        "</xbrli:xbrl>",
    ]).encode()


def _ifrs(concept, context, unit, value):
    return (f'<ifrs-full:{concept} contextRef="{context}" unitRef="{unit}" decimals="-6">'
            f"{value}</ifrs-full:{concept}>")


INSTANCE = _instance(
    _ifrs("Revenue", "fy", "twd", 3800e9),
    _ifrs("Revenue", "fy", "usd", 118e9),  # the convenience translation
    _ifrs("ProfitLossAttributableToOwnersOfParent", "fy", "twd", 1700e9),
    _ifrs("CashAndCashEquivalents", "end", "twd", 2500e9),
    _ifrs("Borrowings", "end", "twd", 900e9),  # no exact twin: never read
)


def test_a_whole_read_takes_ifrs_lines_under_their_twins():
    facts = {(f["concept"], f["unit"]): f["val"] for f in parse_instance(INSTANCE, whole=True)}
    assert facts[("Revenues", "TWD")] == 3800e9
    assert facts[("NetIncomeLoss", "TWD")] == 1700e9
    assert facts[("CashAndCashEquivalentsAtCarryingValue", "TWD")] == 2500e9
    assert not any(c == "Borrowings" for c, _ in facts)
    # Only a whole read: the dividend/EPS read looks at us-gaap members alone.
    assert parse_instance(INSTANCE) == []


def test_a_filing_with_us_gaap_chain_facts_is_read_in_us_gaap_alone():
    both = _instance(
        _ifrs("Revenue", "fy", "usd", 5e9),
        gaap='<us-gaap:Revenues contextRef="fy" unitRef="usd" decimals="-6">4e9</us-gaap:Revenues>',
    )
    facts = parse_instance(both, whole=True)
    assert [(f["concept"], f["val"]) for f in facts] == [("Revenues", 4e9)]


def _companyfacts():
    """An IFRS filer whose companyfacts stops at FY2024, in TWD."""
    def row(start, end, val):
        return {"start": start, "end": end, "val": val, "form": "20-F",
                "accn": "a-2025", "filed": "2025-04-15", "fy": 2024, "fp": "FY"}
    return {"cik": 2, "entityName": "Example IFRS", "facts": {"ifrs-full": {
        "Revenue": {"units": {"TWD": [row("2024-01-01", "2024-12-31", 2900e9),
                                     row("2023-01-01", "2023-12-31", 2200e9)]}},
        "ProfitLossAttributableToOwnersOfParent": {"units": {"TWD": [
            row("2024-01-01", "2024-12-31", 1170e9)]}},
    }}}


def test_the_20f_fills_its_year_in_the_reporting_currency_only():
    facts = [{**f, "accn": "a-2026", "form": "20-F", "filed": "2026-04-16"}
             for f in parse_instance(INSTANCE, whole=True)]
    rows = {(r["period_end"].isoformat(), r["metric"]): r
            for r in normalise(_companyfacts(), facts).rows if r["fiscal_period"] == "FY"}
    assert rows[(FY_END, "revenue")]["value"] == 3800e9
    assert rows[(FY_END, "revenue")]["unit"] == "TWD"
    assert rows[(FY_END, "net_income")]["value"] == 1700e9
    assert rows[("2024-12-31", "revenue")]["value"] == 2900e9
