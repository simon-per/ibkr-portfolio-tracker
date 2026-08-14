"""
Every read endpoint, through the real HTTP stack, against a realistic fixture.

Every other test calls services directly, so nothing exercised FastAPI's
serialization layer — and on 2026-07-29 a `Decimal + None` shipped to production
and 500'd `/api/dividends/breakdown` with a full green suite behind it. The
fixture therefore carries the shapes that actually break things: a dual-listed
ticker (identity is isin + exchange), a closed lot, a dividend row predating the
withholding-fields migration (gross set, net NULL), a pre-ownership zero row, a
non-EUR security, and CHF as the base currency, since production runs CHF while
the tests otherwise run EUR.

`yfinance` is replaced by a raiser for the whole module: these endpoints must
serve from cache, and an accidental network reach has to fail loudly rather than
quietly spend the rate-limit budget (CLAUDE.md rule 1).

POST routes are deliberately not exercised — they start real syncs.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
import app.models  # noqa: F401
from app.models.app_settings import AppSetting
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW
from app.models.dividend_payment import DividendPayment
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade

TODAY = date.today()
START = TODAY - timedelta(days=200)
SOLD_ON = TODAY - timedelta(days=40)

# GETs only. /api/portfolio/benchmark is excluded on purpose: on a cache miss it
# lazy-fetches Yahoo, which is exactly what must never happen in a test.
READ_ENDPOINTS = [
    "/health",
    "/api/settings",
    "/api/sync/status",
    "/api/scheduler/status",
    "/api/scheduler/history?limit=5",
    "/api/portfolio/summary",
    f"/api/portfolio/value-over-time?start_date={START}&end_date={TODAY}",
    "/api/portfolio/positions",
    "/api/portfolio/contributions",
    "/api/portfolio/activity",
    f"/api/portfolio/activity?start_date={START}&end_date={TODAY}",
    "/api/portfolio/activity?kind=trade,cash_flow",
    "/api/portfolio/activity?symbol=ASML&limit=5",
    "/api/portfolio/activity.csv",
    f"/api/portfolio/annualized-return?start_date={START}&end_date={TODAY}",
    f"/api/portfolio/attribution?start_date={START}&end_date={TODAY}",
    "/api/portfolio/benchmarks",
    "/api/market-data/status",
    "/api/analyst-ratings/status",
    "/api/allocation/status",
    "/api/fundamentals/status",
    "/api/fundamentals/portfolio",
    "/api/watchlist",
    "/api/dividends/summary",
    "/api/dividends/breakdown",
    f"/api/dividends/breakdown?year={TODAY.year}",
    f"/api/dividends/breakdown?year={TODAY.year + 1}",   # a future year
    "/api/dividends/breakdown?forecast=false",
    f"/api/tax/report?year={TODAY.year}",
    f"/api/tax/report.csv?year={TODAY.year}",
    "/api/portfolio/lookthrough",
    "/api/portfolio/lookthrough?limit=5",
]


def _weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


async def _seed(session: AsyncSession) -> None:
    session.add(AppSetting(key="base_currency", value="CHF"))

    securities = [
        # Dual listing: same ISIN, two venues, two securities.
        Security(id=1, isin="NL0010273215", symbol="ASML", description="ASML Holding",
                 currency="EUR", conid=101, asset_category="STK", exchange="NASDAQ"),
        Security(id=2, isin="NL0010273215", symbol="ASML", description="ASML Holding",
                 currency="EUR", conid=102, asset_category="STK", exchange="AEB"),
        # Non-EUR, so price FX conversion is exercised.
        Security(id=3, isin="US0378331005", symbol="AAPL", description="Apple Inc",
                 currency="USD", conid=103, asset_category="STK", exchange="NASDAQ"),
        # Bought weeks ago: per-share history but nothing received yet.
        Security(id=4, isin="TW0002330008", symbol="TSMC", description="TSMC",
                 currency="EUR", conid=104, asset_category="STK", exchange="TWSE"),
    ]
    for s in securities:
        session.add(s)

    lots = [
        (1, START, "10", "1000", None),
        (2, START + timedelta(days=10), "4", "400", None),
        (3, START + timedelta(days=5), "20", "2000", SOLD_ON),   # closed mid-window
        (4, TODAY - timedelta(days=30), "50", "500", None),      # bought weeks ago
    ]
    for sid, opened, qty, cost, closed in lots:
        session.add(TaxLot(
            security_id=sid, open_date=opened, close_date=closed,
            is_open=closed is None, quantity=Decimal(qty),
            cost_basis=Decimal(cost), cost_basis_eur=Decimal(cost),
            price_per_unit=Decimal(cost) / Decimal(qty), currency="EUR",
        ))

    session.add(Trade(
        ib_key="T1", conid="103", security_id=3, symbol="AAPL", trade_date=SOLD_ON,
        buy_sell="SELL", quantity=Decimal("-20"), price=Decimal("110"),
        proceeds=Decimal("2200"), commission=Decimal("-1"), currency="USD",
        realized_pnl=Decimal("200"),
    ))

    for d in _weekdays(START - timedelta(days=20), TODAY):
        for sid, px, cur in ((1, "105", "EUR"), (2, "104", "EUR"), (3, "110", "USD")):
            session.add(MarketPrice(security_id=sid, date=d, close_price=Decimal(px),
                                    currency=cur, source="test"))
        session.add(ExchangeRate(date=d, from_currency="USD", to_currency="EUR",
                                 rate=Decimal("0.9"), source="test"))
        session.add(ExchangeRate(date=d, from_currency="EUR", to_currency="CHF",
                                 rate=Decimal("0.94"), source="test"))

    dividends = [
        # Real IBKR income.
        dict(security_id=1, ex_date=TODAY - timedelta(days=60),
             pay_date=TODAY - timedelta(days=60), shares_held=Decimal("0"),
             gross_amount_eur=Decimal("12"), withholding_tax_eur=Decimal("2"),
             net_amount_eur=Decimal("10"), source="ibkr"),
        # A payer bought recently: quarterly per-share history, nothing received
        # yet. This is the shape that had TSMC, Samsung and the SOXQ ETF
        # projecting nothing at all.
        *[
            dict(security_id=4, ex_date=TODAY - timedelta(days=d),
                 amount_per_share=Decimal("0.25"), shares_held=Decimal("0"),
                 gross_amount_eur=Decimal("0"), withholding_tax_eur=Decimal("0"),
                 net_amount_eur=Decimal("0"), source="yfinance_estimate")
            for d in (275, 184, 92)
        ],
        # Legacy shape: gross set, net NULL — the row that 500'd production.
        dict(security_id=1, ex_date=TODAY - timedelta(days=150),
             pay_date=TODAY - timedelta(days=150), shares_held=Decimal("10"),
             gross_amount_eur=Decimal("8"), withholding_tax_eur=None,
             net_amount_eur=None, source="yfinance_estimate"),
        # Pre-ownership zero row from yfinance's full history.
        dict(security_id=1, ex_date=date(1999, 4, 4), pay_date=date(1999, 4, 4),
             shares_held=Decimal("0"), gross_amount_eur=Decimal("0"),
             withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("0"),
             source="yfinance_estimate"),
        # Second listing, so the dual-listing path has income on both.
        dict(security_id=2, ex_date=TODAY - timedelta(days=60),
             pay_date=TODAY - timedelta(days=60), shares_held=Decimal("0"),
             gross_amount_eur=Decimal("3"), withholding_tax_eur=Decimal("0"),
             net_amount_eur=Decimal("3"), source="ibkr"),
    ]
    for d in dividends:
        session.add(DividendPayment(currency="EUR", **d))

    session.add(CashFlow(
        ib_key="C1", flow_date=START + timedelta(days=1), flow_type=DEPOSIT_WITHDRAW,
        amount=Decimal("3000"), amount_eur=Decimal("3000"), currency="EUR",
        description="deposit",
    ))
    await session.flush()
    await session.commit()


@pytest.fixture()
def client(monkeypatch):
    # Any accidental Yahoo reach must fail loudly, not spend rate-limit budget.
    def _forbidden(*_a, **_kw):
        raise AssertionError("a test tried to call Yahoo Finance")

    for mod in ("app.services.market_data_service", "app.services.dividend_service",
                "app.services.benchmark_service", "app.services.watchlist_service",
                "app.services.allocation_service", "app.services.analyst_rating_service",
                "app.services.fundamentals_service"):
        try:
            m = __import__(mod, fromlist=["yf"])
        except ImportError:
            continue
        if hasattr(m, "yf"):
            monkeypatch.setattr(m.yf, "Ticker", _forbidden, raising=False)

    # /api/dividends/summary enqueues a background sync when data looks stale.
    import app.routers.dividends as div_router

    async def _no_sync():
        return None

    monkeypatch.setattr(div_router, "_run_dividend_sync_background", _no_sync)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    session = AsyncSession(engine, expire_on_commit=False)

    import asyncio
    loop = asyncio.new_event_loop()

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed(session)

    loop.run_until_complete(_setup())

    from app.main import app

    async def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    # No context manager: that would run the lifespan and start the scheduler.
    yield TestClient(app)
    app.dependency_overrides.clear()
    loop.run_until_complete(session.close())
    loop.run_until_complete(engine.dispose())
    loop.close()


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoint_responds(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:400]}"


def test_the_shapes_that_broke_production_serialize(client):
    """The regressions this file exists to catch, asserted on the real payloads."""
    bd = client.get(f"/api/dividends/breakdown?year={TODAY.year}").json()

    # The legacy NULL-net row is income, via the gross fallback (this was the 500).
    assert bd["total_net_eur"] > 0
    # The dual listing stays two rows, distinguishable by venue...
    asml = [r for r in bd["securities"] if r["symbol"] == "ASML"]
    assert len(asml) == 2 and {r["exchange"] for r in asml} == {"NASDAQ", "AEB"}
    # ...while the chart merges them into one stack key.
    month = (TODAY - timedelta(days=60)).strftime("%Y-%m")
    bar = next(m for m in bd["months"] if m["month"] == month)
    assert bar["actual"]["ASML"] == pytest.approx(13 * 0.94, rel=1e-3)
    # The 1999 pre-ownership zero row never surfaces.
    assert all(m["month"] >= "2000-01" for m in bd["months"])
    assert bd["base_currency"] == "CHF"

    summary = client.get("/api/dividends/summary").json()
    assert all(abs(m["amount_eur"]) > 0 for m in summary["monthly"])
    # The card's footnote reads off these. /summary now has a response_model, which
    # cuts both ways: it documents the contract, but it also *filters* — so a key the
    # service returns and the model omits vanishes silently. test_dividend_summary_
    # contract.py pins the two sets equal; these assertions pin the values.
    assert summary["source"] in {"ibkr", "mixed", "yfinance_estimate"}
    assert summary["total_eur"] == summary["total_net_eur"]  # back-compat key is NET
    assert summary["total_gross_eur"] >= summary["total_net_eur"]
    assert "total_withholding_eur" in summary and "ibkr_from" in summary

    # A payer bought recently, with per-share history but nothing received yet,
    # must still be forecast — and must say the amount is a gross estimate.
    tsmc = next(r for r in bd["securities"] if r["symbol"] == "TSMC")
    assert tsmc["payouts"] == 0 and tsmc["forecast_payouts"] > 0
    assert tsmc["forecast_basis"] == "gross_estimate"

    # Next year is offered and projects a full twelve months.
    nxt = client.get(f"/api/dividends/breakdown?year={TODAY.year + 1}").json()
    assert TODAY.year + 1 in nxt["years"]
    assert len(nxt["months"]) == 12
    assert nxt["total_forecast_net_eur"] > 0

    # The growth block survives response_model serialization — a Pydantic model
    # silently drops keys it does not declare, so a field added to the service
    # and not to the schema would vanish here rather than raise anywhere.
    growth = bd["growth"]
    for key in ("ttm", "ytd", "avg_month"):
        assert set(growth[key]) == {"net_eur", "prev_net_eur", "pct"}
    assert isinstance(growth["ttm_crosses_era"], bool)
    assert isinstance(growth["annual"], list)
    assert "next_12m_eur" in growth
    # Percentages are nullable by design (a zero base is undefined, not large),
    # so the schema must not coerce them to 0.0.
    assert all(
        row["yoy_pct"] is None or isinstance(row["yoy_pct"], float)
        for row in growth["annual"]
    )
    # Growth is unwindowed: the same numbers whichever year is selected, which is
    # the only reason a year-filtered response can show year-over-year at all.
    unwindowed = client.get("/api/dividends/breakdown").json()
    assert unwindowed["growth"] == growth

    # Completeness travels with the headline. `total_market_value_eur` is a sum over the
    # holdings the backend could price, so a partial one has to declare itself — the SBI
    # shape, where 446.93 CHF left this figure with only a sync warning to catch it. The
    # timeline's per-point field and this one come from the same helper, so they must agree
    # on the fixture: nothing here is unpriced.
    summary_now = client.get("/api/portfolio/summary").json()
    assert "unpriced_holdings" in summary_now, "response_model dropped it"
    assert isinstance(summary_now["unpriced_holdings"], int)
    vot = client.get(
        f"/api/portfolio/value-over-time?start_date={START}&end_date={TODAY}"
    ).json()
    assert all("unpriced_holdings" in p for p in vot)
    assert summary_now["unpriced_holdings"] == vot[-1]["unpriced_holdings"]

    # The forward yield is a SIBLING of growth, not a member — it moves with a market
    # price, so it is neither growth nor history. Same year-invariance, for its own
    # reason: a rate describing the next twelve months cannot depend on the window
    # being viewed.
    fy = bd["forward_yield"]
    assert unwindowed["forward_yield"] == fy
    if fy is not None:
        # Absent as a whole object or complete — never a zeroed one. `paying_holdings: 0`
        # beside a null percentage would read as "nothing here pays a dividend".
        assert isinstance(fy["pct"], float)
        assert fy["on_cost_pct"] is None or isinstance(fy["on_cost_pct"], float)
        assert fy["paying_holdings"] <= fy["priced_holdings"]
        assert fy["basis"] in ("net", "mixed", "gross_estimate")
        assert 0 <= fy["gross_estimate_eur"] <= fy["annual_eur"]
        # An appreciated book yields more on cost than on value, and the two must be
        # computed over the same securities for that gap to mean appreciation at all.
        if fy["on_cost_pct"] is not None:
            assert fy["on_cost_pct"] > 0
    # With no projection there is nothing to divide, and the object must be absent
    # rather than a 0.00% that asserts this portfolio pays nothing.
    assert client.get(
        "/api/dividends/breakdown?forecast=false"
    ).json()["forward_yield"] is None

    # The calendar is dated and ordered, and its rows name a real security.
    upcoming = bd["upcoming"]
    assert [u["date"] for u in upcoming] == sorted(u["date"] for u in upcoming)
    known = {r["security_id"] for r in bd["securities"]}
    assert all(u["security_id"] in known for u in upcoming)

    # A sale mid-window must not read as a total loss.
    attr = client.get(
        f"/api/portfolio/attribution?start_date={START}&end_date={TODAY}"
    ).json()
    aapl = next(r for r in attr["attributions"] if r["symbol"] == "AAPL")
    assert aapl["disposal_proceeds_eur"] > 0
    assert aapl["pnl_contribution_eur"] > -100

    # Attribution's completeness signal must survive its response_model. The model is a
    # FILTER, so an undeclared key is dropped silently -- and this fixture has an
    # unpriced holding (TSMC), which is exactly the case the field exists to report. A
    # zero here would mean the whole book was attributed when it was not.
    assert "unpriced_holdings" in attr, (
        "PerformanceAttributionResponse dropped unpriced_holdings from the wire"
    )
    assert isinstance(attr["unpriced_holdings"], int)
    # TSMC is the fixture's unpriced holding, so this must be the non-zero case rather
    # than an assertion that would pass on any book. Asserting only `>= 0` here is the
    # same vacuous shape as the Sharpe clamp test that asserted `abs(0) <= 10`.
    assert attr["unpriced_holdings"] >= 1
    # Excluded and reported are alternatives, never both: a security left out for being
    # unpriced must not also appear as a contributor with a fabricated -start_value.
    assert "TSMC" not in {r["symbol"] for r in attr["attributions"]}

    # The tax report's honesty fields survive the HTTP layer. holdings_snapshot_total
    # is nullable now — a failed snapshot is a missing wealth-tax base, not a zero
    # one — so the route must not coerce None to 0.0 or drop the flag.
    # The two qualifying fields on a security row must survive DividendSecurityRow —
    # a Pydantic model drops what it does not declare, and both exist precisely to
    # stop a figure being read as more solid than it is.
    for row in bd["securities"]:
        assert "trailing_yield_partial" in row and isinstance(
            row["trailing_yield_partial"], bool
        )
        assert "forecast_samples" in row and "forecast_cadence_days" in row
        # The forward yield's per-security audit. Nullable by design — a row with no
        # projection or no price has no rate, which is not a rate of zero.
        assert row["forward_yield_pct"] is None or isinstance(
            row["forward_yield_pct"], float
        )
        if row["forecast_payouts"] > 0:
            assert row["forecast_samples"] and row["forecast_samples"] >= 2

    tax = client.get(f"/api/tax/report?year={TODAY.year}").json()
    assert tax["holdings_snapshot_error"] is False
    assert tax["holdings_snapshot_total"] is not None
    # This asserted `warnings == []` until 2026-08-05, which was pinning the bug: the
    # fixture holds an unpriced TSMC, so the snapshot really does omit it and the
    # wealth-tax base really does understate. A silent partial snapshot was the whole
    # defect, so the clean-report expectation belongs on a fixture that IS clean —
    # `test_tax_snapshot_partial.py` covers that direction.
    assert any("TSMC" in w and "understates" in w for w in tax["warnings"]), (
        "the Steuerwert omits an unpriceable holding without saying so"
    )
    assert tax["realized_source"] in {"trades", "closed_lot_estimate"}

    # The activity ledger unions four tables that each had no read surface at all.
    act = client.get(f"/api/portfolio/activity?start_date={START}&end_date={TODAY}").json()
    assert act["base_currency"] == "CHF"
    kinds = {row["kind"] for row in act["items"]}
    assert {"trade", "dividend", "cash_flow"} <= kinds
    # Newest first, and the deposit is flagged as money in while nothing else is.
    dates = [row["date"] for row in act["items"]]
    assert dates == sorted(dates, reverse=True)
    deposit = next(r for r in act["items"] if r["ib_key"] == "C1")
    assert deposit["counts_as_money_in"] is True
    assert all(r["counts_as_money_in"] is None for r in act["items"] if r["kind"] != "cash_flow")
    # The fixture's SELL carries IBKR's own realized P&L through to the row.
    sell = next(r for r in act["items"] if r["ib_key"] == "T1")
    assert sell["realized_pnl_base"] is not None

    # The ledger is era-spliced like every reader that computes a figure. Without this
    # it listed the same dividend twice — the yfinance estimate under its ex-date and the
    # IBKR actual under its pay date — and overstated dividend income by 72%. Asserted
    # over the HTTP layer because the splice is the sort of thing a response_model or a
    # windowing change can quietly undo.
    boundary = bd["ibkr_from"]
    if boundary:
        stale = [
            r for r in act["items"]
            if r["kind"] == "dividend"
            and r["source"] == "yfinance_estimate"
            and r["date"] >= boundary
        ]
        assert not stale, f"estimates superseded by IBKR rows are still on the ledger: {stale}"
        # ...and the mirror-image bug: pre-boundary estimates are the only source for
        # that era and must survive, still badged.
        early = [
            r for r in act["items"]
            if r["kind"] == "dividend" and r["source"] == "yfinance_estimate"
        ]
        assert all(r["date"] < boundary for r in early)

    # An unknown kind is refused rather than silently returning everything.
    assert client.get("/api/portfolio/activity?kind=nonsense").status_code == 400
    # And the window is bounded like value-over-time's.
    assert client.get(
        f"/api/portfolio/activity?start_date=1990-01-01&end_date={TODAY}"
    ).status_code == 400

    csv_text = client.get("/api/portfolio/activity.csv").text
    assert csv_text.splitlines()[0].startswith("date,kind,subtype")
    assert "amount_chf" in csv_text.splitlines()[0]


def test_the_lookthrough_folds_a_dual_listing_through_the_real_stack(client):
    """
    The look-through endpoint over the real HTTP stack, `response_model` included.

    This fixture is deliberately all-direct — no fund carries a stored basket — which is the
    state a fresh deployment is in, and the one where the view must still be useful rather
    than empty. What it does exercise is the fold that needs no provider at all: ASML is two
    `securities` rows sharing ISIN NL0010273215, and folding them is the whole reason this
    feature ships before any identity is resolved.

    A fund plus a basket is deliberately NOT added to the shared `_seed`: thirty other
    endpoints read it, and the basket paths are covered against a dedicated fixture in
    `tests/test_lookthrough_partition.py`.
    """
    lt = client.get("/api/portfolio/lookthrough").json()

    # The two ASML listings are one company row carrying both venues.
    asml = next(r for r in lt["companies"] if "NL0010273215" in r["isins"])
    assert sorted(asml["listings"]) == ["ASML@AEB", "ASML@NASDAQ"]
    assert asml["direct_value_eur"] == asml["value_eur"], "nothing here comes via a fund"
    assert asml["via_funds"] == []
    # Folded on the ISIN floor, with no `isin_identities` row in this fixture at all — so
    # the row must also declare that a sibling ISIN could still be missing.
    assert asml["key_type"] == "isin"
    assert asml["partially_resolved"] is True

    # The partition closes over whatever the endpoint served.
    buckets = (
        "direct_equity_eur", "looked_through_equity_eur", "fund_residual_eur",
        "nested_fund_eur", "uncovered_fund_eur",
    )
    assert sum(Decimal(str(lt[b])) for b in buckets) == Decimal(
        str(lt["total_market_value_eur"])
    )

    # And it agrees with the headline, because both come from the same valuation helper.
    # Two figures under one meaning on two screens is this codebase's dominant failure mode.
    summary = client.get("/api/portfolio/summary").json()
    assert lt["total_market_value_eur"] == summary["total_market_value_eur"]
    assert lt["base_currency"] == summary["base_currency"] == "CHF"

    # Truncation reports the tail as a value rather than only a count.
    small = client.get("/api/portfolio/lookthrough?limit=1").json()
    assert small["companies_shown"] == 1
    assert small["company_count_total"] == lt["company_count_total"]
    if small["other_companies_count"]:
        assert small["other_companies_eur"] > 0
    # The clamp is enforced by FastAPI rather than trusted.
    assert client.get("/api/portfolio/lookthrough?limit=0").status_code == 422
    assert client.get("/api/portfolio/lookthrough?limit=9999").status_code == 422
