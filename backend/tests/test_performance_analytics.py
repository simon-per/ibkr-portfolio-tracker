"""
The return decomposition, the segment attribution and the closed-position analytics,
against a fixture carrying the shapes that break them:

- a USD holding whose price did not move while the dollar did (pure FX effect),
- a lot sold mid-window, with IBKR's own realized figure on a SELL trade,
- a holding with no price at all (excluded from both sides and counted),
- a fund with a stored basket that leaves 10% unplaced (cash row),
- a pillar 3a lot that blends like everything else,
- a deposit before the first purchase, and a deposit inside the window,
- a measured IBKR cash balance that differs from the derived one (the fees leg),
- a base currency that is not EUR, so EUR holdings carry an FX effect too.

The identity the response promises — the legs sum exactly to the change in total
value — is asserted on the rounded figures the wire carries, and the wire shape is
pinned against the Pydantic models in both directions (a response_model is a filter).
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.accounts import IBKR, PILLAR3A
from app.database import Base
import app.models  # noqa: F401
from app.models.app_settings import AppSetting
from app.models.cash_balance import CashBalance
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW
from app.models.dividend_payment import DividendPayment
from app.models.etf_basket import EtfBasket, EtfHolding
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.schemas import performance as schemas
from app.services.performance_analytics_service import (
    FUND_RESIDUAL,
    PerformanceAnalyticsService,
)

START = date(2026, 3, 2)     # a Monday
MID = date(2026, 4, 15)
END = date(2026, 6, 1)
FUND_ISIN = "IE00B4L5Y983"   # IWDA — a known ETF ISIN, so it is decomposed


def _weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


async def _session(base_currency: str, eur_chf_end: str = "0.94"):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    s = AsyncSession(engine, expire_on_commit=False)
    s.add(AppSetting(key="base_currency", value=base_currency))

    def sec(id, sym, ccy, isin, sector=None, country=None, account=IBKR):
        return Security(id=id, isin=isin, symbol=sym, description=sym, currency=ccy,
                        conid=100 + id, asset_category="STK", exchange="X",
                        sector=sector, country=country, account=account)

    s.add_all([
        sec(1, "HELD", "EUR", "NL0000000001", "Technology", "Netherlands"),
        sec(2, "USD", "USD", "US0000000002", "Healthcare", "United States"),
        sec(3, "SOLD", "EUR", "DE0000000003", "Industrials", "Germany"),
        sec(4, "UNPRICED", "EUR", "FR0000000004", "Energy", "France"),
        sec(5, "FUND", "EUR", FUND_ISIN),
        sec(6, "P3A", "EUR", "CH0000000006", account=PILLAR3A),
    ])
    await s.flush()

    def lot(sid, opened, qty, cost, ccy="EUR", cost_eur=None, closed=None):
        return TaxLot(security_id=sid, open_date=opened, close_date=closed,
                      is_open=closed is None, quantity=Decimal(qty),
                      cost_basis=Decimal(cost),
                      cost_basis_eur=Decimal(cost_eur if cost_eur is not None else cost),
                      price_per_unit=Decimal(cost) / Decimal(qty), currency=ccy)

    before = START - timedelta(days=20)
    s.add_all([
        lot(1, before, "10", "1000"),
        lot(2, before, "10", "1000", ccy="USD", cost_eur="900"),
        lot(3, before, "10", "100", closed=MID),
        lot(4, before, "5", "50"),
        lot(5, before, "10", "500"),
        lot(6, before, "5", "100"),
    ])
    # IBKR's own realized figure for the sale: 12, not the lot approximation's 10.
    s.add(Trade(ib_key="T1", conid="103", security_id=3, symbol="SOLD", trade_date=MID,
                buy_sell="SELL", quantity=Decimal("-10"), price=Decimal("11"),
                proceeds=Decimal("110"), commission=Decimal("-1"), currency="EUR",
                realized_pnl=Decimal("12")))

    for d in _weekdays(before - timedelta(days=5), END):
        second_half = d >= MID
        px = {
            1: "120" if d >= END else "100",       # +20% on the last day of the window
            2: "100",                              # flat in USD
            3: "13" if d > MID else ("11" if d == MID else "10"),
            5: "60" if d >= END else "50",
            6: "22" if d >= END else "20",
        }
        for sid, p in px.items():
            s.add(MarketPrice(security_id=sid, date=d, close_price=Decimal(p),
                              currency="USD" if sid == 2 else "EUR", source="test"))
        s.add(ExchangeRate(date=d, from_currency="USD", to_currency="EUR",
                           rate=Decimal("0.8" if d >= END else "0.9"), source="test"))
        s.add(ExchangeRate(date=d, from_currency="EUR", to_currency="CHF",
                           rate=Decimal(eur_chf_end if d >= END else "0.94"), source="test"))

    # Deposits: one before the first purchase, one inside the window.
    s.add_all([
        CashFlow(ib_key="C0", flow_date=before - timedelta(days=20), flow_type=DEPOSIT_WITHDRAW,
                 amount=Decimal("1000"), amount_eur=Decimal("1000"), currency="EUR"),
        CashFlow(ib_key="C1", flow_date=MID, flow_type=DEPOSIT_WITHDRAW,
                 amount=Decimal("500"), amount_eur=Decimal("500"), currency="EUR"),
    ])
    # Dividend cash that landed (IBKR row), inside the window.
    s.add(DividendPayment(security_id=1, ex_date=MID, pay_date=MID, currency="EUR",
                          shares_held=Decimal("10"), gross_amount_eur=Decimal("12"),
                          withholding_tax_eur=Decimal("2"), net_amount_eur=Decimal("10"),
                          source="ibkr"))
    # IBKR's own balance five days after MID: derived is 1000 + 500 + 109 + 10 = 1619,
    # so this row is a −3 correction — broker fees the ledgers cannot see.
    s.add(CashBalance(report_date=MID + timedelta(days=5), account=IBKR, currency="EUR",
                      cash=Decimal("1616")))

    # The fund's basket: 60% tech/US, 30% financials/Japan, 10% cash (not invested).
    s.add(EtfBasket(fund_isin=FUND_ISIN, as_of_date=END - timedelta(days=3),
                    as_of_is_issuer_stated=True, source="test", adapter="blackrock",
                    source_rows=3, stored_rows=3, skipped_rows=0,
                    total_weight_pct=Decimal("100"), equity_weight_pct=Decimal("90"),
                    identifier_coverage_pct=Decimal("100"), asset_class_available=True))
    s.add_all([
        EtfHolding(fund_isin=FUND_ISIN, line_no=1, constituent_isin="US0000000010",
                   constituent_name="TechCo", weight_pct=Decimal("60"), asset_class="Equity",
                   sector="Information Technology", country="United States"),
        EtfHolding(fund_isin=FUND_ISIN, line_no=2, constituent_isin="JP0000000011",
                   constituent_name="BankCo", weight_pct=Decimal("30"), asset_class="Equity",
                   sector="Financials", country="Japan"),
        EtfHolding(fund_isin=FUND_ISIN, line_no=3, constituent_isin=None,
                   constituent_name="USD CASH", weight_pct=Decimal("10"), asset_class="Cash",
                   sector=None, country=None),
    ])
    await s.flush()
    return engine, s


def _legs(w):
    return [w[k] for k in ("net_flows_eur", "price_effect_eur", "fx_effect_eur",
                           "unsplit_eur", "dividends_eur", "cash_adjustment_eur",
                           "unexplained_eur")]


@pytest.mark.asyncio
async def test_the_legs_sum_to_the_change_in_total_value_and_each_reads_right():
    engine, s = await _session("EUR")
    try:
        out = await PerformanceAnalyticsService(s).decomposition(START, END)
        w = out["window"]

        # Start: HELD 1000 + USD 900 + SOLD 100 + FUND 500 + P3A 100 = 2600 held,
        # 1000 cash. End: 1200 + 800 + 600 + 110 = 2710 held, 1616 cash.
        assert w["start_total_value_eur"] == pytest.approx(3600.0)
        assert w["end_total_value_eur"] == pytest.approx(4326.0)
        assert w["net_flows_eur"] == pytest.approx(500.0)      # the in-window deposit only
        assert w["gain_eur"] == pytest.approx(226.0)

        # Price: HELD +200, SOLD +10, FUND +100, P3A +10; USD flat in its own currency.
        assert w["price_effect_eur"] == pytest.approx(320.0)
        # FX: the dollar fell 0.9 → 0.8 on a 1,000 USD position, nothing else moved.
        assert w["fx_effect_eur"] == pytest.approx(-100.0)
        assert w["unsplit_eur"] == 0.0 and w["unsplit_securities"] == 0
        assert w["dividends_eur"] == pytest.approx(10.0)
        assert w["cash_adjustment_eur"] == pytest.approx(-3.0)
        # The sale's 1.00 commission: the lot carries proceeds at market (110) while the
        # cash received 109. Reported, not folded into a leg.
        assert w["unexplained_eur"] == pytest.approx(-1.0)

        # The identity, on the rounded wire figures.
        assert round(w["end_total_value_eur"] - w["start_total_value_eur"], 2) == round(sum(_legs(w)), 2)

        # UNPRICED is left out of every leg and counted, and the warning is on the surface.
        assert w["unpriced_holdings"] == 1
        assert any("could not be priced" in m for m in w["warnings"])

        # Modified Dietz: 226 / (3600 + 250).
        assert w["gain_pct"] == pytest.approx(226 / 3850 * 100, abs=0.01)

        # Years: the identity holds on each, and the current one is flagged partial.
        assert out["years"], "the account has a first lot, so it has calendar years"
        for y in out["years"]:
            if y["start_total_value_eur"] is None:
                continue
            assert round(y["end_total_value_eur"] - y["start_total_value_eur"], 2) == round(sum(_legs(y)), 2)
        assert out["years"][-1]["year"] == END.year
        assert out["years"][-1]["partial"] is True
        assert out["cash_source"] == "ibkr"
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_non_eur_base_gives_eur_holdings_an_fx_leg_too():
    # EUR→CHF 0.94 at the start, 0.90 at the end.
    engine, s = await _session("CHF", eur_chf_end="0.90")
    try:
        from app.services.portfolio_service import PortfolioService
        rows = (await PortfolioService(s).attribution_rows(START, END))["rows"]
        held = rows[1]
        # 1200 × 0.90 − 1000 × 0.94 = 140 CHF in total; 200 EUR × 0.90 = 180 of it is
        # price, so the franc's rise cost 40.
        assert float(held["pnl_base"]) == pytest.approx(140.0)
        assert float(held["price_effect_base"]) == pytest.approx(180.0)
        assert float(held["fx_effect_base"]) == pytest.approx(-40.0)

        out = await PerformanceAnalyticsService(s).decomposition(START, END)
        w = out["window"]
        assert out["base_currency"] == "CHF"
        assert round(w["end_total_value_eur"] - w["start_total_value_eur"], 2) == round(sum(_legs(w)), 2)
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_segments_fold_the_fund_by_its_basket_and_leave_the_cash_row_visible():
    engine, s = await _session("EUR")
    try:
        out = await PerformanceAnalyticsService(s).segments(START, END)
        sectors = {r["name"]: r for r in out["by_sector"]}
        countries = {r["name"]: r for r in out["by_country"]}

        # Technology: HELD's 200 plus 60% of the fund's 100. The issuer's
        # "Information Technology" normalises onto the same canonical sector.
        assert sectors["Technology"]["pnl_eur"] == pytest.approx(260.0)
        assert sectors["Financials"]["pnl_eur"] == pytest.approx(30.0)
        assert sectors[FUND_RESIDUAL]["pnl_eur"] == pytest.approx(10.0)
        assert sectors["Healthcare"]["pnl_eur"] == pytest.approx(-100.0)
        # The 3a fund has no sector on record: Unknown, not dropped and not "Other".
        assert sectors["Unknown"]["pnl_eur"] == pytest.approx(10.0)
        # Every segment's gain adds back to the total; nothing renormalised away.
        assert sum(r["pnl_eur"] for r in out["by_sector"]) == pytest.approx(out["total_pnl_eur"])
        assert out["total_pnl_eur"] == pytest.approx(220.0)

        assert countries["Netherlands"]["pnl_eur"] == pytest.approx(200.0)
        assert countries["United States"]["pnl_eur"] == pytest.approx(-40.0)
        assert countries["Japan"]["pnl_eur"] == pytest.approx(30.0)
        assert countries["Japan"]["via_funds_pct"] == pytest.approx(100.0)
        assert countries["Netherlands"]["via_funds_pct"] == pytest.approx(0.0)

        # Weights are shares of the priced book; USD held 900 of 2600 at the start.
        assert countries["United States"]["start_weight_pct"] == pytest.approx((900 + 300) / 2600 * 100, abs=0.01)
        assert out["unpriced_holdings"] == 1
        assert any("current basket" in m for m in out["warnings"])
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_closed_positions_prefer_ibkr_realized_and_measure_the_post_sale_move():
    engine, s = await _session("EUR")
    try:
        out = await PerformanceAnalyticsService(s).closed_positions()
        assert [p["symbol"] for p in out["positions"]] == ["SOLD"]
        p = out["positions"][0]
        assert p["realized_pnl_eur"] == pytest.approx(12.0)    # IBKR's figure, not 110 − 100
        assert p["realized_source"] == "trade"
        assert p["cost_basis_eur"] == pytest.approx(100.0)
        assert p["proceeds_eur"] == pytest.approx(110.0)
        assert p["return_pct"] == pytest.approx(12.0)
        assert p["still_held"] is False
        assert p["holding_days"] == (MID - (START - timedelta(days=20))).days
        # Sold at 11, quoted 13 afterwards: it kept rising.
        assert p["post_sale_pct"] == pytest.approx((13 - 11) / 11 * 100, abs=0.01)
        assert p["post_sale_days"] > 0

        summary = out["summary"]
        assert summary["closed_securities"] == 1
        assert summary["winners"] == 1 and summary["losers"] == 0
        assert summary["hit_rate_pct"] == 100.0
        assert summary["sold_then_rose"] == 1
        assert summary["best"] == "SOLD" and summary["worst"] is None
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_empty_database_answers_with_absences_not_zeros():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    s = AsyncSession(engine, expire_on_commit=False)
    try:
        svc = PerformanceAnalyticsService(s)
        d = await svc.decomposition(START, END)
        assert d["window"]["gain_eur"] is None
        assert d["window"]["price_effect_eur"] is None
        assert d["years"] == []
        seg = await svc.segments(START, END)
        assert seg["total_pnl_eur"] is None and seg["by_sector"] == []
        closed = await svc.closed_positions()
        assert closed["summary"]["hit_rate_pct"] is None
        assert closed["summary"]["total_realized_eur"] is None
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_wire_shape_matches_the_service_in_both_directions():
    """A response_model is a filter — a key the service emits and the model lacks is dropped silently."""
    engine, s = await _session("EUR")
    try:
        svc = PerformanceAnalyticsService(s)
        d = await svc.decomposition(START, END)
        assert set(d) == set(schemas.ReturnDecompositionResponse.model_fields)
        assert set(d["window"]) == set(schemas.DecompositionWindow.model_fields)
        assert set(d["years"][0]) == set(schemas.DecompositionYear.model_fields)
        seg = await svc.segments(START, END)
        assert set(seg) == set(schemas.SegmentAttributionResponse.model_fields)
        assert set(seg["by_sector"][0]) == set(schemas.SegmentRow.model_fields)
        closed = await svc.closed_positions()
        assert set(closed) == set(schemas.ClosedPositionsResponse.model_fields)
        assert set(closed["positions"][0]) == set(schemas.ClosedPosition.model_fields)
        assert set(closed["summary"]) == set(schemas.ClosedPositionsSummary.model_fields)
    finally:
        await s.close()
        await engine.dispose()
