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

async def _quarterly_payer(session):
    """A steady quarterly payer, held, last paying 2026-07 — so it projects."""
    session.add(_lot(1, date(2024, 1, 1), "100"))
    await session.flush()
    for year in (2024, 2025, 2026):
        for month in (1, 4, 7, 10):
            if (year, month) <= (2026, 7):
                await _seed_per_share(session, date(year, month, 15))


@pytest.mark.asyncio
async def test_ttm_reads_history_outside_the_range_showing_it_and_drops_what_it_cannot_cover():
    """
    The rolling series is computed over the whole history and only then sliced, so
    one month's figures are the same whichever range is displaying it. Slice first
    and January's change silently becomes "vs nothing" on a year view and a real
    number on the all-time one.

    It also pins requirement (a): a window without twelve months behind it is
    ABSENT, not a null the client has to strip and not a short sum presented as a
    year.
    """
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
        by_month = {p["month"]: p for p in all_time["ttm_series"]}

        # months[] is untouched by any of this.
        assert rolling["period"] == "24m" and rolling["year"] is None
        assert len(rolling["months"]) == 24
        assert rolling["months"][0]["month"] == "2024-10"
        assert rolling["months"][-1]["month"] == "2026-09"
        assert rolling["total_net_eur"] == sum(range(22, 46))
        assert rolling["securities"][0]["net_eur"] == rolling["total_net_eur"]
        assert rolling["growth"] == selected["growth"] == all_time["growth"]

        # Coverage begins in the first income month, so the eleven months before
        # it produce no point at all rather than a partial sum.
        assert all_time["ttm_series"][0]["month"] == "2023-12"
        assert "2023-11" not in by_month
        assert by_month["2023-12"]["net_eur"] == 78          # amounts 1..12
        assert by_month["2023-12"]["mom_pct"] is None        # nothing precedes it
        assert by_month["2024-02"]["net_eur"] == 102         # leap-year February
        assert by_month["2026-01"]["source"] == "mixed"
        assert by_month["2026-01"]["mom_crosses_era"] is True
        assert by_month["2026-08"]["net_eur"] == 462
        # The open month has no closed window, so it is absent on every range.
        assert "2026-09" not in by_month

        # Every shared month is byte-identical across the three ranges — the whole
        # point of building the series before slicing it.
        for response in (rolling, selected):
            for point in response["ttm_series"]:
                assert point == by_month[point["month"]]
        assert [p["month"] for p in selected["ttm_series"]] == [
            f"2026-{m:02d}" for m in range(1, 9)
        ]
        assert rolling["ttm_series"][0]["month"] == "2024-10"
        assert rolling["ttm_series"][0]["net_eur"] == 198     # Nov 2023..Oct 2024
        assert rolling["ttm_series"][0]["mom_pct"] == 6.5

        # The specific wrong number: a year view's first point compares against the
        # previous December, a window it never displays. A dash here would mean the
        # base was read off the emitted list instead of the series.
        assert selected["ttm_series"][0]["mom_pct"] is not None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_december_window_equals_that_calendar_year_and_so_cannot_drift_from_it():
    """
    A window ending in December IS that calendar year, so it must equal the annual
    row to the cent. The two are accumulated independently — one rolling per month
    over per-symbol buckets, one straight into `annual_actual` — so this is the
    cross-check that catches either of them drifting. Measured on production before
    it was written: both read 147.85 for 2026.
    """
    engine, session = await _make_session()
    try:
        for i in range(45):
            await _seed(session, date(2023 + i // 12, i % 12 + 1, 15), str(i + 1))
        r = await DividendService(session).get_dividend_breakdown(
            as_of=date(2026, 9, 18), include_forecast=False,
        )
        annual = {a["year"]: a for a in r["growth"]["annual"]}
        decembers = {p["month"][:4]: p for p in r["ttm_series"] if p["month"].endswith("-12")}
        assert set(decembers) == {"2023", "2024", "2025"}
        for year, point in decembers.items():
            assert point["total_eur"] == annual[int(year)]["total_eur"], year
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_stopped_payer_falls_to_a_measured_zero_and_the_open_month_is_absent():
    """
    A payer that stops really does take its rolling total to zero, and that is the
    one thing this chart exists to show — so a measured 0.00 is kept while an
    uncovered month is dropped. The two look alike and mean opposite things.
    """
    engine, session = await _make_session()
    try:
        await _seed(session, date(2023, 1, 15), "10")
        r = await DividendService(session).get_dividend_breakdown(
            as_of=date(2024, 3, 1), include_forecast=False,
        )
        ttm = {p["month"]: p for p in r["ttm_series"]}
        assert r["months"][-1]["month"] == "2024-03"      # months[] still reaches it
        assert ttm["2023-12"]["net_eur"] == 10
        assert ttm["2024-01"]["net_eur"] == 0
        assert ttm["2024-01"]["mom_pct"] == -100          # a real fall, reported
        assert ttm["2024-02"]["net_eur"] == 0
        assert ttm["2024-02"]["mom_pct"] is None          # zero base: undefined
        assert "2024-03" not in ttm                        # the open month
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_hiding_the_forecast_yields_exactly_the_closed_prefix_of_showing_it():
    """
    The toggle never refetches, so the client filters `partial` locally — which is
    only honest if the closed points are identical either way. They are, and by
    construction rather than by luck: no projection can be dated on or before
    today (`horizon_start = as_of + 1`), so a window that has fully elapsed cannot
    contain one.

    Byte-identical, not "equal on the two fields someone thought to check" — the
    premise that used to hold for the whole series now holds exactly here.
    """
    engine, session = await _make_session()
    try:
        await _quarterly_payer(session)
        svc = DividendService(session)
        on = await svc.get_dividend_breakdown(year=2026, as_of=AS_OF)
        off = await svc.get_dividend_breakdown(year=2026, as_of=AS_OF, include_forecast=False)
        assert on["total_forecast_net_eur"] > 0
        assert off["total_forecast_net_eur"] == 0

        closed = [p for p in on["ttm_series"] if not p["partial"]]
        assert off["ttm_series"] == closed
        assert [p["month"] for p in closed] == [f"2026-{m:02d}" for m in range(1, 7)]
        assert all(p["total_eur"] == p["net_eur"] == 400 for p in closed)
        assert all(p["forecast_net_eur"] == 0 for p in closed)

        by_m = {p["month"]: p for p in on["ttm_series"]}
        # `partial` and "carries projection" are different facts: this window is
        # open but the next payment falls outside it.
        assert by_m["2026-07"]["partial"] is True
        assert by_m["2026-07"]["forecast_net_eur"] == 0
        assert by_m["2026-07"]["mom_includes_forecast"] is False
        # ...and this one is open AND projected.
        assert by_m["2026-10"]["net_eur"] == 300
        assert by_m["2026-10"]["forecast_net_eur"] == 100
        assert by_m["2026-10"]["total_eur"] == 400
        assert by_m["2026-10"]["mom_includes_forecast"] is True
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_future_year_is_fully_projected_and_claims_no_received_income():
    """
    Requirement (c). A year entirely ahead of us has a rolling total made of
    projections, and it must say so: no received income, and no `source`, which is
    the provenance of money that actually arrived. Stamping the estimate's
    provenance there would claim income turned up from a guess.
    """
    engine, session = await _make_session()
    try:
        await _quarterly_payer(session)
        r = await DividendService(session).get_dividend_breakdown(year=2027, as_of=AS_OF)
        ttm = r["ttm_series"]
        assert [p["month"] for p in ttm] == [f"2027-{m:02d}" for m in range(1, 13)]
        assert all(p["partial"] for p in ttm)
        last = ttm[-1]
        assert last["net_eur"] == 0 and last["actual"] == {}
        assert last["source"] is None
        assert last["forecast_net_eur"] == last["total_eur"] > 0
        # Early 2027 still carries real 2026 receipts — the window straddles today.
        assert ttm[0]["net_eur"] > 0 and ttm[0]["forecast_net_eur"] > 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_folding_the_forecast_in_moves_neither_the_monthly_chart_nor_its_totals():
    """
    The rolling series is a separate list precisely so it can out-reach `months[]`
    without stretching it. Coupling the two once tripled the all-time chart's
    forecast total (46 -> 162), and these two sums are the executable form of
    "the new accumulators cannot perturb the old ones".
    """
    engine, session = await _make_session()
    try:
        await _quarterly_payer(session)
        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        assert r["total_net_eur"] == sum(m["actual_total_eur"] for m in r["months"])
        assert r["total_forecast_net_eur"] == sum(
            m["forecast_total_eur"] for m in r["months"]
        )
        # The chart stops inside this year; the rolling series runs past it.
        assert r["months"][-1]["month"] < f"{AS_OF.year + 1}-01"
        assert r["ttm_series"][-1]["month"] > r["months"][-1]["month"]
        assert r["ttm_series"][-1]["month"] > f"{AS_OF.year}-12"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_per_symbol_split_reconciles_to_the_scalar_it_is_drawn_against():
    """
    Requirement (d): the bar is stacked from `actual`/`forecast` while the figure
    quoted beside it is `net_eur`/`total_eur`. A reader adding up the segments must
    land on the total, so the split and the scalar cannot be two computations.

    Exact equality on a deliberately clean fixture — a tolerance here would hide
    the bucketing bug this exists to catch.
    """
    engine, session = await _make_session()
    try:
        session.add(Security(id=2, isin="US0000000002", symbol="BBB", description="Beta Corp",
                             currency="EUR", conid=200, asset_category="STK", exchange="XETRA"))
        await session.flush()
        for month in range(1, 13):
            await _seed(session, date(2025, month, 15), "10")
            await _seed(session, date(2025, month, 20), "4", security_id=2)
        r = await DividendService(session).get_dividend_breakdown(
            as_of=date(2026, 2, 1), include_forecast=False,
        )
        assert r["ttm_series"]
        for p in r["ttm_series"]:
            assert sum(p["actual"].values()) == p["net_eur"]
            assert sum(p["forecast"].values()) == p["forecast_net_eur"]
            assert p["net_eur"] + p["forecast_net_eur"] == p["total_eur"]
        dec = next(p for p in r["ttm_series"] if p["month"] == "2025-12")
        assert dec["actual"] == {"AAA": 120.0, "BBB": 48.0}
        assert dec["net_eur"] == 168.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_asking_for_a_forecast_that_projects_nothing_does_not_draw_a_decline():
    """
    With the flag set and nothing to project — nothing held, too thin a history, a
    payer past the stopped guard — a window reaching into the future is elapsed
    months plus empty ones, so the series would decay month by month to 0.00 and
    draw a collapse that never happened.

    This service already served exactly that shape once, as
    `next_12m_vs_ttm_pct: -100.0`. The gate is on a projection existing, not on the
    flag asking for one.
    """
    engine, session = await _make_session()
    try:
        # History, but no open lot — so project_dividends returns nothing.
        for month in range(1, 13):
            await _seed_per_share(session, date(2025, month, 15))
        r = await DividendService(session).get_dividend_breakdown(
            as_of=date(2026, 6, 10), include_forecast=True,
        )
        assert r["total_forecast_net_eur"] == 0
        assert r["ttm_series"]
        assert r["ttm_series"][-1]["month"] == "2026-05"   # the last elapsed month
        assert not any(p["partial"] for p in r["ttm_series"])
        assert not any(p["forecast_net_eur"] for p in r["ttm_series"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_history_shorter_than_a_year_yields_no_points_rather_than_a_short_sum():
    """Requirement (a) at its most load-bearing: six months summed and labelled as
    twelve would read as a collapse against the first real year."""
    engine, session = await _make_session()
    try:
        for month in range(1, 7):
            await _seed(session, date(2026, month, 15), "10")
        r = await DividendService(session).get_dividend_breakdown(
            as_of=date(2026, 7, 1), include_forecast=False,
        )
        assert r["months"], "the monthly chart still has something to draw"
        assert r["ttm_series"] == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_three_ranges_agree_on_every_shared_month_with_the_forecast_on():
    """
    The projection is window-invariant by construction (`horizon_end` ignores the
    selected range and `project_dividends` steps deterministically), so folding it
    in must not make a month's rolling total depend on the range showing it. The
    existing agreement test runs with the forecast off and could not see this.
    """
    engine, session = await _make_session()
    try:
        await _quarterly_payer(session)
        svc = DividendService(session)
        args = {"as_of": AS_OF}
        by_month = {p["month"]: p for p in (await svc.get_dividend_breakdown(**args))["ttm_series"]}
        for kwargs in ({"period": "24m"}, {"year": 2026}, {"year": 2027}):
            other = await svc.get_dividend_breakdown(**args, **kwargs)
            assert other["ttm_series"], kwargs
            for point in other["ttm_series"]:
                assert point == by_month[point["month"]], (kwargs, point["month"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_24_month_empty_history_is_absent_and_conflicting_filters_refused():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        r = await svc.get_dividend_breakdown(period="24m", as_of=date(2026, 1, 1))
        assert [r["months"][0]["month"], r["months"][-1]["month"]] == ["2024-02", "2026-01"]
        assert len(r["months"]) == 24
        assert r["ttm_series"] == []
        with pytest.raises(ValueError):
            await svc.get_dividend_breakdown(year=2026, period="24m")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ttm_splices_duplicates_before_applying_payment_date_fx(monkeypatch):
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
        jan = result["ttm_series"][0]
        assert result["base_currency"] == "CHF"
        assert result["months"][0]["actual_total_eur"] == 16
        assert jan["month"] == "2026-01"
        assert jan["net_eur"] == 118  # 5*9 + 6*9.5 + 20*0.8
        assert jan["source"] == "mixed"
        assert jan["mom_pct"] == 6.3  # prior calendar TTM was 111
        # The per-symbol bucket carries the same per-date conversion, not a
        # re-conversion of the rounded total.
        assert jan["actual"] == {"AAA": 118.0}
    finally:
        await session.close()
        await engine.dispose()


async def _seed_doubling_book(session):
    """
    A book whose rolling twelve-month total doubles over six months.

    10 a month through 2025 puts the window ending 2025-12 at 120; 30 a month
    from January leaves the window ending 2026-06 at 60 + 180 = 240. Chosen so
    the pace has a closed form — 2 ** (1/6) a month, 300% a year — rather than a
    figure only this implementation can produce.
    """
    for month in range(1, 13):
        await _seed(session, date(2025, month, 15), "10")
    for month in range(1, 7):
        await _seed(session, date(2026, month, 15), "30")


async def _seed_quarterly_per_share_book(session):
    """
    A steady quarterly payer with a dated per-share series, which is what makes a
    cadence inferable and therefore a projection exist at all.

    Two years of it, deliberately: one year is enough to project forward but
    leaves no window closed, and the measured pace would then be absent for want
    of history rather than for the reason under test.
    """
    session.add(_lot(1, date(2024, 1, 1), "100"))
    await session.flush()
    for year, months in ((2024, (8, 11)), (2025, (2, 5, 8, 11)), (2026, (2, 5))):
        for month in months:
            await _seed_per_share(session, date(year, month, 15))


@pytest.mark.asyncio
async def test_the_growth_pace_compounds_back_to_the_two_windows_it_names():
    """
    The figure on screen sits beside the two window totals it was derived from,
    so the one thing it must never do is disagree with them.

    Pins the arithmetic that would: an ARITHMETIC mean of the six monthly changes
    does not compound back to the endpoints (Jensen), and annualizing by
    multiplying by twelve rather than compounding understates a rising book and
    overstates a falling one. Both produce a plausible number beside two totals
    it contradicts.
    """
    engine, session = await _make_session()
    try:
        await _seed_doubling_book(session)

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        pace = r["ttm_pace_measured"]

        assert (pace["from_month"], pace["from_eur"]) == ("2025-12", 120.0)
        assert (pace["to_month"], pace["to_eur"]) == ("2026-06", 240.0)
        assert pace["months"] == 6
        assert pace["short_history"] is False
        # 2 ** (1/6) - 1
        assert pace["monthly_pct"] == 12.25
        # Compounded over a year, not multiplied: 2 ** 2 - 1, never 12 * 12.25.
        assert pace["annualized_pct"] == 300.0

        # The identity itself, stated rather than implied by the constants above.
        grown = (1 + pace["monthly_pct"] / 100) ** pace["months"]
        assert grown == pytest.approx(pace["to_eur"] / pace["from_eur"], rel=1e-3)

        # One provenance on both sides, so nothing is flagged as a source change.
        assert pace["crosses_era"] is False
        assert pace["includes_forecast"] is False
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_growth_pace_is_the_same_whichever_range_is_selected():
    """
    The pace is derived from `ttm_series`, which IS windowed on the wire — with
    year=2026 selected the response carries no 2025 window at all. Measuring the
    pace after that slice would make it say something different in every range,
    and the 2026 view could not reach back to December to compare against.
    """
    engine, session = await _make_session()
    try:
        await _seed_doubling_book(session)
        svc = DividendService(session)

        paces = [
            (await svc.get_dividend_breakdown(as_of=AS_OF))["ttm_pace_measured"],
            (await svc.get_dividend_breakdown(year=2025, as_of=AS_OF))["ttm_pace_measured"],
            (await svc.get_dividend_breakdown(year=2026, as_of=AS_OF))["ttm_pace_measured"],
            (await svc.get_dividend_breakdown(period="24m", as_of=AS_OF))["ttm_pace_measured"],
        ]

        assert all(p == paces[0] for p in paces)
        # ...while the series they came from genuinely differs between them.
        year_view = await svc.get_dividend_breakdown(year=2026, as_of=AS_OF)
        assert all(p["month"] >= "2026-01" for p in year_view["ttm_series"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_measured_pace_ignores_a_projection_that_reaches_next_year():
    """
    Both bases ride on one response so the Forecast toggle stays instant, which
    only works if each is anchored on its own windows rather than on whichever
    the flag produced.

    The measured side must stop at the last fully elapsed window even with a
    projection running to the end of next year — and that window is necessarily
    all received, because no projection can be dated on or before today.
    """
    engine, session = await _make_session()
    try:
        await _seed_quarterly_per_share_book(session)

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        measured, projected = r["ttm_pace_measured"], r["ttm_pace_projected"]

        assert measured["to_month"] == "2026-06"      # AS_OF is 2026-07-29
        assert measured["includes_forecast"] is False
        # The horizon, not the last projected payment: a quarterly payer's final
        # 2027 projection lands in November, and ending there would put the pace's
        # anchor wherever one payer's schedule happened to fall.
        assert projected["to_month"] == "2027-12"
        assert projected["includes_forecast"] is True
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_stable_payer_projects_a_flat_pace_rather_than_manufactured_growth():
    """
    The forward pace measures two projected windows against each other, and the
    projection is a flat median per-share amount on an inferred cadence. On a
    book that is not changing it must therefore come out at zero.

    Anything else would be the shape this service once served as
    `next_12m_vs_ttm_pct: -100.0` — a figure manufactured by the projection's own
    mechanics and presented as a trend.
    """
    engine, session = await _make_session()
    try:
        await _seed_quarterly_per_share_book(session)

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        projected = r["ttm_pace_projected"]

        assert projected["monthly_pct"] == 0.0
        assert projected["annualized_pct"] == 0.0
        assert projected["from_eur"] == projected["to_eur"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_pace_shrinks_its_span_rather_than_inventing_the_months_it_lacks():
    """
    Six months is the nominal lookback, not a precondition. A book with four
    covered windows has three months of trend to report, and reporting them over
    a stated three-month span is more use than reporting nothing — as long as the
    span travels with the figure, which `months` and `short_history` do.

    One window is a level and not a rate at all, and that degrades to absent.
    """
    engine, session = await _make_session()
    try:
        # First income 2025-04 puts the first covered window at 2026-03, so four
        # windows are closed by AS_OF.
        for d in [date(2025, m, 15) for m in range(4, 13)] + \
                 [date(2026, m, 15) for m in range(1, 4)]:
            await _seed(session, d, "10")
        for month in range(4, 7):
            await _seed(session, date(2026, month, 15), "20")

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        pace = r["ttm_pace_measured"]

        assert (pace["from_month"], pace["to_month"]) == ("2026-03", "2026-06")
        assert pace["months"] == 3
        assert pace["short_history"] is True
        assert (pace["from_eur"], pace["to_eur"]) == (120.0, 150.0)
        grown = (1 + pace["monthly_pct"] / 100) ** pace["months"]
        assert grown == pytest.approx(150 / 120, rel=1e-3)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_single_covered_window_is_a_level_not_a_rate():
    engine, session = await _make_session()
    try:
        await _seed(session, date(2025, 7, 15), "10")

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)

        assert len(r["ttm_series"]) == 1
        assert r["ttm_pace_measured"] is None
        assert r["ttm_pace_projected"] is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_stopped_payer_paces_to_minus_one_hundred_and_a_zero_base_is_absent():
    """
    The two ends of the same rule. Falling to zero is a real, measurable -100% a
    month and the chart exists to show it; growing FROM zero is undefined, not
    large, exactly as `_pct` treats it — so it is absent rather than a number.
    """
    engine, session = await _make_session()
    try:
        for month in range(1, 7):
            await _seed(session, date(2025, month, 15), "10")

        stopped = (await DividendService(session).get_dividend_breakdown(as_of=AS_OF))
        pace = stopped["ttm_pace_measured"]
        assert (pace["from_eur"], pace["to_eur"]) == (60.0, 0.0)
        assert pace["monthly_pct"] == -100.0
        assert pace["annualized_pct"] == -100.0
    finally:
        await session.close()
        await engine.dispose()

    engine, session = await _make_session()
    try:
        # A gap year: the window six months back holds nothing, so there is no
        # base to grow from even though nineteen windows are covered.
        await _seed(session, date(2024, 1, 15), "100")
        for month in range(1, 7):
            await _seed(session, date(2026, month, 15), "10")

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)
        assert len(r["ttm_series"]) > 6
        assert r["ttm_pace_measured"] is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_pace_marks_anchors_that_straddle_the_era_splice():
    """
    Part of a change across the splice is a change of source, not of income —
    the same caveat `growth.ttm_crosses_era` and `DividendTtmPoint.mom_crosses_era`
    already carry, and it has to travel with this figure too.
    """
    engine, session = await _make_session()
    try:
        for month in range(1, 13):
            await _seed(session, date(2025, month, 15), "10")
        for month in range(1, 7):
            await _seed(session, date(2026, month, 15), "30", source="ibkr")

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)

        assert r["ttm_pace_measured"]["crosses_era"] is True
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_without_a_projection_the_two_paces_agree_and_neither_is_an_estimate():
    """
    The gate is on a projection EXISTING, not on the flag asking for one. With
    the forecast requested and nothing projected, the forward pace IS the measured
    pace, and badging it `est.` on the strength of the toggle would mark a
    measurement as a guess.
    """
    engine, session = await _make_session()
    try:
        await _seed_doubling_book(session)

        r = await DividendService(session).get_dividend_breakdown(as_of=AS_OF)

        assert r["total_forecast_net_eur"] == 0
        assert r["ttm_pace_projected"] == r["ttm_pace_measured"]
        assert r["ttm_pace_projected"]["includes_forecast"] is False
    finally:
        await session.close()
        await engine.dispose()
