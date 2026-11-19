"""
A successor filer takes its predecessor's history (docs/company-financials.md, *Successor
filers*; 2026-11-19): ExxonMobil Holdings Corp (CIK 2115436), a new holding company over Exxon
Mobil Corp (CIK 34088), had two 10-Qs and no fiscal year. Its Q2 2026 10-Q was filed jointly
under the old CIK's accession prefix and sits in both filing lists. Offline: a fake SEC.
"""

from datetime import timedelta

import httpx
import pytest

from app.clock import utcnow
from app.models.company_financials import SecCompany
from app.services.company_financials_normaliser import normalise
from app.services.company_financials_sync import (
    CompanyFinancialsSync,
    has_annual_report,
    merge_facts,
)
from app.services.edgar_client import FACTS_URL, SUBMISSIONS_URL, EdgarClient

NEW, OLD, AGENT = 2115436, 34088, 1013  # a filing agent with a lower CIK, asked first
JOINT = "0000034088-26-000093"


def _fact(start, end, val, form, accn, fp):
    return {
        "start": start,
        "end": end,
        "val": val,
        "form": form,
        "accn": accn,
        "filed": "2026-08-03",
        "fy": int(end[:4]),
        "fp": fp,
    }


NEW_FACTS = {
    "cik": NEW,
    "entityName": "ExxonMobil Holdings Corp",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        _fact("2026-04-01", "2026-06-30", 80e9, "10-Q", JOINT, "Q2"),
                        _fact(
                            "2026-07-01",
                            "2026-09-30",
                            85e9,
                            "10-Q",
                            "0002115436-26-000020",
                            "Q3",
                        ),
                    ]
                }
            }
        }
    },
}
OLD_FACTS = {
    "cik": OLD,
    "entityName": "EXXON MOBIL CORP",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        _fact(
                            "2024-01-01",
                            "2024-12-31",
                            340e9,
                            "10-K",
                            "0000034088-25-000010",
                            "FY",
                        ),
                        _fact(
                            "2025-01-01",
                            "2025-12-31",
                            332e9,
                            "10-K",
                            "0000034088-26-000010",
                            "FY",
                        ),
                    ]
                }
            }
        }
    },
}


def _listing(*accns):
    n = len(accns)
    return {
        "filings": {
            "recent": {
                "form": ["10-Q"] * n,
                "reportDate": ["2026-06-30"] * n,
                "accessionNumber": list(accns),
                "filingDate": ["2026-08-03"] * n,
            }
        }
    }


class Sec:
    def __init__(self):
        self.requests = []

    def handler(self, request):
        url = str(request.url)
        self.requests.append(url)
        if url == SUBMISSIONS_URL.format(cik=NEW):
            # A filing agent's prefix on one report, the old registrant's on the joint one.
            return httpx.Response(
                200,
                json=_listing("0002115436-26-000020", JOINT, f"{AGENT:010d}-26-000001"),
            )
        if url == SUBMISSIONS_URL.format(cik=OLD):
            return httpx.Response(200, json=_listing(JOINT))
        if url == SUBMISSIONS_URL.format(cik=AGENT):
            return httpx.Response(200, json=_listing(f"{AGENT:010d}-26-999999"))
        if url == FACTS_URL.format(cik=OLD):
            return httpx.Response(200, json=OLD_FACTS)
        return httpx.Response(404)


async def _none(*_):
    return None


def test_the_merged_document_is_one_company_with_the_older_years():
    assert not has_annual_report(NEW_FACTS) and has_annual_report(OLD_FACTS)
    merged = merge_facts(NEW_FACTS, OLD_FACTS)
    assert merged["cik"] == NEW and merged["entityName"] == "ExxonMobil Holdings Corp"
    rows = {
        (r["fiscal_period"], str(r["period_end"])): r["value"]
        for r in normalise(merged).rows
        if r["metric"] == "revenue"
    }
    assert rows[("FY", "2025-12-31")] == 332e9
    # The inputs are left as they were.
    assert len(NEW_FACTS["facts"]["us-gaap"]["Revenues"]["units"]["USD"]) == 2


@pytest.mark.asyncio
async def test_a_successor_finds_its_predecessor_once_and_reads_it_every_fetch(db):
    db.add(SecCompany(cik=NEW, ticker="XOM"))
    await db.commit()
    sec = Sec()
    sync = CompanyFinancialsSync(
        db, transport=httpx.MockTransport(sec.handler), sleep=_none
    )
    summary = {}
    async with EdgarClient(
        "ua", transport=httpx.MockTransport(sec.handler), sleep=_none
    ) as c:
        merged = await sync._with_predecessor(c, NEW, NEW_FACTS, summary)
    assert has_annual_report(merged)
    assert summary["predecessors"] == [f"{NEW}<-{OLD}"]
    company = await db.get(SecCompany, NEW)
    assert company.predecessor_cik == OLD
    # The agent's prefix was asked and refused (its list holds no report of this company).
    assert SUBMISSIONS_URL.format(cik=AGENT) in sec.requests

    # Next fetch: no filing list, the predecessor's companyfacts again.
    sec.requests.clear()
    async with EdgarClient(
        "ua", transport=httpx.MockTransport(sec.handler), sleep=_none
    ) as c:
        merged = await sync._with_predecessor(c, NEW, NEW_FACTS, {})
    assert has_annual_report(merged)
    assert sec.requests == [FACTS_URL.format(cik=OLD)]


@pytest.mark.asyncio
async def test_a_company_with_its_own_annual_report_or_asked_lately_is_not_searched(db):
    db.add(
        SecCompany(
            cik=NEW, ticker="XOM", predecessor_checked_at=utcnow() - timedelta(days=3)
        )
    )
    db.add(SecCompany(cik=OLD, ticker="XOMX"))
    await db.commit()
    sec = Sec()
    sync = CompanyFinancialsSync(db, sleep=_none)
    async with EdgarClient(
        "ua", transport=httpx.MockTransport(sec.handler), sleep=_none
    ) as c:
        assert await sync._with_predecessor(c, NEW, NEW_FACTS, {}) is NEW_FACTS
        assert await sync._with_predecessor(c, OLD, OLD_FACTS, {}) is OLD_FACTS
    assert sec.requests == []
