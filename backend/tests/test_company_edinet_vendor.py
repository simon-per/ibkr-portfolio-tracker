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
from sqlalchemy import select

from app.config import settings
from app.models.company_financials import Company, CompanyFinancial, CompanyReport
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


def _split_instance(eps_2022: str) -> bytes:
    """Fujitsu's FY2025 summary as filed 2026-06-26, three of its five years: its 10:1
    split of 2024 left FY2022's EPS (to March 2022) unrestated beside FY2023's restated
    one, and the dividends as declared; a cancellation shrinks FY2026's count."""
    years = (("Prior4Year", "2021-04-01", "2022-03-31"),
             ("Prior3Year", "2022-04-01", "2023-03-31"),
             ("Prior1Year", "2024-04-01", "2025-03-31"),
             ("CurrentYear", "2025-04-01", "2026-03-31"))
    contexts = "".join(
        f'<xbrli:context id="{y}Duration"><xbrli:entity><xbrli:identifier scheme="s">E</xbrli:identifier></xbrli:entity>'
        f"<xbrli:period><xbrli:startDate>{s}</xbrli:startDate><xbrli:endDate>{e}</xbrli:endDate></xbrli:period></xbrli:context>"
        f'<xbrli:context id="{y}Duration_NonConsolidatedMember"><xbrli:entity><xbrli:identifier scheme="s">E</xbrli:identifier></xbrli:entity>'
        f"<xbrli:period><xbrli:startDate>{s}</xbrli:startDate><xbrli:endDate>{e}</xbrli:endDate></xbrli:period>"
        '<xbrli:scenario><xbrldi:explicitMember dimension="jppfs_cor:ConsolidatedOrNonConsolidatedAxis">jppfs_cor:NonConsolidatedMember</xbrldi:explicitMember></xbrli:scenario></xbrli:context>'
        f'<xbrli:context id="{y}Instant_NonConsolidatedMember"><xbrli:entity><xbrli:identifier scheme="s">E</xbrli:identifier></xbrli:entity>'
        f"<xbrli:period><xbrli:instant>{e}</xbrli:instant></xbrli:period>"
        '<xbrli:scenario><xbrldi:explicitMember dimension="jppfs_cor:ConsolidatedOrNonConsolidatedAxis">jppfs_cor:NonConsolidatedMember</xbrldi:explicitMember></xbrli:scenario></xbrli:context>'
        for y, s, e in years
    )
    rows = {
        "Prior4Year": ("182691000000", eps_2022, "220.00", "207001821"),
        "Prior3Year": ("215182000000", "110.54", "240.00", "207001821"),
        "Prior1Year": ("219807000000", "120.66", "28.00", "2071108450"),
        "CurrentYear": ("449408000000", "254.05", "50.00", "1739778265"),
    }
    facts = "".join(
        f'<c:ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults contextRef="{y}Duration" unitRef="JPY">{p}</c:ProfitLossAttributableToOwnersOfParentIFRSSummaryOfBusinessResults>'
        f'<c:DilutedEarningsLossPerShareIFRSSummaryOfBusinessResults contextRef="{y}Duration" unitRef="JPYPerShares">{eps}</c:DilutedEarningsLossPerShareIFRSSummaryOfBusinessResults>'
        f'<c:DividendPaidPerShareSummaryOfBusinessResults contextRef="{y}Duration_NonConsolidatedMember" unitRef="JPYPerShares">{dps}</c:DividendPaidPerShareSummaryOfBusinessResults>'
        f'<c:TotalNumberOfIssuedSharesSummaryOfBusinessResults contextRef="{y}Instant_NonConsolidatedMember" unitRef="shares">{n}</c:TotalNumberOfIssuedSharesSummaryOfBusinessResults>'
        for y, (p, eps, dps, n) in rows.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:c="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor"
 xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2024-11-01/jppfs_cor">{contexts}
<xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
<xbrli:unit id="JPYPerShares"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
{facts}</xbrli:xbrl>""".encode()


def test_a_summarys_per_share_figures_go_on_its_latest_share_basis():
    facts, _ = summary_facts(_split_instance("922.97"), "S100YM3K", date(2026, 6, 26))
    eps = {f["end"]: round(f["val"], 3) for f in facts if f["concept"] == "DilutedEarningsLossPerShare"}
    dps = {f["end"]: round(f["val"], 3) for f in facts
           if f["concept"] == "DividendsRecognisedAsDistributionsToOwnersPerShare"}
    # The unrestated year divided by the split; the restated ones and the cancellation kept.
    assert eps == {"2022-03-31": 92.297, "2023-03-31": 110.54,
                   "2025-03-31": 120.66, "2026-03-31": 254.05}
    assert dps == {"2022-03-31": 22.0, "2023-03-31": 24.0,
                   "2025-03-31": 28.0, "2026-03-31": 50.0}
    # A summary that restated every year (Tokyo Electron's) is left as it is.
    facts, _ = summary_facts(_split_instance("92.30"), "S100YM3K", date(2026, 6, 26))
    eps = {f["end"]: f["val"] for f in facts if f["concept"] == "DilutedEarningsLossPerShare"}
    assert eps["2022-03-31"] == 92.30


def test_a_transition_years_ifrs_figure_wins_and_basic_eps_fills_a_blank_diluted():
    # NEC's FY2017 report (IFRS adopted): FY2016 in both standards, its diluted EPS blank.
    instance = _instance().replace(
        b"</xbrli:xbrl>",
        b'<jpcrp_cor:NetSalesSummaryOfBusinessResults contextRef="Prior1YearDuration" unitRef="JPY">9690000000000</jpcrp_cor:NetSalesSummaryOfBusinessResults>'
        b'<jpcrp_cor:DilutedEarningsLossPerShareIFRSSummaryOfBusinessResults contextRef="Prior1YearDuration" unitRef="JPYPerShares" xsi:nil="true" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>'
        b'<jpcrp_cor:BasicEarningsLossPerShareIFRSSummaryOfBusinessResults contextRef="Prior1YearDuration" unitRef="JPYPerShares">140.00</jpcrp_cor:BasicEarningsLossPerShareIFRSSummaryOfBusinessResults>'
        b'<jpcrp_cor:BasicEarningsLossPerShareIFRSSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares">152.00</jpcrp_cor:BasicEarningsLossPerShareIFRSSummaryOfBusinessResults>'
        b"</xbrli:xbrl>",
    )
    facts, _ = summary_facts(instance, "S100TEST", date(2026, 6, 20))
    by = {(f["concept"], f["end"]): f["val"] for f in facts}
    assert by[("Revenue", "2025-03-31")] == 9.7e12  # IFRS, not J-GAAP's 9.69
    assert by[("DilutedEarningsLossPerShare", "2025-03-31")] == 140.0  # basic, diluted blank
    assert by[("DilutedEarningsLossPerShare", "2026-03-31")] == 151.0  # diluted stated


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

    # A report read under an older reader is read once more, by its document id.
    report = (await db.execute(select(CompanyReport))).scalars().one()
    report.reader = None
    await db.commit()
    fake.requests.clear()
    summary = await JapanFinancialsSync(
        db, transport=httpx.MockTransport(fake.handler), sleep=_none
    ).refresh()
    assert [u for u in fake.requests if "/documents/S100TEST" in u]
    assert summary["reports_read"] == 1
    await db.refresh(report)
    assert report.reader == edinet_sync.READER


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
                            "value": "78914000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "consolidated_net_income",
                            "name": "Consolidated Net Income (incl. Minority Interest)",
                            "value": "10387000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "net_income_loss",
                            "name": "Net Income",
                            "value": "9620000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "diluted_eps",
                            "name": "Diluted EPS",
                            "value": "12.11",
                            "currency": "EUR",
                        },
                        {
                            # The vendor puts the statement's currency on a share count.
                            "code": "weighted_avg_diluted_shares_outstanding",
                            "name": "Weighted Avg Diluted Shares Outstanding",
                            "value": "793781000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "full_time_employees",
                            "name": "Full Time Employees",
                            "value": "318000.00",
                            "currency": None,
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
                            "code": "cash_from_operations",
                            "name": "Cash from Ops",
                            "value": "13257000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "capital_expenditure_cf",
                            "name": "Capital Expenditure",
                            "value": "-2445000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "common_dividends_paid_cf",
                            "name": "Common Dividends Paid",
                            "value": "-4093000000.00",
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
                            "name": "Cash And Equivalents",
                            "value": "14495000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "total_common_equity",
                            "name": "Total Common Equity",
                            "value": "62244000000.00",
                            "currency": "EUR",
                        },
                        {
                            "code": "total_equity",
                            "name": "Total Equity",
                            "value": "68371000000.00",
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


def test_the_vendors_lines_are_mapped_by_code():
    # Siemens' FY2025 as the vendor served it (2026-11-20), against its annual report.
    facts, tagged = facts_of(FINANCIALS)
    by = {f["concept"]: f["val"] for f in facts}
    unit = {f["concept"]: f["unit"] for f in facts}
    assert by["Revenue"] == 78.914e9
    assert by["ProfitLossAttributableToOwnersOfParent"] == 9.62e9
    assert by["ProfitLoss"] == 10.387e9
    assert by["DilutedEarningsLossPerShare"] == 12.11
    assert unit["DilutedEarningsLossPerShare"] == "EUR/shares"
    assert by["AdjustedWeightedAverageShares"] == 793.781e6
    assert unit["AdjustedWeightedAverageShares"] == "shares"
    assert by["CashFlowsFromUsedInOperatingActivities"] == 13.257e9
    # Payments as the positive twins the normaliser reads (FCF = OCF − capex).
    assert (
        by["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"]
        == 2.445e9
    )
    assert by["DividendsPaidClassifiedAsFinancingActivities"] == 4.093e9
    assert by["CashAndCashEquivalents"] == 14.495e9
    assert by["EquityAttributableToOwnersOfParent"] == 62.244e9
    assert by["Equity"] == 68.371e9
    assert tagged["Revenue"] == "financialfilings:revenue"
    assert all(f["end"] == "2025-09-30" for f in facts)  # the half-year is not read


def test_an_eps_its_own_profit_and_shares_contradict_is_left_out():
    # Serabi's FY2024 as the vendor served it: its 36.73 cents read as dollars.
    def period(eps):
        return {
            "currency": {"code": "USD"},
            "periods": [
                {
                    "fiscal_period": "FY",
                    "period_start_date": "2024-01-01",
                    "period_end_date": "2024-12-31",
                    "statements": [
                        {
                            "line_items": [
                                {"code": "net_income_loss", "value": "27800000"},
                                {"code": "diluted_eps", "value": eps},
                                {
                                    "code": "weighted_avg_diluted_shares_outstanding",
                                    "value": "75700000",
                                },
                            ]
                        }
                    ],
                }
            ],
        }

    def concepts(body):
        return {f["concept"] for f in facts_of(body)[0]}

    assert "DilutedEarningsLossPerShare" not in concepts(period("36.73"))
    assert "ProfitLossAttributableToOwnersOfParent" in concepts(period("36.73"))
    assert "DilutedEarningsLossPerShare" in concepts(period("0.367"))


def test_the_shares_outstanding_from_each_source():
    facts, _ = facts_of(
        {
            "currency": {"code": "EUR"},
            "periods": [
                {
                    "fiscal_period": "FY",
                    "period_start_date": "2024-10-01",
                    "period_end_date": "2025-09-30",
                    "statements": [
                        {
                            "line_items": [
                                {
                                    "code": "shares_outstanding_on_filing_date",
                                    "value": "781000000",
                                    "currency": "EUR",
                                }
                            ]
                        }
                    ],
                }
            ],
        }
    )
    assert [(f["concept"], f["val"], f["unit"], f["start"]) for f in facts] == [
        ("NumberOfSharesOutstanding", 781e6, "shares", None)
    ]
    # EDINET: the year end's issued count less the treasury shares the report states then.
    instance = _instance().replace(
        b"</xbrli:xbrl>",
        b'<xbrli:context id="CurrentYearInstant_NonConsolidatedMember"><xbrli:entity><xbrli:identifier scheme="s">E</xbrli:identifier></xbrli:entity>'
        b"<xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>"
        b'<xbrli:scenario><xbrldi:explicitMember dimension="jppfs_cor:ConsolidatedOrNonConsolidatedAxis">jppfs_cor:NonConsolidatedMember</xbrldi:explicitMember></xbrli:scenario></xbrli:context>'
        b'<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>'
        b'<jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults contextRef="CurrentYearInstant_NonConsolidatedMember" unitRef="shares">4535560000</jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults>'
        b'<jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc contextRef="CurrentYearInstant" unitRef="shares">5560000</jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc>'
        b"</xbrli:xbrl>",
    )
    facts, _ = summary_facts(instance, "S100TEST", date(2026, 6, 20))
    out = [f for f in facts if f["concept"] == "NumberOfSharesOutstanding"]
    assert [(f["end"], f["val"]) for f in out] == [("2026-03-31", 4.53e9)]


def test_a_banks_vendor_lines():
    bank = {
        "currency": {"code": "AUD"},
        "periods": [
            {
                "fiscal_period": "FY",
                "period_start_date": "2024-07-01",
                "period_end_date": "2025-06-30",
                "statements": [
                    {
                        "statement_type": "IS",
                        "currency": {"code": "AUD"},
                        "line_items": [
                            {"code": "net_interest_income", "value": "23000000000"},
                            {"code": "total_net_revenue", "value": "28000000000"},
                        ],
                    },
                    {
                        "statement_type": "BS",
                        "line_items": [
                            {"code": "cash_and_central_bank_balances", "value": "1e10"},
                        ],
                    },
                ],
            }
        ],
    }
    facts, _ = facts_of(bank)
    by = {f["concept"]: (f["val"], f["unit"]) for f in facts}
    assert by["InterestRevenueExpense"] == (23e9, "AUD")
    assert by["Revenue"] == (28e9, "AUD")
    assert by["CashAndCashEquivalents"] == (1e10, "AUD")


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
    assert revenue == [78.914e9]
    fcf = [p["value"] for p in page["series"]["fcf"]["points"]]
    assert fcf == [13.257e9 - 2.445e9]

    # Within the refresh interval: nothing asked, the page stays listed.
    fake.requests.clear()
    await VendorFinancialsSync(
        db, transport=httpx.MockTransport(fake.handler), sleep=_none
    ).refresh()
    assert fake.requests == []
    assert "vendor:390" in {c["key"] for c in await svc.list_companies()}


@pytest.mark.asyncio
async def test_the_vendor_asks_a_japanese_company_last(db, monkeypatch):
    hitachi = NamedCompany("HITACHI LTD", ["JP3788600009"])
    siemens = NamedCompany("Siemens Aktiengesellschaft", ["DE0007236101"])

    async def named(_db):
        return [hitachi, siemens]

    monkeypatch.setattr(vendor_sync, "named_companies", named)
    assert [c.name for c in await VendorFinancialsSync(db).scope()] == [
        siemens.name,
        hitachi.name,
    ]


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
