"""
The uninvested cash balance, and the reason it exists: a rotation is not a loss.

Read off production on 2026-08-26 — 25,136 CHF of positions sold on 08-21 and 12,682 of
it redeployed on 08-24, leaving ~12,229 idle. Holdings fell 68,342 -> 43,631 and came
back to 56,161, so the value chart drew a 36% cliff and the headline card understated the
account by 18%, for a period in which nothing was lost.

`test_a_rotation_leaves_total_value_flat` is the whole feature in one assertion. The rest
pin the rules that each would be a wrong number the other way — above all that a balance
is derived from **what moved** (trades, deposits, real dividends) rather than from what
is held, which is what lets it anchor at a definitional zero on an account whose holdings
arrived by in-kind transfer carrying years of pre-IBKR open dates.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.app_settings import AppSetting
from app.models.cash_balance import CashBalance
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW, TRANSFER_IN
from app.models.corporate_action import CorporateAction
from app.models.dividend_payment import DividendPayment
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.services.cash_service import CashService, DERIVED, MEASURED, UNKNOWN
from app.services.portfolio_service import BaseFx, PortfolioService
from app.services.sync_helper import resolve_cash_balances

EUR = BaseFx("EUR", {})


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [Security.__table__, TaxLot.__table__, AppSetting.__table__,
              CashFlow.__table__, CashBalance.__table__, ExchangeRate.__table__,
              Trade.__table__, DividendPayment.__table__, MarketPrice.__table__,
              CorporateAction.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(
        id=1, isin="US0000000001", symbol="AAA", description="Test Co",
        currency="EUR", conid=100, asset_category="STK", exchange="XETRA",
    ))
    await session.flush()
    return engine, session


def _trade(trade_date: date, key: str, proceeds: str, commission: str = "0",
           buy_sell: str = "BUY", currency: str = "EUR") -> Trade:
    """`proceeds` carries IBKR's sign: negative for a buy, positive for a sell."""
    return Trade(
        ib_key=key, conid="100", security_id=1, symbol="AAA", trade_date=trade_date,
        buy_sell=buy_sell, quantity=Decimal("10"), price=Decimal("10"),
        proceeds=Decimal(proceeds), commission=Decimal(commission), currency=currency,
    )


def _flow(flow_date: date, amount: str, key: str,
          flow_type: str = DEPOSIT_WITHDRAW) -> CashFlow:
    return CashFlow(
        ib_key=key, flow_date=flow_date, flow_type=flow_type,
        amount=Decimal(amount), currency="EUR", amount_eur=Decimal(amount),
    )


def _lot(open_date: date, cost: str, close_date: Optional[date] = None) -> TaxLot:
    return TaxLot(
        security_id=1, open_date=open_date, quantity=Decimal("10"),
        cost_basis=Decimal(cost), price_per_unit=Decimal(cost) / 10, currency="EUR",
        cost_basis_eur=Decimal(cost), is_open=close_date is None,
        close_date=close_date, close_source="trade" if close_date else None,
    )


async def _balance(session, on_date: date = date(2026, 12, 31)) -> Decimal:
    service = CashService(session)
    return service.balance_as_of(await service.balance_events(EUR), on_date)


# --------------------------------------------------------------------------- derivation


@pytest.mark.asyncio
async def test_cash_is_deposits_minus_purchases_plus_sales():
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 9), "1000", "D1"))
        session.add(_trade(date(2026, 1, 10), "T1", "-400", "-2"))
        session.add(_trade(date(2026, 2, 10), "T2", "250", "-1", buy_sell="SELL"))
        await session.flush()

        # 1000 deposited, 402 spent (price plus commission), 249 received net of it.
        assert await _balance(session) == Decimal("847")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_transferred_holding_consumes_no_cash():
    """
    The anchor, and the reason cash comes from trades rather than from lot events.

    This account's holdings arrived by in-kind transfer carrying their **original** open
    dates — years before the IBKR account existed. A lot-event derivation would read each
    of those as a purchase draining an account that had not been opened, and drive the
    balance tens of thousands negative. A transferred lot has no `Trade` row, so it
    correctly costs nothing.
    """
    engine, session = await _make_session()
    try:
        session.add(_lot(date(2024, 5, 28), "20000"))   # transferred in, no trade
        session.add(_flow(date(2026, 1, 9), "1000", "D1"))
        await session.flush()

        assert await _balance(session) == Decimal("1000")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_in_kind_transfer_row_moves_nothing():
    """
    Every flow type counts here, unlike `get_deposits()`'s deposit whitelist — the
    question is "did cash move", not "was money added". The in-kind rows carry a zero
    amount, so including them is safe *and* correct rather than merely harmless: a
    transfer with a real cash leg genuinely does move cash and must not be dropped.
    """
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 19), "0", "TR1", flow_type=TRANSFER_IN))
        session.add(_flow(date(2026, 1, 20), "500", "TR2", flow_type=TRANSFER_IN))
        await session.flush()

        assert await _balance(session) == Decimal("500")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_yfinance_estimate_is_not_cash():
    """
    An estimate is a guess about a dividend paid into a *previous* broker, so crediting
    it invents money this account never received. This is the one place the era splice
    deliberately does not apply — see `DividendService.ibkr_cash_receipts`.
    """
    engine, session = await _make_session()
    try:
        session.add(DividendPayment(
            security_id=1, ex_date=date(2026, 1, 5), source="yfinance_estimate",
            gross_amount_eur=Decimal("40"), net_amount_eur=Decimal("40"),
        ))
        session.add(DividendPayment(
            # IBKR rows are filed under their PAY date; `ex_date` is NOT NULL on this
            # table, so the ingest stores the same date in both.
            security_id=1, ex_date=date(2026, 3, 5), pay_date=date(2026, 3, 5),
            source="ibkr",
            gross_amount_eur=Decimal("30"), withholding_tax_eur=Decimal("5"),
            net_amount_eur=Decimal("25"),
        ))
        await session.flush()

        # Only the IBKR row, and net of withholding rather than gross.
        assert await _balance(session) == Decimal("25")
    finally:
        await engine.dispose()


# ------------------------------------------------------------------------- provenance


@pytest.mark.asyncio
async def test_an_empty_ledger_is_unknown_rather_than_zero():
    """
    "We have no idea" and "you hold no cash" are different states, and rendering the
    first as 0.00 is the reassuring-zero failure this codebase keeps rediscovering.
    """
    engine, session = await _make_session()
    try:
        assert await CashService(session).cash_source() == UNKNOWN
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_derived_zero_is_a_real_answer():
    """The mirror case: a fully deployed account really does hold nothing."""
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 9), "1000", "D1"))
        session.add(_trade(date(2026, 1, 10), "T1", "-1000"))
        await session.flush()

        assert await CashService(session).cash_source() == DERIVED
        assert await _balance(session) == Decimal("0")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_measured_balance_overrides_the_derivation():
    """
    IBKR's own figure includes the broker interest, fees and FX spread the derivation
    structurally cannot see, so it wins on the days it exists — and the correction is
    the *difference*, so days after it keep moving with real activity.
    """
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 9), "1000", "D1"))
        session.add(CashBalance(
            report_date=date(2026, 1, 31), currency="EUR", cash=Decimal("980"),
        ))
        session.add(_trade(date(2026, 2, 10), "T1", "-100"))
        await session.flush()

        assert await CashService(session).cash_source() == MEASURED
        # Snapped to IBKR's 980 on the 31st...
        assert await _balance(session, date(2026, 1, 31)) == Decimal("980")
        # ...and the later purchase still moves it, rather than the level going flat.
        assert await _balance(session, date(2026, 2, 28)) == Decimal("880")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_days_before_measurement_are_not_reported_as_measured():
    """
    The Flex window is bounded, so measured history begins whenever the portal section
    was enabled. Stamping `ibkr` on the years before it because the tail is measured
    would be the same overclaim as a badge that can never clear.
    """
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 9), "1000", "D1"))
        session.add(MarketPrice(
            security_id=1, date=date(2026, 1, 12), close_price=Decimal("10"),
            currency="EUR", source="test",
        ))
        session.add(_lot(date(2026, 1, 9), "500"))
        session.add(CashBalance(
            report_date=date(2026, 1, 13), currency="EUR", cash=Decimal("500"),
        ))
        await session.flush()

        timeline = await PortfolioService(session).get_portfolio_value_over_time(
            date(2026, 1, 12), date(2026, 1, 14)
        )
        by_date = {r["date"]: r for r in timeline}
        assert by_date["2026-01-12"]["cash_source"] == DERIVED
        assert by_date["2026-01-13"]["cash_source"] == MEASURED
        assert by_date["2026-01-14"]["cash_source"] == MEASURED
    finally:
        await engine.dispose()


# ------------------------------------------------------------------------ the headline


@pytest.mark.asyncio
async def test_a_rotation_leaves_total_value_flat():
    """
    The feature, in one assertion. Sell everything on day 2 and hold the proceeds:
    holdings collapse to zero and `total_value_eur` does not move, because the money
    went to cash rather than out of the account.

    `money_in_eur` is flat throughout for the same reason — a sale is not a
    contribution — which is what makes it the only honest partner for a line that
    contains cash. Pairing total value with *cost basis* would step instead, because
    cost basis falls by what the lot cost while cash rises by what it sold for.
    """
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 5), "1000", "D1"))
        session.add(_lot(date(2026, 1, 5), "1000", close_date=date(2026, 1, 7)))
        session.add(_trade(date(2026, 1, 5), "T1", "-1000"))
        session.add(_trade(date(2026, 1, 7), "T2", "1200", buy_sell="SELL"))
        for day in (5, 6, 7, 8):
            session.add(MarketPrice(
                security_id=1, date=date(2026, 1, day),
                close_price=Decimal("120"), currency="EUR", source="test",
            ))
        await session.flush()

        timeline = await PortfolioService(session).get_portfolio_value_over_time(
            date(2026, 1, 5), date(2026, 1, 8)
        )
        by_date = {r["date"]: r for r in timeline}

        # Holdings fall off a cliff on the sale date (exclude-on-close)...
        assert by_date["2026-01-06"]["market_value_eur"] == pytest.approx(1200)
        assert by_date["2026-01-07"]["market_value_eur"] == pytest.approx(0)
        # ...and total value does not.
        assert by_date["2026-01-06"]["total_value_eur"] == pytest.approx(1200)
        assert by_date["2026-01-07"]["total_value_eur"] == pytest.approx(1200)
        assert by_date["2026-01-08"]["total_value_eur"] == pytest.approx(1200)
        # Cash is where it went.
        assert by_date["2026-01-07"]["cash_eur"] == pytest.approx(1200)
        # And the baseline never moves: nothing was contributed on the 7th.
        assert all(r["money_in_eur"] == pytest.approx(1000) for r in timeline)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_summary_total_is_holdings_plus_cash():
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 5), "1000", "D1"))
        session.add(_trade(date(2026, 1, 5), "T1", "-600"))
        session.add(_lot(date(2026, 1, 5), "600"))
        session.add(MarketPrice(
            security_id=1, date=date.today(), close_price=Decimal("70"),
            currency="EUR", source="test",
        ))
        await session.flush()

        summary = await PortfolioService(session).get_current_portfolio_summary()
        assert summary["total_market_value_eur"] == pytest.approx(700)
        assert summary["total_cash_eur"] == pytest.approx(400)
        assert summary["total_value_eur"] == pytest.approx(1100)
        assert summary["cash_source"] == DERIVED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_liquidated_account_reports_its_cash_rather_than_nothing():
    """
    The empty-portfolio early return used to hard-code zeros. Nothing *held* is not
    nothing *owned*: an account that has sold everything holds its whole value in cash,
    which is precisely the state this feature exists for.
    """
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 5), "1000", "D1"))
        await session.flush()

        summary = await PortfolioService(session).get_current_portfolio_summary()
        assert summary["num_positions"] == 0
        assert summary["total_market_value_eur"] == pytest.approx(0)
        assert summary["total_cash_eur"] == pytest.approx(1000)
        assert summary["total_value_eur"] == pytest.approx(1000)
    finally:
        await engine.dispose()


# ------------------------------------------------------------------- the shared splice


@pytest.mark.asyncio
async def test_the_chart_and_the_strip_agree_about_money_in():
    """
    The chart's Money In line and the contributions strip's all-time figure come from
    one event list (`_contribution_inputs`), so they cannot answer the same question
    two ways — the failure mode CLAUDE.md opens with, and one this app has already had
    twice with the 12-month deployment average.
    """
    engine, session = await _make_session()
    try:
        from app.repositories.app_settings_repository import AppSettingsRepository
        session.add(_lot(date(2025, 6, 1), "5000"))          # pre-coverage: lot cost
        session.add(_lot(date(2026, 2, 1), "800"))           # post-coverage: ignored
        session.add(_flow(date(2026, 1, 9), "1200", "D1"))   # post-coverage: counted
        session.add(_flow(date(2026, 3, 9), "300", "D2"))
        await session.flush()
        await AppSettingsRepository(session).widen_cash_flows_covered_from(date(2026, 1, 9))
        session.add(MarketPrice(
            security_id=1, date=date(2026, 3, 20), close_price=Decimal("10"),
            currency="EUR", source="test",
        ))
        await session.flush()

        service = PortfolioService(session)
        report = await service.get_contributions(as_of=date(2026, 3, 20))
        all_time = next(w for w in report["windows"] if w["label"] == "all")

        timeline = await service.get_portfolio_value_over_time(
            date(2026, 3, 20), date(2026, 3, 20)
        )
        assert timeline
        assert timeline[-1]["money_in_eur"] == pytest.approx(all_time["money_in_eur"])
        # And it really is the splice, not just an equal pair of zeros.
        assert all_time["money_in_eur"] == pytest.approx(6500)
    finally:
        await engine.dispose()


# --------------------------------------------------------------- the Cash Report path


class _FakeCurrency:
    """Converts at a fixed rate, and refuses one currency to exercise the skip."""

    def __init__(self, rates):
        self.rates = rates

    async def convert_to_eur(self, amount, from_currency, target_date):
        if from_currency not in self.rates:
            raise ValueError(f"no rate for {from_currency}")
        return amount * self.rates[from_currency]


@pytest.mark.asyncio
async def test_a_base_summary_row_is_taken_as_is():
    """
    "Base Currency Summary" in the portal emits one row already summed into the account's
    base, whose `currency` is the literal `BASE_SUMMARY` — not a currency. The account's
    own base is what it is denominated in.
    """
    rows = await resolve_cash_balances(
        _FakeCurrency({}),
        [],
        [{'report_date': date(2026, 8, 21), 'currency': 'CHF',
          'ending_cash': Decimal("12212.62"), 'is_base_summary': True}],
    )
    assert rows == [{
        'report_date': date(2026, 8, 21), 'currency': 'CHF',
        'cash': Decimal("12212.62"), 'stock': None, 'total': None,
    }]


@pytest.mark.asyncio
async def test_a_currency_breakout_is_converted_and_summed():
    """Real case, not defensive: this account trades in five currencies."""
    rows = await resolve_cash_balances(
        _FakeCurrency({'USD': Decimal("0.9")}),
        [],
        [
            {'report_date': date(2026, 8, 21), 'currency': 'EUR',
             'ending_cash': Decimal("100"), 'is_base_summary': False},
            {'report_date': date(2026, 8, 21), 'currency': 'USD',
             'ending_cash': Decimal("200"), 'is_base_summary': False},
        ],
    )
    assert len(rows) == 1
    assert rows[0]['currency'] == 'EUR'
    assert rows[0]['cash'] == Decimal("280")  # 100 + 200*0.9


@pytest.mark.asyncio
async def test_a_summary_row_beside_its_own_breakout_is_not_double_counted():
    """
    Ticking both portal options emits both shapes. Summing a breakout *and* the summary
    that already contains it doubles the balance, which is the one arithmetic error here
    that would look entirely plausible on screen.
    """
    rows = await resolve_cash_balances(
        _FakeCurrency({'USD': Decimal("0.9")}),
        [],
        [
            {'report_date': date(2026, 8, 21), 'currency': 'EUR',
             'ending_cash': Decimal("100"), 'is_base_summary': False},
            {'report_date': date(2026, 8, 21), 'currency': 'USD',
             'ending_cash': Decimal("200"), 'is_base_summary': False},
            {'report_date': date(2026, 8, 21), 'currency': 'CHF',
             'ending_cash': Decimal("280"), 'is_base_summary': True},
        ],
    )
    assert len(rows) == 1
    assert rows[0]['cash'] == Decimal("280")
    assert rows[0]['currency'] == 'CHF'


@pytest.mark.asyncio
async def test_an_unconvertible_currency_abandons_the_date():
    """
    A total short by one currency is a *plausible* figure, which is the dangerous kind —
    and it would overwrite a derived balance that is already the better answer. So the
    date is dropped entirely rather than stored partial.
    """
    rows = await resolve_cash_balances(
        _FakeCurrency({}),  # no rates at all
        [],
        [
            {'report_date': date(2026, 8, 21), 'currency': 'EUR',
             'ending_cash': Decimal("100"), 'is_base_summary': False},
            {'report_date': date(2026, 8, 21), 'currency': 'TWD',
             'ending_cash': Decimal("5000"), 'is_base_summary': False},
        ],
    )
    assert rows == []


@pytest.mark.asyncio
async def test_a_zero_currency_balance_needs_no_rate():
    """An emptied currency must not discard the date over a row worth nothing."""
    rows = await resolve_cash_balances(
        _FakeCurrency({}),
        [],
        [
            {'report_date': date(2026, 8, 21), 'currency': 'EUR',
             'ending_cash': Decimal("100"), 'is_base_summary': False},
            {'report_date': date(2026, 8, 21), 'currency': 'TWD',
             'ending_cash': Decimal("0"), 'is_base_summary': False},
        ],
    )
    assert len(rows) == 1
    assert rows[0]['cash'] == Decimal("100")


@pytest.mark.asyncio
async def test_the_daily_series_wins_where_both_sections_are_enabled():
    """
    Both sections answer the same question and should agree; where they do not, the
    per-day figure is the more specific measurement. Deterministic precedence is what
    stops a re-sync from flipping a stored value depending on write order.
    """
    rows = await resolve_cash_balances(
        _FakeCurrency({}),
        [{'report_date': date(2026, 8, 21), 'currency': 'CHF',
          'cash': Decimal("999"), 'stock': Decimal("50000"), 'total': Decimal("50999")}],
        [{'report_date': date(2026, 8, 21), 'currency': 'CHF',
          'ending_cash': Decimal("111"), 'is_base_summary': True}],
    )
    assert len(rows) == 1
    assert rows[0]['cash'] == Decimal("999")
    assert rows[0]['stock'] == Decimal("50000")


@pytest.mark.asyncio
async def test_both_sections_absent_is_not_an_error():
    """The default state of the Flex query, and a supported one."""
    assert await resolve_cash_balances(_FakeCurrency({}), [], []) == []
