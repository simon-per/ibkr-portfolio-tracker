"""
South Korea via OpenDART (docs/company-financials.md, *South Korea*): the converter's dates,
the pass against a fake OpenDART, KSIC, and the key never leaving the request. Offline.
"""

import io
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import settings
from app.models.company_financials import Company, CompanyReport, DartCorpCode
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services import dart_sync
from app.services.company_classification import classify, ksic_model, ksic_sector
from app.services.company_financials_service import CompanyFinancialsService
from app.services.dart_reports import ANNUAL, Q3, UnsupportedYear, convert, period_end
from app.services.dart_sync import KoreaFinancialsSync
from app.services.opendart_client import OpenDartClient, OpenDartError

KEY = "test-opendart-key-0123456789abcdef"
SAMSUNG = "00126380"


def _line(sj, account, **amounts):
    return {
        "sj_div": sj,
        "account_id": account,
        "currency": "KRW",
        "rcept_no": "",
        **amounts,
    }


def _q3_2025():
    rcept = "20251114002447"
    rows = [
        _line(
            "IS",
            "ifrs-full_Revenue",
            thstrm_amount="86,061",
            thstrm_add_amount="239,768",
            frmtrm_q_amount="79,098",
            frmtrm_add_amount="225,082",
        ),
        _line(
            "IS",
            "ifrs-full_ProfitLossAttributableToOwnersOfParent",
            thstrm_amount="12,006",
            thstrm_add_amount="24,968",
            frmtrm_q_amount="9,781",
            frmtrm_add_amount="26,045",
        ),
        _line(
            "IS",
            "ifrs-full_DilutedEarningsLossPerShare",
            thstrm_amount="1801",
            thstrm_add_amount="3723",
            frmtrm_q_amount="1440",
            frmtrm_add_amount="3834",
        ),
        _line(
            "IS",
            "dart_OperatingIncomeLoss",
            thstrm_amount="12,166",
            thstrm_add_amount="23,527",
            frmtrm_q_amount="9,183",
            frmtrm_add_amount="26,233",
        ),
        _line(
            "CF",
            "ifrs-full_CashFlowsFromUsedInOperatingActivities",
            thstrm_amount="56,515",
            frmtrm_amount="50,960",
        ),
        _line(
            "BS",
            "ifrs-full_CashAndCashEquivalents",
            thstrm_amount="53,399",
            frmtrm_amount="53,705",
        ),
        _line("BS", "-표준계정코드 미사용-", thstrm_amount="13,172"),
        _line("SCE", "ifrs-full_Equity", thstrm_amount="1"),
    ]
    for r in rows:
        r["rcept_no"] = rcept
    return rows


def _fy_2024():
    rcept = "20250311001085"
    rows = [
        _line(
            "IS",
            "ifrs-full_Revenue",
            thstrm_amount="300,870",
            frmtrm_amount="258,935",
            bfefrmtrm_amount="302,231",
        ),
        _line(
            "IS",
            "ifrs-full_ProfitLossAttributableToOwnersOfParent",
            thstrm_amount="33,621",
            frmtrm_amount="14,473",
            bfefrmtrm_amount="54,730",
        ),
        _line(
            "IS",
            "ifrs-full_DilutedEarningsLossPerShare",
            thstrm_amount="4950",
            frmtrm_amount="2131",
            bfefrmtrm_amount="8057",
        ),
        _line(
            "IS",
            "dart_OperatingIncomeLoss",
            thstrm_amount="32,725",
            frmtrm_amount="6,566",
            bfefrmtrm_amount="43,376",
        ),
        _line(
            "CF",
            "ifrs-full_CashFlowsFromUsedInOperatingActivities",
            thstrm_amount="72,982",
            frmtrm_amount="44,137",
            bfefrmtrm_amount="62,181",
        ),
        _line(
            "BS",
            "ifrs-full_CashAndCashEquivalents",
            thstrm_amount="53,705",
            frmtrm_amount="69,080",
            bfefrmtrm_amount="49,680",
        ),
    ]
    for r in rows:
        r["rcept_no"] = rcept
    return rows


def test_a_quarterly_report_dates_its_quarter_its_year_to_date_and_a_year_before():
    out = convert(_q3_2025(), 2025, Q3, "20251114002447")
    revenue = {
        (f["start"], f["end"]): f["val"] for f in out.facts if f["concept"] == "Revenue"
    }
    assert revenue == {
        ("2025-07-01", "2025-09-30"): 86061.0,
        ("2025-01-01", "2025-09-30"): 239768.0,
        ("2024-07-01", "2024-09-30"): 79098.0,
        ("2024-01-01", "2024-09-30"): 225082.0,
    }
    # The cash flow statement is year to date only; the balance sheet two instants.
    ocf = {
        (f["start"], f["end"])
        for f in out.facts
        if f["concept"].startswith("CashFlows")
    }
    assert ocf == {("2025-01-01", "2025-09-30"), ("2024-01-01", "2024-09-30")}
    cash = {
        (f["start"], f["end"])
        for f in out.facts
        if f["concept"] == "CashAndCashEquivalents"
    }
    assert cash == {(None, "2025-09-30"), (None, "2024-12-31")}
    # The operating profit OpenDART names, under its IFRS twin; the non-standard line counted;
    # the statement of changes in equity not read.
    assert out.tagged["ProfitLossFromOperatingActivities"] == "dart_OperatingIncomeLoss"
    assert out.non_standard == 1
    assert {f["form"] for f in out.facts} == {"interim report"}
    assert {f["filed"] for f in out.facts} == {"2025-11-14"}
    assert {
        f["unit"] for f in out.facts if f["concept"] == "DilutedEarningsLossPerShare"
    } == {"KRW/shares"}


def test_two_values_for_one_period_leave_it_out_and_an_ifrs_operating_profit_wins():
    rows = _fy_2024() + [
        _line(
            "IS",
            "ifrs-full_DilutedEarningsLossPerShare",
            thstrm_amount="4951",
            rcept_no="20250311001085",
        ),
        _line(
            "IS",
            "ifrs-full_ProfitLossFromOperatingActivities",
            thstrm_amount="32,700",
            rcept_no="20250311001085",
        ),
    ]
    out = convert(rows, 2024, ANNUAL, "20250311001085")
    eps = {f["end"] for f in out.facts if f["concept"] == "DilutedEarningsLossPerShare"}
    assert "2024-12-31" not in eps and "2023-12-31" in eps
    op = [f for f in out.facts if f["concept"] == "ProfitLossFromOperatingActivities"]
    assert [f["val"] for f in op] == [32700.0]
    assert (
        out.tagged["ProfitLossFromOperatingActivities"]
        == "ifrs-full_ProfitLossFromOperatingActivities"
    )


def test_a_year_not_ending_in_december_is_refused():
    with pytest.raises(UnsupportedYear):
        period_end(2024, ANNUAL, fy_end_month=3)


def test_ksic_translates_the_standard_and_comes_first():
    assert ksic_sector("264") == "Technology"
    assert ksic_sector("26110") == "Technology"
    assert ksic_sector("265") == "Consumer Cyclical"
    assert ksic_sector("2711") == "Healthcare"
    assert ksic_sector("") is None and ksic_sector("ABC") is None
    assert ksic_model("64121") == "bank" and ksic_model("66121") == "bank"
    assert ksic_model("65110") == "insurer"
    assert ksic_model("64992") is None  # a holding company, financial or not
    assert ksic_model("264") == "industrial"
    kind = classify(None, None, None, gics="Communications", ksic="264")
    assert (kind.sector, kind.sector_source) == ("Technology", "ksic")
    assert (kind.business_model, kind.business_model_source) == ("industrial", "ksic")
    assert ("sector", "gics", "Communications") in kind.conflicts


# ── The pass, against a fake OpenDART ────────────────────────────────────────


def _corp_zip():
    xml = (
        "<result>"
        "<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
        "<corp_eng_name>SAMSUNG ELECTRONICS CO,.LTD</corp_eng_name><stock_code>005930</stock_code></list>"
        "<list><corp_code>00434003</corp_code><corp_name>다코</corp_name>"
        "<corp_eng_name>Daco</corp_eng_name><stock_code> </stock_code></list>"
        "</result>"
    ).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


class Dart:
    """A fake OpenDART: Samsung's FY2024 and Q3 2025 reports, nothing else."""

    def __init__(self, status=None):
        self.requests = []
        self.status = status

    def handler(self, request):
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert q.get("crtfc_key") == KEY
        self.requests.append((url.path, q.get("bsns_year"), q.get("reprt_code")))
        if self.status:
            return httpx.Response(
                200, json={"status": self.status, "message": f"no ({KEY})"}
            )
        if url.path.endswith("corpCode.xml"):
            return httpx.Response(200, content=_corp_zip())
        if url.path.endswith("company.json"):
            return httpx.Response(
                200,
                json={
                    "status": "000",
                    "corp_code": SAMSUNG,
                    "corp_name_eng": "SAMSUNG ELECTRONICS CO,.LTD",
                    "stock_code": "005930",
                    "induty_code": "264",
                    "acc_mt": "12",
                },
            )
        if url.path.endswith("fnlttSinglAcntAll.json"):
            assert q["fs_div"] == "CFS" and q["corp_code"] == SAMSUNG
            key = (q["bsns_year"], q["reprt_code"])
            if key == ("2024", "11011"):
                return httpx.Response(200, json={"status": "000", "list": _fy_2024()})
            if key == ("2025", "11014"):
                return httpx.Response(200, json={"status": "000", "list": _q3_2025()})
            return httpx.Response(200, json={"status": "013", "message": "no data"})
        return httpx.Response(404)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)

    def statements(self):
        return [r for r in self.requests if r[0].endswith("fnlttSinglAcntAll.json")]


async def _none(seconds):
    return None


async def _hold(db, sid, symbol, isin):
    db.add(
        Security(
            id=sid,
            isin=isin,
            symbol=symbol,
            description=symbol,
            currency="KRW",
            conid=9000 + sid,
            asset_category="STK",
            exchange="KRX",
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
            currency="KRW",
            cost_basis_eur=Decimal("100"),
            is_open=True,
        )
    )
    await db.commit()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(settings, "opendart_api_key", KEY, raising=False)
    monkeypatch.setattr(dart_sync, "utc_today", lambda: date(2025, 12, 1))
    monkeypatch.setattr(dart_sync, "utcnow", lambda: datetime(2025, 12, 1, 5))
    monkeypatch.setattr(dart_sync, "FIRST_YEAR", 2024)


@pytest.mark.asyncio
async def test_without_a_key_nothing_is_asked_or_recorded(db, monkeypatch):
    monkeypatch.setattr(settings, "opendart_api_key", "", raising=False)
    fake = Dart()
    assert await KoreaFinancialsSync(db, transport=fake.transport).refresh() is None
    assert fake.requests == []


@pytest.mark.asyncio
async def test_samsung_both_classes_one_page_every_report_read_once(db, keyed):
    await _hold(db, 1, "005930", "KR7005930003")
    await _hold(
        db, 2, "005935", "KR7005931001"
    )  # the preferred share: not in corpCode.xml
    fake = Dart()
    sync = KoreaFinancialsSync(db, transport=fake.transport, sleep=_none)
    summary = await sync.refresh()

    assert summary["companies"] == 1 and summary["corp_code_lookups"] == 2
    codes = {
        r.isin: (r.stock_code, r.corp_code)
        for r in (await db.execute(DartCorpCode.__table__.select())).all()
    }
    # The preferred share (traded as 005935) is its issuer's: the ISIN's class digit.
    assert codes == {
        "KR7005930003": ("005930", SAMSUNG),
        "KR7005931001": ("005930", SAMSUNG),
    }
    # 2024's four reports and 2025's three due by 2025-12-01; two held figures.
    assert summary["reports_due"] == 7
    assert (summary["reports_read"], summary["reports_empty"]) == (2, 5)
    assert summary["non_standard_lines"] == 1
    company = (await db.execute(Company.__table__.select())).one()
    assert (company.scheme, company.native_id, company.ticker, company.isin) == (
        "dart",
        SAMSUNG,
        "005930",
        "KR7005930003",
    )
    assert company.industry_code == "264" and company.fiscal_year_end == "1231"

    svc = CompanyFinancialsService(db)
    page = await svc.get_financials(
        await svc.company_by_key(f"dart:{SAMSUNG}"), "quarterly", "max"
    )
    revenue = {p["label"]: p["value"] for p in page["series"]["revenue"]["points"]}
    # Q4 2024 is the year less its nine months, as for a 10-K.
    assert revenue["Q4 FY2024"] == 300870.0 - 225082.0
    assert revenue["Q3 FY2025"] == 86061.0
    assert page["currency"] == "KRW"
    assert (
        page["key_stats"]["pe_ratio"] is None
        and page["series"]["price"]["points"] == []
    )
    assert page["profile"]["sector"] == "Technology"
    assert page["profile"]["sector_source"] == "ksic"
    rows = await svc.repo.rows_of(await svc.company_by_key(f"dart:{SAMSUNG}"))
    sources = {r.source_concept for r in rows if r.metric == "operating_income"}
    assert sources and all(s.startswith("dart:") for s in sources)
    assert "dart:dart_OperatingIncomeLoss" in sources

    # Both listings link to the one page; the preferred share's ISIN too.
    listed = [c for c in await svc.list_companies() if c["key"] == f"dart:{SAMSUNG}"]
    assert listed[0]["security_ids"] == [1, 2] and listed[0]["held"]
    assert listed[0]["isins"] == ["KR7005930003", "KR7005931001"]

    # The next pass asks nothing: the reports read are never asked again, the empty ones not
    # within the week, the corp codes and the profile are on record.
    fake.requests.clear()
    summary = await sync.refresh()
    assert fake.requests == [] and summary["reports_due"] == 0


@pytest.mark.asyncio
async def test_an_empty_recent_report_is_asked_again_after_a_week_an_old_one_never(
    db, keyed, monkeypatch
):
    await _hold(db, 1, "005930", "KR7005930003")
    fake = Dart()
    await KoreaFinancialsSync(db, transport=fake.transport, sleep=_none).refresh()
    monkeypatch.setattr(
        dart_sync, "utcnow", lambda: datetime(2025, 12, 1, 5) + timedelta(days=7)
    )
    monkeypatch.setattr(dart_sync, "utc_today", lambda: date(2025, 12, 8))
    fake.requests.clear()
    await KoreaFinancialsSync(db, transport=fake.transport, sleep=_none).refresh()
    # 2025's two empty reports are recent: asked once more. 2024's are past
    # NONE_FINAL_AFTER (400 days): final.
    assert sorted((y, r) for _, y, r in fake.statements()) == [
        ("2025", "11012"),
        ("2025", "11013"),
    ]
    monkeypatch.setattr(dart_sync, "NONE_FINAL_AFTER", timedelta(days=30))
    monkeypatch.setattr(
        dart_sync, "utcnow", lambda: datetime(2025, 12, 1, 5) + timedelta(days=14)
    )
    monkeypatch.setattr(dart_sync, "utc_today", lambda: date(2025, 12, 15))
    fake.requests.clear()
    await KoreaFinancialsSync(db, transport=fake.transport, sleep=_none).refresh()
    assert fake.statements() == []


@pytest.mark.asyncio
async def test_a_refusal_stops_the_pass_and_the_key_stays_out_of_every_word(db, keyed):
    await _hold(db, 1, "005930", "KR7005930003")
    db.add(
        DartCorpCode(
            stock_code="005930",
            corp_code=SAMSUNG,
            isin="KR7005930003",
            checked_at=datetime(2025, 11, 30),
        )
    )
    await db.commit()
    fake = Dart(status="020")
    summary = await KoreaFinancialsSync(
        db, transport=fake.transport, sleep=_none
    ).refresh()
    assert summary["stopped"] == "020"
    assert len(fake.requests) == 1
    assert KEY not in repr(summary)
    assert (await db.execute(CompanyReport.__table__.select())).all() == []


@pytest.mark.asyncio
async def test_a_transport_failure_never_carries_the_key():
    def fail(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    async with OpenDartClient(
        KEY, transport=httpx.MockTransport(fail), sleep=_none
    ) as client:
        with pytest.raises(OpenDartError) as caught:
            await client.company(SAMSUNG)
    assert KEY not in str(caught.value)
