"""
Japan's EDINET pass and the vendor fallback (docs/company-financials.md, *Japan*, *Vendor
fallback*; owner, 2026-11-20). Both need a key the owner registers and make no request
without one. Built from the documented shapes before a key existed: EDINET's day lists and
report zips, the summary of business results' elements; financialfilings.com's company search
and financials. Offline: fakes of both.
"""

import io
import zipfile
from datetime import date

import httpx
import pytest

from app.config import settings
from app.models.company_financials import Company, CompanyFinancial
from app.services import edinet_sync, vendor_sync
from app.services.company_financials_service import CompanyFinancialsService
from app.services.company_markets import NamedCompany
from app.services.edinet_sync import JapanFinancialsSync, sec_code, summary_facts
from app.services.vendor_sync import VendorFinancialsSync, facts_of

HITACHI = NamedCompany("HITACHI LTD", ["JP3788600009"], ticker="6501")


def _instance() -> bytes:
    """Hitachi's FY2025 summary: two years of IFRS figures, the dividend on its
    non-consolidated context, and a segment's revenue that is not read."""

    def ctx(cid, start, end, member=None):
        seg = (
            '<xbrli:scenario><xbrldi:explicitMember dimension="jppfs_cor:ConsolidatedOrNonConsolidatedAxis">'
            f"jppfs_cor:{member}</xbrldi:explicitMember></xbrli:scenario>"
            if member
            else ""
        )
        period = (
            f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"
            if start
            else f"<xbrli:instant>{end}</xbrli:instant>"
        )
        return (
            f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://disclosure.edinet-fsa.go.jp">E01737</xbrli:identifier></xbrli:entity>'
            f"<xbrli:period>{period}</xbrli:period>{seg}</xbrli:context>"
        )

    ns = "http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:jpcrp_cor="{ns}"
 xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2024-11-01/jppfs_cor">
{ctx("CurrentYearDuration", "2025-04-01", "2026-03-31")}
{ctx("Prior1YearDuration", "2024-04-01", "2025-03-31")}
{ctx("CurrentYearInstant", None, "2026-03-31")}
{ctx("CurrentYearDuration_NonConsolidatedMember", "2025-04-01", "2026-03-31", "NonConsolidatedMember")}
<xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
<xbrli:unit id="JPYPerShares"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
<jpcrp_cor:RevenueIFRSSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPY" decimals="-6">10000000000000</jpcrp_cor:RevenueIFRSSummaryOfBusinessResults>
<jpcrp_cor:RevenueIFRSSummaryOfBusinessResults contextRef="Prior1YearDuration" unitRef="JPY" decimals="-6">9700000000000</jpcrp_cor:RevenueIFRSSummaryOfBusinessResults>
<jpcrp_cor:ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPY" decimals="-6">700000000000</jpcrp_cor:ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults>
<jpcrp_cor:DilutedEarningsLossPerShareIFRSSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">151.00</jpcrp_cor:DilutedEarningsLossPerShareIFRSSummaryOfBusinessResults>
<jpcrp_cor:CashAndCashEquivalentsIFRSSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPY" decimals="-6">800000000000</jpcrp_cor:CashAndCashEquivalentsIFRSSummaryOfBusinessResults>
<jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration_NonConsolidatedMember" unitRef="JPYPerShares" decimals="2">48.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
<jpcrp_cor:NetSalesSummaryOfBusinessResults contextRef="CurrentYearDuration_NonConsolidatedMember" unitRef="JPY" decimals="-6">1</jpcrp_cor:NetSalesSummaryOfBusinessResults>
</xbrli:xbrl>""".encode()


def _zip(instance: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(
            "XBRL/PublicDoc/jpcrp030000-asr-001_E01737-000_2026-03-31_01_2026-06-20.xbrl",
            instance,
        )
    return out.getvalue()


def test_the_summary_of_business_results_becomes_ifrs_facts():
    facts, tagged = summary_facts(_instance(), "S100TEST", date(2026, 6, 20))
    by = {(f["concept"], f["end"]): f["val"] for f in facts}
    assert (
        by[("Revenue", "2026-03-31")] == 1.0e13
        and by[("Revenue", "2025-03-31")] == 9.7e12
    )
    assert by[("DilutedEarningsLossPerShare", "2026-03-31")] == 151.0
    assert by[("CashAndCashEquivalents", "2026-03-31")] == 8.0e11
    # The dividend on its non-consolidated context; no other non-consolidated figure.
    assert (
        by[("DividendsRecognisedAsDistributionsToOwnersPerShare", "2026-03-31")] == 48.0
    )
    assert len([f for f in facts if f["concept"] == "Revenue"]) == 2
    assert tagged["Revenue"] == "edinet:RevenueIFRSSummaryOfBusinessResults"
    assert sec_code(HITACHI) == "65010"


class Edinet:
    def __init__(self):
        self.requests = []

    def handler(self, request):
        self.requests.append(str(request.url))
        assert request.url.params.get("Subscription-Key") == "edinet-test-key"
        if request.url.path.endswith("/documents.json"):
            day = request.url.params["date"]
            results = []
            if day == "2026-06-20":
                results = [
                    {
                        "docID": "S100TEST",
                        "secCode": "65010",
                        "docTypeCode": "120",
                        "xbrlFlag": "1",
                        "withdrawalStatus": "0",
                        "periodEnd": "2026-03-31",
                        "submitDateTime": "2026-06-20 15:00",
                    }
                ]
            return httpx.Response(
                200, json={"metadata": {"status": "200"}, "results": results}
            )
        if request.url.path.endswith("/documents/S100TEST"):
            return httpx.Response(200, content=_zip(_instance()))
        return httpx.Response(404)


async def _none(*_):
    return None


@pytest.mark.asyncio
async def test_without_a_key_the_japanese_pass_asks_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "edinet_api_key", "", raising=False)
    assert await JapanFinancialsSync(db).refresh() is None


@pytest.mark.asyncio
async def test_the_japanese_pass_finds_reads_and_lists_a_report(db, monkeypatch):
    monkeypatch.setattr(settings, "edinet_api_key", "edinet-test-key", raising=False)

    async def named(_db):
        return [HITACHI]

    monkeypatch.setattr(edinet_sync, "named_companies", named)
    monkeypatch.setattr(edinet_sync, "utc_today", lambda: date(2026, 7, 1))
    monkeypatch.setattr(edinet_sync, "FIRST_LOOKBACK_DAYS", 30)
    monkeypatch.setattr(edinet_sync, "HISTORY_WINDOW_DAYS", 1)
    fake = Edinet()
    sync = JapanFinancialsSync(
        db, transport=httpx.MockTransport(fake.handler), sleep=_none
    )
    summary = await sync.refresh()
    assert summary["reports_read"] == 1 and summary["companies"] == 1
    svc = CompanyFinancialsService(db)
    page = await svc.get_financials(
        await svc.company_by_key("edinet:6501"), "annual", "max"
    )
    revenue = {p["label"]: p["value"] for p in page["series"]["revenue"]["points"]}
    assert revenue["FY2026"] == 1.0e13 and revenue["FY2025"] == 9.7e12  # years end in March

    # Next pass: the report is not asked again; only its history's window is listed.
    fake.requests.clear()
    await JapanFinancialsSync(
        db, transport=httpx.MockTransport(fake.handler), sleep=_none
    ).refresh()
    assert not [u for u in fake.requests if "/documents/S100TEST" in u]


# ── the vendor fallback ──────────────────────────────────────────────────────

FINANCIALS = {
    "company_id": 390,
    "currency": {"code": "EUR"},
    "periods": [
        {
            "fiscal_year": 2025,
            "fiscal_period": "FY",
            "period_start_date": "2024-10-01",
            "period_end_date": "2025-09-30",
            "statements": [
                {
                    "statement_type": "IS",
                    "source_filing": {
                        "id": 77,
                        "release_datetime": "2025-11-13T07:00:00Z",
                    },
                    "line_items": [
                        {
                            "code": "revenue",
                            "name": "Total Revenues",
                            "value": "78900000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "net_income_attributable_to_parent",
                            "name": "Net income attributable to shareholders of the parent",
                            "value": "10300000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "eps_diluted",
                            "name": "Diluted EPS",
                            "value": "12.80",
                            "currency": "EUR",
                        },
                    ],
                },
                {
                    "statement_type": "CFS",
                    "source_filing": {
                        "id": 77,
                        "release_datetime": "2025-11-13T07:00:00Z",
                    },
                    "line_items": [
                        {
                            "code": "cash_from_operating_activities",
                            "name": "Cash flows from operating activities",
                            "value": "11000000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "capex",
                            "name": "Capital expenditures",
                            "value": "2600000000.00",
                            "currency": "EUR",
                        },
                    ],
                },
                {
                    "statement_type": "BS",
                    "source_filing": {
                        "id": 77,
                        "release_datetime": "2025-11-13T07:00:00Z",
                    },
                    "line_items": [
                        {
                            "code": "cash_and_equivalents",
                            "name": "Cash and cash equivalents",
                            "value": "9200000000.00",
                            "currency": "EUR",
                        },
                    ],
                },
            ],
        },
        {
            "fiscal_year": 2025,
            "fiscal_period": "H1",
            "period_start_date": "2024-10-01",
            "period_end_date": "2025-03-31",
            "statements": [],
        },
    ],
}


def test_the_vendors_lines_are_mapped_by_code_or_name():
    facts, _ = facts_of(FINANCIALS)
    by = {f["concept"]: f["val"] for f in facts}
    assert by["Revenue"] == 78.9e9
    assert by["ProfitLossAttributableToOwnersOfParent"] == 10.3e9
    assert by["DilutedEarningsLossPerShare"] == 12.8
    assert by["CashFlowsFromUsedInOperatingActivities"] == 11.0e9
    assert (
        by["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"]
        == -2.6e9
    )
    assert by["CashAndCashEquivalents"] == 9.2e9
    assert all(f["end"] == "2025-09-30" for f in facts)  # the half-year is not read


class Vendor:
    def __init__(self, remaining=480):
        self.requests = []
        self.remaining = remaining

    def handler(self, request):
        self.requests.append(str(request.url))
        assert request.headers["X-API-Key"] == "vendor-test-key"
        headers = {"X-RateLimit-Monthly-Remaining": str(self.remaining)}
        if request.url.path == "/companies/":
            if request.url.params.get("isin") == "DE0007236101":
                return httpx.Response(
                    200, json={"results": [{"id": 390}]}, headers=headers
                )
            return httpx.Response(200, json={"results": []}, headers=headers)
        if request.url.path == "/companies/390/financials/":
            self.remaining -= 40
            return httpx.Response(200, json=FINANCIALS, headers=headers)
        return httpx.Response(404, headers=headers)


@pytest.mark.asyncio
async def test_the_vendor_fills_only_what_no_regulator_covers(db, monkeypatch):
    monkeypatch.setattr(
        settings, "financialreports_api_key", "vendor-test-key", raising=False
    )
    siemens = NamedCompany("Siemens Aktiengesellschaft", ["DE0007236101"])
    shell = NamedCompany("Shell plc", ["GB00BP6MXD84"])

    async def named(_db):
        return [siemens, shell]

    monkeypatch.setattr(vendor_sync, "named_companies", named)
    # Shell has an ESEF page with figures: never asked of the vendor.
    lei = Company(scheme="lei", native_id="SHELLLEI", name="Shell", isin="GB00BP6MXD84")
    db.add(lei)
    await db.flush()
    lei.fetched_at = date(2026, 11, 1)
    db.add(
        CompanyFinancial(
            company_id=lei.id,
            period_end=date(2025, 12, 31),
            fiscal_year=2025,
            fiscal_period="FY",
            metric="revenue",
            value=1.0,
            unit="USD",
            source_concept="esef:ifrs-full:Revenue",
            filed=date(2026, 3, 1),
            fetched_at=date(2026, 11, 1),
        )
    )
    await db.commit()
    fake = Vendor()
    sync = VendorFinancialsSync(
        db, transport=httpx.MockTransport(fake.handler), sleep=_none
    )
    summary = await sync.refresh()
    assert summary["companies"] == 1 and summary["in_scope"] == 1
    assert not [u for u in fake.requests if "GB00BP6MXD84" in u]
    svc = CompanyFinancialsService(db)
    page = await svc.get_financials(
        await svc.company_by_key("vendor:390"), "annual", "max"
    )
    revenue = [p["value"] for p in page["series"]["revenue"]["points"]]
    assert revenue == [78.9e9]

    # Within the refresh interval: nothing asked, the page stays listed.
    fake.requests.clear()
    await VendorFinancialsSync(
        db, transport=httpx.MockTransport(fake.handler), sleep=_none
    ).refresh()
    assert fake.requests == []
    assert "vendor:390" in {c["key"] for c in await svc.list_companies()}


@pytest.mark.asyncio
async def test_spent_credits_or_a_refused_key_stop_the_vendor_pass(db, monkeypatch):
    monkeypatch.setattr(
        settings, "financialreports_api_key", "vendor-test-key", raising=False
    )

    async def named(_db):
        return [
            NamedCompany("Siemens", ["DE0007236101"]),
            NamedCompany("Roche", ["CH0012032113"]),
        ]

    monkeypatch.setattr(vendor_sync, "named_companies", named)
    fake = Vendor(remaining=30)
    summary = await VendorFinancialsSync(
        db, transport=httpx.MockTransport(fake.handler), sleep=_none
    ).refresh()
    assert not [u for u in fake.requests if "/financials/" in u]
    assert any("credits" in w for w in summary["warnings"])

    monkeypatch.setattr(settings, "financialreports_api_key", "", raising=False)
    assert await VendorFinancialsSync(db).refresh() is None
