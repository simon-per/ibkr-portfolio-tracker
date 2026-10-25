"""
The EDGAR pass (docs/company-financials.md): scope, one request per company, the User-Agent,
bounded retries, back-off, refuse-whole writes, and the missing-key path.

Offline: EDGAR is an `httpx.MockTransport` serving the trimmed NVDA fixture, the database
is in memory, and the client's sleeps are recorded rather than slept.
"""

import json
import logging
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.clock import utc_today, utcnow
from app.config import settings
from app.models.company_financials import CompanyFinancial, SecurityCik
from app.models.security import Security
from app.models.sync_run import SyncRun
from app.models.taxlot import TaxLot
from app.services import scheduler_service
from app.services.company_financials_sync import (
    NOT_CONFIGURED_WARNING,
    SCHEDULED_SYNC_TYPE,
    STALE_AFTER,
    CompanyFinancialsSync,
)
from app.services.edgar_client import (
    FACTS_URL,
    MIN_INTERVAL_S,
    RETRY_DELAYS_S,
    TICKERS_URL,
    sec_ticker,
)

UA = "test-suite-agent contact@example.invalid"
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "edgar_nvda_companyfacts.json").read_text(
        "utf-8"
    )
)
NVDA_CIK, ALPHABET_CIK, ASML_CIK = 1045810, 1652044, 937966
TICKERS = {
    "0": {"cik_str": NVDA_CIK, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "1": {"cik_str": ALPHABET_CIK, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "2": {"cik_str": ALPHABET_CIK, "ticker": "GOOG", "title": "Alphabet Inc."},
    "3": {"cik_str": ASML_CIK, "ticker": "ASML", "title": "ASML HOLDING NV"},
}
IFRS_ONLY = {"cik": ASML_CIK, "entityName": "ASML", "facts": {"ifrs-full": {}}}


class Edgar:
    """A fake SEC: records every request, answers from `facts` by CIK."""

    def __init__(self, facts=None, status=None):
        self.requests = []
        self.facts = (
            facts
            if facts is not None
            else {
                NVDA_CIK: FIXTURE,
                ALPHABET_CIK: FIXTURE,
                ASML_CIK: IFRS_ONLY,
            }
        )
        self.status = status or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        queued = self.status.get(url)
        if queued:
            return httpx.Response(queued.pop(0))
        if url == TICKERS_URL:
            return httpx.Response(200, json=TICKERS)
        for cik, body in self.facts.items():
            if url == FACTS_URL.format(cik=cik):
                return httpx.Response(200, json=body)
        return httpx.Response(404)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)

    def urls(self):
        return [str(r.url) for r in self.requests]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)


async def _hold(
    db, sid, symbol, exchange="NASDAQ", isin=None, currency="USD", open_=True
):
    db.add(
        Security(
            id=sid,
            isin=isin or f"US{sid:010d}",
            symbol=symbol,
            description=symbol,
            currency=currency,
            conid=1000 + sid,
            asset_category="STK",
            exchange=exchange,
        )
    )
    db.add(
        TaxLot(
            security_id=sid,
            open_date=utc_today() - timedelta(days=100),
            close_date=None if open_ else utc_today() - timedelta(days=5),
            quantity=Decimal("1"),
            cost_basis=Decimal("100"),
            price_per_unit=Decimal("100"),
            currency=currency,
            cost_basis_eur=Decimal("100"),
            is_open=open_,
        )
    )
    await db.flush()


async def _book(db):
    await _hold(db, 1, "NVDA")
    await _hold(db, 2, "GOOGL")
    await _hold(db, 3, "GOOG")
    await _hold(db, 4, "ASML")  # a 20-F filer
    await _hold(db, 5, "ABEA", exchange="IBIS", currency="EUR")  # Alphabet on Xetra
    await _hold(db, 6, "SMH", exchange="LSEETF", isin="IE00BMC38736")  # a UCITS fund
    await _hold(
        db, 7, "NVDA", exchange="NYSE", open_=False, isin="US9999999999"
    )  # sold
    await db.commit()


async def _count(db, model, *where):
    return (
        await db.execute(select(func.count()).select_from(model).where(*where))
    ).scalar()


def _sync(db, edgar, sleeps=None):
    async def sleep(seconds):
        (sleeps if sleeps is not None else []).append(seconds)

    return CompanyFinancialsSync(db, transport=edgar.transport, sleep=sleep)


@pytest.mark.asyncio
async def test_without_a_user_agent_nothing_is_asked_and_the_run_says_so(
    db, monkeypatch
):
    monkeypatch.setattr(settings, "edgar_user_agent", "", raising=False)
    await _book(db)
    edgar = Edgar()
    summary = await _sync(db, edgar).refresh()
    assert summary["status"] == "skipped"
    assert summary["warnings"] == [NOT_CONFIGURED_WARNING]
    assert edgar.requests == []
    assert await _count(db, CompanyFinancial) == 0


@pytest.mark.asyncio
async def test_the_scope_is_held_us_listed_and_not_a_fund(db, configured):
    await _book(db)
    scope = await _sync(db, Edgar()).in_scope()
    assert sorted(s.symbol for s in scope) == ["ASML", "GOOG", "GOOGL", "NVDA"]
    assert {s.id for s in scope} == {1, 2, 3, 4}


@pytest.mark.asyncio
async def test_one_request_per_company_and_rows_under_every_listing(db, configured):
    await _book(db)
    edgar = Edgar()
    summary = await _sync(db, edgar).refresh()

    assert summary["status"] == "success", summary
    urls = edgar.urls()
    assert urls.count(TICKERS_URL) == 1
    assert urls.count(FACTS_URL.format(cik=ALPHABET_CIK)) == 1  # GOOG and GOOGL: once
    assert urls.count(FACTS_URL.format(cik=NVDA_CIK)) == 1
    assert urls.count(FACTS_URL.format(cik=ASML_CIK)) == 1
    assert len(urls) == 4
    assert all(r.headers["user-agent"] == UA for r in edgar.requests)

    nvda = await _count(db, CompanyFinancial, CompanyFinancial.security_id == 1)
    assert nvda > 400
    assert await _count(db, CompanyFinancial, CompanyFinancial.security_id == 2) == nvda
    assert await _count(db, CompanyFinancial, CompanyFinancial.security_id == 3) == nvda
    assert await _count(db, CompanyFinancial, CompanyFinancial.security_id == 4) == 0
    assert summary["fetched"] == 2 and summary["no_data"] == 1

    ciks = {r.security_id: r for r in (await db.execute(select(SecurityCik))).scalars()}
    assert {sid: r.cik for sid, r in ciks.items()} == {
        1: NVDA_CIK,
        2: ALPHABET_CIK,
        3: ALPHABET_CIK,
        4: ASML_CIK,
    }
    # The 20-F filer is a miss, backed off; the filers carry their fetch stamp.
    assert ciks[4].facts_miss_count == 1 and ciks[4].facts_checked_at is not None
    assert ciks[1].fetched_at is not None and ciks[1].facts_miss_count is None


@pytest.mark.asyncio
async def test_a_second_pass_inside_the_week_asks_nothing(db, configured):
    await _book(db)
    await _sync(db, Edgar()).refresh()
    edgar = Edgar()
    summary = await _sync(db, edgar).refresh()
    assert edgar.requests == []
    assert summary["companies_due"] == 0


@pytest.mark.asyncio
async def test_a_stale_company_is_fetched_again_and_a_miss_waits_out_its_back_off(
    db, configured
):
    await _book(db)
    await _sync(db, Edgar()).refresh()
    for row in (await db.execute(select(SecurityCik))).scalars():
        if row.fetched_at is not None:
            row.fetched_at = utcnow() - STALE_AFTER - timedelta(hours=1)
    await db.commit()
    edgar = Edgar()
    await _sync(db, edgar).refresh()
    assert sorted(edgar.urls()) == sorted(
        [
            FACTS_URL.format(cik=NVDA_CIK),
            FACTS_URL.format(cik=ALPHABET_CIK),
        ]
    )
    # --force asks the backed-off one too.
    edgar = Edgar()
    await _sync(db, edgar).refresh(force=True)
    assert FACTS_URL.format(cik=ASML_CIK) in edgar.urls()


@pytest.mark.asyncio
async def test_a_refetch_replaces_the_rows_whole(db, configured):
    await _book(db)
    await _sync(db, Edgar()).refresh(symbols=["NVDA"])
    db.add(
        CompanyFinancial(
            security_id=1,
            period_end=utc_today(),
            fiscal_year=2099,
            fiscal_period="Q1",
            metric="revenue",
            value=1.0,
            unit="USD",
            source_concept="stale",
            filed=utc_today(),
            fetched_at=utcnow(),
        )
    )
    await db.commit()
    await _sync(db, Edgar()).refresh(symbols=["NVDA"], force=True)
    assert (
        await _count(db, CompanyFinancial, CompanyFinancial.source_concept == "stale")
        == 0
    )


@pytest.mark.asyncio
async def test_a_failing_company_keeps_its_rows_stamps_nothing_and_is_warned(
    db, configured
):
    await _book(db)
    await _sync(db, Edgar()).refresh(symbols=["NVDA"])
    before = await _count(db, CompanyFinancial, CompanyFinancial.security_id == 1)
    url = FACTS_URL.format(cik=NVDA_CIK)
    edgar = Edgar(status={url: [503, 503, 503]})
    sleeps = []
    summary = await _sync(db, edgar, sleeps).refresh(symbols=["NVDA"], force=True)
    assert edgar.urls().count(url) == len(RETRY_DELAYS_S) + 1  # bounded
    # The back-off between attempts, beside the 5-a-second pacing before each request.
    assert [s for s in sleeps if s >= 1] == list(RETRY_DELAYS_S)
    assert all(s <= MIN_INTERVAL_S for s in sleeps if s < 1)
    assert summary["errors"] == 1
    assert any("could not be fetched" in w for w in summary["warnings"])
    assert (
        await _count(db, CompanyFinancial, CompanyFinancial.security_id == 1) == before
    )
    stamps = await db.get(SecurityCik, 1)
    assert stamps.facts_miss_count is None


@pytest.mark.asyncio
async def test_a_retry_that_succeeds_is_not_a_failure(db, configured):
    await _book(db)
    url = FACTS_URL.format(cik=NVDA_CIK)
    edgar = Edgar(status={url: [429]})
    summary = await _sync(db, edgar).refresh(symbols=["NVDA"])
    assert summary["fetched"] == 1 and summary["errors"] == 0
    assert edgar.urls().count(url) == 2


@pytest.mark.asyncio
async def test_a_refused_user_agent_stops_the_pass(db, configured):
    await _book(db)
    edgar = Edgar(status={TICKERS_URL: [403]})
    summary = await _sync(db, edgar).refresh()
    assert edgar.urls() == [TICKERS_URL]
    assert summary["stopped"] == 403
    assert any("EDGAR_USER_AGENT" in w for w in summary["warnings"])


@pytest.mark.asyncio
async def test_a_persistent_rate_limit_stops_the_pass(db, configured):
    await _book(db)
    await _sync(db, Edgar()).refresh(symbols=["NVDA", "GOOGL"])
    first = FACTS_URL.format(cik=ALPHABET_CIK)
    edgar = Edgar(
        status={first: [429, 429, 429], FACTS_URL.format(cik=NVDA_CIK): [429] * 3}
    )
    summary = await _sync(db, edgar).refresh(symbols=["NVDA", "GOOGL"], force=True)
    assert summary["stopped"] == 429
    assert (
        len(edgar.requests) == len(RETRY_DELAYS_S) + 1
    )  # the next company is not asked


@pytest.mark.asyncio
async def test_a_dry_run_asks_and_writes_nothing(db, configured):
    await _book(db)
    edgar = Edgar()
    summary = await _sync(db, edgar).refresh(dry_run=True)
    assert edgar.requests == []
    assert summary["cik_lookups"] == 4
    assert await _count(db, SecurityCik) == 0


def test_tickers_are_spelled_the_secs_way():
    assert sec_ticker("BRK B") == "BRK-B"
    assert sec_ticker("brk.b") == "BRK-B"
    assert sec_ticker("NVDA") == "NVDA"


@pytest.mark.asyncio
async def test_the_scheduled_job_records_a_run_and_never_logs_the_user_agent(
    db, configured, monkeypatch, caplog
):
    await _book(db)
    edgar = Edgar()

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
    caplog.set_level(logging.DEBUG)
    result = await scheduler_service.SchedulerService().company_financials_refresh_job()

    assert result["type"] == SCHEDULED_SYNC_TYPE
    assert result["status"] == "success", result
    run = (
        await db.execute(
            select(SyncRun).where(SyncRun.sync_type == SCHEDULED_SYNC_TYPE)
        )
    ).scalar_one()
    assert run.status == "success"
    assert UA not in caplog.text
    assert UA not in json.dumps(run.details) + (run.message or "")


@pytest.mark.asyncio
async def test_the_scheduled_job_without_a_user_agent_records_its_warning(
    db, monkeypatch
):
    monkeypatch.setattr(settings, "edgar_user_agent", "", raising=False)

    class _Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(scheduler_service, "AsyncSessionLocal", lambda: _Session())
    result = await scheduler_service.SchedulerService().company_financials_refresh_job()
    assert result["status"] == "skipped"
    assert result["warnings"] == [NOT_CONFIGURED_WARNING]
    run = (await db.execute(select(SyncRun))).scalar_one()
    assert run.warnings == [NOT_CONFIGURED_WARNING]
