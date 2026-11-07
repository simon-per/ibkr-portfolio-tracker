"""
The weekday filings check (docs/company-financials.md, *Schedule*): between the Sunday
passes, a company whose next 10-Q or 10-K is due costs one `submissions` request, and
companyfacts is fetched only when the SEC lists a filing for a later period than stored.
Before it, AMD's Q3 10-Q (filed 2026-11-04) waited for the Sunday pass while Yahoo's
figures elsewhere in the app already had the quarter.

Offline, on the EDGAR pass's own fake SEC (`test_company_financials_sync.Edgar`).
"""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.clock import utcnow
from app.config import settings
from app.models.company_financials import CompanyFinancial, SecCompany
from app.models.sync_run import SyncRun
from app.services import company_financials_sync, scheduler_service
from app.services.company_financials_sync import (
    CHECK_SYNC_TYPE,
    CHECKED_WITHIN,
    FILING_EXPECTED_AFTER,
    FILING_WATCH_UNTIL,
    NOT_CONFIGURED_WARNING,
    SCHEDULED_SYNC_TYPE,
    CompanyFinancialsSync,
)
from app.services.edgar_client import FACTS_URL, SUBMISSIONS_URL, latest_periodic_report
from tests.test_company_financials_sync import NVDA_CIK, UA, Edgar, _hold, _sync


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)


class FilingEdgar(Edgar):
    """The fake SEC, with each company's recent filings in its submissions document."""

    def __init__(self, filings):
        super().__init__()
        self.filings = filings  # cik -> [(form, reportDate)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for cik, rows in self.filings.items():
            if url == SUBMISSIONS_URL.format(cik=cik):
                self.requests.append(request)
                return httpx.Response(
                    200,
                    json={
                        "filings": {
                            "recent": {
                                "form": [form for form, _ in rows],
                                "reportDate": [day for _, day in rows],
                            }
                        }
                    },
                )
        return super().handler(request)


async def _stored(db):
    """NVDA held and fetched once, as a weekly pass leaves it; its newest period end."""
    await _hold(db, 1, "NVDA")
    await db.commit()
    await _sync(db, Edgar()).refresh()
    company = await db.get(SecCompany, NVDA_CIK)
    company.fetched_at = utcnow() - timedelta(days=3)
    await db.commit()
    return (
        await db.execute(
            select(func.max(CompanyFinancial.period_end)).where(
                CompanyFinancial.cik == NVDA_CIK
            )
        )
    ).scalar()


def _on(monkeypatch, day):
    monkeypatch.setattr(company_financials_sync, "utc_today", lambda: day)


@pytest.mark.asyncio
async def test_a_new_filing_fetches_the_company(db, configured, monkeypatch):
    newest = await _stored(db)
    _on(monkeypatch, newest + FILING_EXPECTED_AFTER + timedelta(days=5))
    quarter_later = (newest + timedelta(days=91)).isoformat()
    edgar = FilingEdgar({NVDA_CIK: [("8-K", "2099-01-01"), ("10-Q", quarter_later)]})

    summary = await _sync(db, edgar).check_filings()

    assert summary["status"] == "success", summary
    assert (summary["watching"], summary["checked"], summary["new_filings"]) == (
        1,
        1,
        1,
    )
    assert edgar.urls() == [
        SUBMISSIONS_URL.format(cik=NVDA_CIK),
        FACTS_URL.format(cik=NVDA_CIK),
    ]
    assert summary["fetched"] == 1
    assert summary["outcomes"][0]["symbols"] == ["NVDA"]
    company = await db.get(SecCompany, NVDA_CIK)
    assert utcnow() - company.fetched_at < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_no_new_filing_costs_one_small_request(db, configured, monkeypatch):
    newest = await _stored(db)
    _on(monkeypatch, newest + FILING_EXPECTED_AFTER)
    # The stored period itself, and a date a few days off it: neither is a new quarter.
    edgar = FilingEdgar(
        {
            NVDA_CIK: [
                ("10-Q", newest.isoformat()),
                ("10-K", (newest + timedelta(days=3)).isoformat()),
            ]
        }
    )

    summary = await _sync(db, edgar).check_filings()

    assert edgar.urls() == [SUBMISSIONS_URL.format(cik=NVDA_CIK)]
    assert (summary["checked"], summary["new_filings"], summary["fetched"]) == (1, 0, 0)


@pytest.mark.parametrize(
    "age",
    [
        FILING_EXPECTED_AFTER
        - timedelta(days=1),  # the quarter is not over long enough
        FILING_WATCH_UNTIL + timedelta(days=1),  # past it: the weekly pass's alone
    ],
)
@pytest.mark.asyncio
async def test_outside_the_window_nothing_is_asked(db, configured, monkeypatch, age):
    newest = await _stored(db)
    _on(monkeypatch, newest + age)
    edgar = FilingEdgar({NVDA_CIK: [("10-Q", "2099-01-01")]})

    summary = await _sync(db, edgar).check_filings()

    assert edgar.requests == []
    assert summary["watching"] == 0


@pytest.mark.asyncio
async def test_a_company_fetched_lately_is_not_checked(db, configured, monkeypatch):
    newest = await _stored(db)
    company = await db.get(SecCompany, NVDA_CIK)
    company.fetched_at = utcnow() - CHECKED_WITHIN + timedelta(hours=1)
    await db.commit()
    _on(monkeypatch, newest + FILING_EXPECTED_AFTER)
    edgar = FilingEdgar({NVDA_CIK: [("10-Q", "2099-01-01")]})

    await _sync(db, edgar).check_filings()

    assert edgar.requests == []


@pytest.mark.asyncio
async def test_without_a_user_agent_nothing_is_asked(db, monkeypatch):
    monkeypatch.setattr(settings, "edgar_user_agent", "", raising=False)
    edgar = FilingEdgar({})

    summary = await CompanyFinancialsSync(db, transport=edgar.transport).check_filings()

    assert summary["status"] == "skipped"
    assert summary["warnings"] == [NOT_CONFIGURED_WARNING]
    assert edgar.requests == []


@pytest.mark.asyncio
async def test_the_scheduled_check_records_its_own_type(db, configured, monkeypatch):
    """Its own `sync_type`, so the weekly pass's `_new_findings` never reads a check's
    partial lists as the previous weekly run's."""
    newest = await _stored(db)
    _on(monkeypatch, newest + FILING_EXPECTED_AFTER)
    edgar = FilingEdgar({NVDA_CIK: [("10-Q", newest.isoformat())]})

    class _Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(scheduler_service, "AsyncSessionLocal", lambda: _Session())
    original = CompanyFinancialsSync.__init__

    def with_fake_sec(self, session, transport=None, sleep=None):
        async def no_sleep(_):
            return None

        original(self, session, transport=edgar.transport, sleep=no_sleep)

    monkeypatch.setattr(CompanyFinancialsSync, "__init__", with_fake_sec)
    result = await scheduler_service.SchedulerService().company_filings_check_job()

    assert result["type"] == CHECK_SYNC_TYPE and result["status"] == "success", result
    assert result["company_filings_result"]["checked"] == 1
    runs = (await db.execute(select(SyncRun.sync_type))).scalars().all()
    assert CHECK_SYNC_TYPE in runs and SCHEDULED_SYNC_TYPE not in runs
    assert UA not in json.dumps(result)


def test_the_newest_periodic_report_is_read_off_the_filing_list():
    body = {
        "filings": {
            "recent": {
                "form": ["8-K", "10-Q", "10-K", "10-Q", "10-Q"],
                "reportDate": [
                    "2026-11-01",
                    "2026-09-28",
                    "2026-01-25",
                    "not a date",
                    "",
                ],
            }
        }
    }
    assert latest_periodic_report(body).isoformat() == "2026-09-28"
    assert latest_periodic_report({}) is None
    assert latest_periodic_report({"filings": {"recent": {"form": "10-Q"}}}) is None
