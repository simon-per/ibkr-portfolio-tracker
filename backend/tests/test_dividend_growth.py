"""
Month-over-month and year-over-year growth of dividend income.

Every test here pins one specific way this arithmetic produces a confidently
wrong number on screen. None of them are hypothetical — each is a property of
the real account: income that begins mid-2024, a quarterly cadence that leaves
most months at zero, and an era splice in February 2026 where the source of the
figures changes from yfinance estimates to IBKR actuals.

Growth is computed server-side precisely because it needs data the windowed
response does not carry: with year=2026 selected there are no 2025 months, so
no client could derive year-over-year at all.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.dividend_repository import DividendRepository
from app.services.dividend_service import DividendService

AS_OF = date(2026, 7, 29)


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="Alpha Corp",
                         currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    await session.flush()
    return engine, session


async def _seed(session, on_date, net, source="yfinance_estimate",
                gross=None, wht="0", security_id=1):
    """A realized payment: what landed, with no per-share figure."""
    await DividendRepository(session).upsert_payment({
        "security_id": security_id,
        "ex_date": on_date,
        "pay_date": on_date,
        "currency": "EUR",
        "shares_held": Decimal("0") if source == "ibkr" else Decimal("10"),
        "gross_amount_eur": Decimal(gross if gross is not None else net),
        "withholding_tax_eur": Decimal(wht),
        "net_amount_eur": Decimal(net),
        "source": source,
    })


async def _seed_per_share(session, on_date, per_share="1.00", shares="100",
                          security_id=1):
    """
    A payment carrying amount_per_share, which is what makes a cadence
    inferable — the forecast reads the dated per-share series, never realized
    income (keying on income left every recently-bought payer projecting
    nothing).
    """
    amount = Decimal(per_share) * Decimal(shares)
    await DividendRepository(session).upsert_payment({
        "security_id": security_id,
        "ex_date": on_date,
        "pay_date": on_date,
        "currency": "EUR",
        "amount_per_share": Decimal(per_share),
        "shares_held": Decimal(shares),
        "gross_amount_eur": amount,
        "withholding_tax_eur": Decimal("0"),
        "net_amount_eur": amount,
        "source": "yfinance_estimate",
    })


def _lot(security_id, open_date, qty):
    return TaxLot(
        security_id=security_id, open_date=open_date, quantity=Decimal(qty),
        cost_basis=Decimal(qty) * 10, cost_basis_eur=Decimal(qty) * 10,
        price_per_unit=Decimal("10"), currency="EUR", is_open=True,
    )


@pytest.mark.asyncio
async def test_ytd_compares_like_for_like_not_against_a_whole_prior_year():
    """
    The headline trap. Measuring income-so-far against the WHOLE of last year is
    not growth: it mixes a 7-month window with a 12-month one. The comparable
    must stop at the same calendar day a year earlier.
    """
    engine, session = await _make_session()
    try:
        # Last year: 10 before the cutoff day, 90 after it. Only the 10 is
        # comparable to a Jan..Jul window.
        await _seed(session, date(2025, 3, 1), "10.00")
        await _seed(session, date(2025, 11, 1), "90.00")
        await _seed(session, date(2026, 3, 1), "20.00")

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        ytd = r["growth"]["ytd"]

        assert ytd["net_eur"] == 20.00
        assert ytd["prev_net_eur"] == 10.00   # NOT 100.00
        assert ytd["pct"] == 100.0            # NOT -80.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_leap_day_as_of_still_finds_a_comparable_day():
    """29 February has no counterpart, and .replace(year=...) raises on it."""
    engine, session = await _make_session()
    try:
        await _seed(session, date(2023, 2, 1), "5.00")
        await _seed(session, date(2024, 2, 1), "7.00")

        r = await DividendService(session).get_dividend_breakdown(as_of=date(2024, 2, 29))
        assert r["growth"]["ytd"]["net_eur"] == 7.00
        assert r["growth"]["ytd"]["prev_net_eur"] == 5.00
        assert DividendService._same_day_last_year(date(2024, 2, 29)) == date(2023, 2, 28)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_first_year_of_history_is_flagged_and_taints_the_next_chip():
    """
    Income starts whenever the first dividend landed, not in January. On the real
    account 2024 holds seven months, so 2025/2024 reads +1300% and means almost
    nothing. The row must carry that caveat rather than presenting it as growth.
    """
    engine, session = await _make_session()
    try:
        await _seed(session, date(2024, 6, 15), "1.00")
        await _seed(session, date(2025, 3, 15), "14.00")

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        rows = {a["year"]: a for a in r["growth"]["annual"]}

        assert rows[2024]["partial"] is True         # history begins mid-year
        assert rows[2024]["yoy_pct"] is None         # nothing to compare against
        assert rows[2025]["yoy_pct"] == 1300.0       # arithmetically true, and
        assert rows[2025]["yoy_vs_partial"] is True  # ...not to be trusted
        assert rows[2025]["partial"] is False
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_zero_base_yields_null_never_a_percentage():
    """
    A quarterly payer pays nothing in most months, so the previous month is
    routinely 0.00. Growth against zero is undefined, not large — returning a
    number would put a fabricated figure beside measured ones.
    """
    engine, session = await _make_session()
    try:
        # March pays, April does not, May pays: May's MoM base is zero.
        await _seed(session, date(2026, 3, 10), "8.00")
        await _seed(session, date(2026, 5, 10), "9.00")

        r = await DividendService(session).get_dividend_breakdown(year=2026, as_of=AS_OF)
        months = {m["month"]: m for m in r["months"]}

        assert months["2026-05"]["mom_pct"] is None   # April was 0.00
        assert months["2026-03"]["yoy_pct"] is None   # no 2025-03 either
        assert months["2026-04"]["mom_pct"] is None   # no income at all this month

        assert DividendService._pct(Decimal("5"), Decimal("0")) is None
        assert DividendService._pct(Decimal("5"), None) is None
        assert DividendService._pct(Decimal("6"), Decimal("4")) == 50.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_month_growth_is_measured_on_realized_income_only():
    """
    A projected month's "change" is an artifact of the forecast's own flat
    median — it would read as the payout schedule shifting when nothing has. So
    a forecast-only month reports no growth, and a realized month's comparison
    never picks up a projection as its base.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2024, 1, 1), "100"))
        await session.flush()
        for d in [date(2025, 10, 15), date(2025, 11, 15),
                  date(2026, 6, 15), date(2026, 7, 15)]:
            await _seed_per_share(session, d)

        r = await DividendService(session).get_dividend_breakdown(year=2026, as_of=AS_OF)
        months = {m["month"]: m for m in r["months"]}

        # August is projected...
        assert months["2026-08"]["forecast_total_eur"] > 0
        assert months["2026-08"]["actual_total_eur"] == 0
        # ...so it reports no growth at all.
        assert months["2026-08"]["mom_pct"] is None
        assert months["2026-08"]["yoy_pct"] is None
        # July is realized and June preceded it, so that one is measurable.
        assert months["2026-07"]["mom_pct"] == 0.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ttm_comparison_flags_that_it_straddles_the_era_splice():
    """
    The current 12 months are IBKR actuals while the 12 before them are yfinance
    estimates — comparable in size, not in provenance. Unflagged, the card would
    present a change of source as growth in income.
    """
    engine, session = await _make_session()
    try:
        await _seed(session, date(2025, 3, 1), "5.00")
        await _seed(session, date(2026, 2, 18), "20.00", source="ibkr",
                    gross="24.00", wht="4.00")

        svc = DividendService(session)
        r = await svc.get_dividend_breakdown(as_of=AS_OF)
        assert r["ibkr_from"] == "2026-02-18"
        assert r["growth"]["ttm_crosses_era"] is True

        # Same data seen from far enough ahead that both windows sit inside the
        # IBKR era: the comparison is then like-for-like.
        r2 = await svc.get_dividend_breakdown(as_of=date(2029, 1, 1))
        assert r2["growth"]["ttm_crosses_era"] is False
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_yoy_is_not_measured_across_a_gap_year():
    """
    Comparing 2026 against 2024 because 2025 happens to be empty would report
    two years of growth as one. Only adjacent years compare.
    """
    engine, session = await _make_session()
    try:
        await _seed(session, date(2024, 6, 1), "4.00")
        await _seed(session, date(2026, 6, 1), "16.00")

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        rows = {a["year"]: a for a in r["growth"]["annual"]}

        assert 2025 not in rows
        assert rows[2026]["yoy_pct"] is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_rolling_forecast_reaches_next_year_without_widening_the_chart():
    """
    The growth figures need a projection past 31 December; the chart must not
    inherit that reach. Widening both at once tripled the all-time chart's
    forecast total (46 -> 162 on the real account) by pulling next year's
    projections into it.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2024, 1, 1), "100"))
        await session.flush()
        # A clean quarterly per-share series, so a cadence is inferable.
        for d in [date(2025, 8, 15), date(2025, 11, 15),
                  date(2026, 2, 15), date(2026, 5, 15)]:
            await _seed_per_share(session, d)

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)

        # The chart still stops inside this year — the all-time axis spans the
        # months that have data, and the last 2026 projection of a quarterly
        # payer starting in May is November.
        assert r["months"][-1]["month"] == "2026-11"
        assert not any(m["month"] >= "2027-01" for m in r["months"])
        # ...while the growth block has a full next year to compare.
        years = {a["year"]: a for a in r["growth"]["annual"]}
        assert years[2027]["forecast_net_eur"] > 0
        assert years[2027]["yoy_includes_forecast"] is True
        # Next-12M spans the year boundary, so it exceeds what the chart shows.
        assert r["growth"]["next_12m_eur"] > r["total_forecast_net_eur"]

        # The calendar is dated, ordered, and confined to those 12 months.
        dates = [u["date"] for u in r["upcoming"]]
        assert dates == sorted(dates)
        assert all(AS_OF.isoformat() < d <= date(2027, 7, 29).isoformat() for d in dates)
        assert all(u["symbol"] == "AAA" for u in r["upcoming"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_past_year_gains_no_forecast_rows_from_the_wider_horizon():
    """
    Projecting further must not leak forecast-only rows into a year that is over.
    Selecting 2025 should still show exactly what was received then.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2024, 1, 1), "100"))
        await session.flush()
        for d in [date(2025, 8, 15), date(2025, 11, 15),
                  date(2026, 2, 15), date(2026, 5, 15)]:
            await _seed_per_share(session, d)

        r = await DividendService(session).get_dividend_breakdown(year=2025, as_of=AS_OF)

        assert r["total_forecast_net_eur"] == 0.0
        assert all(row["forecast_payouts"] == 0 for row in r["securities"])
        assert all(m["forecast_total_eur"] == 0 for m in r["months"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_growth_is_identical_whichever_year_is_selected():
    """
    The whole reason this lives server-side: with year=2026 selected the response
    carries no 2025 months, so the client could not derive year-over-year.
    Windowing the response must not window the growth block.
    """
    engine, session = await _make_session()
    try:
        await _seed(session, date(2025, 4, 1), "10.00")
        await _seed(session, date(2026, 4, 1), "25.00")

        svc = DividendService(session)
        all_time = await svc.get_dividend_breakdown(as_of=AS_OF)
        just_2026 = await svc.get_dividend_breakdown(year=2026, as_of=AS_OF)

        assert just_2026["total_net_eur"] == 25.00      # windowed, as before
        assert all_time["total_net_eur"] == 35.00
        assert just_2026["growth"] == all_time["growth"]
        assert just_2026["growth"]["ytd"]["prev_net_eur"] == 10.00
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_latest_month_reports_the_last_realized_month_not_a_projected_one():
    """
    'Latest month' must mean the last month money actually arrived. Letting a
    projected month win would report an inferred figure as received income.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2024, 1, 1), "100"))
        await session.flush()
        for d in [date(2026, 1, 15), date(2026, 4, 15), date(2026, 7, 15)]:
            await _seed_per_share(session, d)

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)

        assert any(m["forecast_total_eur"] > 0
                   for m in r["months"] if m["month"] > "2026-07")
        assert r["growth"]["latest_month"]["month"] == "2026-07"
        assert r["growth"]["latest_month"]["net_eur"] == 100.00
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_empty_history_produces_zeros_and_nulls_rather_than_failing():
    """
    A fresh account, or one whose dividends have not synced yet, must render the
    cards as empty — not 500 the view or divide by zero.
    """
    engine, session = await _make_session()
    try:
        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        g = r["growth"]

        assert g["ttm"]["net_eur"] == 0.0
        assert g["ttm"]["pct"] is None
        assert g["ytd"]["pct"] is None
        assert g["next_12m_eur"] == 0.0
        assert g["next_12m_vs_ttm_pct"] is None
        assert g["annual"] == []
        assert g["latest_month"] is None
        assert r["upcoming"] == []
    finally:
        await session.close()
        await engine.dispose()

@pytest.mark.asyncio
async def test_calendar_ttm_uses_history_outside_each_display_window():
    engine, session = await _make_session()
    try:
        # Unequal monthly amounts catch shifted boundaries and missing lookback.
        for i in range(45):
            await _seed(session, date(2023 + i // 12, i % 12 + 1, 15), str(i + 1),
                        source="ibkr" if i >= 36 else "yfinance_estimate")
        svc = DividendService(session)
        args = {"as_of": date(2026, 9, 18), "include_forecast": False}
        all_time = await svc.get_dividend_breakdown(**args)
        rolling = await svc.get_dividend_breakdown(period="24m", **args)
        selected = await svc.get_dividend_breakdown(year=2026, **args)
        by_month = {m["month"]: m for m in all_time["months"]}

        assert rolling["period"] == "24m" and rolling["year"] is None
        assert len(rolling["months"]) == 24
        assert rolling["months"][0]["month"] == "2024-10"
        assert rolling["months"][-1]["month"] == "2026-09"
        assert rolling["months"][0]["ttm_net_eur"] == 198  # Nov 2023..Oct 2024
        assert rolling["months"][0]["ttm_mom_pct"] == 6.5
        assert rolling["total_net_eur"] == sum(range(22, 46))
        assert rolling["securities"][0]["net_eur"] == rolling["total_net_eur"]
        assert rolling["growth"] == selected["growth"] == all_time["growth"]
        for response in (rolling, selected):
            for month in response["months"]:
                if month["month"] in by_month:
                    for field in ("ttm_net_eur", "ttm_mom_pct", "ttm_source", "ttm_mom_crosses_era"):
                        assert month[field] == by_month[month["month"]][field]
        assert by_month["2023-11"]["ttm_net_eur"] is None
        assert by_month["2023-12"]["ttm_net_eur"] == 78
        assert by_month["2023-12"]["ttm_mom_pct"] is None
        assert by_month["2024-02"]["ttm_net_eur"] == 102  # leap-year February
        assert by_month["2026-01"]["ttm_source"] == "mixed"
        assert by_month["2026-01"]["ttm_mom_crosses_era"] is True
        assert by_month["2026-08"]["ttm_net_eur"] == 462
        assert by_month["2026-09"]["ttm_net_eur"] is None
        assert all(m["ttm_net_eur"] is None for m in selected["months"][8:])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_stopped_payer_ttm_reaches_zero_without_truncating_elapsed_months():
    engine, session = await _make_session()
    try:
        await _seed(session, date(2023, 1, 15), "10")
        r = await DividendService(session).get_dividend_breakdown(
            as_of=date(2024, 3, 1), include_forecast=False,
        )
        months = {m["month"]: m for m in r["months"]}
        assert r["months"][-1]["month"] == "2024-03"
        assert months["2023-12"]["ttm_net_eur"] == 10
        assert months["2024-01"]["ttm_net_eur"] == 0
        assert months["2024-01"]["ttm_mom_pct"] == -100
        assert months["2024-02"]["ttm_net_eur"] == 0
        assert months["2024-02"]["ttm_mom_pct"] is None
        assert months["2024-03"]["ttm_net_eur"] is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_calendar_ttm_forecast_independence_and_quarterly_cadence():
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2024, 1, 1), "100"))
        await session.flush()
        for year in (2024, 2025, 2026):
            for month in (1, 4, 7, 10):
                if (year, month) <= (2026, 7):
                    await _seed_per_share(session, date(year, month, 15))
        svc = DividendService(session)
        on = await svc.get_dividend_breakdown(year=2026, as_of=AS_OF)
        off = await svc.get_dividend_breakdown(year=2026, as_of=AS_OF, include_forecast=False)
        assert on["total_forecast_net_eur"] > 0
        assert off["total_forecast_net_eur"] == 0
        for a, b in zip(on["months"], off["months"]):
            assert a["ttm_net_eur"] == b["ttm_net_eur"]
            assert a["ttm_mom_pct"] == b["ttm_mom_pct"]
        assert [m["ttm_net_eur"] for m in on["months"][:6]] == [400] * 6
        assert [m["ttm_mom_pct"] for m in on["months"][:6]] == [0] * 6
        assert all(m["ttm_net_eur"] is None for m in on["months"][6:])
        future = await svc.get_dividend_breakdown(year=2027, as_of=AS_OF)
        assert all(m["ttm_net_eur"] is None for m in future["months"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_24_month_empty_history_is_unknown_and_conflicting_filters_refused():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        r = await svc.get_dividend_breakdown(period="24m", as_of=date(2026, 1, 1))
        assert [r["months"][0]["month"], r["months"][-1]["month"]] == ["2024-02", "2026-01"]
        assert len(r["months"]) == 24
        assert all(m["ttm_net_eur"] is None and m["ttm_mom_pct"] is None for m in r["months"])
        with pytest.raises(ValueError):
            await svc.get_dividend_breakdown(year=2026, period="24m")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_calendar_ttm_splices_duplicates_before_applying_payment_date_fx(monkeypatch):
    from app.services.portfolio_service import BaseFx, PortfolioService

    fx = BaseFx("CHF", {
        date(2025, 1, 1): Decimal("0.90"),
        date(2025, 7, 1): Decimal("0.95"),
        date(2026, 1, 1): Decimal("0.80"),
    })

    async def load_fx(self):
        return fx

    monkeypatch.setattr(PortfolioService, "_load_base_fx", load_fx)
    engine, session = await _make_session()
    try:
        for month in range(1, 13):
            await _seed(session, date(2025, month, 15), "10")
        # The estimate is the same payment on its ex-date. It must be consumed
        # by the Jan 15 IBKR row, never entering any TTM bucket a second time.
        await _seed(session, date(2026, 1, 2), "999")
        await _seed(session, date(2026, 1, 15), "20", source="ibkr")
        result = await DividendService(session).get_dividend_breakdown(
            year=2026, as_of=date(2026, 2, 1), include_forecast=False,
        )
        jan = result["months"][0]
        assert result["base_currency"] == "CHF"
        assert jan["actual_total_eur"] == 16
        assert jan["ttm_net_eur"] == 118  # 5*9 + 6*9.5 + 20*0.8
        assert jan["ttm_source"] == "mixed"
        assert jan["ttm_mom_pct"] == 6.3  # prior calendar TTM was 111
    finally:
        await session.close()
        await engine.dispose()
