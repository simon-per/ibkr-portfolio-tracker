"""
A company's own concepts, read from its filings by their labels (2026-11-16, the source
waterfall's third layer).

companyfacts carries the standard taxonomies only, so a figure a company tags under its own
namespace is not there at all: NVDA's capex FY2013-FY2021 ("Purchases of property and
equipment and intangible assets", `nvda:`), CDNS's debt ("Carrying value of debt
outstanding", `cdns:`). The filing's label linkbase names each concept; a label shaped like
a metric (`EXTENSION_LABELS`) makes it a candidate, and the figures prove it: where the
concept and the chain both report a period they must agree, as bridging asks. Only fills.
Synthetic documents, offline.
"""

from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.company_financials import CompanyFinancial, SecCompany, SecFilingRead
from app.services.company_financials_normaliser import normalise
from app.services.company_financials_sync import CompanyFinancialsSync
from app.services.edgar_filing_facts import (
    READER,
    extension_gaps,
    extension_metrics,
    label_linkbase,
    parse_instance,
)
from tests.test_company_financials_sync import FIXTURE, NVDA_CIK, UA, Edgar, _hold, _sync


def _lab(*concepts):
    """A label linkbase: (concept id, standard label) pairs."""
    parts = ['<?xml version="1.0"?><link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
             'xmlns:xlink="http://www.w3.org/1999/xlink"><link:labelLink>']
    for i, (concept, label) in enumerate(concepts):
        parts.append(f'<link:loc xlink:href="x.xsd#{concept}" xlink:label="loc{i}"/>')
        parts.append(f'<link:label xlink:label="lab{i}" '
                     f'xlink:role="http://www.xbrl.org/2003/role/label">{label}</link:label>')
        parts.append(f'<link:label xlink:label="lab{i}" '
                     f'xlink:role="http://www.xbrl.org/2003/role/terseLabel">Terse</link:label>')
        parts.append(f'<link:labelArc xlink:from="loc{i}" xlink:to="lab{i}"/>')
    parts.append("</link:labelLink></link:linkbase>")
    return "".join(parts).encode()


@pytest.mark.parametrize("label, metric", [
    ("Purchases of property and equipment and intangible assets", "capex"),  # NVDA
    ("Capital expenditures", "capex"),
    ("Payments for property, plant and equipment", "capex"),
    ("Carrying Value Of Debt Outstanding", "debt_total"),  # CDNS
    ("Total debt", "debt_total"),
    ("Current portion of long-term debt", "debt_current"),
    ("Long-term debt, net of current portion", "debt_noncurrent"),
    ("Purchases of businesses, net of cash acquired", None),
    ("Proceeds from sale of property and equipment", None),
    ("Remaining Principal Amount Of Debt Outstanding", None),  # CDNS: principal, not carrying
    ("Fair value of long-term debt", None),
    ("Current portion of finance lease liabilities", None),
])
def test_a_label_names_a_metric_or_nothing(label, metric):
    named = extension_metrics(_lab(("abc_Concept", label)))
    assert named.get("abc_Concept", (None,))[0] == metric


def test_standard_concepts_are_never_extensions():
    assert extension_metrics(_lab(("us-gaap_LongTermDebt", "Total debt"))) == {}


def _instance(*facts):
    return "".join([
        '<?xml version="1.0" encoding="utf-8"?>',
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" ',
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" ',
        'xmlns:abc="http://www.example.com/20180128" ',
        'xmlns:us-gaap="http://fasb.org/us-gaap/2025" ',
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217">',
        '<xbrli:context id="fy"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>'
        "</xbrli:entity><xbrli:period><xbrli:startDate>2017-01-30</xbrli:startDate>"
        "<xbrli:endDate>2018-01-28</xbrli:endDate></xbrli:period></xbrli:context>",
        '<xbrli:context id="end"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>'
        "</xbrli:entity><xbrli:period><xbrli:instant>2018-01-28</xbrli:instant></xbrli:period>"
        "</xbrli:context>",
        '<xbrli:context id="dim"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>'
        '<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">'
        "abc:OneMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period>"
        "<xbrli:instant>2018-01-28</xbrli:instant></xbrli:period></xbrli:context>",
        '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>',
        *facts,
        "</xbrli:xbrl>",
    ]).encode()


def _fact(concept, context, value):
    return f'<{concept} contextRef="{context}" unitRef="usd" decimals="-6">{value}</{concept}>'


NAMED = {"abc_PurchasesOfPropertyAndEquipment": ("capex", "Purchases of property and equipment"),
         "abc_CarryingValueOfDebt": ("debt_total", "Carrying value of debt outstanding")}


def test_the_instance_keeps_the_named_concepts_undimensioned_and_timed():
    body = _instance(
        _fact("abc:PurchasesOfPropertyAndEquipment", "fy", 593e6),
        _fact("abc:PurchasesOfPropertyAndEquipment", "end", 1),  # a flow on an instant
        _fact("abc:CarryingValueOfDebt", "end", 2e9),
        _fact("abc:CarryingValueOfDebt", "dim", 1e9),  # a segment's: not read here
        _fact("abc:SomethingElse", "end", 5),
    )
    found = [f for f in parse_instance(body, extensions=NAMED) if "metric" in f]
    assert [(f["concept"], f["metric"], f["start"], f["end"], f["val"]) for f in found] == [
        ("abc:CarryingValueOfDebt", "debt_total", None, "2018-01-28", 2e9),
        ("abc:PurchasesOfPropertyAndEquipment", "capex", "2017-01-30", "2018-01-28", 593e6),
    ]
    # Without the linkbase's names, nothing of the company's own.
    assert not [f for f in parse_instance(body) if "metric" in f]


def test_the_label_linkbase_is_found_among_the_files():
    index = {"directory": {"item": [{"name": "abc-20180128.xml"},
                                    {"name": "abc-20180128_lab.xml"}, {"name": "R1.htm"}]}}
    assert label_linkbase(index) == "abc-20180128_lab.xml"
    assert label_linkbase({"directory": {"item": []}}) is None


# ── the normaliser: fill, never replace; agree where both report ─────────────


def _f(start, end, val, filed, form="10-K", accn=None):
    fact = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn or f"a-{filed}"}
    if start:
        fact["start"] = start
    return fact


def _companyfacts(capex=None):
    """Three fiscal years of revenue; capex (standard) for the years given."""
    years = [("2016-01-01", "2016-12-31", "2017-02-20"), ("2017-01-01", "2017-12-31",
             "2018-02-20"), ("2018-01-01", "2018-12-31", "2019-02-20")]
    gaap = {"Revenues": {"units": {"USD": [_f(s, e, 10e9, f) for s, e, f in years]}}}
    if capex:
        gaap["PaymentsToAcquirePropertyPlantAndEquipment"] = {"units": {"USD": [
            _f(s, e, capex[e], f) for s, e, f in years if e in capex]}}
    return {"cik": 1, "entityName": "Example", "facts": {"us-gaap": gaap}}


def _own(end, val, start=None, metric="capex", concept="abc:Purchases"):
    return {"concept": concept, "metric": metric, "unit": "USD", "start": start, "end": end,
            "val": val, "accn": "a-ext", "form": "10-K", "filed": "2019-02-20"}


def _fy(result, metric):
    return {r["period_end"].isoformat(): (r["value"], r["source_concept"])
            for r in result.rows if r["metric"] == metric and r["fiscal_period"] == "FY"}


def test_an_extension_fills_the_years_the_chain_lacks():
    result = normalise(_companyfacts(), [
        _own("2017-12-31", 176e6, "2017-01-01"), _own("2018-12-31", 593e6, "2018-01-01")])
    assert _fy(result, "capex") == {"2017-12-31": (176e6, "abc:Purchases"),
                                    "2018-12-31": (593e6, "abc:Purchases")}
    assert result.extended == {"capex": {"abc:Purchases": 2}}


def test_an_extension_agreeing_where_both_report_fills_and_never_replaces():
    result = normalise(_companyfacts({"2016-12-31": 86e6}), [
        _own("2016-12-31", 86e6, "2016-01-01"), _own("2018-12-31", 593e6, "2018-01-01")])
    assert _fy(result, "capex") == {"2016-12-31": (86e6, "PaymentsToAcquirePropertyPlantAndEquipment"),
                                    "2018-12-31": (593e6, "abc:Purchases")}


def test_an_extension_disagreeing_where_both_report_fills_nothing():
    """A label is a name: a concept 20% off the chain's figure is another line."""
    result = normalise(_companyfacts({"2016-12-31": 86e6}), [
        _own("2016-12-31", 103e6, "2016-01-01"), _own("2018-12-31", 593e6, "2018-01-01")])
    assert _fy(result, "capex") == {"2016-12-31": (86e6, "PaymentsToAcquirePropertyPlantAndEquipment")}
    assert result.extended == {}


def test_a_debt_total_is_proved_against_the_halves_and_never_doubles_them():
    doc = _companyfacts()
    doc["facts"]["us-gaap"].update({
        "LongTermDebtNoncurrent": {"units": {"USD": [_f(None, "2017-12-31", 1.5e9, "2018-02-20")]}},
        "LongTermDebtCurrent": {"units": {"USD": [_f(None, "2017-12-31", 0.5e9, "2018-02-20")]}},
    })
    result = normalise(doc, [_own("2017-12-31", 2e9, metric="debt_total", concept="abc:Debt"),
                             _own("2018-12-31", 2.4e9, metric="debt_total", concept="abc:Debt")])
    totals = _fy(result, "debt_total")
    assert totals == {"2018-12-31": (2.4e9, "abc:Debt")}  # 2017's is the halves already
    wrong = normalise(doc, [_own("2017-12-31", 3e9, metric="debt_total", concept="abc:Debt"),
                            _own("2018-12-31", 2.4e9, metric="debt_total", concept="abc:Debt")])
    assert _fy(wrong, "debt_total") == {}


def test_a_figure_in_another_currency_is_not_read():
    result = normalise(_companyfacts(), [{**_own("2018-12-31", 5e8, "2018-01-01"), "unit": "EUR"}])
    assert _fy(result, "capex") == {}


def test_a_year_missing_capex_or_debt_is_an_extension_gap():
    rows = [{"period_end": date(2018, 12, 31), "fiscal_period": "FY", "metric": "capex",
             "value": 1.0},
            {"period_end": date(2017, 12, 31), "fiscal_period": "FY", "metric": "debt_total",
             "value": 1.0},
            {"period_end": date(2008, 12, 31), "fiscal_period": "FY", "metric": "revenue",
             "value": 1.0}]
    assert extension_gaps(rows) == [(date(2018, 12, 31), "extension:debt"),
                                    (date(2017, 12, 31), "extension:capex")]


# ── the pass: a year's own 10-K, with its label linkbase, once ───────────────


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)


class ExtensionEdgar(Edgar):
    """Every 10-K of the fixture serves an instance and a label linkbase naming capex."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/Archives/edgar/data/" in url:
            self.requests.append(request)
            name = url.rsplit("/", 1)[1]
            if name == "index.json":
                return httpx.Response(200, json={"directory": {"item": [
                    {"name": "abc-20180128.xml"}, {"name": "abc-20180128_lab.xml"}]}})
            if name.endswith("_lab.xml"):
                return httpx.Response(200, content=_lab(NAMED_CAPEX))
            return httpx.Response(200, content=_instance())
        return super().handler(request)


NAMED_CAPEX = ("abc_PurchasesOfPropertyAndEquipment", "Purchases of property and equipment")


@pytest.mark.asyncio
async def test_the_pass_reads_a_years_10k_and_its_labels_once(db, configured, monkeypatch):
    await _hold(db, 1, "NVDA")
    await db.commit()
    # The fixture has no debt either: its segment reads are `test_company_segment_debt`'s.
    monkeypatch.setattr(CompanyFinancialsSync, "_reads_segments", lambda *a: False)
    edgar = ExtensionEdgar()

    first = await _sync(db, edgar).refresh()

    reads = (await db.execute(select(SecFilingRead))).scalars().all()
    assert first["filings_read"] == len(reads) > 0
    assert {r.reader for r in reads} == {READER}
    assert {r.form for r in reads} == {"10-K"}  # a year's own 10-K, nothing else
    labs = [u for u in edgar.urls() if u.endswith("_lab.xml")]
    assert len(labs) == len(reads)  # one label linkbase beside each instance

    # The next pass: every one of those filings is read; nothing is asked again.
    company_rows = (await db.execute(select(CompanyFinancial.cik))).scalars().first()
    assert company_rows == NVDA_CIK
    company = await db.get(SecCompany, NVDA_CIK)
    company.fetched_at -= timedelta(days=7)
    await db.commit()
    asked = len(edgar.requests)
    second = await _sync(db, edgar).refresh()
    archives = [r for r in edgar.requests[asked:] if "/Archives/" in str(r.url)]
    # Bounded and resumable: what the first pass's cap left is read now, never a filing twice.
    read_twice = {str(r.url) for r in archives} & {u for u in edgar.urls()[:asked]}
    assert not read_twice
    assert second["filings_read"] * 3 == len(archives)
    assert FIXTURE  # the NVDA fixture: years without capex in its 10-Ks' us-gaap facts
