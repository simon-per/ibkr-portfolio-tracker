"""
Service tests for the dividends breakdown endpoint and the era-spliced summary.

The splice is the money-critical part: yfinance estimates are kept strictly
BEFORE the first IBKR payment date and IBKR rows carry everything from there on
— a global "prefer ibkr" switch silently erased every pre-IBKR month from the
dividend card, and keeping estimates inside the IBKR era would double-count the
same dividend from both sources.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.dividend_repository import DividendRepository
from app.services.dividend_service import DividendService

AS_OF = date(2026, 5, 1)


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
    session.add(Security(id=2, isin="US0000000002", symbol="BBB", description="Beta PLC",
                         currency="EUR", conid=200, asset_category="STK", exchange="XETRA"))
    await session.flush()
    return engine, session


async def _seed_payment(session, security_id, on_date, net, source, gross=None, wht="0"):
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


def _lot(security_id, open_date, qty, close_date=None, unit_cost="10"):
    """`unit_cost` defaults to 10 so every existing caller is unchanged; it exists so a
    rebuy can cost more per share than the lot it replaced."""
    unit = Decimal(unit_cost)
    return TaxLot(
        security_id=security_id, open_date=open_date, quantity=Decimal(qty),
        cost_basis=Decimal(qty) * unit, cost_basis_eur=Decimal(qty) * unit,
        price_per_unit=unit, currency="EUR",
        is_open=close_date is None, close_date=close_date,
    )


def _price(security_id, close):
    """
    A cached close, for the yield denominators.

    Dated **today** rather than at this module's `AS_OF`, and that is not an oversight:
    `get_positions_breakdown()` hardcodes `date.today()` (`portfolio_service.py`) and
    ignores the `as_of` the dividend service is given, with only a 14-day lookback. A
    price seeded at `AS_OF` is therefore invisible and every yield comes back `None` —
    which no assertion explains, so it is worth saying once here. Nothing in this file
    seeded a price before the forward yield existed, which is why the yields had never
    actually been exercised.
    """
    return MarketPrice(
        security_id=security_id, date=date.today(), close_price=Decimal(close),
        currency="EUR", source="test",
    )


@pytest.mark.asyncio
async def test_summary_keeps_pre_ibkr_estimate_months_and_never_sums_the_two():
    engine, session = await _make_session()
    try:
        # Pre-IBKR era: estimates only. IBKR era starts 2026-03-10; an estimate
        # inside that era duplicates the same dividend and must be dropped.
        await _seed_payment(session, 1, date(2025, 5, 12), "7.50", "yfinance_estimate")
        await _seed_payment(session, 1, date(2026, 3, 10), "10.00", "ibkr", gross="12.00", wht="2.00")
        await _seed_payment(session, 1, date(2026, 3, 11), "9.99", "yfinance_estimate")

        summary = await DividendService(session).get_dividend_summary()
        months = {m["month"]: m["amount_eur"] for m in summary["monthly"]}
        assert months["2025-05"] == 7.50           # the old global switch erased this
        assert months["2026-03"] == 10.00          # ibkr only — estimate not added on top
        assert summary["ibkr_from"] == "2026-03-10"
        assert summary["total_net_eur"] == 17.50
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_breakdown_groups_by_month_and_symbol_within_the_year():
    engine, session = await _make_session()
    try:
        await _seed_payment(session, 1, date(2026, 2, 10), "5.00", "ibkr")
        await _seed_payment(session, 2, date(2026, 2, 20), "3.00", "ibkr")
        await _seed_payment(session, 2, date(2025, 11, 5), "4.00", "yfinance_estimate")

        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=False, as_of=AS_OF,
        )
        assert out["year"] == 2026
        # Next year is selectable so a full year of payouts can be planned.
        assert out["years"] == [2025, 2026, 2027]
        assert len(out["months"]) == 12            # full axis so chart bars align
        feb = next(m for m in out["months"] if m["month"] == "2026-02")
        assert feb["actual"] == {"AAA": 5.00, "BBB": 3.00}
        assert feb["actual_total_eur"] == 8.00
        assert out["total_net_eur"] == 8.00        # 2025 row filtered out by the year
        rows = {r["symbol"]: r for r in out["securities"]}
        assert rows["AAA"]["payouts"] == 1 and rows["AAA"]["source"] == "ibkr"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_breakdown_projects_a_quarterly_payer_into_future_months_only():
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2025, 1, 2), "10"))
        await session.flush()
        for d in (date(2025, 10, 15), date(2026, 1, 15), date(2026, 4, 15)):
            await _seed_payment(session, 1, d, "10.00", "ibkr")

        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=True, as_of=AS_OF,
        )
        by_month = {m["month"]: m for m in out["months"]}
        # 2026-04-15 + 91d = 07-15, + 91d = 10-14; both inside the year, after as_of.
        assert by_month["2026-07"]["forecast"] == {"AAA": 10.00}
        assert by_month["2026-10"]["forecast"] == {"AAA": 10.00}
        assert by_month["2026-01"]["forecast"] == {}          # past months never get forecast
        assert by_month["2026-01"]["actual"] == {"AAA": 10.00}
        assert out["total_forecast_net_eur"] == 20.00
        row = out["securities"][0]
        assert row["forecast_payouts"] == 2 and row["payouts"] == 2  # 2026 actuals: Jan + Apr

        off = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=False, as_of=AS_OF,
        )
        assert off["total_forecast_net_eur"] == 0.0
        assert all(m["forecast"] == {} for m in off["months"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_sold_position_gets_no_forecast():
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2025, 1, 2), "10", close_date=date(2026, 4, 20)))
        await session.flush()
        for d in (date(2025, 10, 15), date(2026, 1, 15), date(2026, 4, 15)):
            await _seed_payment(session, 1, d, "10.00", "ibkr")

        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=True, as_of=AS_OF,
        )
        assert out["total_forecast_net_eur"] == 0.0
        assert out["securities"][0]["forecast_payouts"] == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_dual_listed_ticker_stays_distinguishable():
    """
    Identity is isin + exchange, so ASML on NASDAQ and on AEB are two securities.
    The chart merges them by symbol (one company, one colour) but the table lists
    both — carrying the venue is what keeps those rows apart.
    """
    engine, session = await _make_session()
    try:
        session.add(Security(id=3, isin="NL0010273215", symbol="ASML", description="ASML",
                             currency="EUR", conid=300, asset_category="STK", exchange="NASDAQ"))
        session.add(Security(id=4, isin="NL0010273215", symbol="ASML", description="ASML",
                             currency="EUR", conid=400, asset_category="STK", exchange="AEB"))
        await session.flush()
        await _seed_payment(session, 3, date(2026, 2, 10), "10.00", "ibkr")
        await _seed_payment(session, 4, date(2026, 2, 10), "2.50", "ibkr")

        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=False, as_of=AS_OF,
        )
        asml = [r for r in out["securities"] if r["symbol"] == "ASML"]
        assert len(asml) == 2
        assert {r["exchange"] for r in asml} == {"NASDAQ", "AEB"}
        # The chart still sees one company: both payments land on one stack key.
        feb = next(m for m in out["months"] if m["month"] == "2026-02")
        assert feb["actual"]["ASML"] == 12.50
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_one_ticker_two_instruments_is_never_summed_into_one_series():
    """
    The same symbol under two ISINs may be one company on two venues or two
    unrelated companies (SBI is Sprott in Toronto and SBI Holdings in Tokyo).
    Nothing here can tell those apart, so the chart key takes the venue.
    """
    engine, session = await _make_session()
    try:
        session.add(Security(id=5, isin="USN070592100", symbol="ASML", description="ASML ADR",
                             currency="USD", conid=500, asset_category="STK", exchange="NASDAQ"))
        session.add(Security(id=6, isin="NL0010273215", symbol="ASML", description="ASML NV",
                             currency="EUR", conid=600, asset_category="STK", exchange="AEB"))
        await session.flush()
        await _seed_payment(session, 5, date(2026, 2, 10), "10.00", "ibkr")
        await _seed_payment(session, 6, date(2026, 2, 10), "2.50", "ibkr")

        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=False, as_of=AS_OF,
        )
        feb = next(m for m in out["months"] if m["month"] == "2026-02")
        assert feb["actual"] == {"ASML (NASDAQ)": 10.00, "ASML (AEB)": 2.50}
        assert feb["actual_total_eur"] == 12.50
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_legacy_row_with_no_net_falls_back_to_gross():
    """
    Rows predating the withholding-fields migration carry gross but a NULL net.
    Both endpoints must fall back to gross — the breakdown crashed on them in
    production (Decimal += None) the moment the income filter stopped keying on
    net alone.
    """
    engine, session = await _make_session()
    try:
        await DividendRepository(session).upsert_payment({
            "security_id": 1, "ex_date": date(2026, 2, 10), "pay_date": date(2026, 2, 10),
            "currency": "EUR", "shares_held": Decimal("10"),
            "gross_amount_eur": Decimal("7.00"),
            "withholding_tax_eur": None, "net_amount_eur": None,
            "source": "yfinance_estimate",
        })
        svc = DividendService(session)

        out = await svc.get_dividend_breakdown(year=2026, include_forecast=False, as_of=AS_OF)
        assert out["total_net_eur"] == 7.00
        assert out["securities"][0]["net_eur"] == 7.00

        summary = await svc.get_dividend_summary()
        assert summary["total_net_eur"] == 7.00
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_summary_ignores_decades_of_empty_pre_ownership_rows():
    """
    yfinance returns a security's whole dividend history, and
    compute_dividend_income writes a zero row for every ex-date with no shares
    held. Counting those gave the card 439 months reaching back to 1985, of
    which 419 were empty — the heatmap would render 41 blank year-rows.
    """
    engine, session = await _make_session()
    try:
        for y in (1985, 1999, 2015):
            await _seed_payment(session, 1, date(y, 8, 12), "0", "yfinance_estimate", gross="0")
        await _seed_payment(session, 1, date(2025, 11, 5), "4.00", "yfinance_estimate")
        await _seed_payment(session, 1, date(2026, 3, 10), "6.00", "ibkr")

        summary = await DividendService(session).get_dividend_summary()
        assert [m["month"] for m in summary["monthly"]] == ["2025-11", "2026-03"]
        assert summary["total_net_eur"] == 10.00
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_zero_amount_estimate_rows_are_not_income():
    engine, session = await _make_session()
    try:
        # Computed while no shares were held at the ex-date: net == 0, bookkeeping only.
        await _seed_payment(session, 1, date(2026, 2, 10), "0", "yfinance_estimate")
        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=False, as_of=AS_OF,
        )
        assert out["securities"] == []
        assert out["total_net_eur"] == 0.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_recently_bought_payer_is_forecast_from_its_own_history():
    """
    TSMC, Samsung, SK Hynix, HPE and the SOXQ ETF were all missing from the
    forecast: bought weeks ago, so no dividend had yet been *received*, even
    though each has years of per-share history. The schedule belongs to the
    company, not to how long we have held it.
    """
    engine, session = await _make_session()
    try:
        bought = date(2026, 4, 20)
        session.add(_lot(1, bought, "100"))
        await session.flush()
        # Quarterly per-share history, all of it from before we owned a share.
        for d in (date(2025, 7, 15), date(2025, 10, 15),
                  date(2026, 1, 15), date(2026, 4, 15)):
            await DividendRepository(session).upsert_payment({
                "security_id": 1, "ex_date": d, "pay_date": d, "currency": "EUR",
                "amount_per_share": Decimal("0.50"), "shares_held": Decimal("0"),
                "gross_amount_eur": Decimal("0"), "withholding_tax_eur": Decimal("0"),
                "net_amount_eur": Decimal("0"), "source": "yfinance_estimate",
            })

        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=True, as_of=AS_OF,
        )
        row = next(r for r in out["securities"] if r["symbol"] == "AAA")
        assert row["payouts"] == 0          # nothing received yet...
        assert row["forecast_payouts"] >= 2  # ...but the schedule is known
        assert row["forecast_basis"] == "gross_estimate"
        # 0.50/share x 100 shares
        assert row["forecast_net_eur"] == pytest.approx(50.0 * row["forecast_payouts"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_next_year_is_forecast_in_full():
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2025, 1, 2), "10"))
        await session.flush()
        for d in (date(2025, 10, 15), date(2026, 1, 15), date(2026, 4, 15)):
            await _seed_payment(session, 1, d, "10.00", "ibkr")

        out = await DividendService(session).get_dividend_breakdown(
            year=2027, include_forecast=True, as_of=AS_OF,
        )
        assert 2027 in out["years"]
        assert [m["month"][:4] for m in out["months"]] == ["2027"] * 12
        assert out["total_net_eur"] == 0.0          # nothing received in 2027 yet
        assert out["total_forecast_net_eur"] > 0    # but the year is projected
        assert sum(1 for m in out["months"] if m["forecast"]) >= 3
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_accumulating_etf_is_never_invented_into_the_forecast():
    """No distributions means no rows means no projection — not a zero-filled one."""
    engine, session = await _make_session()
    try:
        session.add(_lot(2, date(2025, 1, 2), "50"))
        await session.flush()
        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=True, as_of=AS_OF,
        )
        assert all(r["symbol"] != "BBB" for r in out["securities"])
        assert out["total_forecast_net_eur"] == 0.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_two_sources_do_not_halve_the_inferred_cadence():
    """
    The same dividend is stored twice - yfinance under its ex-date, IBKR under
    its pay date, weeks apart. Counting both made ASML's quarterly schedule read
    as ~74 days (5 payouts a year instead of 4) and SBI's monthly as 28 (13
    instead of 12). Dedup can't fix it: Mastercard's ex-to-pay lag of 29 days is
    longer than a monthly payer's whole cycle.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2024, 1, 2), "100"))
        await session.flush()
        repo = DividendRepository(session)
        # Clean quarterly ex-dates from yfinance...
        for d in (date(2025, 7, 9), date(2025, 10, 9),
                  date(2026, 1, 9), date(2026, 4, 9)):
            await repo.upsert_payment({
                "security_id": 1, "ex_date": d, "currency": "EUR",
                "amount_per_share": Decimal("0.20"), "shares_held": Decimal("100"),
                "gross_amount_eur": Decimal("20"), "withholding_tax_eur": Decimal("0"),
                "net_amount_eur": Decimal("20"), "source": "yfinance_estimate",
            })
        # ...and IBKR's record of the same two dividends, at their pay dates.
        for d in (date(2026, 2, 8), date(2026, 5, 8)):
            await repo.upsert_payment({
                "security_id": 1, "ex_date": d, "pay_date": d, "currency": "EUR",
                "shares_held": Decimal("0"), "gross_amount_eur": Decimal("20"),
                "withholding_tax_eur": Decimal("0"), "net_amount_eur": Decimal("20"),
                "source": "ibkr",
            })

        out = await DividendService(session).get_dividend_breakdown(
            year=2027, include_forecast=True, as_of=AS_OF,
        )
        row = next(r for r in out["securities"] if r["forecast_payouts"] > 0)
        assert row["forecast_payouts"] == 4      # quarterly, not 5
    finally:
        await session.close()
        await engine.dispose()


# ── Two figures that used to mislead silently ────────────────────────────


@pytest.mark.asyncio
async def test_a_recently_bought_holding_flags_its_partial_trailing_yield():
    """
    trailing_yield_pct divides trailing-12M income by the FULL position value, so a
    holding bought weeks ago reads a fraction of its real yield — SBI showed 1.0%
    off a single payment. Badged rather than annualized: scaling up would invent
    income the schedule may not support.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2026, 3, 20), "10"))          # ~6 weeks before AS_OF
        session.add(_lot(2, date(2024, 1, 5), "10"))           # long held
        await _seed_payment(session, 1, date(2026, 4, 10), "3.00", "ibkr")
        await _seed_payment(session, 2, date(2026, 4, 10), "3.00", "ibkr")
        await session.commit()

        out = await DividendService(session).get_dividend_breakdown(
            include_forecast=False, as_of=AS_OF
        )
        rows = {r["security_id"]: r for r in out["securities"]}

        assert rows[1]["trailing_yield_partial"] is True
        assert rows[1]["days_held_in_ttm"] < 90
        assert rows[2]["trailing_yield_partial"] is False
        assert rows[2]["days_held_in_ttm"] >= 350
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_forecast_reports_how_thin_its_inference_is():
    """
    forecast_basis said net-vs-estimate but nothing about sample count, so SBI's
    five projected payouts off two rows from the wrong ticker looked like any other
    projection. Two samples is a guess with a schedule attached; say so.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2024, 1, 5), "10"))
        # Exactly two dated per-share payments, a month apart — the SBI shape.
        for on_date in (date(2026, 3, 20), date(2026, 4, 20)):
            await DividendRepository(session).upsert_payment({
                "security_id": 1, "ex_date": on_date, "currency": "EUR",
                "amount_per_share": Decimal("0.5"), "shares_held": Decimal("10"),
                "gross_amount_eur": Decimal("5"), "net_amount_eur": Decimal("5"),
                "withholding_tax_eur": Decimal("0"), "source": "yfinance_estimate",
            })
        await session.commit()

        out = await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF
        )
        row = next(r for r in out["securities"] if r["security_id"] == 1)

        assert row["forecast_payouts"] > 0, "it must actually be projecting"
        assert row["forecast_samples"] == 2
        assert 26 <= row["forecast_cadence_days"] <= 35   # read as monthly
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Forward yield — the portfolio's dividend rate, and its per-security audit.
#
# The headline is Σ(projected next 12M) / Σ(market value), which *is* the
# market-value-weighted average of the per-security yields rather than merely
# resembling one: Σ(Vi/V × Di/Vi) == Σ(Di)/V, every non-payer entering at zero. The
# first test pins that equality from both ends, because it is the only thing making
# the card auditable against the column beside it — and either denominator could be
# "improved" later without anyone noticing the two had stopped agreeing.
# ---------------------------------------------------------------------------

QUARTERLY = (date(2025, 10, 15), date(2026, 1, 15), date(2026, 4, 15))


async def _two_payers(session, price_a="20", price_b="10", per_payment_b="2.50"):
    """
    AAA and BBB, 10 shares each (so `cost_basis_eur` is 100 apiece), priced as asked.
    `price_b=None` leaves BBB unpriced, which is the interesting case.
    """
    session.add(_lot(1, date(2025, 1, 2), "10"))
    session.add(_lot(2, date(2025, 1, 2), "10"))
    session.add(_price(1, price_a))
    if price_b is not None:
        session.add(_price(2, price_b))
    await session.flush()
    for d in QUARTERLY:
        await _seed_payment(session, 1, d, "10.00", "ibkr")
        await _seed_payment(session, 2, d, per_payment_b, "ibkr")
    await session.commit()


@pytest.mark.asyncio
async def test_the_portfolio_yield_is_the_market_value_weighted_average_of_its_holdings():
    engine, session = await _make_session()
    try:
        await _two_payers(session)
        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=True, as_of=AS_OF,
        )
        fy = out["forward_yield"]
        rows = {r["security_id"]: r for r in out["securities"]}

        # Four quarterly payments land in the 365 days after AS_OF for each payer.
        # AAA: 40 over a 200 market value = 20%. BBB: 10 over 100 = 10%.
        assert rows[1]["forward_yield_pct"] == 20.00
        assert rows[2]["forward_yield_pct"] == 10.00
        assert fy["annual_eur"] == 50.00
        assert fy["pct"] == 16.67                        # 50 / 300

        # The identity, rebuilt from the rows the UI actually renders.
        weights = {1: Decimal("200"), 2: Decimal("100")}
        total = sum(weights.values())
        rebuilt = sum(
            weights[sid] / total * Decimal(str(rows[sid]["forward_yield_pct"]))
            for sid in weights
        )
        assert round(float(rebuilt), 2) == fy["pct"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_yield_on_cost_shares_the_numerator_and_the_security_set():
    """
    The pair is only worth showing because the gap between the two is appreciation, so
    `on_cost_pct / pct` must equal `market value / cost basis` exactly. Give either
    denominator a different set of securities and that gap silently starts measuring
    something else.
    """
    engine, session = await _make_session()
    try:
        await _two_payers(session)                       # cost 200, market 300
        fy = (await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF,
        ))["forward_yield"]
        assert fy["on_cost_pct"] == 25.00                # 50 / 200
        assert fy["on_cost_pct"] > fy["pct"]
        # `rel`, not an exact equality: both sides are rounded to 2dp before we see
        # them, so 25.00/16.67 is 1.4997 rather than 1.5. The invariant is about the
        # ratio, not about the last decimal place of a percentage.
        assert fy["on_cost_pct"] / fy["pct"] == pytest.approx(300 / 200, rel=1e-3)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unpriced_holding_is_excluded_from_both_sides_of_the_yield():
    """
    portfolio_service values a position with no cached price at 0.00, so leaving it in
    adds its projected income to the numerator and nothing to the denominator — reading
    the yield HIGH. That is the SBI shape. Its cost basis *is* known and is dropped
    anyway, or the gap against yield-on-cost stops being appreciation.
    """
    engine, session = await _make_session()
    try:
        await _two_payers(session, price_b=None)
        out = await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF,
        )
        fy = out["forward_yield"]
        assert fy["pct"] == 20.00                        # 40 / 200, not 50 / 200
        assert fy["annual_eur"] == 40.00
        assert fy["on_cost_pct"] == 40.00                # 40 / 100, BBB's cost excluded
        assert fy["unpriced_holdings"] == 1
        assert fy["priced_holdings"] == 1
        assert fy["paying_holdings"] == 1
        rows = {r["security_id"]: r for r in out["securities"]}
        assert rows[2]["forward_yield_pct"] is None       # absent, never 0.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_held_non_payer_counts_as_priced_but_never_as_paying():
    """
    The accumulating-ETF case. It drags the yield down, correctly, and must not be
    mistaken for missing data — CLAUDE.md is explicit that their absence is right.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2025, 1, 2), "10"))
        session.add(_lot(2, date(2025, 1, 2), "10"))     # never pays anything
        session.add(_price(1, "20"))
        session.add(_price(2, "20"))
        await session.flush()
        for d in QUARTERLY:
            await _seed_payment(session, 1, d, "10.00", "ibkr")
        await session.commit()

        fy = (await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF,
        ))["forward_yield"]
        assert fy["priced_holdings"] == 2
        assert fy["paying_holdings"] == 1
        assert fy["pct"] == 10.00                        # 40 / 400 — halved by the non-payer
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_projection_run_is_absent_rather_than_a_zero_yield():
    """
    With the forecast off there is nothing to divide, and 0.00% would assert "this
    portfolio pays no dividends" when the truth is that nobody asked. The whole object
    is None, because `paying_holdings: 0` beside `pct: None` is the same lie in
    miniature — 0 of 0 holdings on an account that holds two payers.
    """
    engine, session = await _make_session()
    try:
        await _two_payers(session)
        out = await DividendService(session).get_dividend_breakdown(
            include_forecast=False, as_of=AS_OF,
        )
        assert out["forward_yield"] is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_nothing_priced_is_absent_rather_than_a_zero_yield():
    """The local-dev shape — every position unpriced. Not a 0% portfolio."""
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2025, 1, 2), "10"))
        await session.flush()
        for d in QUARTERLY:
            await _seed_payment(session, 1, d, "10.00", "ibkr")
        await session.commit()

        out = await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF,
        )
        assert out["forward_yield"] is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_priced_book_whose_only_payer_is_unpriced_reports_no_yield():
    """
    The nastiest of the zero cases, and the one that reached a green suite first: the
    priced holdings genuinely project nothing *because the security that projects was
    excluded for having no price*. Dividing gives a confident 0.00% whose real meaning
    is "the interesting holding is missing" — the SBI shape, pointed the other way.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2025, 1, 2), "10"))      # pays, but has no price
        session.add(_lot(2, date(2025, 1, 2), "10"))      # priced, never pays
        session.add(_price(2, "20"))
        await session.flush()
        for d in QUARTERLY:
            await _seed_payment(session, 1, d, "10.00", "ibkr")
        await session.commit()

        out = await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF,
        )
        assert out["forward_yield"] is None              # not 0.00% over BBB alone
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_forward_yield_is_the_same_whichever_year_is_selected():
    """
    A yield describes the next twelve months, so it cannot depend on the window being
    viewed. The per-security column is unwindowed for the same reason, even though
    WHICH rows appear is not — so a row can show a zero forecast beside a real yield
    when its next payment falls past the selected year. Don't reconcile those.
    """
    engine, session = await _make_session()
    try:
        await _two_payers(session)
        svc = DividendService(session)
        all_time = await svc.get_dividend_breakdown(include_forecast=True, as_of=AS_OF)
        this_year = await svc.get_dividend_breakdown(
            year=AS_OF.year, include_forecast=True, as_of=AS_OF,
        )
        last_year = await svc.get_dividend_breakdown(
            year=AS_OF.year - 1, include_forecast=True, as_of=AS_OF,
        )
        assert all_time["forward_yield"] == this_year["forward_yield"]
        assert all_time["forward_yield"] == last_year["forward_yield"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_yield_on_cost_is_not_dragged_down_by_adding_to_a_holding():
    """
    The defect this column had, live on nine of fifteen rows before it was fixed.

    Trailing income over CURRENT cost compares two different positions the moment the
    position grows: the income was earned while the holding was small, the cost is the
    finished holding's. MCO read 0.35% against a real forward rate of 0.84%.

    Forward over cost has no such mismatch — the projection is sized on the shares held
    now, and `cost` is what those shares cost.
    """
    engine, session = await _make_session()
    try:
        # A tenth of the position for most of the year, the rest bought last month.
        session.add(_lot(1, date(2025, 6, 1), "1"))          # cost 10
        session.add(_lot(1, date(2026, 4, 1), "9"))          # cost 90 -> 100 total
        session.add(_price(1, "20"))
        await session.flush()
        for d in QUARTERLY:
            await _seed_payment(session, 1, d, "10.00", "ibkr")
        await session.commit()

        row = (await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF,
        ))["securities"][0]

        # Three payments of 10.00 landed inside the trailing year, two of them while the
        # holding was a single share. Trailing income is therefore 30 — and the old
        # definition divided that by the finished position's cost of 100, giving 30%.
        #
        # The projection instead scales the per-share rate to the shares held now, so it
        # is 100 a quarter, 400 a year, and yield on cost is 400%. The replaced figure
        # understated this holding by more than thirteenfold, which is the same shape as
        # MCO's 0.35% against 0.84% on the real account — just larger because the
        # fixture grows the position tenfold rather than by half.
        assert row["yield_on_cost_pct"] == 400.00
        assert round(30 / 100 * 100, 2) == 30.00           # what it used to report
        # Over market value the same projection reads 400 / (10 x 20).
        assert row["forward_yield_pct"] == 200.00
        # Trailing income spans only 334 of the 350 days the flag requires, so the
        # *trailing* yield stays badged. Yield on cost no longer needs that caveat.
        assert row["trailing_yield_partial"] is True
        assert row["trailing_yield_pct"] == 15.00          # 30 realised over 200
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_selling_and_rebuying_dearer_lowers_yield_on_cost_by_exactly_the_new_cost():
    """
    The case asked about directly. Selling and rebuying at a higher price *should* lower
    yield on cost — you now have more capital committed for the same income — and the
    number must reflect the new cost exactly, not a blend of the old holding's income
    with the new holding's cost.

    Under the old trailing definition the numerator still carried dividends earned on
    the cheap shares that are gone, so the figure was neither the old yield nor the new
    one. Here it is exactly the new one.
    """
    engine, session = await _make_session()
    try:
        # Bought 10 at 10 (cost 100), sold, rebought 10 at 25 (cost 250).
        session.add(_lot(1, date(2025, 1, 2), "10", close_date=date(2026, 4, 20)))
        session.add(_lot(1, date(2026, 4, 21), "10", unit_cost="25"))
        session.add(_price(1, "25"))
        await session.flush()
        for d in QUARTERLY:
            await _seed_payment(session, 1, d, "10.00", "ibkr")
        await session.commit()

        row = (await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF,
        ))["securities"][0]

        # 40 projected over the 250 actually committed now — not over the original 100,
        # and not a mixture. Yield on cost equals the market yield because the rebuy
        # reset cost to market, which is the honest answer.
        assert row["yield_on_cost_pct"] == 16.00
        assert row["forward_yield_pct"] == 16.00
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_row_and_the_card_agree_on_yield_on_cost():
    """
    They are one figure on two screens, and they were computed two different ways: the
    Performance card divided the forward projection by cost from the day it shipped
    while this column divided trailing income. Two definitions under one name is the
    failure this codebase names first, so pin them equal on a single-security book,
    where the weighted aggregate must reduce to the one row exactly.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2025, 1, 2), "10"))
        session.add(_price(1, "20"))
        await session.flush()
        for d in QUARTERLY:
            await _seed_payment(session, 1, d, "10.00", "ibkr")
        await session.commit()

        out = await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF,
        )
        row, fy = out["securities"][0], out["forward_yield"]
        assert row["yield_on_cost_pct"] == fy["on_cost_pct"]
        assert row["forward_yield_pct"] == fy["pct"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_forward_yield_declares_a_gross_estimate_contribution():
    """
    A projection sized from yfinance's gross per-share deducts no withholding, so a
    total carrying any of it is not the net figure a bare label would claim — and a
    yield is the figure most likely to be checked against a broker's own, which quotes
    gross. Quantified rather than flagged, so the footnote can say how much.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2025, 1, 2), "10"))       # received real dividends
        session.add(_lot(2, date(2026, 4, 20), "100"))     # bought after its history
        session.add(_price(1, "20"))
        session.add(_price(2, "20"))
        await session.flush()
        for d in QUARTERLY:
            await _seed_payment(session, 1, d, "10.00", "ibkr")
        # Per-share history with nothing received: the only thing that can size BBB's
        # projection is yfinance's gross per-share, which is what `gross_estimate` means.
        for d in QUARTERLY:
            await DividendRepository(session).upsert_payment({
                "security_id": 2, "ex_date": d, "pay_date": d, "currency": "EUR",
                "amount_per_share": Decimal("0.50"), "shares_held": Decimal("0"),
                "gross_amount_eur": Decimal("0"), "withholding_tax_eur": Decimal("0"),
                "net_amount_eur": Decimal("0"), "source": "yfinance_estimate",
            })
        await session.commit()

        fy = (await DividendService(session).get_dividend_breakdown(
            include_forecast=True, as_of=AS_OF,
        ))["forward_yield"]
        assert fy["basis"] == "mixed"
        assert 0 < fy["gross_estimate_eur"] < fy["annual_eur"]
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# `days_held_in_ttm` is the union of the lot intervals inside the trailing year, not
# `as_of - min(open_date)`. The older form answered "how long ago was this first bought",
# which is the same thing only while the holding is unbroken — and diverges in exactly the
# case the flag exists for.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gap_between_selling_and_rebuying_is_not_counted_as_held():
    """
    Sold out and rebought months later: the old measure reported a full year because the
    FIRST purchase was a year ago, so a yield built from two partial stretches of income
    was presented unqualified. Under-reporting in the case that most needs the badge.
    """
    engine, session = await _make_session()
    try:
        # Held for ~2 months, out for ~7, back for ~1. Roughly 90 days inside the window.
        session.add(_lot(1, AS_OF - timedelta(days=330), "10",
                         close_date=AS_OF - timedelta(days=270)))
        session.add(_lot(1, AS_OF - timedelta(days=30), "10"))
        session.add(_price(1, "20"))
        await session.flush()
        await _seed_payment(session, 1, AS_OF - timedelta(days=300), "5.00", "ibkr")
        await _seed_payment(session, 1, AS_OF - timedelta(days=20), "5.00", "ibkr")
        await session.commit()

        row = (await DividendService(session).get_dividend_breakdown(
            include_forecast=False, as_of=AS_OF,
        ))["securities"][0]

        # 60 days in the first stretch + 30 in the second. The old form gave 330.
        assert row["days_held_in_ttm"] == 90
        assert row["trailing_yield_partial"] is True
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_overlapping_lots_do_not_double_count_a_day():
    """Three lots open across the same stretch is one stretch held, not three."""
    engine, session = await _make_session()
    try:
        for _ in range(3):
            session.add(_lot(1, AS_OF - timedelta(days=100), "5"))
        session.add(_price(1, "20"))
        await session.flush()
        await _seed_payment(session, 1, AS_OF - timedelta(days=20), "5.00", "ibkr")
        await session.commit()

        row = (await DividendService(session).get_dividend_breakdown(
            include_forecast=False, as_of=AS_OF,
        ))["securities"][0]
        assert row["days_held_in_ttm"] == 100
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_continuously_held_position_still_reports_full_coverage():
    """
    The direction that must not regress: a holding bought years ago and never sold is
    fully covered, and switching to an interval union must not start badging it.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, AS_OF - timedelta(days=800), "10"))
        session.add(_price(1, "20"))
        await session.flush()
        await _seed_payment(session, 1, AS_OF - timedelta(days=20), "5.00", "ibkr")
        await session.commit()

        row = (await DividendService(session).get_dividend_breakdown(
            include_forecast=False, as_of=AS_OF,
        ))["securities"][0]
        assert row["days_held_in_ttm"] == 365
        assert row["trailing_yield_partial"] is False
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_estimate_that_landed_while_shares_were_held_is_still_gross():
    """
    `compute_dividend_income` writes a yfinance_estimate row's net as its gross with
    zero withholding. Dividing that by the shares gives the gross per-share figure
    straight back — and the forecast stamped it `net`, labelling a projection that
    deducts no withholding as one that did. SK Hynix read `net` on production with
    zero IBKR payouts on record. Only an IBKR row is money that actually landed.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(1, date(2025, 1, 2), "10"))   # AAA: estimates only
        session.add(_lot(2, date(2025, 1, 2), "10"))   # BBB: the broker's own rows
        session.add(_price(1, "20"))                    # priced, so a yield exists
        session.add(_price(2, "20"))
        await session.flush()
        for d in (date(2025, 7, 15), date(2025, 10, 15),
                  date(2026, 1, 15), date(2026, 4, 15)):
            # Held on every ex-date, so the estimate carries a real gross figure...
            await DividendRepository(session).upsert_payment({
                "security_id": 1, "ex_date": d, "pay_date": d, "currency": "EUR",
                "amount_per_share": Decimal("0.50"), "shares_held": Decimal("10"),
                "gross_amount_eur": Decimal("5"), "withholding_tax_eur": Decimal("0"),
                "net_amount_eur": Decimal("5"), "source": "yfinance_estimate",
            })
            # ...while BBB's history is what IBKR paid, net of 15% withholding.
            await _seed_payment(session, 2, d, net="4.25", source="ibkr",
                                gross="5.00", wht="0.75")

        out = await DividendService(session).get_dividend_breakdown(
            year=2026, include_forecast=True, as_of=AS_OF,
        )
        rows = {r["symbol"]: r for r in out["securities"]}
        assert rows["AAA"]["forecast_basis"] == "gross_estimate"
        assert rows["BBB"]["forecast_basis"] == "net"
        # And the total's three-way flag sees the gross half rather than a flat "net".
        assert out["forward_yield"]["basis"] == "mixed"
    finally:
        await session.close()
        await engine.dispose()
