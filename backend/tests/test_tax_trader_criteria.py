"""
The ESTV Kreisschreiben 36 securities-dealer indicators on the tax report (GitHub issue #78):
turnover (the circular's own test), sales of positions held under six months, margin used.

Each is a figure or absent -- never a reassuring "no" the record cannot back. IBKR only:
a pillar 3a sale, lot or cash flow moves none of them. EUR base, synthetic book, offline.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.accounts import IBKR, PILLAR3A
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.services.currency_service import CurrencyService
from app.services.portfolio_service import PortfolioService
from app.services.tax_service import TaxService
from app.services.trader_criteria import (
    held_under_six_months,
    lowest_balance,
    turnover_multiple,
)

YEAR = 2025
OPENING = date(YEAR - 1, 12, 31)


def _lot(sid, opened, qty, price, closed=None, source=None):
    cost = Decimal(qty) * Decimal(price)
    return TaxLot(security_id=sid, open_date=opened, quantity=Decimal(qty),
                  cost_basis=cost, price_per_unit=Decimal(price), currency="EUR",
                  cost_basis_eur=cost, is_open=closed is None, close_date=closed,
                  close_source=source)


def _trade(key, sid, conid, on, kind, qty, proceeds, category="STK", account=IBKR,
           currency="EUR"):
    return Trade(ib_key=key, conid=conid, security_id=sid, symbol=f"S{conid}",
                 trade_date=on, buy_sell=kind, quantity=Decimal(qty), price=None,
                 proceeds=Decimal(proceeds), commission=Decimal("0"),
                 realized_pnl=Decimal("0"), currency=currency, asset_category=category,
                 account=account)


async def _book(db) -> None:
    """
    Two IBKR holdings worth 2000 at the prior 31 December, and in YEAR: a long-held sale
    of AAA, a short-held sale of BBB in two fills, a sale linked to no security, a sale
    IBKR cancelled, and a buy that takes cash to -500.
    """
    db.add_all([
        Security(id=1, isin="US1111111111", symbol="AAA", description="A", currency="EUR",
                 conid=1, exchange="NASDAQ", account=IBKR),
        Security(id=2, isin="US2222222222", symbol="BBB", description="B", currency="EUR",
                 conid=2, exchange="NASDAQ", account=IBKR),
    ])
    await db.flush()
    db.add_all([
        # Transferred in during YEAR-1: no trade, so no cash.
        _lot(1, date(YEAR - 1, 3, 1), "5", "80"),
        _lot(1, date(YEAR - 1, 3, 1), "5", "80", closed=date(YEAR, 3, 10), source="trade"),
        _lot(2, date(YEAR - 1, 6, 1), "20", "40"),
        # Bought in January, sold on 1 April: under six months.
        _lot(2, date(YEAR, 1, 15), "10", "350", closed=date(YEAR, 4, 1), source="trade"),
        MarketPrice(security_id=1, date=OPENING, close_price=Decimal("100"),
                    currency="EUR", source="test"),
        MarketPrice(security_id=2, date=OPENING, close_price=Decimal("50"),
                    currency="EUR", source="test"),
        CashFlow(ib_key="d1", flow_date=date(YEAR - 1, 1, 2), flow_type=DEPOSIT_WITHDRAW,
                 amount=Decimal("3000"), currency="EUR", amount_eur=Decimal("3000"),
                 account=IBKR),
        _trade("b1", 2, "2", date(YEAR, 1, 15), "BUY", "10", "-3500"),
        _trade("s1", 1, "1", date(YEAR, 3, 10), "SELL", "-5", "600"),
        _trade("s2a", 2, "2", date(YEAR, 4, 1), "SELL", "-5", "300"),
        _trade("s2b", 2, "2", date(YEAR, 4, 1), "SELL", "-5", "300"),
        _trade("s3", None, "999", date(YEAR, 5, 5), "SELL", "-1", "100"),
        _trade("s4", 1, "1", date(YEAR, 6, 1), "SELL", "-2", "250"),
        _trade("s4c", 1, "1", date(YEAR, 6, 1), "CANCELSELL", "2", "-250"),
    ])
    await db.commit()


async def _pillar3a(db) -> None:
    """A 3a fund sold within weeks of buying it, with its own cash going negative."""
    db.add(Security(id=9, isin="CH0117044948", symbol="CH0117044948", description="3a",
                    currency="EUR", conid=None, exchange="FUND", account=PILLAR3A,
                    price_source="manual"))
    await db.flush()
    db.add_all([
        _lot(9, date(YEAR, 2, 1), "4", "500", closed=date(YEAR, 2, 20), source="trade"),
        _trade("fp-b", 9, "CH0117044948", date(YEAR, 2, 1), "BUY", "4", "-2000",
               category="FUND", account=PILLAR3A),
        _trade("fp-s", 9, "CH0117044948", date(YEAR, 2, 20), "SELL", "-4", "9000",
               category="FUND", account=PILLAR3A),
    ])
    await db.commit()


# --- the pure parts --------------------------------------------------------------------

def test_six_months_is_calendar_months_clamped_to_the_month():
    assert held_under_six_months(date(2025, 1, 31), date(2025, 7, 30))
    assert not held_under_six_months(date(2025, 1, 31), date(2025, 7, 31))
    # 31 August + 6 months is the last day of February.
    assert held_under_six_months(date(2025, 8, 31), date(2026, 2, 27))
    assert not held_under_six_months(date(2025, 8, 31), date(2026, 2, 28))


def test_the_lowest_balance_counts_the_carried_balance_and_closes_days():
    start, end = date(YEAR, 1, 1), date(YEAR, 12, 31)
    assert lowest_balance([], start, end) is None
    # Nothing on record until after the window: the record does not reach the year.
    assert lowest_balance([(date(YEAR + 1, 1, 2), Decimal("5"))], start, end) is None
    # Only a carried balance: that is the year's level throughout.
    assert lowest_balance([(date(YEAR - 1, 5, 1), Decimal("-7"))], start, end) == Decimal("-7")
    # A same-day buy and sale net before the day closes: end of day, not intraday.
    events = [(date(YEAR, 2, 1), Decimal("100")), (date(YEAR, 3, 1), Decimal("-150")),
              (date(YEAR, 3, 1), Decimal("60")), (date(YEAR, 4, 1), Decimal("-20"))]
    assert lowest_balance(events, start, end) == Decimal("-10")


def test_the_turnover_is_volume_over_securities_and_signed_cash():
    d = Decimal
    assert turnover_multiple(d("900"), d("400"), d("100")) == 1.8
    # A margin debit at the start shrinks the base it is set against.
    assert turnover_multiple(d("100"), d("100"), d("-50")) == 2.0
    # Any part unknown, or a base of nothing or of a debt: no figure.
    assert turnover_multiple(None, d("100"), d("0")) is None
    assert turnover_multiple(d("100"), None, d("0")) is None
    assert turnover_multiple(d("100"), d("100"), None) is None
    assert turnover_multiple(d("100"), d("0"), d("0")) is None
    assert turnover_multiple(d("100"), d("50"), d("-80")) is None


# --- the report --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_indicators_for_a_year_with_sales(db):
    await _book(db)
    tc = (await TaxService(db).get_tax_report(YEAR))["trader_criteria"]

    # Purchases 3500 plus sales 600 + 300 + 300 + 100 (the cancelled pair netting to
    # nothing), over securities 1000 + 1000 and 3000 of cash at the prior 31 December.
    assert tc["opening_as_of"] == OPENING.isoformat()
    assert tc["opening_securities"] == 2000.0
    assert tc["opening_cash"] == 3000.0
    assert tc["purchases"] == 3500.0
    assert tc["sales_proceeds"] == 1300.0
    assert tc["turnover_multiple"] == 0.96
    assert tc["turnover_estimated"] is False
    # AAA, BBB's two fills as one sale, and the unlinked one; the busted sale is none.
    assert tc["sales"] == 3
    assert tc["short_held_sales"] == 1
    assert tc["sales_holding_unknown"] == 1
    # 3000 - 3500 on 15 January.
    assert tc["margin_used"] is True
    assert tc["min_cash_balance"] == -500.0
    assert "derivatives_traded" not in tc


@pytest.mark.asyncio
async def test_a_cancelled_purchase_is_no_purchase(db):
    await _book(db)
    db.add_all([
        _trade("b2", 2, "2", date(YEAR, 7, 1), "BUY", "4", "-200"),
        _trade("b2c", 2, "2", date(YEAR, 7, 1), "CANCELBUY", "-4", "200"),
    ])
    await db.commit()
    tc = (await TaxService(db).get_tax_report(YEAR))["trader_criteria"]
    assert tc["purchases"] == 3500.0 and tc["turnover_multiple"] == 0.96


@pytest.mark.asyncio
async def test_pillar3a_moves_none_of_them(db):
    await _book(db)
    before = (await TaxService(db).get_tax_report(YEAR))["trader_criteria"]
    await _pillar3a(db)
    after = (await TaxService(db).get_tax_report(YEAR))["trader_criteria"]
    assert after == before


@pytest.mark.asyncio
async def test_a_year_the_ibkr_record_does_not_reach_is_absent_not_no(db):
    """The holdings were elsewhere: no sale, margin or derivative is on record either way."""
    await _book(db)
    tc = (await TaxService(db).get_tax_report(YEAR - 2))["trader_criteria"]
    for key in ("opening_securities", "opening_cash", "purchases", "sales_proceeds",
                "turnover_multiple", "sales", "short_held_sales", "margin_used",
                "min_cash_balance"):
        assert tc[key] is None, key


@pytest.mark.asyncio
async def test_a_year_without_sales_and_without_an_opening_base(db):
    await _book(db)
    tc = (await TaxService(db).get_tax_report(YEAR - 1))["trader_criteria"]
    assert (tc["sales"], tc["short_held_sales"], tc["sales_holding_unknown"]) == (0, 0, 0)
    assert (tc["purchases"], tc["sales_proceeds"]) == (0.0, 0.0)
    # Nothing held and no cash on the 31 December before: a multiple of nothing is absent.
    assert (tc["opening_securities"], tc["opening_cash"]) == (0.0, 0.0)
    assert tc["turnover_multiple"] is None
    assert (tc["margin_used"], tc["min_cash_balance"]) == (False, 3000.0)


@pytest.mark.asyncio
async def test_disposals_only_the_closed_lot_estimate_knows_are_not_counted(db):
    """No SELL on record but a lot closed: the turnover is an estimate, the count absent."""
    await _book(db)
    db.add_all([
        _lot(2, date(YEAR - 1, 6, 1), "2", "40", closed=date(YEAR - 1, 9, 1),
             source="heuristic"),
        MarketPrice(security_id=2, date=date(YEAR - 1, 9, 1), close_price=Decimal("45"),
                    currency="EUR", source="test"),
    ])
    await db.commit()
    report = await TaxService(db).get_tax_report(YEAR - 1)
    assert report["realized_source"] == "closed_lot_estimate"
    tc = report["trader_criteria"]
    assert tc["sales"] is None and tc["short_held_sales"] is None
    assert tc["sales_proceeds"] == 90.0 and tc["turnover_estimated"] is True


@pytest.mark.asyncio
async def test_no_holding_period_on_record_is_absent(db):
    """Every sale unmatched: the short-held count is unknown, not zero."""
    await _book(db)
    await db.execute(TaxLot.__table__.delete().where(TaxLot.__table__.c.is_open.is_(False)))
    await db.commit()
    tc = (await TaxService(db).get_tax_report(YEAR))["trader_criteria"]
    assert tc["sales"] == 3 and tc["sales_holding_unknown"] == 3
    assert tc["short_held_sales"] is None


@pytest.mark.asyncio
async def test_an_unconvertible_trade_makes_the_margin_unknown(db, monkeypatch):
    await _book(db)
    db.add(_trade("usd", None, "777", date(YEAR, 8, 1), "BUY", "1", "-10", currency="USD"))
    await db.commit()

    async def no_rate(self, currency, *args, **kwargs):
        raise ValueError(f"no {currency} rate")

    monkeypatch.setattr(CurrencyService, "get_exchange_rate", no_rate)
    tc = (await TaxService(db).get_tax_report(YEAR))["trader_criteria"]
    assert tc["margin_used"] is None and tc["min_cash_balance"] is None
    # The purchase cannot be valued, and neither can the opening cash: no turnover.
    assert tc["purchases"] is None and tc["opening_cash"] is None
    assert tc["turnover_multiple"] is None
    assert tc["sales"] == 3


@pytest.mark.asyncio
async def test_an_unconvertible_sale_leaves_the_turnover_absent(db, monkeypatch):
    """The realized section omits it, so its sales side is short: no multiple from it."""
    await _book(db)
    db.add(_trade("usd-s", None, "778", date(YEAR, 8, 1), "SELL", "-1", "10", currency="USD"))
    await db.commit()

    async def no_rate(self, currency, *args, **kwargs):
        raise ValueError(f"no {currency} rate")

    monkeypatch.setattr(CurrencyService, "get_exchange_rate", no_rate)
    report = await TaxService(db).get_tax_report(YEAR)
    assert report["realized_source"] == "trades"
    tc = report["trader_criteria"]
    assert tc["sales_proceeds"] is None and tc["turnover_multiple"] is None
    assert tc["purchases"] == 3500.0


@pytest.mark.asyncio
async def test_a_failing_opening_snapshot_leaves_the_turnover_absent(db, monkeypatch):
    await _book(db)
    real = PortfolioService.holdings_snapshot_as_of

    async def flaky(self, base_fx, on_date, exclude_accounts=None, only_accounts=None):
        if on_date == OPENING:
            raise RuntimeError("snapshot failed")
        return await real(self, base_fx, on_date, exclude_accounts=exclude_accounts,
                          only_accounts=only_accounts)

    monkeypatch.setattr(PortfolioService, "holdings_snapshot_as_of", flaky)
    report = await TaxService(db).get_tax_report(YEAR)
    tc = report["trader_criteria"]
    assert tc["turnover_multiple"] is None and tc["opening_securities"] is None
    assert tc["opening_cash"] == 3000.0 and tc["sales"] == 3  # the rest still stands
    assert any("turnover is absent" in w for w in report["warnings"])


@pytest.mark.asyncio
async def test_a_partial_opening_snapshot_says_the_turnover_overstates(db):
    await _book(db)
    await db.execute(MarketPrice.__table__.delete().where(
        MarketPrice.__table__.c.security_id == 2))
    await db.commit()
    report = await TaxService(db).get_tax_report(YEAR)
    tc = report["trader_criteria"]
    # 4800 over 1000 of securities and 3000 of cash: high, because BBB is missing.
    assert (tc["opening_securities"], tc["turnover_multiple"]) == (1000.0, 1.2)
    (warning,) = [w for w in report["warnings"] if "turnover" in w]
    assert "BBB" in warning and "overstates" in warning


@pytest.mark.asyncio
async def test_the_csv_carries_the_indicators(db):
    await _book(db)
    service = TaxService(db)
    lines = service.to_csv(await service.get_tax_report(YEAR)).splitlines()
    assert "Securities-dealer indicators (ESTV KS 36)" in lines
    assert "Turnover (x),0.96," in lines
    assert "Purchases (EUR),3500.0" in lines
    assert f"Opening securities at {OPENING.isoformat()} (EUR),2000.0" in lines
    assert f"Opening cash (IBKR) at {OPENING.isoformat()} (EUR),3000.0" in lines
    assert "Sales held under 6 months,1" in lines
    assert "Margin used,yes" in lines
    assert not any(line.startswith("Derivatives") for line in lines)
