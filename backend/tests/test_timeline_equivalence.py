"""
The swept timeline must be NUMERICALLY IDENTICAL to the per-day per-lot loop it
replaced — same Decimal arithmetic, same skip rules, same rounding — across the
awkward shapes: closed lots, a same-day rotation, price gaps that exercise the
forward-fill, a non-EUR security, and a non-EUR base currency.
"""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.portfolio_service import BaseFx, PortfolioService
from tests.memory_db import memory_engine

START = date(2026, 3, 2)
END = date(2026, 4, 10)


def _sec(sid, sym, currency):
    return SimpleNamespace(id=sid, symbol=sym, currency=currency)


def _lot(open_date, qty, cost_eur, close_date=None):
    return SimpleNamespace(
        open_date=open_date, close_date=close_date,
        quantity=Decimal(qty), cost_basis_eur=Decimal(cost_eur),
    )


def _fixture():
    eur = _sec(1, "EUR1", "EUR")
    usd = _sec(2, "USD1", "USD")
    rot_old = _sec(3, "ROLD", "EUR")
    rot_new = _sec(4, "RNEW", "EUR")

    lots = [
        (_lot(date(2026, 1, 5), "10", "100"), eur),                       # held throughout
        (_lot(date(2026, 3, 10), "5", "50"), eur),                        # opens mid-range
        (_lot(date(2026, 2, 1), "8", "200", close_date=date(2026, 3, 25)), usd),  # closes mid-range
        (_lot(date(2026, 1, 10), "10", "110", close_date=date(2026, 3, 18)), rot_old),
        (_lot(date(2026, 3, 18), "10", "115"), rot_new),                  # same-day rotation
    ]

    # Prices with gaps (forward-fill must kick in); none for ROLD after the 17th
    # and none for RNEW before the 18th.
    price_cache = {1: {}, 2: {}, 3: {}, 4: {}}
    d = date(2026, 1, 5)
    while d <= END:
        if d.weekday() < 5 and d.day % 7 != 0:  # punch holes
            price_cache[1][d] = Decimal("11")
            price_cache[2][d] = Decimal("30")
            if d < date(2026, 3, 18):
                price_cache[3][d] = Decimal("11.5")
            if d >= date(2026, 3, 18):
                price_cache[4][d] = Decimal("11.6")
        d += timedelta(days=1)

    # USD->EUR with weekend gaps too.
    fx_cache = {"USD": {}}
    d = date(2026, 1, 5)
    while d <= END:
        if d.weekday() < 5:
            fx_cache["USD"][d] = Decimal("0.9")
        d += timedelta(days=1)

    currency_map = {1: "EUR", 2: "USD", 3: "EUR", 4: "EUR"}

    # Non-EUR base with a drifting rate, so per-date projection matters.
    base_rates = {}
    d = date(2026, 1, 1)
    while d <= END:
        base_rates[d] = Decimal("0.93") + Decimal(d.toordinal() % 5) / 1000
        d += timedelta(days=1)
    base_fx = BaseFx("CHF", base_rates)

    return lots, price_cache, fx_cache, currency_map, base_fx


@pytest.mark.parametrize("use_chf_base", [False, True])
def test_swept_timeline_equals_the_per_day_loop(use_chf_base):
    lots, price_cache, fx_cache, currency_map, chf_fx = _fixture()
    base_fx = chf_fx if use_chf_base else BaseFx("EUR", {})

    svc = PortfolioService.__new__(PortfolioService)  # sync methods need no DB

    swept = svc._calculate_timeline_swept(
        START, END, lots, price_cache, fx_cache, currency_map, base_fx,
    )

    per_day = []
    d = START
    while d <= END:
        if d.weekday() < 5:
            per_day.append(svc._calculate_daily_value(
                d, lots, price_cache, fx_cache,
                price_currency_cache=currency_map, base_fx=base_fx,
            ))
        d += timedelta(days=1)

    # The sweep additionally reports the day's external flow, which the per-day
    # helper has no notion of; the valuation numbers must still match exactly.
    valuation_keys = set(per_day[0])
    assert [{k: v for k, v in row.items() if k in valuation_keys} for row in swept] == per_day
    # Sanity that the fixture exercised what it claims to.
    assert any(row["cost_basis_eur"] != per_day[0]["cost_basis_eur"] for row in per_day)


def test_the_two_walks_agree_when_a_position_starts_after_its_lots():
    """
    A spun-off line's lots predate its listing, so both walks skip it for *value* while
    still counting its cost. That has to be the same skip in both, or the chart and every
    point query (summary, XIRR, Steuerwert) disagree about the same day — and the whole
    point of the floor is that the day stops being reported as partial.
    """
    lots, price_cache, fx_cache, currency_map, base_fx = _fixture()
    svc = PortfolioService.__new__(PortfolioService)
    # Security 1 is held and priced across the whole window, so a floor inside it bites
    # visibly rather than coinciding with a lot boundary.
    floor = {1: date(2026, 3, 20)}

    def _per_day(position_start):
        out, d = [], START
        while d <= END:
            if d.weekday() < 5:
                out.append(svc._calculate_daily_value(
                    d, lots, price_cache, fx_cache, price_currency_cache=currency_map,
                    base_fx=base_fx, position_start=position_start,
                ))
            d += timedelta(days=1)
        return out

    swept = svc._calculate_timeline_swept(
        START, END, lots, price_cache, fx_cache, currency_map, base_fx,
        position_start=floor,
    )
    per_day = _per_day(floor)
    valuation_keys = set(per_day[0])
    assert [{k: v for k, v in r.items() if k in valuation_keys} for r in swept] == per_day

    # And the floor actually changed something: before it, security 1 leaves market value
    # while its cost stays — and the day is still not reported as unpriced.
    unfloored = {r["date"]: r for r in _per_day(None)}
    floored = {r["date"]: r for r in per_day}
    assert floored["2026-03-19"]["market_value_eur"] < unfloored["2026-03-19"]["market_value_eur"]
    assert floored["2026-03-19"]["cost_basis_eur"] == unfloored["2026-03-19"]["cost_basis_eur"]
    assert floored["2026-03-19"]["unpriced_holdings"] == unfloored["2026-03-19"]["unpriced_holdings"]
    assert floored["2026-03-20"] == unfloored["2026-03-20"]


def test_external_flow_reports_purchases_at_cost_and_sales_at_proceeds():
    """
    A sale leaves the holdings at its market value, not at the cost it was
    bought for. Inferring the flow from the cost-basis line instead books the
    difference as a phantom return on the sale date.
    """
    lots, price_cache, fx_cache, currency_map, _ = _fixture()
    svc = PortfolioService.__new__(PortfolioService)
    base_fx = BaseFx("EUR", {})

    # ROLD: 10 shares closed 2026-03-18, priced 11.5 -> 115 of proceeds.
    swept = svc._calculate_timeline_swept(
        START, END, lots, price_cache, fx_cache, currency_map, base_fx,
        disposals_by_day={date(2026, 3, 18): Decimal("115")},
    )
    by_date = {row["date"]: row for row in swept}

    # That day also opens RNEW at 115 of cost, so the rotation nets to zero...
    assert by_date["2026-03-18"]["external_flow_eur"] == pytest.approx(0.0)
    # ...while the cost-basis line moves by +115 − 110, which is what made the
    # old inference wrong.
    assert by_date["2026-03-10"]["external_flow_eur"] == pytest.approx(50.0)  # a purchase
    assert by_date["2026-03-11"]["external_flow_eur"] == pytest.approx(0.0)   # a quiet day


def test_first_row_reports_only_that_days_flow():
    """
    Lots opened before the window seed the opening state; they are history, not
    day-0 flows. The catch-up loop used to feed every pre-window purchase into
    pending_flow, so the first row reported the whole acquisition history
    (410 EUR here) as that day's external flow.
    """
    lots, price_cache, fx_cache, currency_map, _ = _fixture()
    svc = PortfolioService.__new__(PortfolioService)
    base_fx = BaseFx("EUR", {})

    swept = svc._calculate_timeline_swept(
        START, END, lots, price_cache, fx_cache, currency_map, base_fx,
    )
    assert swept[0]["date"] == START.isoformat()
    # Three lots (100 + 200 + 110 EUR) predate the window; nothing happened on
    # the first day itself, so its flow is zero.
    assert swept[0]["external_flow_eur"] == pytest.approx(0.0)

    # A purchase ON the window start IS that day's own flow — and only it.
    eur = lots[0][1]
    swept = svc._calculate_timeline_swept(
        START, END, lots + [(_lot(START, "3", "40"), eur)],
        price_cache, fx_cache, currency_map, base_fx,
    )
    assert swept[0]["external_flow_eur"] == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_a_lot_closed_on_the_window_start_adds_no_disposal_flow():
    """
    The disposal window is (start, end], matching exclude-on-close: a lot sold
    ON the window start never contributed value to the series (its own close
    event removes it from day 0), so its proceeds must not land in day 0's
    external_flow_eur — that would be a flow against value the series never
    held. A lot closed strictly inside the window still flows normally.
    """
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        window_start, window_end = date(2026, 3, 2), date(2026, 3, 6)  # Mon-Fri
        session.add(Security(id=1, isin="US0000000001", symbol="AAA",
                             description="AAA", currency="EUR", conid=101,
                             asset_category="STK", exchange="XETRA"))

        def _db_lot(qty, cost, close_date=None):
            return TaxLot(
                security_id=1, open_date=date(2026, 1, 5), close_date=close_date,
                is_open=close_date is None, quantity=Decimal(qty),
                cost_basis=Decimal(cost), cost_basis_eur=Decimal(cost),
                price_per_unit=Decimal(cost) / Decimal(qty), currency="EUR",
            )

        session.add_all([
            _db_lot("10", "100", close_date=window_start),   # sold ON day 0
            _db_lot("10", "100"),                             # held throughout
            _db_lot("5", "50", close_date=date(2026, 3, 4)),  # sold mid-window
        ])
        d = window_start
        while d <= window_end:
            session.add(MarketPrice(security_id=1, date=d,
                                    close_price=Decimal("11"),
                                    currency="EUR", source="test"))
            d += timedelta(days=1)
        await session.flush()

        timeline = await PortfolioService(session).get_portfolio_value_over_time(
            window_start, window_end
        )
        flows = {row["date"]: row["external_flow_eur"] for row in timeline}

        # Day 0: the lot sold that day was never in the series (day-0 value
        # carries only the other 15 shares), so no disposal flow either.
        assert timeline[0]["date"] == window_start.isoformat()
        assert flows["2026-03-02"] == pytest.approx(0.0)
        # The mid-window sale still reports its proceeds (5 sh x 11) as money
        # leaving the tracked holdings on its own day.
        assert flows["2026-03-04"] == pytest.approx(-55.0)
        assert flows["2026-03-03"] == pytest.approx(0.0)
        assert flows["2026-03-05"] == pytest.approx(0.0)
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Completeness. An unpriced holding is dropped from market value while its cost stays in
# cost_basis_eur, so the point silently understates by that holding's whole value.
#
# Measured against production: fifteen days past the last cached price every holding
# falls out of the 14-day lookback and the endpoint reports market value 0.00 with
# gain_loss_percent −100.0 — a fabricated total loss rather than a gap. The PARTIAL case
# is the dangerous one: at fourteen days some securities still resolve and the total read
# +15.3%, which looks like an answer and invites no doubt. That is what a stalled price
# feed looks like on the chart: a smooth decay to zero, not a missing line.
#
# `unpriced_holdings` is the signal. It was previously only a logger.warning, at up to
# ~29k lines for a 730-day window over 40 securities.
# ---------------------------------------------------------------------------


async def _priced_and_unpriced_session():
    """Two EUR securities, both held; only one has any cached price."""
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    for sid, sym in ((1, "AAA"), (2, "BBB")):
        session.add(Security(id=sid, isin=f"X{sid:011d}", symbol=sym, description=sym,
                             currency="EUR", conid=100 + sid, asset_category="STK",
                             exchange="XETRA"))
    for sid in (1, 2):
        session.add(TaxLot(
            security_id=sid, open_date=START, quantity=Decimal("10"),
            cost_basis=Decimal("100"), cost_basis_eur=Decimal("100"),
            price_per_unit=Decimal("10"), currency="EUR", is_open=True,
        ))
    d = START
    while d <= END:                      # AAA only — BBB is never priced
        session.add(MarketPrice(security_id=1, date=d, close_price=Decimal("12"),
                                currency="EUR", source="test"))
        d += timedelta(days=1)
    await session.flush()
    await session.commit()
    return engine, session


@pytest.mark.asyncio
async def test_a_partial_valuation_says_so_instead_of_looking_complete():
    engine, session = await _priced_and_unpriced_session()
    try:
        svc = PortfolioService(session)
        timeline = await svc.get_portfolio_value_over_time(START, END)

        weekday = next(r for r in timeline if r["unpriced_holdings"] is not None)
        # Both lots' cost counts; only AAA's value does — so the point is a partial sum
        # and must not present itself as a whole one.
        assert weekday["cost_basis_eur"] == pytest.approx(200.0)
        assert weekday["market_value_eur"] == pytest.approx(120.0)
        assert weekday["unpriced_holdings"] == 1

        # Every emitted day is partial here, and none of them claims otherwise.
        assert all(r["unpriced_holdings"] == 1 for r in timeline)
        # The loss it appears to show is entirely the missing holding.
        assert weekday["gain_loss_eur"] == pytest.approx(-80.0)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_fully_priced_day_reports_nothing_unpriced():
    """The other direction: the field must stay 0 on healthy data, or it is just noise."""
    engine, session = await _priced_and_unpriced_session()
    try:
        d = START
        while d <= END:
            session.add(MarketPrice(security_id=2, date=d, close_price=Decimal("12"),
                                    currency="EUR", source="test"))
            d += timedelta(days=1)
        await session.commit()

        timeline = await PortfolioService(session).get_portfolio_value_over_time(START, END)
        assert timeline
        assert all(r["unpriced_holdings"] == 0 for r in timeline)
        assert timeline[-1]["market_value_eur"] == pytest.approx(240.0)
    finally:
        await session.close()
        await engine.dispose()
