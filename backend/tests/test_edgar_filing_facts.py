"""
The figures companyfacts leaves out, read from a filing's XBRL instance
(docs/company-financials.md, *Filing instances*): which facts are kept, which file is the
instance, which filing reports a period, and which quarters are gaps. Offline: the instance
is `tests/fixtures/edgar_dimensioned_dividends.xbrl`, synthetic.
"""

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from app.services.edgar_filing_facts import (
    Filing,
    covers,
    dividend_gaps,
    filings,
    instance_name,
    parse_instance,
    reporting,
    sources,
)

INSTANCE = (
    Path(__file__).parent / "fixtures" / "edgar_dimensioned_dividends.xbrl"
).read_bytes()
DPS = "CommonStockDividendsPerShareDeclared"


def _fact(start, end, val):
    return {
        "concept": DPS,
        "unit": "USD/shares",
        "start": start,
        "end": end,
        "val": val,
    }


def test_only_a_single_whitelisted_us_gaap_member_is_kept():
    """MA tags the column of common stock, PCAR the one of retained earnings; a second
    class, two dimensions, a typed member, a company's own member of the same name, a
    nil, a unit that is not per share and another concept are not read, and an
    undimensioned fact is companyfacts' to give."""
    assert parse_instance(INSTANCE) == [
        _fact("2020-04-01", "2020-06-30", 0.20),  # two columns agreeing: one fact
        _fact("2021-01-01", "2021-06-30", 0.44),
        _fact("2021-04-01", "2021-06-30", 0.22),
    ]


def test_two_members_that_disagree_leave_the_period_out_whole():
    kept = {(f["start"], f["end"]) for f in parse_instance(INSTANCE)}
    assert ("2020-01-01", "2020-06-30") not in kept  # Class A 0.38 against 0.40


def test_a_document_that_is_not_xml_raises():
    with pytest.raises(ET.ParseError):
        parse_instance(b"<html><body>Request Rate Threshold Exceeded")


def _index(*names):
    return {"directory": {"item": [{"name": n, "type": "text.gif"} for n in names]}}


@pytest.mark.parametrize(
    "names, expected",
    [
        # Inline XBRL (2019 on): EDGAR's extracted instance.
        (
            ("ma-20210630.htm", "ma-20210630_htm.xml", "FilingSummary.xml"),
            "ma-20210630_htm.xml",
        ),
        # An older filing: its own instance beside the linkbases.
        (
            (
                "wdc-20140328.xml",
                "wdc-20140328_cal.xml",
                "wdc-20140328_lab.xml",
                "wdc-20140328.xsd",
            ),
            "wdc-20140328.xml",
        ),
        # 2009-2013: rendered reports as XML and a `defnref.xml` beside it.
        (
            (
                "defnref.xml",
                "R1.xml",
                "R2.xml",
                "ma-20090630.xml",
                "ma-20090630_pre.xml",
            ),
            "ma-20090630.xml",
        ),
        # No instance, or no single one: nothing to read.
        (("FilingSummary.xml", "R1.htm"), None),
        (("a-20210630_htm.xml", "b-20210630_htm.xml"), None),
        (("../etc/passwd_htm.xml",), None),
    ],
)
def test_the_instance_is_the_one_file_named_like_one(names, expected):
    assert instance_name(_index(*names)) == expected


def test_no_index_has_no_instance():
    assert instance_name(None) is None and instance_name({"directory": {}}) is None


def _row(start, end, accn, form="10-Q", filed="2021-07-29", val=1.0):
    return {
        "start": start,
        "end": end,
        "accn": accn,
        "form": form,
        "filed": filed,
        "val": val,
    }


def test_a_filings_period_is_the_end_most_of_its_facts_share():
    """A dividend declared after the quarter is tagged as a later duration (a subsequent
    event); the quarter is still the period the filing reports."""
    doc = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _row("2021-04-01", "2021-06-30", "q2"),
                            _row("2021-01-01", "2021-06-30", "q2"),
                            _row("2020-04-01", "2020-06-30", "q2"),
                            _row("2021-07-20", "2021-07-20", "q2"),
                            _row("2021-01-01", "2021-06-30", "dei-only", form="8-K"),
                        ]
                    }
                },
                "Cash": {
                    "units": {
                        "USD": [
                            {
                                "end": "2021-06-30",
                                "accn": "q2",
                                "form": "10-Q",
                                "filed": "2021-07-29",
                                "val": 1,
                            }
                        ]
                    }
                },
            }
        }
    }
    assert filings(doc) == {
        "q2": Filing("q2", "10-Q", date(2021, 7, 29), date(2021, 6, 30))
    }


def test_the_original_reports_a_period_before_its_amendment():
    by_accn = {
        "a": Filing("a", "10-K/A", date(2022, 5, 1), date(2021, 12, 31)),
        "k": Filing("k", "10-K", date(2022, 2, 14), date(2021, 12, 31)),
        "q": Filing("q", "10-Q", date(2021, 10, 28), date(2021, 9, 30)),
    }
    assert reporting(by_accn, date(2021, 12, 31)).accn == "k"
    assert reporting(by_accn, date(2021, 10, 2)).accn == "q"  # a 52/53-week end
    assert reporting(by_accn, date(2021, 6, 30)) is None


def _r(metric, end, value, period="Q2"):
    return {
        "metric": metric,
        "period_end": end,
        "fiscal_period": period,
        "value": value,
    }


def test_a_gap_is_a_quarter_with_dividends_paid_and_no_dividend_per_share():
    rows = [
        _r("dividends_paid", date(2021, 6, 30), 434e6),
        _r("dividends_paid", date(2021, 3, 31), 434e6, "Q1"),
        _r("dividends_per_share", date(2021, 3, 31), 0.44, "Q1"),
        _r("dividends_paid", date(2020, 12, 31), 400e6, "FY"),  # a year is no gap
        _r("dividends_paid", date(2020, 9, 30), 0.0, "Q3"),  # nothing paid
        _r("dividends_paid", date(2020, 6, 30), 390e6),
    ]
    assert dividend_gaps(rows) == [date(2021, 6, 30), date(2020, 6, 30)]


def test_facts_read_cover_the_periods_they_end_on():
    facts = parse_instance(INSTANCE)
    assert covers(facts, date(2021, 6, 30)) and covers(facts, date(2020, 7, 4))
    assert not covers(facts, date(2021, 3, 31))


def test_a_gap_asks_its_own_filing_then_the_ones_that_repeat_it():
    """BKR's 2018 filings tag the dividend on two members (not read); its 2019 10-Qs repeat
    2018's quarters, and the next two 10-Ks a year's figure, on one."""

    def f(accn, form, filed, end):
        return Filing(accn, form, date.fromisoformat(filed), date.fromisoformat(end))

    by_accn = {
        f.accn: f
        for f in (
            f("q3-18", "10-Q", "2018-10-30", "2018-09-30"),
            f("q3-19", "10-Q", "2019-10-30", "2019-09-30"),
            f("q3-20", "10-Q", "2020-10-30", "2020-09-30"),
            f("k18", "10-K", "2019-02-19", "2018-12-29"),
            f("k19", "10-K", "2020-02-13", "2019-12-28"),
            f("k20", "10-K", "2021-02-12", "2020-12-26"),
            f("k21", "10-K", "2022-02-11", "2021-12-25"),
        )
    }
    assert [s.accn for s in sources(by_accn, date(2018, 9, 30))] == ["q3-18", "q3-19"]
    # A fiscal year end, in 52/53-week years: the next two 10-Ks repeat it.
    assert [s.accn for s in sources(by_accn, date(2018, 12, 29))] == ["k18", "k19", "k20"]
    # No filing of its own in companyfacts: the one a year later may still hold it.
    assert [s.accn for s in sources(by_accn, date(2017, 9, 30))] == ["q3-18"]


def test_one_set_of_periodic_forms():
    """
    DR6: the normaliser, the filing reads and the weekday check each decided "is this a
    10-Q/10-K" — the check with `{10-Q, 10-K}`, the others with amendments and transition
    reports. One set now; no module under app/ spells a second one.
    """
    import ast
    import pathlib

    from app.services import company_financials_normaliser, edgar_client, edgar_filing_facts

    assert company_financials_normaliser.FORMS is edgar_client.PERIODIC_FORMS
    assert edgar_filing_facts.FORMS is edgar_client.PERIODIC_FORMS
    assert "10-QT" in edgar_client.PERIODIC_FORMS

    app_dir = pathlib.Path(edgar_client.__file__).resolve().parents[1]
    owners = []
    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
                values = {
                    e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
                if {"10-Q", "10-K"} <= values:
                    owners.append(f"{path.relative_to(app_dir).as_posix()}:{node.lineno}")
    assert owners == [owners[0]] and owners[0].startswith("services/edgar_client.py"), owners
