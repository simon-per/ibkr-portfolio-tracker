"""
A filing companyfacts has not taken in (review item CO5, 2026-11-08).

Six companies (two held) sat on their Q1 2026 for 221 days: their Q2 10-Qs were filed and
listed in `submissions`, and companyfacts never carried them. The weekday check refetched
companyfacts every day inside its window (about sixty fetches that moved nothing) and never
looked past it. Now:

- the fetch reads the listed filing's own XBRL instance when companyfacts stops short of
  it, undimensioned chain facts included (`parse_instance(whole=True)`), once ever;
- a fetch that still stops short backs off on the shared doubling, counted across fetches;
- past the daily window the check asks a stale company once a week, up to a year on.

Offline: the EDGAR pass's fake SEC, a synthetic instance.
"""

from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models.company_financials import CompanyFinancial, SecCompany
from app.services.company_financials_normaliser import normalise
from app.services.company_financials_sync import (
    CHASE_WEEKDAY,
    FILING_CHASE_UNTIL,
    FILING_WATCH_UNTIL,
)
from app.services.edgar_client import (
    SUBMISSIONS_URL,
    filing_url,
    latest_periodic_filing,
)
from app.services.edgar_filing_facts import parse_instance
from app.config import settings
from tests.test_company_filings_check import _on, _stored
from tests.test_company_financials_sync import UA, FIXTURE, NVDA_CIK, Edgar, _sync

ACCN = "0001045810-26-000123"
Q3_END = "2026-10-25"
INSTANCE_NAME = "nvda-20261025.xml"

_CONTEXT = (
    '<xbrli:context id="{id}"><xbrli:entity><xbrli:identifier scheme="x">1'
    "</xbrli:identifier>{segment}</xbrli:entity><xbrli:period>{period}</xbrli:period>"
    "</xbrli:context>"
)
_QUARTER = f"<xbrli:startDate>2026-07-27</xbrli:startDate><xbrli:endDate>{Q3_END}</xbrli:endDate>"
_MEMBER = (
    "<xbrli:segment><xbrldi:explicitMember "
    'dimension="us-gaap:StatementEquityComponentsAxis">us-gaap:CommonStockMember'
    "</xbrldi:explicitMember></xbrli:segment>"
)


def _fact(concept, context, unit, value):
    return (
        f'<us-gaap:{concept} contextRef="{context}" unitRef="{unit}" decimals="-6">'
        f"{value}</us-gaap:{concept}>"
    )


INSTANCE = "".join(
    [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" ',
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" ',
        'xmlns:us-gaap="http://fasb.org/us-gaap/2025" ',
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217">',
        _CONTEXT.format(id="q3", segment="", period=_QUARTER),
        _CONTEXT.format(
            id="now", segment="", period=f"<xbrli:instant>{Q3_END}</xbrli:instant>"
        ),
        _CONTEXT.format(id="q3_cs", segment=_MEMBER, period=_QUARTER),
        '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>',
        '<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>',
        '<xbrli:unit id="usdps"><xbrli:divide><xbrli:unitNumerator>',
        "<xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unitNumerator>",
        "<xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure>",
        "</xbrli:unitDenominator></xbrli:divide></xbrli:unit>",
        _fact("Revenues", "q3", "usd", 101000000000),
        _fact("NetIncomeLoss", "q3", "usd", 62000000000),
        _fact("EarningsPerShareDiluted", "q3", "usdps", 2.55),
        _fact(
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "q3",
            "shares",
            24300000000,
        ),
        _fact("CashAndCashEquivalentsAtCarryingValue", "now", "usd", 25000000000),
        _fact("CommonStockDividendsPerShareDeclared", "q3", "usdps", 0.25),
        _fact("CommonStockDividendsPerShareDeclared", "q3_cs", "usdps", 0.26),
        "</xbrli:xbrl>",
    ]
).encode()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)


class LaggingEdgar(Edgar):
    """companyfacts stops at the fixture's newest quarter; `submissions` lists the next
    10-Q, with its accession number and filing date; the filing serves its instance."""

    def __init__(self, period_end=Q3_END, instance=True):
        super().__init__()
        self.period_end, self.instance = period_end, instance

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == SUBMISSIONS_URL.format(cik=NVDA_CIK):
            self.requests.append(request)
            recent = {
                "form": ["10-Q"],
                "reportDate": [self.period_end],
                "accessionNumber": [ACCN],
                "filingDate": ["2026-11-19"],
            }
            return httpx.Response(200, json={"filings": {"recent": recent}})
        if url == filing_url(NVDA_CIK, ACCN, "index.json"):
            self.requests.append(request)
            items = [{"name": INSTANCE_NAME}] if self.instance else []
            return httpx.Response(200, json={"directory": {"item": items}})
        if url == filing_url(NVDA_CIK, ACCN, INSTANCE_NAME):
            self.requests.append(request)
            return httpx.Response(200, content=INSTANCE)
        return super().handler(request)


def test_a_whole_read_keeps_the_undimensioned_facts_and_they_win():
    facts = {
        (f["concept"], f["start"], f["end"]): f
        for f in parse_instance(INSTANCE, whole=True)
    }
    assert facts[("Revenues", "2026-07-27", Q3_END)]["unit"] == "USD"
    assert facts[("CashAndCashEquivalentsAtCarryingValue", None, Q3_END)]["val"] == 25e9
    shares = ("WeightedAverageNumberOfDilutedSharesOutstanding", "2026-07-27", Q3_END)
    assert facts[shares]["unit"] == "shares"
    # The undimensioned 0.25 is what companyfacts would carry; the member's 0.26 is not.
    dps = facts[("CommonStockDividendsPerShareDeclared", "2026-07-27", Q3_END)]
    assert (dps["unit"], dps["val"]) == ("USD/shares", 0.25)
    # Not whole: the member's alone, as before.
    assert [f["val"] for f in parse_instance(INSTANCE)] == [0.26]


def test_the_filing_list_names_its_newest_filing():
    recent = {
        "form": ["10-Q", "8-K", "10-Q"],
        "reportDate": ["2026-07-26", "2026-12-01", Q3_END],
        "accessionNumber": ["a", "b", ACCN],
        "filingDate": ["2026-08-27", "2026-12-02", "2026-11-19"],
    }
    newest = latest_periodic_filing({"filings": {"recent": recent}})
    assert (newest.accn, newest.filed, newest.period_end.isoformat()) == (
        ACCN,
        date(2026, 11, 19),
        Q3_END,
    )


def test_the_instance_fills_only_what_companyfacts_lacks():
    facts = [
        {**f, "accn": ACCN, "form": "10-Q", "filed": "2026-11-19"}
        for f in parse_instance(INSTANCE, whole=True)
    ]
    rows = {
        (r["period_end"].isoformat(), r["metric"]): r
        for r in normalise(FIXTURE, facts).rows
    }
    assert rows[(Q3_END, "revenue")]["value"] == 101e9
    assert rows[(Q3_END, "revenue")]["fiscal_period"] == "Q3"
    assert rows[(Q3_END, "cash")]["value"] == 25e9


@pytest.mark.asyncio
async def test_the_check_reads_the_filing_companyfacts_lacks(
    db, configured, monkeypatch
):
    newest = await _stored(db)
    _on(monkeypatch, newest + timedelta(days=110))
    edgar = LaggingEdgar()

    summary = await _sync(db, edgar).check_filings()

    assert summary["filings_read"] == 1 and "lagging" not in summary
    assert edgar.urls()[-2:] == [
        filing_url(NVDA_CIK, ACCN, "index.json"),
        filing_url(NVDA_CIK, ACCN, INSTANCE_NAME),
    ]
    stored = (
        await db.execute(
            select(CompanyFinancial.value).where(
                CompanyFinancial.cik == NVDA_CIK,
                CompanyFinancial.metric == "revenue",
                CompanyFinancial.period_end == date.fromisoformat(Q3_END),
            )
        )
    ).scalar()
    assert stored == 101e9
    assert (await db.get(SecCompany, NVDA_CIK)).facts_miss_count is None


@pytest.mark.asyncio
async def test_a_fetch_that_stays_short_backs_off_and_counts_on(
    db, configured, monkeypatch
):
    newest = await _stored(db)
    _on(monkeypatch, newest + timedelta(days=110))
    edgar = LaggingEdgar(instance=False)  # the filing lists no instance

    first = await _sync(db, edgar).check_filings()
    assert first["lagging"] == ["NVDA"]
    company = await db.get(SecCompany, NVDA_CIK)
    assert company.facts_miss_count == 1

    # The next weekday: backed off, nothing asked.
    _on(monkeypatch, newest + timedelta(days=111))
    asked = len(edgar.requests)
    again = await _sync(db, edgar).check_filings()
    assert again["watching"] == 0 and len(edgar.requests) == asked

    # Once the wait is over: fetched again, still short — the count goes on, not back to 1.
    company.facts_checked_at -= timedelta(days=3)
    company.fetched_at -= timedelta(days=3)
    await db.commit()
    third = await _sync(db, edgar).check_filings()
    assert third["lagging"] == ["NVDA"]
    assert (await db.get(SecCompany, NVDA_CIK)).facts_miss_count == 2


def _weekday_on_or_after(day, weekday):
    return day + timedelta(days=(weekday - day.weekday()) % 7)


@pytest.mark.asyncio
async def test_past_the_watch_a_stale_company_is_asked_once_a_week(
    db, configured, monkeypatch
):
    newest = await _stored(db)
    monday = _weekday_on_or_after(
        newest + FILING_WATCH_UNTIL + timedelta(days=30), CHASE_WEEKDAY
    )
    edgar = LaggingEdgar(period_end=(newest + timedelta(days=91)).isoformat())

    _on(monkeypatch, monday + timedelta(days=1))
    assert (await _sync(db, edgar).check_filings())["watching"] == 0

    _on(monkeypatch, monday)
    assert (await _sync(db, edgar).check_filings())["watching"] == 1

    # Past a year of missing filings, it has stopped filing: not asked.
    _on(
        monkeypatch,
        _weekday_on_or_after(
            newest + FILING_CHASE_UNTIL + timedelta(days=1), CHASE_WEEKDAY
        ),
    )
    assert (await _sync(db, edgar).check_filings())["watching"] == 0
