"""
A 20-F filer found by its name (docs/company-financials.md, *Scope* and *Prices without
Yahoo*; owner, 2026-11-19: "use SEC for SAP etc. where it exists"). Measured on production
that day: SAP, Novartis, Toyota, MUFG, BHP, GlobalFoundries and Tower file a current 20-F;
ABB's last is FY2023 and Infineon's FY2009; Shell and Santander already have ESEF pages.

- Taken while it still files (`ANNUAL_CURRENT_WITHIN`), never over another regulator's page.
- A basket ticker the SEC does not list (Novartis's NOVN) sends the name over the whole
  ticker list: NVS, found once and kept (`sec_name_matches`).
- Priced by its ordinary share (the basket's price, a held listing's close), never at Yahoo:
  a New York receipt can stand for ten shares (Toyota's) beside an EPS per share.
- The reader: a filer that switched to IFRS is read in IFRS (Toyota's us-gaap ended at
  FY2020); net sales of goods are an IFRS filer's revenue when it tags no total (Novartis).
Offline: a fake SEC.
"""

from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from app.clock import utc_today, utcnow
from app.models.company_financials import (
    Company,
    CompanyFinancial,
    SecCompany,
    SecNameMatch,
)
from app.models.etf_basket import ConstituentPrice, EtfHolding
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.services import company_financials_service
from app.services.company_financials_normaliser import normalise
from app.services.company_financials_service import CompanyFinancialsService
from app.services.company_financials_sync import CompanyFinancialsSync
from app.services.company_price_sync import CompanyPriceSync
from app.services.company_profile_sync import CompanyProfileSync
from app.services.edgar_client import FACTS_URL, SUBMISSIONS_URL, TICKERS_URL
from app.services.lookthrough_service import LookthroughService
from tests.test_company_financials_sync import FIXTURE, NASDAQ_100, TICKERS, _qqqm
from tests.test_company_name_match import OTHER_FUND, configured  # noqa: F401

SAP, NVS, ABB, SHELL, ROGERS = 1000184, 1114448, 1091587, 1306965, 84748
SAP_ISIN, NVS_ISIN, ABB_ISIN = "DE0007164600", "CH0012005267", "CH0012221716"
SHELL_ISIN, ROCHE_ISIN = "GB00BP6MXD84", "CH0012032048"
RECENT = (utc_today() - timedelta(days=300)).isoformat()
STOPPED = (utc_today() - timedelta(days=700)).isoformat()

LISTED = {
    **TICKERS,
    "20": {"cik_str": SAP, "ticker": "SAP", "title": "SAP SE"},
    "21": {"cik_str": NVS, "ticker": "NVS", "title": "NOVARTIS AG"},
    "22": {"cik_str": ABB, "ticker": "ABLZF", "title": "ABB LTD"},
    "23": {"cik_str": SHELL, "ticker": "SHEL", "title": "Shell plc"},
    "24": {"cik_str": ROGERS, "ticker": "ROG", "title": "ROGERS CORP"},
}
ANNUAL = {SAP: RECENT, NVS: RECENT, ABB: STOPPED, SHELL: RECENT}


def _filings(end):
    return {
        "filings": {
            "recent": {
                "form": ["20-F"],
                "reportDate": [end],
                "accessionNumber": ["0001-26-000001"],
                "filingDate": [end],
            }
        }
    }


class Sec:
    def __init__(self):
        self.requests = []

    def handler(self, request):
        url = str(request.url)
        self.requests.append(url)
        if url == TICKERS_URL:
            return httpx.Response(200, json=LISTED)
        for cik, end in ANNUAL.items():
            if url == SUBMISSIONS_URL.format(cik=cik):
                return httpx.Response(200, json=_filings(end))
        if url.startswith("https://data.sec.gov/api/xbrl/companyfacts/"):
            return httpx.Response(200, json=FIXTURE)
        return httpx.Response(404)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


async def _none(*_):
    return None


async def _named(db, monkeypatch):
    """The look-through names SAP, Novartis, ABB, Shell and Roche (no US ISIN); the baskets
    give them their home tickers; Shell already has an ESEF page with figures."""

    async def view(self, limit=50):
        return {
            "companies": [
                {"name": "SAP SE", "isins": [SAP_ISIN]},
                {"name": "NOVARTIS AG", "isins": [NVS_ISIN]},
                {"name": "ABB Ltd", "isins": [ABB_ISIN]},
                {"name": "Shell plc", "isins": [SHELL_ISIN]},
                {"name": "Roche Holding AG", "isins": [ROCHE_ISIN]},
            ]
        }

    monkeypatch.setattr(LookthroughService, "get_lookthrough", view)
    await _qqqm(db, NASDAQ_100)
    lines = [
        (SAP_ISIN, "SAP", "SAP SE"),
        (NVS_ISIN, "NOVN", "NOVARTIS AG-REG"),
        (ABB_ISIN, "ABBN", "ABB LTD-REG"),
        (SHELL_ISIN, "SHEL", "SHELL PLC"),
        (ROCHE_ISIN, "ROG", "ROCHE HOLDING AG"),
    ]
    for i, (isin, ticker, name) in enumerate(lines):
        db.add(
            EtfHolding(
                fund_isin=OTHER_FUND,
                line_no=i,
                constituent_isin=isin,
                constituent_ticker=ticker,
                constituent_name=name,
                weight_pct=Decimal("1"),
                asset_class="Equity",
            )
        )
    shell = Company(
        scheme="lei", native_id="21380068P1DRHMJ8KU70", name="Shell", isin=SHELL_ISIN
    )
    db.add(shell)
    await db.flush()
    db.add(
        CompanyFinancial(
            company_id=shell.id,
            period_end=date(2025, 12, 31),
            fiscal_year=2025,
            fiscal_period="FY",
            metric="revenue",
            value=284e9,
            unit="USD",
            source_concept="esef:ifrs-full:Revenue",
            filed=date(2026, 3, 12),
            fetched_at=utcnow(),
        )
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_a_current_20f_filer_is_found_by_name_and_priced_without_yahoo(db, monkeypatch):
    await _named(db, monkeypatch)
    sec = Sec()
    sync = CompanyFinancialsSync(db, transport=sec.transport, sleep=_none)
    summary = await sync.refresh()

    facts = {u for u in sec.requests if "/companyfacts/" in u}
    # NVDA (the index's one), SAP by its ticker, Novartis by its name. Never ABB (its last
    # 20-F is two years old), Shell (its ESEF page stays), Rogers for Roche.
    assert facts == {FACTS_URL.format(cik=c) for c in (1045810, SAP, NVS)}
    assert summary["name_matched"] == ["NVS", "SAP"]
    # NOVN and ABBN are not on the list; ROG is Rogers: three names held against it.
    assert summary["name_searches"] == 3 and sorted(summary["name_found"]) == [
        "ABLZF",
        "NVS",
    ]
    novartis = await db.get(SecCompany, NVS)
    assert novartis.ticker == "NVS" and novartis.isin == NVS_ISIN
    assert (await db.get(SecNameMatch, ROCHE_ISIN)).cik is None
    for cik in (ABB, SHELL, ROGERS):
        assert await db.get(SecCompany, cik) is None

    repo = CompanyFinancialsRepository(db)
    assert await repo.ordinary_priced_ciks() == {SAP, NVS}
    # Neither Yahoo loop asks about them; NVDA stays.
    priced = {c.cik for c in await CompanyPriceSync(db)._targets(None)}
    assert 1045810 in priced and not priced & {SAP, NVS}
    profiled = {
        c["cik"] for c in await CompanyProfileSync(db, sleep=_none).index_only()
    }
    assert not profiled & {SAP, NVS}

    # The next pass asks no list again: names, matches and forms are on record.
    sec.requests.clear()
    summary = await sync.refresh()
    assert TICKERS_URL not in sec.requests
    assert not [u for u in sec.requests if "/submissions/" in u]
    assert summary["name_matched"] == ["NVS", "SAP"] and summary["name_searches"] == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_an_ordinary_priced_filer_takes_the_basket_price(db, monkeypatch):
    await _named(db, monkeypatch)
    await CompanyFinancialsSync(db, transport=Sec().transport, sleep=_none).refresh()
    today = utc_today()
    # The basket's USD price of SAP's ordinary share (the fixture reports in USD).
    db.add(
        ConstituentPrice(
            isin=SAP_ISIN, date=today, price=250.0, currency="USD", fund_isin=OTHER_FUND
        )
    )
    await db.commit()
    monkeypatch.setattr(company_financials_service, "utc_today", lambda: today)

    svc = CompanyFinancialsService(db)
    priced = await svc._priced(await svc.company_by_key(f"sec:{SAP}"))
    assert priced["current"] == pytest.approx(250.0)
    # Novartis has no basket price yet: no multiple, rather than a receipt's.
    priced = await svc._priced(await svc.company_by_key(f"sec:{NVS}"))
    assert priced["current"] is None and priced["closes"] == []


def _fy(end, val, form="20-F", accn=None):
    return {
        "start": f"{int(end[:4]) - 1}{end[4:]}",
        "end": end,
        "val": val,
        "accn": accn or f"a-{end}",
        "filed": f"{int(end[:4])}-06-20",
        "form": form,
        "fy": int(end[:4]),
        "fp": "FY",
    }


def test_a_filer_that_moved_to_ifrs_is_read_in_ifrs():
    doc = {
        "cik": 1094517,
        "entityName": "TOYOTA MOTOR CORP/",
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"JPY": [_fy("2020-03-31", 29.9e12)]}},
            },
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "JPY": [_fy("2024-03-31", 45.1e12), _fy("2025-03-31", 48.0e12)]
                    }
                },
            },
        },
    }
    rows = {(r["period_end"], r["metric"]): r["value"] for r in normalise(doc).rows}
    assert rows[(date(2025, 3, 31), "revenue")] == 48.0e12
    # Never both: the us-gaap year is the other taxonomy's.
    assert (date(2020, 3, 31), "revenue") not in rows


def test_net_sales_of_goods_are_an_ifrs_filers_revenue_without_a_total():
    def doc(extra):
        return {
            "cik": 1114448,
            "entityName": "Novartis AG",
            "facts": {
                "ifrs-full": {
                    "RevenueFromSaleOfGoods": {
                        "units": {"USD": [_fy("2025-12-31", 54.5e9)]}
                    },
                    "OtherRevenue": {"units": {"USD": [_fy("2025-12-31", 2.1e9)]}},
                    **extra,
                }
            },
        }

    def revenue(d):
        return {r["metric"]: r["value"] for r in normalise(d).rows}.get("revenue")

    assert revenue(doc({})) == 54.5e9
    # A total, where tagged, comes first.
    assert (
        revenue(doc({"Revenue": {"units": {"USD": [_fy("2025-12-31", 56.6e9)]}}}))
        == 56.6e9
    )


def test_a_40f_is_an_annual_report():
    """RBC, TD and Thomson Reuters file a 40-F in IFRS (2026-11-19); until then they read as
    holding nothing."""
    doc = {
        "cik": 1000275,
        "entityName": "ROYAL BANK OF CANADA",
        "facts": {
            "ifrs-full": {
                "ProfitLossAttributableToOwnersOfParent": {
                    "units": {"CAD": [_fy("2025-10-31", 20.4e9, form="40-F")]}
                },
                "InterestRevenueExpense": {
                    "units": {"CAD": [_fy("2025-10-31", 31.0e9, form="40-F")]}
                },
            }
        },
    }
    rows = {r["metric"]: r["value"] for r in normalise(doc).rows if r["fiscal_period"] == "FY"}
    assert rows["net_income"] == 20.4e9
    # An IFRS bank's net interest income, as a US bank's.
    assert rows["net_interest_income"] == 31.0e9


def test_continuing_eps_is_the_eps_only_without_discontinued_operations():
    """Shell tags only its continuing EPS and has no discontinued operations: that is its EPS.
    Iberdrola's 2025 had 0.054 per share discontinued beside 0.843: no EPS rather than a part."""
    doc = {
        "cik": 1,
        "entityName": "Example",
        "facts": {
            "ifrs-full": {
                "DilutedEarningsLossPerShareFromContinuingOperations": {
                    "units": {
                        "EUR/shares": [_fy("2024-12-31", 0.589), _fy("2025-12-31", 0.843)]
                    }
                },
                "ProfitLossFromDiscontinuedOperations": {
                    "units": {"EUR": [_fy("2024-12-31", 0.0), _fy("2025-12-31", 0.35e9)]}
                },
            }
        },
    }
    eps = {
        r["period_end"]: r["value"] for r in normalise(doc).rows if r["metric"] == "eps_diluted"
    }
    assert eps == {date(2024, 12, 31): 0.589}
    assert not any(r["metric"] == "discontinued_income" for r in normalise(doc).rows)

    # With the discontinued operations' EPS tagged in the standard taxonomy (Iberdrola tags
    # it only in its own extension), the EPS is the two together (same count).
    doc["facts"]["ifrs-full"]["DilutedEarningsLossPerShareFromDiscontinuedOperations"] = {
        "units": {"EUR/shares": [_fy("2025-12-31", 0.054)]}
    }
    rows = normalise(doc).rows
    eps = {r["period_end"]: r["value"] for r in rows if r["metric"] == "eps_diluted"}
    assert eps[date(2024, 12, 31)] == 0.589
    assert abs(eps[date(2025, 12, 31)] - 0.897) < 1e-9
    assert not any(r["metric"] == "discontinued_eps" for r in rows)


def test_none_listed_is_asked_again_once_then_yearly():
    """RBC's, TD's and Thomson Reuters' lists said "-" while the 40-F did not count."""
    from types import SimpleNamespace

    from app.services.company_financials_sync import form_due

    now = utcnow()
    assert form_due(SimpleNamespace(annual_form="-", form_checked_at=None), now)
    lately = SimpleNamespace(annual_form="-", form_checked_at=now - timedelta(days=30))
    assert not form_due(lately, now)
    long_ago = SimpleNamespace(annual_form="-", form_checked_at=now - timedelta(days=401))
    assert form_due(long_ago, now)


@pytest.mark.asyncio
async def test_an_annual_only_filers_year_is_not_stale_within_a_year_and_five_months(db):
    from app.services.company_financials_sync import Target

    today = utc_today()
    for cik, ends, period in (
        (1, [today - timedelta(days=300)], "FY"),  # a 20-F filer's year: current
        (2, [today - timedelta(days=300)], "Q3"),  # a quarterly filer's: stale
    ):
        db.add(SecCompany(cik=cik, ticker=f"T{cik}"))
        for end in ends:
            db.add(CompanyFinancial(cik=cik, period_end=end, fiscal_year=end.year,
                                    fiscal_period=period, metric="net_income", value=1.0,
                                    unit="USD", source_concept="NetIncomeLoss", filed=end,
                                    fetched_at=utcnow()))
    await db.commit()
    summary = {"ttm_dividend_gaps": [], "stale_figures": []}
    await CompanyFinancialsSync(db)._findings([Target(1, ["A"]), Target(2, ["Q"])], summary)
    assert summary["stale_figures"] == ["Q"]
