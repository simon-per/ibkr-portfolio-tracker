"""
Alpha Vantage, last and fill-only (2026-11-16, the source waterfall's fifth layer).

The owner's rules: a figure SEC gave is never replaced; a figure the company stopped
reporting stays blank (ABNB's capex) — so only an interior gap of a recent quarter is
filled; Alpha Vantage is proven per company and metric against SEC before any of it is
used; its "None", 0 and missing fields are no figure; SEC's currency only; no bank, broker
or insurer; a company asked at most weekly, under the one daily budget. Offline: a fake
Alpha Vantage, never the network.
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.clock import utcnow
from app.models.company_financials import CompanyFinancial, SecCompany, VendorStatement
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.services import alpha_vantage as av
from app.services.company_vendor_fill import (
    CompanyVendorFill,
    compact,
    fill,
    interior_gaps,
    vendor_backoff_until,
)

QUARTERS = [date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31),
            date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)]
TODAY = date(2026, 2, 20)


def _rows(capex_missing=(QUARTERS[5],), capex=lambda i: 100e6 + i * 1e6):
    rows = []
    for i, end in enumerate(QUARTERS):
        label = {"fiscal_year": end.year, "fiscal_period": f"Q{(end.month - 1) // 3 + 1}"}
        rows.append({"period_end": end, **label, "metric": "revenue", "value": 1e9,
                     "unit": "USD", "source_concept": "Revenues", "filed": end})
        if end not in capex_missing:
            rows.append({"period_end": end, **label, "metric": "capex", "value": capex(i),
                         "unit": "USD", "source_concept": "PaymentsToAcquirePropertyPlantAndEquipment",
                         "filed": end})
    return rows


def _cash_flow(values, currency="USD"):
    """Alpha Vantage's CASH_FLOW answer: strings, "None" for an absent field."""
    return {"symbol": "ABC", "annualReports": [], "quarterlyReports": [
        {"fiscalDateEnding": end.isoformat(), "reportedCurrency": currency,
         "capitalExpenditures": value, "operatingCashflow": "None"}
        for end, value in values.items()
    ]}


def _docs(values, currency="USD"):
    return {"CASH_FLOW": {**compact("CASH_FLOW", _cash_flow(values, currency)),
                          "fetched": "2026-02-20"}}


def _agreeing(gap_value="111000000"):
    return {end: (gap_value if i == 5 else str(int(100e6 + i * 1e6)))
            for i, end in enumerate(QUARTERS)}


def _capex(rows):
    return {r["period_end"]: (r["value"], r["source_concept"]) for r in rows if r["metric"] == "capex"}


def test_a_proven_series_fills_an_interior_gap_and_tags_its_source():
    rows, filled = fill(_rows(), _docs(_agreeing()), TODAY)
    assert _capex(rows)[QUARTERS[5]] == (111e6, "alpha_vantage:capitalExpenditures")
    assert filled == {"capex": {"alpha_vantage:capitalExpenditures": 1}}
    # SEC's figures are untouched.
    assert _capex(rows)[QUARTERS[4]] == (104e6, "PaymentsToAcquirePropertyPlantAndEquipment")


def test_a_series_that_disagrees_with_sec_is_used_nowhere():
    theirs = _agreeing()
    theirs[QUARTERS[0]] = "130000000"  # 30% off SEC's 100M
    theirs[QUARTERS[1]] = "131000000"
    rows, filled = fill(_rows(), _docs(theirs), TODAY)
    assert QUARTERS[5] not in _capex(rows) and filled == {}


def test_one_agreement_is_no_proof():
    rows, filled = fill(_rows(), _docs({QUARTERS[0]: "100000000", QUARTERS[5]: "111000000"}),
                        TODAY)
    assert filled == {}


def test_a_line_the_company_stopped_reporting_stays_blank():
    """ABNB: capex until a quarter, then never again — a trailing gap, not a hole."""
    rows = _rows(capex_missing=QUARTERS[5:])
    assert interior_gaps(rows) == {}
    filled_rows, filled = fill(rows, _docs(_agreeing()), TODAY)
    assert filled == {} and QUARTERS[6] not in _capex(filled_rows)


@pytest.mark.parametrize("absent", ["None", "0", "", None])
def test_none_zero_and_missing_are_no_figure(absent):
    rows, filled = fill(_rows(), _docs(_agreeing(gap_value=absent)), TODAY)
    assert QUARTERS[5] not in _capex(rows)


def test_another_currency_is_not_read():
    rows, filled = fill(_rows(), _docs(_agreeing(), currency="EUR"), TODAY)
    assert filled == {}


def test_a_previous_fill_is_replaced_not_stacked():
    once, _ = fill(_rows(), _docs(_agreeing()), TODAY)
    twice, _ = fill(once, _docs(_agreeing()), TODAY)
    assert len([r for r in twice if r["source_concept"].startswith("alpha_vantage:")]) == 1


# ── the pass ─────────────────────────────────────────────────────────────────


class FakeAV:
    calls = []

    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, params=None):
        FakeAV.calls.append(dict(params))
        body = (_cash_flow(_agreeing()) if params["function"] == "CASH_FLOW"
                else {"annualReports": [], "quarterlyReports": []})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


@pytest.fixture
def fake_av(monkeypatch):
    FakeAV.calls = []
    monkeypatch.setattr(av.httpx, "AsyncClient", FakeAV)
    monkeypatch.setattr(av.settings, "alpha_vantage_api_key", "test-key-123456")
    return FakeAV


async def _no_sleep(_s):
    return None


async def _company(db, cik, ticker, sic=3674, rows=None):
    repo = CompanyFinancialsRepository(db)
    company = await repo.ensure_company(cik, ticker=ticker)
    company.sic = sic
    await repo.replace_rows(cik, rows if rows is not None else _rows(), utcnow())
    await db.commit()


@pytest.mark.asyncio
async def test_the_pass_asks_only_the_statements_a_gap_needs_and_fills(db, fake_av):
    await _company(db, 1, "ABC")
    summary = await CompanyVendorFill(db, sleep=_no_sleep).run()

    assert [c["function"] for c in fake_av.calls] == ["CASH_FLOW"]
    assert summary["spent"] == 1 and summary["filled"] == [
        "ABC capex <- alpha_vantage:capitalExpenditures (1)"]
    stored = (await db.execute(select(CompanyFinancial).where(
        CompanyFinancial.source_concept.like("alpha_vantage:%")))).scalars().all()
    assert [(r.period_end, r.value) for r in stored] == [(QUARTERS[5], 111e6)]
    assert (await db.get(VendorStatement, (1, "CASH_FLOW"))) is not None
    company = await db.get(SecCompany, 1)
    assert company.vendor_checked_at is not None and company.vendor_miss_count is None


@pytest.mark.asyncio
async def test_a_company_is_asked_at_most_weekly(db, fake_av):
    await _company(db, 1, "ABC")
    await CompanyVendorFill(db, sleep=_no_sleep).run()
    again = await CompanyVendorFill(db, sleep=_no_sleep).run()
    assert len(fake_av.calls) == 1 and again["due"] == 0
    company = await db.get(SecCompany, 1)
    assert vendor_backoff_until(company) == company.vendor_checked_at + timedelta(days=7)


@pytest.mark.asyncio
async def test_banks_brokers_and_insurers_are_never_asked(db, fake_av):
    await _company(db, 1, "BNK", sic=6021)
    await _company(db, 2, "INS", sic=6331)
    assert (await CompanyVendorFill(db, sleep=_no_sleep).run())["due"] == 0
    assert fake_av.calls == []


@pytest.mark.asyncio
async def test_the_backlog_waits_for_another_days_budget(db, fake_av):
    for cik in range(1, 20):
        await _company(db, cik, f"C{cik}")
    summary = await CompanyVendorFill(db, sleep=_no_sleep).run()
    assert len(fake_av.calls) == av.LIMITS[av.STATEMENTS]
    assert summary["spent"] == av.LIMITS[av.STATEMENTS] and summary["due"] == 19
    assert (await av.budget(db, av.PRICES))["left"] == av.PRICE_RESERVE


@pytest.mark.asyncio
async def test_without_a_key_nothing_is_asked(db, monkeypatch):
    monkeypatch.setattr(av.settings, "alpha_vantage_api_key", "")
    await _company(db, 1, "ABC")
    assert (await CompanyVendorFill(db, sleep=_no_sleep).run())["status"] == "skipped"


@pytest.mark.asyncio
async def test_the_weekly_rewrite_applies_the_stored_statements_again(db, monkeypatch):
    """`replace_rows` replaces every row; the EDGAR fetch proves the stored answer again,
    so Sunday's rewrite does not wipe what Alpha Vantage filled on Saturday."""
    from app.config import settings
    from app.services import company_financials_sync
    from tests.test_company_financials_sync import NVDA_CIK, UA, Edgar, _hold, _sync

    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)
    monkeypatch.setattr(company_financials_sync.CompanyFinancialsSync, "_reads_segments",
                        lambda *a: False)

    async def no(self, cik, facts):
        return False

    monkeypatch.setattr(company_financials_sync.CompanyFinancialsSync, "_reads_extensions", no)
    seen = []

    def spy(rows, statements, today):
        seen.append(sorted(statements))
        return list(rows), {}

    monkeypatch.setattr(company_financials_sync, "vendor_fill", spy)
    await _hold(db, 1, "NVDA")
    repo = CompanyFinancialsRepository(db)
    await repo.ensure_company(NVDA_CIK)
    await repo.write_vendor_statement(NVDA_CIK, "CASH_FLOW", {"quarterly": []}, utcnow())
    await db.commit()
    await _sync(db, Edgar()).refresh()
    assert seen == [["CASH_FLOW"]]
