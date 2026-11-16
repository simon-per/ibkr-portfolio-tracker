"""
Segment columns summed where no debt total exists (2026-11-16, the source waterfall's
fourth layer).

companyfacts carries only undimensioned facts, and BRK and PCAR state their debt only per
segment (Insurance and Other / Railroad, Utilities and Energy; Truck, Parts and Other /
Financial Services), so their debt was unknown and `_no_debt_at_all` rightly withheld a
zero. The filing's segment columns are summed only when the members provably partition the
company: the same members' assets (or liabilities) add up to the total the filing states.
A member with no debt line at all owes nothing (PCAR's trucks); a member showing a
borrowing the summed concept does not state refuses the sum. Synthetic documents, offline.
"""

from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.company_financials import SecCompany, SecFilingRead
from app.services.company_financials_normaliser import normalise
from app.services.company_financials_sync import CompanyFinancialsSync
from app.services.edgar_filing_facts import debt_blind, parse_instance, segment_gaps
from tests.test_company_financials_sync import NVDA_CIK, UA, Edgar, _hold, _sync

END = "2024-12-31"


def _f(start, end, val, filed="2025-02-20", form="10-K"):
    fact = {"end": end, "val": val, "filed": filed, "form": form, "accn": f"a-{filed}"}
    if start:
        fact["start"] = start
    return fact


def _companyfacts():
    return {"cik": 1, "entityName": "Example", "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [_f("2024-01-01", END, 50e9)]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [_f(None, END, 7e9)]}},
    }}}


def _seg(concept, member, val, axis="StatementBusinessSegmentsAxis"):
    return {"concept": concept, "unit": "USD", "start": None, "end": END, "val": val,
            "axis": axis, "member": member,
            "accn": "a-2025-02-20", "form": "10-K", "filed": "2025-02-20"}


def _total(concept, val):
    return _seg(concept, "", val, axis="")


def _named(concept, member, axis="StatementBusinessSegmentsAxis"):
    return {"concept": concept, "end": END, "axis": axis, "member": member, "debt_like": True,
            "accn": "a-2025-02-20", "form": "10-K", "filed": "2025-02-20"}


def _debt(result):
    return {(r["period_end"].isoformat(), r["metric"]): (r["value"], r["source_concept"])
            for r in result.rows if r["metric"].startswith("debt") and r["fiscal_period"] == "FY"}


PARTITION = [
    _total("Assets", 43.4e9),
    _seg("Assets", "TruckMember", 21.0e9),
    _seg("Assets", "FinancialServicesMember", 22.4e9),
]


def test_a_partitioned_debt_is_summed_and_tagged_with_its_axis():
    """BRK: both columns state their debt."""
    result = normalise(_companyfacts(), PARTITION + [
        _seg("DebtAndCapitalLeaseObligations", "TruckMember", 44.9e9),
        _seg("DebtAndCapitalLeaseObligations", "FinancialServicesMember", 79.9e9),
    ])
    value, source = _debt(result)[(END, "debt_total")]
    assert abs(value - 124.8e9) < 1e3 and source == "segments:StatementBusinessSegmentsAxis"
    assert result.segments == {"debt_total": {"segments:StatementBusinessSegmentsAxis": 1}}


def test_the_carrying_amount_wins_over_the_maturity_tables_principal():
    """PCAR: the balance sheet's 15,895.0M, not the maturity table's 15,946.7M."""
    result = normalise(_companyfacts(), PARTITION + [
        _seg("DebtLongtermAndShorttermCombinedAmount", "FinancialServicesMember", 15.9467e9),
        _seg("CommercialPaperBankLoansAndTermDebtAtCarryingValue", "FinancialServicesMember",
             15.895e9),
    ])
    assert _debt(result)[(END, "debt_total")][0] == 15.895e9


def test_a_column_with_no_debt_line_owes_nothing():
    """PCAR: Financial Services borrows, the truck column shows no borrowing at all."""
    result = normalise(_companyfacts(), PARTITION + [
        _seg("DebtLongtermAndShorttermCombinedAmount", "FinancialServicesMember", 15.9e9),
        _named("CommercialPaper", "FinancialServicesMember"),
    ])
    assert _debt(result)[(END, "debt_total")][0] == 15.9e9


def test_a_column_showing_another_borrowing_refuses_the_sum():
    result = normalise(_companyfacts(), PARTITION + [
        _seg("DebtLongtermAndShorttermCombinedAmount", "FinancialServicesMember", 15.9e9),
        _named("NotesPayable", "TruckMember"),
    ])
    assert _debt(result) == {}


def test_members_that_do_not_add_up_to_the_total_prove_nothing():
    """A partial view (a parent company's column, one segment of three) is no partition."""
    result = normalise(_companyfacts(), [
        _total("Assets", 60e9),
        _seg("Assets", "TruckMember", 21.0e9),
        _seg("Assets", "FinancialServicesMember", 22.4e9),
        _seg("DebtAndCapitalLeaseObligations", "TruckMember", 1e9),
        _seg("DebtAndCapitalLeaseObligations", "FinancialServicesMember", 2e9),
    ])
    assert _debt(result) == {}


def test_a_debt_figure_from_any_other_source_wins():
    doc = _companyfacts()
    doc["facts"]["us-gaap"]["LongTermDebt"] = {"units": {"USD": [_f(None, END, 3e9)]}}
    result = normalise(doc, PARTITION + [
        _seg("DebtAndCapitalLeaseObligations", "TruckMember", 1e9),
        _seg("DebtAndCapitalLeaseObligations", "FinancialServicesMember", 2e9),
    ])
    assert _debt(result)[(END, "debt_total")] == (3e9, "LongTermDebt")


def test_only_a_company_no_other_source_gives_debt_reads_its_columns():
    rows = [{"period_end": date(2024, 12, 31), "fiscal_period": "FY", "metric": "revenue",
             "value": 1.0},
            {"period_end": date(2023, 12, 31), "fiscal_period": "FY", "metric": "debt_total",
             "value": 1.0, "source_concept": "segments:X"}]
    assert debt_blind(rows)
    assert segment_gaps(rows, date(2026, 11, 16)) == [(date(2024, 12, 31), "segments:debt")]
    assert not debt_blind(rows + [{"period_end": date(2024, 12, 31), "fiscal_period": "FY",
                                   "metric": "debt_current", "value": 0.0,
                                   "source_concept": "no current debt on the balance sheet"}])


def test_the_instance_keeps_single_member_columns_and_the_totals():
    member = ('<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:'
              'StatementBusinessSegmentsAxis">{m}</xbrldi:explicitMember></xbrli:segment>')
    ctx = ('<xbrli:context id="{id}"><xbrli:entity><xbrli:identifier scheme="x">1'
           '</xbrli:identifier>{seg}</xbrli:entity><xbrli:period><xbrli:instant>'
           f'{END}</xbrli:instant></xbrli:period></xbrli:context>')
    body = "".join([
        '<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" ',
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:pcar="http://www.paccar.com/2024" ',
        'xmlns:us-gaap="http://fasb.org/us-gaap/2024" ',
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217">',
        ctx.format(id="all", seg=""),
        ctx.format(id="fs", seg=member.format(m="pcar:FinancialServicesMember")),
        ctx.format(id="tr", seg=member.format(m="pcar:TruckMember")),
        '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>',
        '<us-gaap:Assets contextRef="all" unitRef="usd">43</us-gaap:Assets>',
        '<us-gaap:Assets contextRef="fs" unitRef="usd">22</us-gaap:Assets>',
        '<us-gaap:Assets contextRef="tr" unitRef="usd">21</us-gaap:Assets>',
        '<us-gaap:DebtLongtermAndShorttermCombinedAmount contextRef="fs" unitRef="usd">16'
        '</us-gaap:DebtLongtermAndShorttermCombinedAmount>',
        '<us-gaap:CommercialPaper contextRef="fs" unitRef="usd">5</us-gaap:CommercialPaper>',
        "</xbrli:xbrl>",
    ]).encode()
    found = parse_instance(body, segments=True)
    assert {(f["concept"], f["member"], f.get("val")) for f in found} == {
        ("Assets", "", 43.0), ("Assets", "FinancialServicesMember", 22.0),
        ("Assets", "TruckMember", 21.0),
        ("DebtLongtermAndShorttermCombinedAmount", "FinancialServicesMember", 16.0),
        ("CommercialPaper", "FinancialServicesMember", None),
    }
    assert parse_instance(body) == []  # only when asked


# ── the pass: a debt-blind company's own filings, segment columns kept, once ─


@pytest.mark.asyncio
async def test_the_pass_reads_a_debt_blind_companys_filings_once(db, monkeypatch):
    """The NVDA fixture states no debt anywhere: its balance sheets' own filings are read
    for segment columns (one request pair each, no label linkbase), each once ever."""
    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)

    async def no(self, cik, facts):
        return False

    monkeypatch.setattr(CompanyFinancialsSync, "_reads_extensions", no)
    await _hold(db, 1, "NVDA")
    await db.commit()
    edgar = ArchiveEdgar()

    first = await _sync(db, edgar).refresh()
    reads = (await db.execute(select(SecFilingRead))).scalars().all()
    assert first["filings_read"] == len(reads) > 0
    assert not [u for u in edgar.urls() if u.endswith("_lab.xml")]

    company = await db.get(SecCompany, NVDA_CIK)
    company.fetched_at -= timedelta(days=7)
    await db.commit()
    asked = len(edgar.requests)
    await _sync(db, edgar).refresh()
    again = {str(r.url) for r in edgar.requests[asked:] if "/Archives/" in str(r.url)}
    assert not again & set(edgar.urls()[:asked])  # never a filing twice


class ArchiveEdgar(Edgar):
    """Every filing serves an instance with nothing in it."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/Archives/edgar/data/" in url:
            self.requests.append(request)
            if url.endswith("index.json"):
                return httpx.Response(200, json={"directory": {"item": [
                    {"name": "abc-20180128.xml"}, {"name": "abc-20180128_lab.xml"}]}})
            return httpx.Response(200, content=b'<?xml version="1.0"?><xbrl/>')
        return super().handler(request)
