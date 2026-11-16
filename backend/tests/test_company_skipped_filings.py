"""
A filing companyfacts skipped, older than the newest (STATUS 7a, 2026-11-16).

`_filing_behind` reads the newest filing the weekday check is told about, and nothing older:
AEP's and EXC's Q2 2026 10-Qs never reached companyfacts although their Q3s did, so Q2's
diluted EPS (not additive, so no ladder gives it) stayed empty until read by hand
(2026-11-11). NU's FY2025 20-F was past every window the check watches. Now the figures say
when a filing is missing (`skipped`) and the pass asks the filing list once a month at most,
reading every listed filing companyfacts carries no statement fact of, whole and once.
Measured over the 132 companies: 11 asked, 8 filings read (AEP, EXC, ALNY, NU, TSMC, SPGI).
Offline: the EDGAR pass's fake SEC.
"""

from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models.company_financials import CompanyFinancial, SecCompany, SecFilingRead
from app.services.company_financials_sync import LISTING_RECHECK_AFTER
from app.services.edgar_client import SUBMISSIONS_URL, filing_url
from app.services.edgar_filing_facts import companyfacts_accns, skipped
from tests.test_company_filing_fallback import ACCN, INSTANCE, INSTANCE_NAME, Q3_END
from tests.test_company_filings_check import _on, _stored
from app.config import settings
from tests.test_company_financials_sync import FIXTURE, NVDA_CIK, UA, Edgar, _sync

TODAY = date(2026, 11, 16)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)


def _q(end, metric="net_income", fp="Q2"):
    return {"period_end": end, "fiscal_period": fp, "metric": metric, "value": 1.0}


def _cf(*periods):
    """A companyfacts document whose filings each report one quarter as their own."""
    rows = []
    for i, (start, end) in enumerate(periods):
        rows.append({"start": start, "end": end, "val": 1e6, "form": "10-Q",
                     "accn": f"a-{i}", "filed": end})
    return {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": rows}}},
                      "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                          {"end": "2026-10-20", "val": 5, "form": "10-Q", "accn": "cover"}]}}}}}


Q1, Q2, Q3 = date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30)


def test_a_quarter_no_filing_reports_is_a_sign():
    """AEP: Q2 comes from Q3's year-to-date ladder; no filing companyfacts names is Q2's."""
    doc = _cf(("2026-01-01", "2026-03-31"), ("2026-07-01", "2026-09-30"))
    rows = [_q(Q1), _q(Q2), _q(Q3)]
    assert skipped(rows, doc, [], TODAY) == [Q2]
    # Once read, its own period is known: no sign.
    assert skipped(rows, doc, [Q2], TODAY) == []


def test_a_quarter_missing_between_two_is_a_sign():
    doc = _cf(("2026-01-01", "2026-03-31"), ("2026-07-01", "2026-09-30"))
    signs = skipped([_q(Q1), _q(Q3)], doc, [], TODAY)
    assert len(signs) == 1 and Q1 < signs[0] < Q3


def test_figures_past_due_are_a_sign_and_a_complete_history_is_none():
    doc = _cf(("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30"),
              ("2026-07-01", "2026-09-30"))
    rows = [_q(Q1), _q(Q2), _q(Q3)]
    assert skipped(rows, doc, [], TODAY) == []
    assert skipped(rows, doc, [], Q3 + timedelta(days=151)) == [Q3 + timedelta(days=151)]
    # An annual-only filer is judged by its years: a 20-F a year and a few months on.
    years = [_q(date(2024, 12, 31), fp="FY")]
    assert skipped(years, {}, [date(2024, 12, 31)], date(2025, 9, 1)) == []
    assert skipped(years, {}, [date(2024, 12, 31)], TODAY) == [TODAY]


def test_the_cover_page_alone_is_not_a_filing_companyfacts_carries():
    """TSMC's FY2025 20-F was in companyfacts with two cover facts and no figure."""
    assert companyfacts_accns(_cf(("2026-01-01", "2026-03-31"))) == {"a-0"}


class ListingEdgar(Edgar):
    """companyfacts is the fixture; `submissions` lists one 10-Q it lacks and one it has."""

    def __init__(self, listed_in_companyfacts):
        super().__init__()
        self.known = listed_in_companyfacts

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == SUBMISSIONS_URL.format(cik=NVDA_CIK):
            self.requests.append(request)
            recent = {
                "form": ["10-Q", "8-K", "10-Q"],
                "reportDate": [Q3_END, Q3_END, "2025-01-01"],
                "accessionNumber": [ACCN, "8k", self.known],
                "filingDate": ["2026-11-19", "2026-11-19", "2025-02-01"],
            }
            return httpx.Response(200, json={"filings": {"recent": recent}})
        if url == filing_url(NVDA_CIK, ACCN, "index.json"):
            self.requests.append(request)
            return httpx.Response(200, json={"directory": {"item": [{"name": INSTANCE_NAME}]}})
        if url == filing_url(NVDA_CIK, ACCN, INSTANCE_NAME):
            self.requests.append(request)
            return httpx.Response(200, content=INSTANCE)
        return super().handler(request)


@pytest.mark.asyncio
async def test_the_weekly_pass_reads_a_skipped_filing_once(db, configured, monkeypatch):
    newest = await _stored(db)
    _on(monkeypatch, newest + timedelta(days=160))  # the figures are past due
    known = next(iter(companyfacts_accns(FIXTURE)))
    edgar = ListingEdgar(known)
    company = await db.get(SecCompany, NVDA_CIK)
    company.fetched_at -= timedelta(days=7)
    await db.commit()

    summary = await _sync(db, edgar).refresh()

    assert summary["filings_listed"] == 1
    assert summary["skipped_filings"] == [f"NVDA 10-Q {Q3_END}"]
    reads = (await db.execute(select(SecFilingRead.accn))).scalars().all()
    assert reads == [ACCN]  # not the 8-K, not the 10-Q companyfacts carries
    value = (await db.execute(select(CompanyFinancial.value).where(
        CompanyFinancial.cik == NVDA_CIK, CompanyFinancial.metric == "revenue",
        CompanyFinancial.period_end == date.fromisoformat(Q3_END)))).scalar()
    assert value == 101e9 and newest < date.fromisoformat(Q3_END)
    assert (await db.get(SecCompany, NVDA_CIK)).filings_listed_at is not None

    # The next week: the filing is read; the list is not asked again within the month.
    company = await db.get(SecCompany, NVDA_CIK)
    company.fetched_at -= timedelta(days=7)
    await db.commit()
    asked = len(edgar.requests)
    again = await _sync(db, edgar).refresh()
    assert "filings_listed" not in again
    assert not [r for r in edgar.requests[asked:] if "/Archives/" in str(r.url)]


class EmptyListing(Edgar):
    """`submissions` lists nothing: the sign stays, nothing answers it."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == SUBMISSIONS_URL.format(cik=NVDA_CIK):
            self.requests.append(request)
            return httpx.Response(200, json={"filings": {"recent": {}}})
        return super().handler(request)


async def _weekly(db, edgar):
    company = await db.get(SecCompany, NVDA_CIK)
    company.fetched_at -= timedelta(days=7)
    await db.commit()
    return await _sync(db, edgar).refresh()


@pytest.mark.asyncio
async def test_a_sign_no_listed_filing_answers_asks_once_a_month(
    db, configured, monkeypatch
):
    newest = await _stored(db)
    assert (await db.get(SecCompany, NVDA_CIK)).filings_listed_at is None  # no sign yet
    _on(monkeypatch, newest + timedelta(days=160))  # past due: a sign every week
    edgar = EmptyListing()

    first = await _weekly(db, edgar)
    assert first["filings_listed"] == 1 and first["filings_read"] == 0
    for _ in range(3):  # three more weekly passes within LISTING_RECHECK_AFTER
        assert "filings_listed" not in await _weekly(db, edgar)

    company = await db.get(SecCompany, NVDA_CIK)
    company.filings_listed_at -= LISTING_RECHECK_AFTER
    await db.commit()
    fourth = await _weekly(db, edgar)
    assert fourth["filings_listed"] == 1 and fourth["filings_read"] == 0
