"""
The year-end half of the tax report (GitHub issue #63): the pillar 3a snapshot is guarded
like the Steuerwert's, every realized and holdings row carries its security's identity,
and the IBKR cash balance at the same date stands beside the holdings total.

EUR base, so no FX. Offline: no network -- an unconvertible trade is made so by a stub,
never by asking a provider.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.accounts import IBKR, PILLAR3A
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW, TRANSFER_IN
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.services.currency_service import CurrencyService
from app.services.portfolio_service import PortfolioService
from app.services.tax_service import TaxService

YEAR = 2025
ON = date(YEAR, 12, 31)


async def _book(db) -> None:
    """An IBKR holding with a sale, a 3a fund, and cash on both sides."""
    db.add_all([
        Security(id=1, isin="US1111111111", symbol="AAA", description="US Co",
                 currency="EUR", conid=1, exchange="NASDAQ", account=IBKR),
        Security(id=2, isin="CH0117044948", symbol="CH0117044948",
                 description="Swisscanto World ex CH", currency="EUR", conid=None,
                 exchange="FUND", account=PILLAR3A, price_source="manual"),
    ])
    await db.flush()
    db.add_all([
        TaxLot(security_id=1, open_date=date(YEAR, 1, 5), quantity=Decimal("10"),
               cost_basis=Decimal("1000"), price_per_unit=Decimal("100"),
               currency="EUR", cost_basis_eur=Decimal("1000"), is_open=True),
        TaxLot(security_id=2, open_date=date(YEAR, 1, 5), quantity=Decimal("4"),
               cost_basis=Decimal("2000"), price_per_unit=Decimal("500"),
               currency="EUR", cost_basis_eur=Decimal("2000"), is_open=True),
        MarketPrice(security_id=1, date=ON, close_price=Decimal("110"),
                    currency="EUR", source="test"),
        MarketPrice(security_id=2, date=ON, close_price=Decimal("550"),
                    currency="EUR", source="test"),
        # IBKR: 3000 in, 1000 spent, 120 back from a sale -> 2120 at year end.
        CashFlow(ib_key="ibkr-d1", flow_date=date(YEAR, 1, 2), flow_type=DEPOSIT_WITHDRAW,
                 amount=Decimal("3000"), currency="EUR", amount_eur=Decimal("3000"),
                 account=IBKR),
        Trade(ib_key="ibkr-buy", conid="1", security_id=1, symbol="AAA",
              trade_date=date(YEAR, 1, 5), buy_sell="BUY", quantity=Decimal("10"),
              price=Decimal("100"), proceeds=Decimal("-1000"), commission=Decimal("0"),
              realized_pnl=Decimal("0"), currency="EUR", account=IBKR),
        Trade(ib_key="ibkr-sell", conid="1", security_id=1, symbol="AAA",
              trade_date=date(YEAR, 2, 10), buy_sell="SELL", quantity=Decimal("-1"),
              price=Decimal("120"), proceeds=Decimal("120"), commission=Decimal("0"),
              realized_pnl=Decimal("20"), currency="EUR", account=IBKR),
        # After 31 December: not in that day's balance.
        CashFlow(ib_key="ibkr-d2", flow_date=date(YEAR + 1, 1, 15),
                 flow_type=DEPOSIT_WITHDRAW, amount=Decimal("700"), currency="EUR",
                 amount_eur=Decimal("700"), account=IBKR),
        # Pillar 3a money, which must not reach the IBKR line.
        CashFlow(ib_key="fp-d1", flow_date=date(YEAR, 1, 3), flow_type=DEPOSIT_WITHDRAW,
                 amount=Decimal("1758"), currency="EUR", amount_eur=Decimal("1758"),
                 account=PILLAR3A),
        CashFlow(ib_key="fp-t1", flow_date=date(YEAR, 1, 4), flow_type=TRANSFER_IN,
                 amount=Decimal("50000"), currency="EUR", amount_eur=Decimal("50000"),
                 account=PILLAR3A),
        Trade(ib_key="fp-buy", conid="CH0117044948", security_id=2,
              symbol="CH0117044948", trade_date=date(YEAR, 1, 5), buy_sell="BUY",
              quantity=Decimal("4"), price=Decimal("500"), proceeds=Decimal("-2000"),
              commission=Decimal("0"), realized_pnl=Decimal("0"), currency="EUR",
              account=PILLAR3A),
    ])
    await db.commit()


# --- the pillar 3a snapshot ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_failing_3a_snapshot_does_not_sink_the_report(db, monkeypatch):
    """The audit's repro: this raised out of `get_tax_report`, a 500 for the whole page."""
    await _book(db)
    real = PortfolioService.holdings_snapshot_as_of

    async def flaky(self, base_fx, on_date, exclude_accounts=None, only_accounts=None):
        if only_accounts:
            raise RuntimeError("snapshot failed")
        return await real(self, base_fx, on_date, exclude_accounts=exclude_accounts)

    monkeypatch.setattr(PortfolioService, "holdings_snapshot_as_of", flaky)
    service = TaxService(db)
    report = await service.get_tax_report(YEAR)

    assert report["pillar3a"] is not None
    assert report["pillar3a"]["holdings_value"] is None       # missing, not zero
    assert report["pillar3a"]["contributions"] == 1758.0
    assert any("pillar 3a" in w and "not zero" in w for w in report["warnings"])
    # The Steuerwert beside it is untouched.
    assert report["holdings_snapshot_total"] == 1100.0
    assert "Assets held (NOT part of the Steuerwert),unavailable" in service.to_csv(report)


@pytest.mark.asyncio
async def test_a_partial_3a_snapshot_names_what_it_omits(db):
    await _book(db)
    await db.execute(MarketPrice.__table__.delete().where(
        MarketPrice.__table__.c.security_id == 2))
    await db.commit()
    report = await TaxService(db).get_tax_report(YEAR)
    assert report["pillar3a"]["holdings_value"] == 0.0
    (warning,) = [w for w in report["warnings"] if "pillar 3a" in w]
    assert "CH0117044948" in warning and "understates" in warning


# --- identity on every row ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_realized_and_holdings_rows_carry_isin_and_security_id(db):
    await _book(db)
    service = TaxService(db)
    report = await service.get_tax_report(YEAR)
    (sale,) = report["realized_gains"]
    assert (sale["security_id"], sale["isin"]) == (1, "US1111111111")
    (holding,) = report["holdings_snapshot"]
    assert (holding["security_id"], holding["isin"]) == (1, "US1111111111")

    lines = service.to_csv(report).splitlines()
    assert "Symbol,ISIN,Trade date,Quantity,Proceeds (EUR),Cost basis (EUR),Gain/Loss (EUR)" in lines
    assert any(l.startswith("AAA,US1111111111,2025-02-10,") for l in lines)
    assert any(l.startswith("AAA,US1111111111,10.0,") for l in lines)


@pytest.mark.asyncio
async def test_closed_lot_realized_rows_carry_the_identity_too(db):
    """No SELL trade: the closed-lot fallback builds the rows, from its own query."""
    db.add(Security(id=1, isin="US1111111111", symbol="AAA", description="US Co",
                    currency="EUR", conid=1, exchange="NASDAQ"))
    await db.flush()
    db.add_all([
        TaxLot(security_id=1, open_date=date(YEAR, 1, 5), close_date=date(YEAR, 3, 2),
               quantity=Decimal("2"), cost_basis=Decimal("200"),
               price_per_unit=Decimal("100"), currency="EUR",
               cost_basis_eur=Decimal("200"), is_open=False),
        MarketPrice(security_id=1, date=date(YEAR, 3, 2), close_price=Decimal("110"),
                    currency="EUR", source="test"),
    ])
    await db.commit()
    report = await TaxService(db).get_tax_report(YEAR)
    assert report["realized_source"] == "closed_lot_estimate"
    (sale,) = report["realized_gains"]
    assert (sale["security_id"], sale["isin"]) == (1, "US1111111111")


# --- the cash line -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_31_december_ibkr_cash_stands_beside_the_holdings(db):
    await _book(db)
    service = TaxService(db)
    report = await service.get_tax_report(YEAR)
    # 3000 - 1000 + 120: no 3a money, nothing dated after 31 December.
    assert report["cash_balance"] == 2120.0
    # Beside the holdings total, not inside it.
    assert report["holdings_snapshot_total"] == 1100.0
    assert "Cash (IBKR),,,2120.0," in service.to_csv(report).splitlines()


@pytest.mark.asyncio
async def test_a_margin_debit_is_reported_negative(db):
    db.add(Security(id=1, isin="US1111111111", symbol="AAA", description="US Co",
                    currency="EUR", conid=1, exchange="NASDAQ"))
    await db.flush()
    db.add(Trade(ib_key="buy", conid="1", security_id=1, symbol="AAA",
                 trade_date=date(YEAR, 1, 5), buy_sell="BUY", quantity=Decimal("10"),
                 price=Decimal("100"), proceeds=Decimal("-1000"), commission=Decimal("-1"),
                 realized_pnl=Decimal("0"), currency="EUR"))
    await db.commit()
    assert (await TaxService(db).get_tax_report(YEAR))["cash_balance"] == -1001.0


@pytest.mark.asyncio
async def test_no_ledger_means_no_cash_line_and_no_banner(db):
    db.add(Security(id=1, isin="US1111111111", symbol="AAA", description="US Co",
                    currency="EUR", conid=1, exchange="NASDAQ"))
    await db.commit()
    service = TaxService(db)
    report = await service.get_tax_report(YEAR)
    assert report["cash_balance"] is None
    assert report["warnings"] == []
    assert not any(l.startswith("Cash (IBKR)") for l in service.to_csv(report).splitlines())


@pytest.fixture
def no_foreign_rate(monkeypatch):
    """Every native->EUR lookup fails, as for a currency no provider serves."""
    async def no_rate(self, currency, *args, **kwargs):
        raise ValueError(f"no {currency} rate")
    monkeypatch.setattr(CurrencyService, "get_exchange_rate", no_rate)


@pytest.mark.asyncio
async def test_an_unconvertible_ibkr_trade_makes_the_cash_unknown_and_says_so(
    db, no_foreign_rate
):
    await _book(db)
    db.add(Trade(ib_key="twd-buy", conid="1", security_id=1, symbol="AAA",
                 trade_date=date(YEAR, 3, 1), buy_sell="BUY", quantity=Decimal("1"),
                 price=Decimal("3000"), proceeds=Decimal("-3000"), commission=Decimal("0"),
                 realized_pnl=Decimal("0"), currency="TWD", account=IBKR))
    await db.commit()
    report = await TaxService(db).get_tax_report(YEAR)
    assert report["cash_balance"] is None
    assert any("cash balance is unknown" in w for w in report["warnings"])


@pytest.mark.asyncio
async def test_an_unconvertible_3a_trade_does_not_unknow_the_ibkr_cash(db, no_foreign_rate):
    await _book(db)
    db.add(Trade(ib_key="fp-twd", conid="CH0117044948", security_id=2,
                 symbol="CH0117044948", trade_date=date(YEAR, 3, 1), buy_sell="BUY",
                 quantity=Decimal("1"), price=Decimal("3000"), proceeds=Decimal("-3000"),
                 commission=Decimal("0"), realized_pnl=Decimal("0"), currency="TWD",
                 account=PILLAR3A))
    await db.commit()
    report = await TaxService(db).get_tax_report(YEAR)
    assert report["cash_balance"] == 2120.0
    assert not any("cash balance" in w for w in report["warnings"])


@pytest.mark.asyncio
async def test_a_failing_cash_read_is_absent_and_warned_not_a_500(db, monkeypatch):
    await _book(db)
    from app.services.cash_service import CashService

    async def broken(self, *args, **kwargs):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(CashService, "uninvested_cash", broken)
    report = await TaxService(db).get_tax_report(YEAR)
    assert report["cash_balance"] is None
    assert any("cash balance could not be computed" in w for w in report["warnings"])
