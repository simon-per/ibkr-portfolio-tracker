"""
A look-through company without a US ISIN, found at the SEC by the baskets' ticker
(docs/company-financials.md, *Scope*, 2026-11-17): only when the SEC's name for the ticker
agrees with the company's under `name_tokens`, and the filer files a current 10-K or (since
2026-11-19, tests/test_company_twenty_f.py) 20-F. Eaton,
Johnson Controls, Accenture and Medtronic are Irish 10-K filers with no US ISIN; Roche's
ROG is Rogers Corp's ticker at the SEC, Schneider's SU Suncor's. Offline: a fake SEC.
"""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.clock import utc_today
from app.models.company_financials import SecCompany, SecTicker
from app.models.etf_basket import EtfHolding
from app.services import company_financials_sync
from app.services.company_financials_sync import (
    CompanyFinancialsSync,
    name_tokens,
    names_agree,
)
from app.services.edgar_client import FACTS_URL, SUBMISSIONS_URL, TICKERS_URL
from app.services.lookthrough_service import LookthroughService
from tests.test_company_financials_sync import (
    FIXTURE,
    FUND,
    NASDAQ_100,
    TICKERS,
    UA,
    _qqqm,
)
from app.config import settings

EATON_CIK, ROGERS_CIK, SAP_CIK = 1551182, 84748, 1000184
# A current annual report's period: within `ANNUAL_CURRENT_WITHIN` of whatever day the suite runs.
RECENT = (utc_today() - timedelta(days=300)).isoformat()
OTHER_FUND = "IE00B5BMR087"


@pytest.mark.parametrize(
    "ours, sec",
    [
        ("EATON CORP PLC", "Eaton Corp plc"),
        ("Eaton Corporation public limited company", "Eaton Corp plc"),
        ("EATON PLC", "Eaton Corp plc"),
        ("JOHNSON CONTROLS INTERNATION", "Johnson Controls International plc"),
        ("ACCENTURE PLC-CL A", "Accenture plc"),
        ("Accenture plc Class A", "Accenture plc"),
        ("MEDTRONIC PLC", "Medtronic plc"),
        ("nVent Electric plc", "nVent Electric plc"),
        ("BANCO SANTANDER", "BANCO SANTANDER, S.A."),
        ("Nestlé S.A.", "NESTLE SA"),
    ],
)
def test_names_that_agree(ours, sec):
    assert names_agree([ours], sec)


@pytest.mark.parametrize(
    "ours, sec",
    [
        ("Roche Holding AG (Bearer)", "ROGERS CORP"),
        ("ROCHE HOLDING AG", "ROPER TECHNOLOGIES INC"),
        ("Schneider Electric SE", "SUNCOR ENERGY INC"),
        ("AIRBUS SE", "AAR CORP"),
        ("Allianz SE", "Autoliv Inc"),
        ("Terna - Rete Elettrica Nazionale", "TRINITY INDUSTRIES INC"),
        # Short names are never taken for cut-short ones.
        ("ABB Ltd", "AbbVie Inc."),
        ("JOHNSON CONTROLS", "Johnson Controls International plc"),
        ("", "Eaton Corp plc"),
        ("EATON CORP PLC", None),
    ],
)
def test_names_that_do_not(ours, sec):
    assert not names_agree([ours], sec)


def test_name_tokens_drop_legal_forms_from_the_end_only():
    assert name_tokens("Eaton Corporation public limited company") == ["eaton"]
    assert name_tokens("S&P Global Inc.") == ["s", "and", "p", "global"]
    assert name_tokens("The Coca-Cola Company") == ["the", "coca", "cola"]


TICKERS_PLUS = {
    **TICKERS,
    "10": {"cik_str": EATON_CIK, "ticker": "ETN", "title": "Eaton Corp plc"},
    "11": {"cik_str": ROGERS_CIK, "ticker": "ROG", "title": "ROGERS CORP"},
    "12": {"cik_str": SAP_CIK, "ticker": "SAP", "title": "SAP SE"},
}


def _filings(form, end):
    return {
        "filings": {
            "recent": {
                "form": [form],
                "reportDate": [end],
                "accessionNumber": ["0001-26-000001"],
                "filingDate": ["2026-02-20"],
            }
        }
    }


class Sec:
    """A fake SEC with Eaton (10-K), Rogers and SAP (20-F) on its ticker list."""

    def __init__(self):
        self.requests = []

    def handler(self, request):
        url = str(request.url)
        self.requests.append(url)
        if url == TICKERS_URL:
            return httpx.Response(200, json=TICKERS_PLUS)
        if url == SUBMISSIONS_URL.format(cik=EATON_CIK):
            return httpx.Response(200, json=_filings("10-K", RECENT))
        if url == SUBMISSIONS_URL.format(cik=SAP_CIK):
            return httpx.Response(200, json=_filings("20-F", RECENT))
        if url.startswith("https://data.sec.gov/api/xbrl/companyfacts/"):
            return httpx.Response(200, json=FIXTURE)
        return httpx.Response(404)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)
    monkeypatch.setattr(company_financials_sync, "UNIVERSE_FUNDS", (FUND,))
    monkeypatch.setattr(company_financials_sync, "UNIVERSE_SIZE", 1)

    async def no(self, cik, facts):
        return False

    monkeypatch.setattr(CompanyFinancialsSync, "_reads_extensions", no)
    monkeypatch.setattr(CompanyFinancialsSync, "_reads_segments", lambda *a: False)


async def _baskets(db):
    lines = [
        ("IE00B8KQN827", "ETN", "EATON PLC"),
        ("IE00B8KQN827", "ETN", "Eaton Corp. plc"),
        ("CH0012032048", "ROG", "ROCHE HOLDING AG"),
        ("DE0007164600", "SAP", "SAP SE"),
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
    await db.commit()


@pytest.mark.asyncio
async def test_a_filer_is_found_by_name_and_nothing_else_is(
    db, configured, monkeypatch
):
    async def view(self, limit=50):
        return {
            "companies": [
                {
                    "name": "Eaton Corporation public limited company",
                    "isins": ["IE00B8KQN827"],
                },
                {"name": "Roche Holding AG", "isins": ["CH0012032048"]},
                {"name": "SAP SE", "isins": ["DE0007164600"]},
            ]
        }

    monkeypatch.setattr(LookthroughService, "get_lookthrough", view)
    await _qqqm(db, NASDAQ_100)
    await _baskets(db)

    sec = Sec()
    sync = CompanyFinancialsSync(db, transport=sec.transport, sleep=lambda s: _none())
    summary = await sync.refresh()

    facts = {u for u in sec.requests if "/companyfacts/" in u}
    # NVDA (the index's one), Eaton and SAP (a current 20-F); never Rogers for Roche.
    assert facts == {FACTS_URL.format(cik=c) for c in (1045810, EATON_CIK, SAP_CIK)}
    assert summary["name_matched"] == ["ETN", "SAP"]
    # Roche's name, held against the whole list, finds no filer either.
    assert summary["name_searches"] == 1 and summary["name_found"] == []
    # One filing list for each name that agreed (Eaton, SAP), none for Rogers.
    assert summary["name_checks"] == 2
    assert SUBMISSIONS_URL.format(cik=ROGERS_CIK) not in sec.requests
    eaton = await db.get(SecCompany, EATON_CIK)
    assert eaton.universe_seen_at is not None and eaton.isin == "IE00B8KQN827"
    assert await db.get(SecCompany, ROGERS_CIK) is None
    assert (await db.get(SecCompany, SAP_CIK)).isin == "DE0007164600"
    rows = {
        t: (r.title, r.annual_form)
        for t, r in [(t, await db.get(SecTicker, t)) for t in ("ETN", "ROG", "SAP")]
    }
    assert rows == {
        "ETN": ("Eaton Corp plc", "10-K"),
        "ROG": ("ROGERS CORP", None),
        "SAP": ("SAP SE", "20-F"),
    }

    # The next pass asks neither list again: the names and forms are on record.
    sec.requests.clear()
    summary = await sync.refresh()
    assert TICKERS_URL not in sec.requests
    assert not [u for u in sec.requests if "/submissions/" in u and str(EATON_CIK) in u]
    assert summary["name_checks"] == 0 and summary["name_matched"] == ["ETN", "SAP"]
    assert summary["name_searches"] == 0


async def _none():
    return None
