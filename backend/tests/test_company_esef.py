"""
Europe via ESEF (docs/company-financials.md, *Europe*): the xBRL-JSON converter's dates, units
and dimensions, and the pass against a fake filings.xbrl.org. Offline.
"""

from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.models.company_financials import Company, CompanyReport
from app.models.isin_identity import IsinIdentity
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services import esef_sync
from app.services.company_financials_service import CompanyFinancialsService
from app.services.esef_client import BASE_URL, MAX_PAGES, EsefClient
from app.services.esef_reports import convert, period, unit_of
from app.services.esef_sync import EuropeFinancialsSync, is_european, newest_per_period

LEI = "969500A1YF1XUYYXS284"
OTHER_LEI = "529900D6BF99LW9R2E68"
EMPTY_LEI = "TSI2PJM6EPETEQ4X1U25"
ISIN = "FR0000121972"


def _fact(concept, value, period_text, unit="iso4217:EUR", **extra):
    dims = {"concept": concept, "entity": f"scheme:{LEI}", "period": period_text}
    if unit:
        dims["unit"] = unit
    dims.update(extra)
    return {"value": value, "decimals": -6, "dimensions": dims}


FY24 = "2024-01-01T00:00:00/2025-01-01T00:00:00"
FY23 = "2023-01-01T00:00:00/2024-01-01T00:00:00"


def _report_2024():
    facts = [
        _fact("ifrs-full:Revenue", "38153000000.0", FY24),
        _fact("ifrs-full:Revenue", "35902000000.0", FY23),
        _fact("ifrs-full:ProfitLossAttributableToOwnersOfParent", "4269000000.0", FY24),
        _fact("ifrs-full:ProfitLossAttributableToOwnersOfParent", "4003000000.0", FY23),
        _fact(
            "ifrs-full:DilutedEarningsLossPerShare",
            "7.53",
            FY24,
            "iso4217:EUR/xbrli:shares",
        ),
        _fact("ifrs-full:CashFlowsFromUsedInOperatingActivities", "5580000000.0", FY24),
        _fact("ifrs-full:Assets", "65943000000.0", "2025-01-01T00:00:00"),
        _fact(
            "ifrs-full:CashAndCashEquivalents", "6887000000.0", "2025-01-01T00:00:00"
        ),
        # A segment's revenue: dimensioned, not read.
        _fact(
            "ifrs-full:Revenue",
            "9000000000.0",
            FY24,
            **{"ifrs-full:SegmentsAxis": "x:Energy"},
        ),
        # A half year: not a fiscal year, not read.
        _fact(
            "ifrs-full:Revenue",
            "18000000000.0",
            "2024-01-01T00:00:00/2024-07-01T00:00:00",
        ),
        # The company's own concept: counted, not read.
        _fact("se:AdjustedEBITA", "7000000000.0", FY24),
        # Another entity's fact.
        {
            "value": "1.0",
            "dimensions": {
                "concept": "ifrs-full:Revenue",
                "entity": f"scheme:{OTHER_LEI}",
                "period": FY24,
                "unit": "iso4217:EUR",
            },
        },
        # Revenue in a per-share unit: the kind disagrees, not read.
        _fact("ifrs-full:Revenue", "3.0", FY24, "iso4217:EUR/xbrli:shares"),
        # Text.
        _fact(
            "ifrs-full:NameOfReportingEntityOrOtherMeansOfIdentification",
            "SE",
            FY24,
            None,
        ),
    ]
    return {"documentInfo": {}, "facts": {f"f{i}": f for i, f in enumerate(facts)}}


def _report_2023():
    facts = [
        _fact("ifrs-full:Revenue", "35902000000.0", FY23),
        _fact(
            "ifrs-full:Revenue",
            "34176000000.0",
            "2022-01-01T00:00:00/2023-01-01T00:00:00",
        ),
        _fact("ifrs-full:ProfitLossAttributableToOwnersOfParent", "4003000000.0", FY23),
    ]
    return {"documentInfo": {}, "facts": {f"f{i}": f for i, f in enumerate(facts)}}


def test_xbrl_json_periods_end_the_day_before_their_instant():
    assert period(FY24) == (date(2024, 1, 1), date(2024, 12, 31))
    assert period("2025-01-01T00:00:00") == (None, date(2024, 12, 31))
    assert period("2024-12-31") == (None, date(2024, 12, 31))
    assert period("2025-01-01T00:00:00/2024-01-01T00:00:00") is None
    assert unit_of("iso4217:EUR")[0] == "EUR"
    assert unit_of("iso4217:GBP/xbrli:shares")[0] == "GBP"
    assert unit_of("xbrli:pure") == ("", None)


def test_only_undimensioned_annual_ifrs_facts_of_the_entity_are_read():
    out = convert(_report_2024(), LEI, "fxo-2024", date(2025, 4, 15))
    got = {(f["concept"], f["start"], f["end"], f["unit"]): f["val"] for f in out.facts}
    assert got == {
        ("Revenue", "2024-01-01", "2024-12-31", "EUR"): 38153000000.0,
        ("Revenue", "2023-01-01", "2023-12-31", "EUR"): 35902000000.0,
        (
            "ProfitLossAttributableToOwnersOfParent",
            "2024-01-01",
            "2024-12-31",
            "EUR",
        ): 4269000000.0,
        (
            "ProfitLossAttributableToOwnersOfParent",
            "2023-01-01",
            "2023-12-31",
            "EUR",
        ): 4003000000.0,
        ("DilutedEarningsLossPerShare", "2024-01-01", "2024-12-31", "EUR/shares"): 7.53,
        (
            "CashFlowsFromUsedInOperatingActivities",
            "2024-01-01",
            "2024-12-31",
            "EUR",
        ): 5580000000.0,
        ("CashAndCashEquivalents", None, "2024-12-31", "EUR"): 6887000000.0,
    }
    assert out.non_standard == 1
    assert {f["form"] for f in out.facts} == {"annual report"}
    assert {f["accn"] for f in out.facts} == {"fxo-2024"}
    assert out.tagged["Revenue"] == "ifrs-full:Revenue"


def test_europe_is_the_region_map_and_the_crown_dependencies():
    assert is_european("FR0000121972") and is_european("GB0002374006")
    assert is_european("CH0012032048") and is_european("JE00B4T3BW64")
    assert not is_european("US0378331005") and not is_european("KR7005930003")
    assert not is_european("KYG017191142")


def test_one_filing_per_period_the_newest_added():
    from app.services.esef_client import Filing

    first = Filing("a-0", date(2024, 12, 31), date(2025, 3, 1), "/a.json", "FR")
    fixed = Filing("a-1", date(2024, 12, 31), date(2025, 5, 1), "/b.json", "FR")
    older = Filing("b-0", date(2023, 12, 31), date(2024, 3, 1), "/c.json", "FR")
    assert newest_per_period([fixed, first, older]) == {
        date(2024, 12, 31): fixed,
        date(2023, 12, 31): older,
    }


def _filing(fxo, end, added, json_url):
    return {
        "type": "filing",
        "id": fxo,
        "attributes": {
            "period_end": end,
            "country": "FR",
            "fxo_id": fxo,
            "date_added": added,
            "json_url": json_url,
        },
    }


class Index:
    """A fake filings.xbrl.org: two annual reports for one LEI, one unprocessed, nothing for
    any other LEI; the list in two pages."""

    def __init__(self):
        self.requests = []

    def handler(self, request):
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.requests.append(url.path)
        if url.path == f"/api/entities/{LEI}/filings":
            if q.get("page[number]") == "2":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            _filing(
                                f"{LEI}-2022-12-31-ESEF-FR-0",
                                "2022-12-31",
                                "2023-03-30 16:36:27",
                                None,
                            )
                        ],
                        "links": {},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        _filing(
                            f"{LEI}-2024-12-31-ESEF-FR-0",
                            "2024-12-31",
                            "2025-04-15 12:08:31",
                            "/se/2024.json",
                        ),
                        _filing(
                            f"{LEI}-2023-12-31-ESEF-FR-0",
                            "2023-12-31",
                            "2024-04-02 11:49:59",
                            "/se/2023.json",
                        ),
                    ],
                    "links": {
                        "next": f"{BASE_URL}/api/entities/{LEI}/filings?page%5Bsize%5D=100&page%5Bnumber%5D=2"
                    },
                },
            )
        if url.path == f"/api/entities/{EMPTY_LEI}/filings":
            # An entity the index knows with no filing.
            return httpx.Response(
                200, json={"data": [], "meta": {"count": 0}, "links": {}}
            )
        if url.path.startswith("/api/entities/"):
            # An LEI the index never received a filing for.
            return httpx.Response(404, json={"errors": [{"status": "404"}]})
        if url.path == "/se/2024.json":
            return httpx.Response(200, json=_report_2024())
        if url.path == "/se/2023.json":
            return httpx.Response(200, json=_report_2023())
        return httpx.Response(404)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


async def _none(seconds):
    return None


async def _hold(db, sid, symbol, isin, lei):
    db.add(
        Security(
            id=sid,
            isin=isin,
            symbol=symbol,
            description=f"{symbol} SE",
            currency="EUR",
            conid=9100 + sid,
            asset_category="STK",
            exchange="SBF",
        )
    )
    db.add(
        TaxLot(
            security_id=sid,
            open_date=date(2025, 1, 2),
            close_date=None,
            quantity=Decimal("1"),
            cost_basis=Decimal("100"),
            price_per_unit=Decimal("100"),
            currency="EUR",
            cost_basis_eur=Decimal("100"),
            is_open=True,
        )
    )
    if lei:
        db.add(IsinIdentity(isin=isin, lei=lei, lei_source="gleif"))
    await db.commit()


@pytest.fixture
def dated(monkeypatch):
    monkeypatch.setattr(esef_sync, "utc_today", lambda: date(2025, 12, 1))
    monkeypatch.setattr(esef_sync, "utcnow", lambda: datetime(2025, 12, 1, 5))


@pytest.mark.asyncio
async def test_a_european_company_gets_its_annual_figures_each_filing_read_once(
    db, dated
):
    await _hold(db, 1, "SU", ISIN, LEI)
    await _hold(
        db, 2, "SAP", "DE0007164600", OTHER_LEI
    )  # the index holds nothing for it
    await _hold(db, 3, "NOLEI", "NL0000000001", None)
    await _hold(db, 4, "IFX", "DE0006231004", EMPTY_LEI)
    fake = Index()
    sync = EuropeFinancialsSync(db, transport=fake.transport, sleep=_none)
    summary = await sync.refresh()

    assert summary["in_scope"] == 4 and summary["no_lei"] == 1
    assert summary["errors"] == 0
    assert summary["companies"] == 1 and summary["without_filings"] == 2
    assert summary["filings_read"] == 2 and summary["filings_unprocessed"] == 1
    assert summary["non_standard_facts"] == 1
    # Two list pages for Schneider, one each for SAP and Infineon, two reports.
    assert summary["requests"] == 6

    companies = {
        c.native_id: c for c in (await db.execute(Company.__table__.select())).all()
    }
    assert companies[LEI].scheme == "lei" and companies[LEI].isin == ISIN
    assert companies[LEI].fiscal_year_end == "1231"
    assert companies[OTHER_LEI].universe_seen_at is None
    assert companies[OTHER_LEI].facts_miss_count == 1

    svc = CompanyFinancialsService(db)
    company = await svc.company_by_key(f"lei:{LEI}")
    page = await svc.get_financials(company, "annual", "max")
    revenue = {p["label"]: p["value"] for p in page["series"]["revenue"]["points"]}
    assert revenue["FY2024"] == 38153e6 and revenue["FY2023"] == 35902e6
    assert page["currency"] == "EUR"
    assert page["key_stats"]["pe_ratio"] is None
    assert page["series"]["price"]["points"] == []
    rows = await svc.repo.rows_of(company)
    assert rows and all(r.source_concept.startswith("esef:") for r in rows)
    # Annual only: the trailing twelve months are the fiscal year.
    ttm = await svc.get_financials(company, "ttm", "max")
    assert ttm["series"]["revenue"]["points"][-1]["value"] == 38153e6

    listed = [c for c in await svc.list_companies() if c["key"] == f"lei:{LEI}"]
    assert listed and listed[0]["security_ids"] == [1]
    assert listed[0]["symbol"] == "SU"  # the held listing's, never the key
    assert not [c for c in await svc.list_companies() if c["key"] == f"lei:{OTHER_LEI}"]

    # The next pass asks nothing: both reports read, the list not due until FY2025's report
    # is (2026-03-31), SAP backing off.
    fake.requests.clear()
    summary = await sync.refresh()
    assert fake.requests == [] and summary["companies"] == 1
    assert [
        c["key"] for c in await svc.list_companies() if c["key"].startswith("lei:")
    ] == [f"lei:{LEI}"]
    reports = (await db.execute(CompanyReport.__table__.select())).all()
    assert len(reports) == 2


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_figures_and_the_listing(db, dated, monkeypatch):
    await _hold(db, 1, "SU", ISIN, LEI)
    fake = Index()
    sync = EuropeFinancialsSync(db, transport=fake.transport, sleep=_none)
    await sync.refresh()

    def broken(request):
        return httpx.Response(503)

    monkeypatch.setattr(esef_sync, "utcnow", lambda: datetime(2026, 5, 1, 5))
    monkeypatch.setattr(esef_sync, "utc_today", lambda: date(2026, 5, 1))
    summary = await EuropeFinancialsSync(
        db, transport=httpx.MockTransport(broken), sleep=_none
    ).refresh()
    assert summary["errors"] == 1 and summary["warnings"]
    svc = CompanyFinancialsService(db)
    listed = [c for c in await svc.list_companies() if c["key"] == f"lei:{LEI}"]
    assert listed
    assert await svc.repo.rows_of(await svc.company_by_key(f"lei:{LEI}"))


@pytest.mark.asyncio
async def test_the_filings_list_follows_at_most_max_pages(monkeypatch):
    calls = []

    def endless(request):
        calls.append(str(request.url))
        n = len(calls)
        return httpx.Response(
            200,
            json={
                "data": [_filing(f"x-{n}", "2024-12-31", "2025-01-01", None)],
                "links": {
                    "next": f"{BASE_URL}/api/entities/{LEI}/filings?page%5Bnumber%5D={n + 1}"
                },
            },
        )

    async with EsefClient(
        transport=httpx.MockTransport(endless), sleep=_none
    ) as client:
        filings = await client.filings(LEI)
    assert len(calls) == MAX_PAGES and len(filings) == MAX_PAGES


@pytest.mark.asyncio
async def test_a_filing_too_large_to_read_is_skipped_once_and_named(
    db, dated, monkeypatch
):
    from app.services import esef_client

    monkeypatch.setattr(esef_client, "MAX_REPORT_BYTES", 10)
    await _hold(db, 1, "SU", ISIN, LEI)
    fake = Index()
    sync = EuropeFinancialsSync(db, transport=fake.transport, sleep=_none)
    summary = await sync.refresh()
    assert len(summary["too_large"]) == 2 and summary["errors"] == 0
    assert sum("larger than the reader takes" in w for w in summary["warnings"]) == 2
    statuses = {
        r.status for r in (await db.execute(CompanyReport.__table__.select())).all()
    }
    assert statuses == {"skipped"}

    fake.requests.clear()
    summary = await sync.refresh()
    assert fake.requests == [] and summary["too_large"] == []

    # The cap grew (Santander's 60 MB to 150 MB): each skipped filing is read once more, the
    # list asked again for it although it is not due.
    monkeypatch.setattr(esef_client, "MAX_REPORT_BYTES", 10_000_000)
    summary = await sync.refresh()
    assert summary["filings_read"] == 2 and summary["too_large"] == []
    statuses = {
        r.status for r in (await db.execute(CompanyReport.__table__.select())).all()
    }
    assert statuses == {"read"}
    fake.requests.clear()
    await sync.refresh()
    assert fake.requests == []


@pytest.mark.asyncio
async def test_a_pass_stopped_part_way_asks_the_list_again_and_reads_the_rest(
    db, dated
):
    await _hold(db, 1, "SU", ISIN, LEI)

    class Flaky(Index):
        def handler(self, request):
            if urlparse(str(request.url)).path == "/se/2023.json" and self.fail:
                self.requests.append("/se/2023.json")
                return httpx.Response(503)
            return super().handler(request)

    fake = Flaky()
    fake.fail = True
    sync = EuropeFinancialsSync(db, transport=fake.transport, sleep=_none)
    summary = await sync.refresh()
    assert summary["errors"] == 1 and summary["filings_read"] == 1

    fake.fail = False
    fake.requests.clear()
    summary = await sync.refresh()
    assert summary["errors"] == 0 and summary["filings_read"] == 1
    assert "/se/2024.json" not in fake.requests  # read once
    svc = CompanyFinancialsService(db)
    page = await svc.get_financials(
        await svc.company_by_key(f"lei:{LEI}"), "annual", "max"
    )
    labels = {p["label"] for p in page["series"]["revenue"]["points"]}
    assert {"FY2022", "FY2023", "FY2024"} <= labels


def _year_facts(dps):
    def f(concept, val, unit="EUR"):
        return {
            "concept": concept,
            "unit": unit,
            "start": "2024-01-01",
            "end": "2024-12-31",
            "val": val,
            "accn": "fxo-2024",
            "filed": "2025-04-01",
            "form": "annual report",
        }

    return [
        f("Revenue", 3616.2e6),
        f("ProfitLossAttributableToOwnersOfParent", 1061.9e6),
        f("DilutedEarningsLossPerShare", 0.523, "EUR/shares"),
        f("DividendsPaidClassifiedAsFinancingActivities", 690.6e6),
        f("DividendsRecognisedAsDistributionsToOwnersPerShare", dps, "EUR/shares"),
    ]


@pytest.mark.parametrize("dps,kept", [(11.92, False), (0.3946, True)])
def test_a_dividend_per_share_the_year_cannot_have_paid_is_absent(dps, kept):
    # Terna's FY2024 report tags its interim dividend in cents as euros (#83).
    from app.services.regulator_facts import normalise_reports

    rows = normalise_reports("Terna", _year_facts(dps), "esef", {}).rows
    got = [r["value"] for r in rows if r["metric"] == "dividends_per_share"]
    assert got == ([dps] if kept else [])
    assert any(r["metric"] == "revenue" for r in rows)


@pytest.mark.asyncio
async def test_a_bank_without_a_revenue_line_has_its_profits_currency(db):
    # HSBC's and Santander's ESEF reports tag no revenue (#85).
    from app.repositories.company_financials_repository import (
        CompanyFinancialsRepository,
    )

    repo = CompanyFinancialsRepository(db)
    bank = await repo.ensure_other_company("lei", "MLU0ZO3ML4LN2LL2TL39", ticker="HSBA")
    await repo.replace_company_rows(
        bank,
        [
            {
                "period_end": date(2024, 12, 31),
                "fiscal_year": 2024,
                "fiscal_period": "FY",
                "metric": "net_income",
                "value": 22.9e9,
                "unit": "USD",
                "source_concept": "esef:ifrs-full:ProfitLossAttributableToOwnersOfParent",
                "filed": date(2025, 2, 19),
            }
        ],
        datetime(2025, 3, 1),
    )
    await db.commit()
    coverage = await repo.coverage_of([bank])
    assert coverage[bank.key]["currency"] == "USD"
