"""
The benchmark can be anchored to the selected window, and its seed is the chart's own first point.

Both lines used to be absolute and inception-based, so on a 3M range the portfolio's first
point was its own value that day while the benchmark's carried every contribution since
2024 — two years of relative performance in the starting gap, on a chart that drew none of
it. Window mode seeds the hypothetical with the portfolio's Total Value on the anchor day
and applies only the contributions after it, so the two lines start at one figure and the
gap between them is what happened inside the window.

What the tests below pin, and why each would be a wrong number the other way:

- The seed is the portfolio's **first chart point**, read through the same pipeline —
  under CHF too, where seeding from the EUR components misses by the FX projection on cash.
- Pre-window contributions never enter; a contribution inside the window buys shares at
  that day's price, so the portfolio cannot appear to outperform by what was paid in.
- Flow-free daily ratios are identical to the absolute series (beta is unchanged by the
  anchor), and a contribution day is *named* so beta can skip it.
- Over the whole history the two anchors agree to the cent.
- Window mode never touches `benchmark_timeline_cache`, which is keyed on (benchmark, date)
  and holds the absolute series — a per-window series written there would poison every
  other range.
"""
import asyncio
from datetime import date
from decimal import Decimal
from typing import Dict, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
import app.models  # noqa: F401
from app.models.benchmark_price import BenchmarkPrice
from app.models.benchmark_timeline_cache import BenchmarkTimelineCache
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.benchmark_service import BenchmarkService, reset_upstream_throttle
from app.services.portfolio_service import PortfolioService

# March 2026: the 1st is a Sunday, the 2nd a Monday, the 7th/8th a weekend.
MARCH = [date(2026, 3, d) for d in range(1, 32)]
WINDOW = (date(2026, 3, 5), date(2026, 3, 20))


@pytest.fixture(autouse=True)
def _clear_throttle():
    reset_upstream_throttle()
    yield
    reset_upstream_throttle()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """
    No provider, ever. `_ensure_prices_available` swallows its own errors and serves the
    cache, so a raiser alone is silent — the reach is recorded and asserted on teardown,
    which is what makes an accidental fetch fail the test instead of spending budget.
    The fetch steps themselves are stubbed to no-ops: they have their own tests, and a
    window whose index is unpriced at its start (`index_from`) must not trigger one here.
    """
    import app.services.benchmark_service as bs

    reached = []

    def _forbidden(*args, **kwargs):
        reached.append(args)
        raise AssertionError("a test tried to call Yahoo Finance")

    async def _no_prices(self, *args, **kwargs):
        return 0

    async def _no_fx(self, *args, **kwargs):
        return None

    monkeypatch.setattr(bs.yf, "Ticker", _forbidden, raising=False)
    monkeypatch.setattr(BenchmarkService, "_ensure_prices_available", _no_prices)
    monkeypatch.setattr(BenchmarkService, "_ensure_fx_rates_available", _no_fx)
    yield
    assert reached == [], "a test reached Yahoo Finance"


async def _session(index_from: date = MARCH[0], stock_price: Optional[str] = "120"):
    """
    Every table, not the handful the sibling tests create: the anchor is read through
    `get_portfolio_value_over_time`, the chart's own pipeline, which touches cash, trades,
    dividends and corporate actions.

    One EUR security and the DAX (a EUR index) keep the arithmetic readable; the FX leg of
    the seed has its own test below. Index and stock are flat so any movement a test sees
    is the event under test.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(
        id=1, isin="US0000000001", symbol="AAA", description="Test Co",
        currency="EUR", conid=100, asset_category="STK", exchange="XETRA",
    ))
    for day in MARCH:
        if day >= index_from:
            session.add(BenchmarkPrice(
                ticker="^GDAXI", date=day, close_price=Decimal("100"), currency="EUR",
            ))
        if stock_price is not None:
            session.add(MarketPrice(
                security_id=1, date=day, close_price=Decimal(stock_price),
                currency="EUR", source="test",
            ))
    await session.flush()
    return engine, session


def _lot(open_date, cost, close_date=None):
    return TaxLot(
        security_id=1, open_date=open_date, quantity=Decimal("10"),
        cost_basis=Decimal(cost), price_per_unit=Decimal(cost) / 10, currency="EUR",
        cost_basis_eur=Decimal(cost), is_open=close_date is None,
        close_date=close_date, close_source="trade" if close_date else None,
    )


def _deposit(key, day, amount):
    return CashFlow(
        ib_key=key, flow_date=day, flow_type=DEPOSIT_WITHDRAW,
        amount=Decimal(amount), currency="EUR", amount_eur=Decimal(amount),
    )


async def _standard_book(session):
    """
    A lot bought on the 2nd for 1,000 (10 shares, quoted at 120 -> worth 1,200), deposits
    of 1,000 on the 2nd and 500 on the 3rd, ledger covered from the 1st. No trade rows, so
    the derived cash is the deposits themselves: the portfolio's Total Value is 2,700 from
    the 3rd on, while money in is 1,500 — two different figures, which is what lets a test
    tell "seeded from the portfolio's value" apart from "seeded from the contributions".
    """
    session.add(_lot(date(2026, 3, 2), "1000"))
    session.add(_deposit("D1", date(2026, 3, 2), "1000"))
    session.add(_deposit("D2", date(2026, 3, 3), "500"))
    await session.flush()
    await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 3, 1))
    await session.flush()


async def _double_the_index_from(session, day: date):
    for d in MARCH:
        if d >= day:
            await session.execute(
                BenchmarkPrice.__table__.update()
                .where(BenchmarkPrice.date == d)
                .values(close_price=Decimal("200"))
            )
    await session.flush()


async def _series(session, start, end, anchor) -> Dict[str, Dict]:
    pts = await BenchmarkService(session).calculate_benchmark_value_over_time(
        start, end, "dax", anchor=anchor
    )
    return {p["date"]: p for p in pts}


async def _chart(session, start, end) -> Dict[str, Dict]:
    pts = await PortfolioService(session).get_portfolio_value_over_time(start, end)
    return {p["date"]: p for p in pts}


async def _cache_rows(session) -> int:
    return (await session.execute(
        select(func.count()).select_from(BenchmarkTimelineCache)
    )).scalar()


# ── The seed is the chart's own first point ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_first_window_point_is_the_portfolios_first_chart_point():
    engine, session = await _session()
    try:
        await _standard_book(session)
        svc = BenchmarkService(session)
        pts = await svc.calculate_benchmark_value_over_time(*WINDOW, "dax", anchor="window")
        chart = await PortfolioService(session).get_portfolio_value_over_time(*WINDOW)

        assert pts[0]["date"] == chart[0]["date"] == "2026-03-05"
        # The VALUE of the book, not the money put into it.
        assert chart[0]["total_value_eur"] == pytest.approx(2700)
        assert pts[0]["benchmark_value_eur"] == pytest.approx(chart[0]["total_value_eur"], abs=0.02)

        assert svc.last_anchor is not None
        assert svc.last_anchor.on_date == date(2026, 3, 5)
        assert svc.last_anchor.value_eur == pytest.approx(2700)
        assert svc.last_anchor.unpriced_holdings == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_under_a_non_eur_base_the_seed_still_lands_on_the_chart_point():
    """
    The chart projects each cash event at its own date and the holdings at the valuation
    date; the benchmark pipeline computes in EUR and projects once at each point's date.
    Seeding from the EUR components (1,200 + 1,500 = 2,700 EUR) and letting the projection
    multiply by the anchor-day rate gives 2,565 CHF against a chart point of 2,490 — the
    deposits were made when the franc was cheaper. Dividing the chart's base figure back
    through the anchor-day rate is what makes the two land on one number.
    """
    engine, session = await _session()
    try:
        await AppSettingsRepository(session).set_base_currency("CHF")
        for d in MARCH:
            session.add(ExchangeRate(
                date=d, from_currency="EUR", to_currency="CHF",
                rate=Decimal("0.90") if d <= date(2026, 3, 3) else Decimal("0.95"),
                source="test",
            ))
        await session.flush()
        await _standard_book(session)

        pts = await BenchmarkService(session).calculate_benchmark_value_over_time(
            *WINDOW, "dax", anchor="window"
        )
        chart = await PortfolioService(session).get_portfolio_value_over_time(*WINDOW)

        # 1,200 EUR of stock at 0.95 plus 1,000 at 0.90 and 500 at 0.90 of cash.
        assert chart[0]["total_value_eur"] == pytest.approx(1140 + 900 + 450)
        assert pts[0]["benchmark_value_eur"] == pytest.approx(chart[0]["total_value_eur"], abs=0.02)
        assert pts[0]["benchmark_value_eur"] != pytest.approx(2700 * 0.95, abs=1)
    finally:
        await engine.dispose()


# ── Contributions: before the window never enter, inside it always do ───────────────


@pytest.mark.asyncio
async def test_contributions_before_the_window_do_not_enter():
    engine, session = await _session()
    try:
        await _standard_book(session)
        window = await _series(session, *WINDOW, anchor="window")
        inception = await _series(session, *WINDOW, anchor="inception")

        # The absolute series is the two deposits bought into a flat index: 1,500.
        assert inception["2026-03-05"]["benchmark_value_eur"] == pytest.approx(1500)
        # The window series is what the book was worth that day, and not 1,500 + 2,700.
        assert window["2026-03-05"]["benchmark_value_eur"] == pytest.approx(2700)
        assert window["2026-03-20"]["benchmark_value_eur"] == pytest.approx(2700)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_contribution_inside_the_window_buys_index_shares():
    """
    The whole difficulty. Scaling or shifting the absolute series so its first point
    matched the portfolio's would scale the in-window deposit too, and the portfolio would
    then appear to beat the index by exactly what was paid in. A seeded walk buys the
    deposit into the index on the day, so with a flat index both sides step by exactly it.
    """
    engine, session = await _session()
    try:
        await _standard_book(session)
        session.add(_deposit("D3", date(2026, 3, 11), "300"))
        await session.flush()

        window = await _series(session, *WINDOW, anchor="window")
        chart = await _chart(session, *WINDOW)

        assert window["2026-03-10"]["benchmark_value_eur"] == pytest.approx(2700)
        assert window["2026-03-11"]["benchmark_value_eur"] == pytest.approx(3000)
        assert window["2026-03-11"]["benchmark_value_eur"] == pytest.approx(
            chart["2026-03-11"]["total_value_eur"], abs=0.02
        )
        # The baseline is "what the window started with plus what was added".
        assert window["2026-03-11"]["cost_basis_eur"] == pytest.approx(3000)
        assert window["2026-03-11"]["gain_loss_eur"] == pytest.approx(0)
        # ...and the day is NAMED, so the client's beta regression can skip it.
        assert window["2026-03-10"]["external_flow_eur"] == pytest.approx(0)
        assert window["2026-03-11"]["external_flow_eur"] == pytest.approx(300)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_flow_free_daily_ratios_are_identical_to_the_absolute_series():
    """
    Beta regresses day-over-day ratios on flow-free days. Between contributions the share
    count is constant under either anchor, so the ratio is the index's own return either
    way — a seeded walk preserves returns where an additive shift would not. The one day
    the two ratios differ is the contribution day, which is exactly the one that carries
    a flow for beta to skip.
    """
    engine, session = await _session()
    try:
        await _standard_book(session)
        session.add(_deposit("D3", date(2026, 3, 11), "300"))
        await session.flush()
        await _double_the_index_from(session, date(2026, 3, 16))

        window = await _series(session, *WINDOW, anchor="window")
        inception = await _series(session, *WINDOW, anchor="inception")
        days = sorted(window)
        assert days == sorted(inception)

        compared = 0
        for prev, curr in zip(days, days[1:]):
            w = window[curr]["benchmark_value_eur"] / window[prev]["benchmark_value_eur"]
            a = inception[curr]["benchmark_value_eur"] / inception[prev]["benchmark_value_eur"]
            if window[curr]["external_flow_eur"]:
                assert w != pytest.approx(a), curr   # the deposit, on a different base
                continue
            assert w == pytest.approx(a, rel=1e-9), curr
            compared += 1
        assert compared >= 9
        # The index doubling is in both, on the same day.
        assert window["2026-03-16"]["benchmark_value_eur"] == pytest.approx(
            2 * window["2026-03-13"]["benchmark_value_eur"]
        )
    finally:
        await engine.dispose()


# ── The two anchors agree over the whole history, and the cache is untouched ───────


@pytest.mark.asyncio
async def test_over_the_whole_history_the_two_anchors_agree_to_the_cent():
    """
    ALL is the no-op case. When the window starts where the money did, the seed *is* the
    first contribution — here a deposit that sat in cash for two days before it was
    invested, so the anchor-day value equals the leg exactly — and the two series must be
    the same series, value and baseline alike, through an index move and a later deposit.
    """
    engine, session = await _session()
    try:
        session.add(_deposit("D1", date(2026, 3, 2), "1000"))
        session.add(_lot(date(2026, 3, 4), "1000"))
        session.add(_deposit("D2", date(2026, 3, 10), "500"))
        await session.flush()
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 3, 1))
        await session.flush()
        await _double_the_index_from(session, date(2026, 3, 16))

        start, end = date(2026, 3, 2), date(2026, 3, 20)
        window = await _series(session, start, end, anchor="window")
        inception = await _series(session, start, end, anchor="inception")

        assert sorted(window) == sorted(inception)
        for d in window:
            assert window[d]["benchmark_value_eur"] == pytest.approx(
                inception[d]["benchmark_value_eur"], abs=0.01), d
            assert window[d]["cost_basis_eur"] == pytest.approx(
                inception[d]["cost_basis_eur"], abs=0.01), d
        assert window["2026-03-20"]["benchmark_value_eur"] == pytest.approx(3000)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_window_mode_never_touches_the_timeline_cache():
    engine, session = await _session()
    try:
        await _standard_book(session)
        assert await _cache_rows(session) == 0

        await _series(session, *WINDOW, anchor="window")
        assert await _cache_rows(session) == 0

        # The absolute series still caches, exactly as before.
        await _series(session, *WINDOW, anchor="inception")
        assert await _cache_rows(session) > 0

        # ...and a cached absolute series does not leak into a window request.
        window = await _series(session, *WINDOW, anchor="window")
        assert window["2026-03-05"]["benchmark_value_eur"] == pytest.approx(2700)
    finally:
        await engine.dispose()


# ── Where the anchor lands ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_weekend_start_anchors_on_the_monday_the_chart_also_starts_on():
    engine, session = await _session()
    try:
        await _standard_book(session)
        start, end = date(2026, 3, 7), date(2026, 3, 20)   # a Saturday
        svc = BenchmarkService(session)
        pts = await svc.calculate_benchmark_value_over_time(start, end, "dax", anchor="window")
        chart = await PortfolioService(session).get_portfolio_value_over_time(start, end)

        assert svc.last_anchor.on_date == date(2026, 3, 9)
        assert pts[0]["date"] == chart[0]["date"] == "2026-03-09"
        assert pts[0]["benchmark_value_eur"] == pytest.approx(chart[0]["total_value_eur"], abs=0.02)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_index_with_no_price_at_the_start_anchors_on_its_first_priced_day():
    engine, session = await _session(index_from=date(2026, 3, 5))
    try:
        await _standard_book(session)
        svc = BenchmarkService(session)
        pts = await svc.calculate_benchmark_value_over_time(
            date(2026, 3, 2), date(2026, 3, 20), "dax", anchor="window"
        )
        assert svc.last_anchor.on_date == date(2026, 3, 5)
        assert pts[0]["date"] == "2026-03-05"
        assert pts[0]["benchmark_value_eur"] == pytest.approx(2700)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_anchor_the_portfolio_could_not_fully_value_is_declared():
    """
    No stock price at all, so the anchor day values the holding at 0 and the seed is the
    cash alone. The series is still served — refusing would silently flip the line's
    meaning — but the shortfall is declared, and the chart folds it into its notice.
    """
    engine, session = await _session(stock_price=None)
    try:
        await _standard_book(session)
        svc = BenchmarkService(session)
        pts = await svc.calculate_benchmark_value_over_time(*WINDOW, "dax", anchor="window")

        assert svc.last_anchor.unpriced_holdings == 1
        assert svc.last_anchor.value_eur == pytest.approx(1500)
        assert pts and pts[0]["benchmark_value_eur"] == pytest.approx(1500)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unknown_anchor_is_refused_before_any_work():
    engine, session = await _session()
    try:
        with pytest.raises(ValueError):
            await BenchmarkService(session).calculate_benchmark_value_over_time(
                *WINDOW, "dax", anchor="rebased"
            )
    finally:
        await engine.dispose()


# ── Through the HTTP stack: the new fields survive the response_model ─────────────


@pytest.fixture()
def client(monkeypatch):
    """
    The smoke-test fixture shape. `/api/portfolio/benchmark` is excluded from
    `test_api_smoke.py` because it lazy-fetches Yahoo on a cache miss; the module-wide
    `_offline` fixture is what keeps this one from doing the same.
    """
    loop = asyncio.new_event_loop()
    holder = {}

    async def _setup():
        engine, session = await _session()
        await _standard_book(session)
        session.add(_deposit("D3", date(2026, 3, 11), "300"))
        await session.flush()
        holder["engine"], holder["session"] = engine, session

    loop.run_until_complete(_setup())

    from app.main import app

    async def _override():
        yield holder["session"]

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()
    loop.run_until_complete(holder["session"].close())
    loop.run_until_complete(holder["engine"].dispose())
    loop.close()


def test_the_window_anchor_fields_survive_the_response_model(client):
    r = client.get(
        "/api/portfolio/benchmark?start_date=2026-03-05&end_date=2026-03-20"
        "&benchmark=dax&anchor=window"
    )
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert body["anchor"] == "window"
    assert body["anchor_date"] == "2026-03-05"
    assert body["anchor_value_eur"] == pytest.approx(2700)
    assert body["anchor_unpriced_holdings"] == 0
    by_date = {p["date"]: p for p in body["data"]}
    assert by_date["2026-03-05"]["benchmark_value_eur"] == pytest.approx(2700)
    assert by_date["2026-03-10"]["external_flow_eur"] == 0
    assert by_date["2026-03-11"]["external_flow_eur"] == pytest.approx(300)


def test_the_default_is_still_the_inception_series(client):
    r = client.get(
        "/api/portfolio/benchmark?start_date=2026-03-05&end_date=2026-03-20&benchmark=dax"
    )
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert body["anchor"] == "inception"
    assert body["anchor_date"] is None
    assert body["anchor_value_eur"] is None
    assert body["data"][0]["benchmark_value_eur"] == pytest.approx(1500)
    # Not reported, never zero: the cached rows carry no flow.
    assert body["data"][0]["external_flow_eur"] is None


def test_an_unknown_anchor_is_a_400(client):
    r = client.get(
        "/api/portfolio/benchmark?start_date=2026-03-05&end_date=2026-03-20"
        "&benchmark=dax&anchor=rebased"
    )
    assert r.status_code == 400
    assert "anchor" in r.json()["detail"]
