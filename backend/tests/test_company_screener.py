"""
The screener (2026-11-08): every company the Company tab lists, one row each, with the
overview's key figures, its own dividend record, and P/E, P/FCF and P/S against their own
5-year medians.

The family check is that a row is the company's own page in one line: its key figures are
the overview's (`key_stats` through `_priced`) and its medians are the Valuation card's
(`build_series` on the TTM view) — so the screener cannot disagree with the page a row links
to. The fixture carries six years of quarters and closes, so the medians exist and the check
is not satisfied by two columns of nulls. Pure DB.
"""

from datetime import date, timedelta

import pytest

from app.clock import utc_today, utcnow
from app.models.company_financials import CompanyFinancial, CompanyPrice, SecCompany
from app.schemas.company_financials import CompanyScreenerRow
from app.services.company_financials_service import (
    SCREENED_MULTIPLES,
    CompanyFinancialsService,
    premium,
)

STAMP = utcnow()


def _quarter_ends(first_year: int, last: date):
    ends = []
    for year in range(first_year, last.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            end = date(year, month, day)
            if end <= last:
                ends.append(end)
    return ends


def _row(cik, end, fiscal_year, period, metric, value):
    return CompanyFinancial(
        cik=cik, period_end=end, fiscal_year=fiscal_year, fiscal_period=period,
        metric=metric, value=value, unit="USD" if metric != "eps_diluted" else "USD/shares",
        source_concept="X", filed=end + timedelta(days=30), fetched_at=STAMP,
    )


async def _company(db, cik, ticker, *, first_year, listed=True):
    db.add(SecCompany(
        cik=cik, ticker=ticker, name=f"{ticker} Inc", sic=3674,
        sic_description="Semiconductors", universe_seen_at=STAMP if listed else None,
        fetched_at=STAMP,
    ))
    newest = date(2026, 9, 30)
    for i, end in enumerate(_quarter_ends(first_year, newest)):
        quarter = (end.month - 1) // 3 + 1
        growth = 1.03 ** i
        for metric, value in (
            ("revenue", 100.0 * growth),
            ("net_income", 20.0 * growth),
            ("operating_income", 25.0 * growth),
            ("eps_diluted", 1.0 * growth),
            ("diluted_shares", 20.0),
            ("shares_outstanding", 20.0),
            ("operating_cash_flow", 30.0 * growth),
            ("capex", 6.0 * growth),
            ("dividends_per_share", 0.25 * 1.05 ** (end.year - first_year)),
        ):
            db.add(_row(cik, end, end.year, f"Q{quarter}", metric, value))
        # A close at every quarter end, drifting up faster than the earnings: a P/E history.
        db.add(CompanyPrice(cik=cik, date=end, close=60.0 * growth * (1 + i / 100),
                            currency="USD"))
    for year in range(first_year, newest.year):
        db.add(_row(cik, date(year, 12, 31), year, "FY", "dividends_per_share",
                    1.0 * 1.05 ** (year - first_year)))
    # Today's close, inside PRICE_MAX_AGE_DAYS: the multiples' "today".
    db.add(CompanyPrice(cik=cik, date=utc_today() - timedelta(days=2), close=95.0,
                        currency="USD"))


async def _book(db):
    await _company(db, 1, "AAA", first_year=2020)
    await _company(db, 2, "BBB", first_year=2026)              # one year: no medians
    await _company(db, 3, "CCC", first_year=2020, listed=False)  # not on the tab
    await db.commit()


@pytest.mark.asyncio
async def test_a_row_is_its_companys_page_in_one_line(db):
    await _book(db)
    service = CompanyFinancialsService(db)
    rows = await service.screener()

    for row in rows:
        page = await service.get_financials(await db.get(SecCompany, row["cik"]), "ttm")
        stats = page["key_stats"]
        for key in ("period_end", "market_cap", "revenue_yoy", "revenue_cagr_5y", "eps_yoy",
                    "eps_cagr_5y", "gross_margin", "operating_margin", "fcf_margin", "roe",
                    "net_cash", "pe_ratio", "pfcf_ratio", "ps_ratio", "dividend_yield",
                    "payout_ratio"):
            assert row[key] == stats[key], f"{row['symbol']} {key}"
        assert row["currency"] == page["currency"]
        for name, short in (("pe_ratio", "pe"), ("pfcf_ratio", "pfcf"), ("ps_ratio", "ps")):
            assert row[f"{short}_median_5y"] == page["series"][name]["medians"]["y5"], (
                f"{row['symbol']} {name}: the screener's median is not the Valuation card's"
            )


@pytest.mark.asyncio
async def test_a_company_with_history_reads_today_against_its_own_median(db):
    """Not vacuous: AAA's six years give every screened multiple a median, and the premium is
    today's over it."""
    await _book(db)
    aaa = next(r for r in await CompanyFinancialsService(db).screener() if r["symbol"] == "AAA")
    for short in ("pe", "pfcf", "ps"):
        today, median = aaa[f"{short}_ratio"], aaa[f"{short}_median_5y"]
        assert today is not None and median is not None, short
        assert aaa[f"{short}_vs_median"] == pytest.approx(today / median - 1), short
    assert aaa["raise_years"] == 5          # FY2021..FY2025 each above the year before
    assert aaa["dps_cagr_5y"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_a_company_without_five_years_has_no_median_rather_than_a_short_one(db):
    await _book(db)
    bbb = next(r for r in await CompanyFinancialsService(db).screener() if r["symbol"] == "BBB")
    for short in ("pe", "pfcf", "ps"):
        assert bbb[f"{short}_median_5y"] is None
        assert bbb[f"{short}_vs_median"] is None


@pytest.mark.asyncio
async def test_the_screener_lists_what_the_tab_lists(db):
    await _book(db)
    service = CompanyFinancialsService(db)
    assert [r["cik"] for r in await service.screener()] == [
        c["cik"] for c in await service.list_companies()
    ] == [1, 2]


def test_a_loss_or_a_missing_figure_has_no_premium():
    assert premium(30.0, 20.0) == pytest.approx(0.5)
    assert premium(15.0, 20.0) == pytest.approx(-0.25)
    assert premium(-5.0, 20.0) is None
    assert premium(None, 20.0) is None
    assert premium(10.0, None) is None
    assert premium(10.0, 0.0) is None


def test_every_screened_multiple_is_a_valuation_series():
    from app.services.company_financials_service import VALUATIONS

    assert set(SCREENED_MULTIPLES) <= set(VALUATIONS)


@pytest.mark.asyncio
async def test_the_response_declares_every_key_the_service_returns(db):
    await _book(db)
    for row in await CompanyFinancialsService(db).screener():
        assert set(row) == set(CompanyScreenerRow.model_fields)
