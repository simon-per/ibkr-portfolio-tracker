"""
Taiwan's annual reports from MOPS (docs/company-financials.md, *Taiwan*; 2026-11-19): Inline
XBRL read into the ESEF converter's shape (`ixbrl.parse`), each year's fourth-quarter report
read once, a year no form answers stamped once the next year is out. Hon Hai's FY2024 report
states revenue of NT$6,859,615,493 thousand and a diluted EPS of 10.79. Offline: a fake MOPS.
"""

from datetime import date

import httpx
import pytest

from app.models.company_financials import CompanyReport
from app.services import twse_sync
from app.services.company_financials_service import CompanyFinancialsService
from app.services.company_markets import NamedCompany
from app.services.esef_reports import convert
from app.services.ixbrl import number, parse
from app.services.twse_sync import TaiwanFinancialsSync, is_report, stock_code


def _report(year: int, revenue_k: str, eps: str) -> bytes:
    """A fourth-quarter report: the year's revenue (thousands, scale 3) and diluted EPS, the
    year before as a comparative, one dimensioned fact and one extension concept."""
    prev = year - 1
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
 xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2015-02-26"
 xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
 xmlns:ifrs-full="http://xbrl.ifrs.org/taxonomy/2017-03-09/ifrs-full"
 xmlns:tifrs-notes="http://www.xbrl.org/tifrs/notes/2018-03-31"><body>
<ix:header><ix:resources>
 <xbrli:context id="Y"><xbrli:entity><xbrli:identifier scheme="http://www.twse.com.tw">2317</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>{year}-01-01</xbrli:startDate><xbrli:endDate>{year}-12-31</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="P"><xbrli:entity><xbrli:identifier scheme="http://www.twse.com.tw">2317</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>{prev}-01-01</xbrli:startDate><xbrli:endDate>{prev}-12-31</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="D"><xbrli:entity><xbrli:identifier scheme="http://www.twse.com.tw">2317</xbrli:identifier>
  <xbrli:segment><xbrldi:explicitMember dimension="ifrs-full:SegmentsAxis">ifrs-full:ReportableSegmentsMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
  <xbrli:period><xbrli:startDate>{year}-01-01</xbrli:startDate><xbrli:endDate>{year}-12-31</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:unit id="TWD"><xbrli:measure>iso4217:TWD</xbrli:measure></xbrli:unit>
 <xbrli:unit id="EPS"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:TWD</xbrli:measure></xbrli:unitNumerator>
  <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
</ix:resources></ix:header>
<p><ix:nonFraction name="ifrs-full:Revenue" contextRef="Y" unitRef="TWD" scale="3" decimals="-3" format="ixt:numdotdecimal">{revenue_k}</ix:nonFraction>
<ix:nonFraction name="ifrs-full:Revenue" contextRef="P" unitRef="TWD" scale="3" decimals="-3" format="ixt:numdotdecimal">1,000</ix:nonFraction>
<ix:nonFraction name="ifrs-full:Revenue" contextRef="D" unitRef="TWD" scale="3" decimals="-3" format="ixt:numdotdecimal">9</ix:nonFraction>
<ix:nonFraction name="ifrs-full:DilutedEarningsLossPerShare" contextRef="Y" unitRef="EPS" scale="0" decimals="2" format="ixt:numdotdecimal">{eps}</ix:nonFraction>
<ix:nonFraction name="ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities" contextRef="Y" unitRef="TWD" scale="3" sign="-" decimals="-3" format="ixt:numdotdecimal">136,339</ix:nonFraction>
<ix:nonFraction name="tifrs-notes:Other" contextRef="Y" unitRef="TWD" scale="3" decimals="-3" format="ixt:numdotdecimal">5</ix:nonFraction>
</p></body></html>""".encode()


MISSING = "<html><body>no such file</body></html>".encode()


def test_inline_xbrl_reads_into_the_esef_shape():
    doc = parse(_report(2024, "6,859,615,493", "10.79"))
    out = convert(doc, "2317", "2024Q4", date(2025, 3, 31))
    by = {(f["concept"], f["end"]): f["val"] for f in out.facts}
    assert by[("Revenue", "2024-12-31")] == 6_859_615_493_000
    assert by[("Revenue", "2023-12-31")] == 1_000_000
    assert by[("DilutedEarningsLossPerShare", "2024-12-31")] == 10.79
    # `sign="-"` is the shown figure negated.
    assert (
        by[
            (
                "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                "2024-12-31",
            )
        ]
        == -136_339_000
    )
    assert (
        out.non_standard == 1
    )  # the extension concept, counted; the segment, not read
    assert len(out.facts) == 4


@pytest.mark.parametrize(
    "shown, fmt, value",
    [
        ("1,234.5", "ixt:numdotdecimal", 1234.5),
        ("1.234,5", "ixt:numcommadecimal", 1234.5),
        ("-", "ixt:numdotdecimal", 0.0),
        ("-", "ixt:fixed-zero", 0.0),
        ("one", "ixt:numdotdecimal", None),
        ("12", "ixt:numwordsen", None),
    ],
)
def test_the_shown_number_in_its_format(shown, fmt, value):
    assert number(shown, fmt) == value


def test_a_taiwan_isin_carries_the_stock_code():
    assert stock_code("TW0002317005") == "2317" and stock_code("US0378331005") is None
    assert is_report(_report(2024, "1", "1")) and not is_report(MISSING)


class Mops:
    """Hon Hai's 2023 and 2024 annual reports under the general company's form; every
    other year and form missing."""

    def __init__(self):
        self.requests = []

    def handler(self, request):
        url = str(request.url)
        self.requests.append(url)
        for year, revenue, eps in (
            (2024, "6,859,615,493", "10.79"),
            (2023, "6,162,221,359", "10.07"),
        ):
            if f"tifrs-fr1-m1-ci-cr-2317-{year}Q4.html" in url:
                return httpx.Response(200, content=_report(year, revenue, eps))
        return httpx.Response(200, content=MISSING)


async def _none(*_):
    return None


@pytest.mark.asyncio
async def test_each_years_report_is_read_once_and_a_missing_year_stamped(
    db, monkeypatch
):
    async def named(_db):
        return [NamedCompany("HON HAI PRECISION INDUSTRY", ["TW0002317005"])]

    monkeypatch.setattr(twse_sync, "named_companies", named)
    monkeypatch.setattr(twse_sync, "utc_today", lambda: date(2025, 6, 1))
    mops = Mops()
    sync = TaiwanFinancialsSync(
        db, transport=httpx.MockTransport(mops.handler), sleep=_none
    )
    summary = await sync.refresh()
    assert summary["reports_read"] == 2 and summary["companies"] == 1
    # 2024 and 2023 on the first form; 2022-2019 tried on every form, then stamped.
    assert summary["requests"] == 2 + 4 * len(twse_sync.FORMS)
    statuses = {
        r.report_id: r.status
        for r in (await db.execute(CompanyReport.__table__.select())).all()
    }
    assert statuses == {
        "2024Q4": "read",
        "2023Q4": "read",
        "2022Q4": "none",
        "2021Q4": "none",
        "2020Q4": "none",
        "2019Q4": "none",
    }

    svc = CompanyFinancialsService(db)
    listed = {c["key"] for c in await svc.list_companies()}
    assert "twse:2317" in listed
    page = await svc.get_financials(
        await svc.company_by_key("twse:2317"), "annual", "max"
    )
    revenue = {p["label"]: p["value"] for p in page["series"]["revenue"]["points"]}
    assert revenue["FY2024"] == 6_859_615_493_000

    # Next pass: nothing asked; the company stays listed.
    mops.requests.clear()
    summary = await sync.refresh()
    assert mops.requests == [] and "twse:2317" in {
        c["key"] for c in await svc.list_companies()
    }

    # A year later the next report is due: asked on the form that answered before, first.
    monkeypatch.setattr(twse_sync, "utc_today", lambda: date(2026, 6, 1))
    summary = await TaiwanFinancialsSync(
        db, transport=httpx.MockTransport(mops.handler), sleep=_none
    ).refresh()
    assert mops.requests[0].count("tifrs-fr1-m1-ci-cr-2317-2025Q4.html") == 1
