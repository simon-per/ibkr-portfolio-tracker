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

from app.accounts import IBKR, PILLAR3A
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
from app.services.cash_service import CashService, DERIVED, MEASURED, MIXED, UNKNOWN
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
async def test_the_chart_the_strip_and_the_monthly_series_agree_about_money_in():
    """
    Three readers, one event list (`_contribution_inputs`): the value chart's daily
    Money In line, the contributions strip's all-time figure, and the monthly series
    the deployment card draws. They cannot answer the same question three ways — the
    failure mode CLAUDE.md opens with, and one this app has already had twice with the
    12-month deployment average, in these very components.

    The third reader arrived on 2026-09-06 and is pinned here from the day it existed
    rather than after it drifts.
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
        # The monthly series is the third reader. Summing it over all time has to
        # reproduce the window, which needs both the splice AND the union of months --
        # the January deposit lands in a month with no lot of its own.
        assert sum(m["money_in_eur"] for m in report["monthly"]) == pytest.approx(
            all_time["money_in_eur"]
        )
        # And it really is the splice, not just an equal pair of zeros.
        assert all_time["money_in_eur"] == pytest.approx(6500)
        # Spot the shape: pre-coverage months carry lot cost, post-coverage months
        # carry deposits, and February's 800 EUR purchase contributes nothing.
        by_month = {m["month"]: m for m in report["monthly"]}
        assert by_month["2025-06"]["money_in_eur"] == pytest.approx(5000)
        assert by_month["2026-01"]["money_in_eur"] == pytest.approx(1200)
        assert by_month["2026-02"]["money_in_eur"] == 0.0
        assert by_month["2026-02"]["deployed_eur"] == pytest.approx(800)
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


# ------------------------------------------------- the real document, end to end


#: A `<CashReport>` shaped exactly like the saved App_OpenLots query: the **Base
#: Currency Summary** option, and the four fields it emits. Two things here are the
#: point rather than scenery.
#:
#: `currency="BASE_SUMMARY"` is **not a currency**, and this document goes through the
#: real `_fix_currency_codes` -> `_sanitize_flex_xml` -> `ibflex.parser` chain that drops
#: any attribute ibflex cannot convert. If ibflex ever types that field as a currency
#: enum, the attribute is dropped, the row stops looking like a base summary, and its
#: total is silently summed as though it were one currency among several — a doubled
#: balance that would look entirely plausible. That is why `extract_cash_report` reads
#: two independent tells and why this test parses rather than stubbing.
#:
#: The `<FlexStatement>` also carries `AccountInformation`, because a base-summary row
#: is denominated in the account's base and `BASE_SUMMARY` cannot say what that is.
CASH_REPORT_XML = """<FlexQueryResponse queryName="App_OpenLots" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20260727" toDate="20260825"
 period="Last30CalendarDays" whenGenerated="20260826;120000">
 <AccountInformation accountId="U1234567" currency="CHF" name="TEST" />
 <CashReport>
  <CashReportCurrency accountId="U1234567" currency="BASE_SUMMARY"
   fromDate="20260727" toDate="20260825" startingCash="198.43"
   endingCash="12212.62" levelOfDetail="BaseCurrency" />
 </CashReport>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>""".encode()


#: The same statement under **Currency Breakout**, which is what the other portal option
#: emits: one row per currency held, plus the summary. Both are present on purpose —
#: ticking both options is legal, and summing the breakout into a total that already
#: contains it is the failure this shape exists to catch.
CASH_REPORT_BOTH_XML = """<FlexQueryResponse queryName="App_OpenLots" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20260727" toDate="20260825"
 period="Last30CalendarDays" whenGenerated="20260826;120000">
 <AccountInformation accountId="U1234567" currency="CHF" name="TEST" />
 <CashReport>
  <CashReportCurrency accountId="U1234567" currency="EUR" fromDate="20260727"
   toDate="20260825" endingCash="100" levelOfDetail="Currency" />
  <CashReportCurrency accountId="U1234567" currency="USD" fromDate="20260727"
   toDate="20260825" endingCash="200" levelOfDetail="Currency" />
  <CashReportCurrency accountId="U1234567" currency="BASE_SUMMARY"
   fromDate="20260727" toDate="20260825" endingCash="12212.62"
   levelOfDetail="BaseCurrency" />
 </CashReport>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>""".encode()


def _parse(raw: bytes):
    """The production chain: currency repair, sanitize, ibflex."""
    from ibflex import parser as flex_parser
    from app.services.ibkr_service import IBKRService

    svc = IBKRService(token="t", query_id="q")
    fixed = svc._fix_currency_codes(raw)
    sanitized, warnings = svc._sanitize_flex_xml(fixed)
    statement = flex_parser.parse(sanitized).FlexStatements[0]
    return svc, {"statement": statement}, warnings


@pytest.mark.asyncio
async def test_a_base_summary_survives_the_sanitizer_and_the_parser():
    """
    The check that could not be made against a stub: `BASE_SUMMARY` reaches the
    extractor intact, and the row is recognised as a base summary rather than as a
    currency called BASE_SUMMARY.
    """
    svc, flex_data, warnings = _parse(CASH_REPORT_XML)
    rows = await svc.extract_cash_report(flex_data)

    assert len(rows) == 1
    row = rows[0]
    assert row["is_base_summary"] is True
    assert row["report_date"] == date(2026, 8, 25)
    assert row["ending_cash"] == Decimal("12212.62")
    # Denominated in the account's base, taken from AccountInformation — never the
    # literal BASE_SUMMARY, and never guessed from the app's own display setting.
    assert row["currency"] == "CHF"
    # Nothing the ingest reads was dropped, so no warning may claim otherwise.
    assert not [w for w in warnings if "endingCash" in w or "levelOfDetail" in w]


@pytest.mark.asyncio
async def test_the_parsed_document_becomes_one_cash_balance_row():
    svc, flex_data, _ = _parse(CASH_REPORT_XML)
    balances = await resolve_cash_balances(
        _FakeCurrency({}), await svc.extract_equity_summary(flex_data),
        await svc.extract_cash_report(flex_data),
    )
    assert balances == [{
        "report_date": date(2026, 8, 25), "currency": "CHF",
        "cash": Decimal("12212.62"), "stock": None, "total": None,
    }]


@pytest.mark.asyncio
async def test_both_portal_options_together_do_not_double_the_balance():
    """
    Ticking Base Currency Summary *and* Currency Breakout emits both shapes in one
    document. 100 EUR + 200 USD + a 12,212.62 summary must be 12,212.62, not more.
    """
    svc, flex_data, _ = _parse(CASH_REPORT_BOTH_XML)
    rows = await svc.extract_cash_report(flex_data)
    assert len(rows) == 3
    assert sum(1 for r in rows if r["is_base_summary"]) == 1

    balances = await resolve_cash_balances(
        _FakeCurrency({"USD": Decimal("0.9")}), [], rows
    )
    assert len(balances) == 1
    assert balances[0]["cash"] == Decimal("12212.62")
    assert balances[0]["currency"] == "CHF"


@pytest.mark.asyncio
async def test_a_statement_without_the_section_yields_nothing():
    """
    The state every statement was in before the portal edit, and the one a fresh
    install stays in. It must be silent rather than an error.

    The section is deleted rather than renamed, because ibflex rejects an unknown child
    *element* outright — `_sanitize_flex_xml` absorbs unknown attributes and values, not
    unknown sections. That is a real boundary on what a portal edit can safely enable:
    a section ibflex does not model aborts the whole document, open positions included.
    """
    start = CASH_REPORT_XML.index(b" <CashReport>")
    end = CASH_REPORT_XML.index(b"</CashReport>") + len(b"</CashReport>")
    no_section = CASH_REPORT_XML[:start] + CASH_REPORT_XML[end:]
    svc, flex_data, _ = _parse(no_section)
    assert await svc.extract_cash_report(flex_data) == []
    assert await svc.extract_equity_summary(flex_data) == []


@pytest.mark.asyncio
async def test_a_balance_already_in_the_base_currency_is_not_round_tripped():
    """
    IBKR reports its base-summary balance in the account's base. When that equals the
    display base, converting it to EUR and back costs only rounding — and this is the
    one figure in the app people reconcile against a broker statement line for line.

    Measured: 12,501.583564544 CHF came back as 12,502.03 before the short-circuit,
    because the stored CHF->EUR rate and the EUR->CHF rate are not exact inverses.
    """
    engine, session = await _make_session()
    try:
        chf = BaseFx("CHF", {date(2026, 8, 25): Decimal("0.93")})
        session.add(CashBalance(
            report_date=date(2026, 8, 25), currency="CHF",
            cash=Decimal("12501.583564544"),
        ))
        await session.flush()

        service = CashService(session)
        events = await service.balance_events(chf)
        # 6dp, not 9: `cash` is Numeric(18, 6), so the column truncates the ninth
        # decimal IBKR sends. That is storage precision and not conversion drift —
        # which is the whole point, because the round trip this pins moved the figure
        # in the *fourth* significant place, 0.45 CHF.
        assert service.balance_as_of(events, date(2026, 8, 25)) == Decimal("12501.583565")
    finally:
        await engine.dispose()


# ------------------------------------------- what a *InBase figure is denominated in


#: The live query's actual shape, and the case the first draft got wrong: **no
#: `<AccountInformation>` section**. It is optional in the Flex editor and this account
#: does not have it enabled, so the base currency has to come from somewhere else.
#:
#: `<ConversionRates>` is that somewhere: every row converts *into* the base, and the
#: query already emits it (`Include Currency Rates? Yes`). On the real 2026-08-26
#: statement that is 1,014 rows, all `toCurrency="CHF"`.
#:
#: Getting this wrong is silent and expensive. An unlabelled figure is read as EUR by
#: `NativeToBase`, so 12,501.58 CHF becomes ~13,400 after projection — a 7% overstatement
#: of a balance that looks entirely reasonable, overwriting a derived figure that was
#: already right to within a couple of percent.
NO_ACCOUNT_INFO_XML = """<FlexQueryResponse queryName="App_OpenLots" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20260727" toDate="20260825"
 period="Last30CalendarDays" whenGenerated="20260826;142958">
 <CashReport>
  <CashReportCurrency accountId="U1234567" currency="BASE_SUMMARY"
   toDate="20260825" endingCash="12501.583564544" levelOfDetail="BaseCurrency" />
 </CashReport>
 <ConversionRates>
  <ConversionRate reportDate="20260727" fromCurrency="MXN" toCurrency="CHF" rate="0.046935" />
  <ConversionRate reportDate="20260728" fromCurrency="USD" toCurrency="CHF" rate="0.9" />
 </ConversionRates>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>""".encode()


@pytest.mark.asyncio
async def test_the_base_currency_comes_from_conversion_rates_when_unstated():
    svc, flex_data, _ = _parse(NO_ACCOUNT_INFO_XML)
    assert svc._base_currency(flex_data["statement"]) == "CHF"

    rows = await svc.extract_cash_report(flex_data)
    assert len(rows) == 1
    assert rows[0]["currency"] == "CHF"
    assert rows[0]["ending_cash"] == Decimal("12501.583564544")


@pytest.mark.asyncio
async def test_a_stated_account_currency_beats_the_inference():
    """`AccountInformation` is the direct answer where the section is enabled."""
    with_info = NO_ACCOUNT_INFO_XML.replace(
        b" <CashReport>",
        b' <AccountInformation accountId="U1234567" currency="USD" name="T" />\n <CashReport>',
    )
    svc, flex_data, _ = _parse(with_info)
    assert svc._base_currency(flex_data["statement"]) == "USD"


@pytest.mark.asyncio
async def test_disagreeing_conversion_targets_yield_no_currency_at_all():
    """
    The section's whole premise is that one currency is the base, so a split answer
    means the premise is wrong. Guessing which half to believe is how a plausible wrong
    number gets made — `None`, and the caller drops the figure.
    """
    split = NO_ACCOUNT_INFO_XML.replace(
        b'fromCurrency="USD" toCurrency="CHF"', b'fromCurrency="USD" toCurrency="EUR"'
    )
    svc, flex_data, _ = _parse(split)
    assert svc._base_currency(flex_data["statement"]) is None

    rows = await svc.extract_cash_report(flex_data)
    assert rows[0]["currency"] is None


@pytest.mark.asyncio
async def test_an_unlabelled_base_summary_is_dropped_rather_than_assumed():
    """
    The refusal that makes the `None` above safe. Storing it would put a CHF figure in
    an EUR-labelled column and overwrite a derived balance that was already close —
    `import_prices.py` refuses a whole file for the same reason.
    """
    balances = await resolve_cash_balances(
        _FakeCurrency({}),
        [],
        [{'report_date': date(2026, 8, 25), 'currency': None,
          'ending_cash': Decimal("12501.58"), 'is_base_summary': True}],
    )
    assert balances == []


@pytest.mark.asyncio
async def test_the_reader_refuses_an_unlabelled_row_too():
    """
    Second gate, for rows written before the ingest learned to refuse them. `NativeToBase`
    reads a None currency as EUR, which is right for the EUR-converted breakout path and
    wrong for a base-summary row — so the distinction cannot be left to it.
    """
    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 9), "1000", "D1"))
        session.add(CashBalance(
            report_date=date(2026, 1, 31), currency=None, cash=Decimal("5000"),
        ))
        await session.flush()

        # The derived balance stands; the unlabelled 5000 is not applied.
        assert await _balance(session, date(2026, 2, 28)) == Decimal("1000")
    finally:
        await engine.dispose()


# ------------------------------------------------- a second account's locked cash

@pytest.mark.asyncio
async def test_an_ibkr_measured_level_never_subtracts_another_accounts_balance():
    """
    The sharpest defect the account dimension had to fix, and it is latent rather than
    live: `cash_balances` is empty until the Cash Report section is enabled in the Flex
    portal, so nothing bites until somebody does that.

    A measured row is IBKR's *level*, and `_apply_measured` turns it into
    ``level - derived_running``. Differenced against a total that also carried pillar
    3a money, every measured day would emit a correction that silently subtracts the 3a
    balance — and every day between two measured rows would put it back. The line would
    sawtooth, and each individual point would look entirely plausible.

    Here: IBKR holds 1,000 measured, 3a holds 500 derived. The answer is 1,500 on the
    measured day and 1,500 the day after. Before the partition it was 1,000 and 1,500.
    """
    engine, session = await _make_session()
    try:
        eur = BaseFx("EUR", {})
        session.add_all([
            # IBKR: a derived deposit that the measured level then corrects.
            CashFlow(ib_key="ibkr-d", flow_date=date(2026, 8, 20),
                     flow_type=DEPOSIT_WITHDRAW, amount=Decimal("900"),
                     currency="EUR", amount_eur=Decimal("900"), account=IBKR),
            CashBalance(report_date=date(2026, 8, 25), currency="EUR",
                        cash=Decimal("1000"), account=IBKR),
            # Pillar 3a: derived only, and IBKR has never seen a franc of it.
            CashFlow(ib_key="fp-d", flow_date=date(2026, 8, 21),
                     flow_type=DEPOSIT_WITHDRAW, amount=Decimal("500"),
                     currency="EUR", amount_eur=Decimal("500"), account=PILLAR3A),
        ])
        await session.flush()

        service = CashService(session)
        events = await service.balance_events(eur)

        assert service.balance_as_of(events, date(2026, 8, 25)) == Decimal("1500")
        assert service.balance_as_of(events, date(2026, 8, 26)) == Decimal("1500")
        # And the IBKR correction is exactly the 100 it was short, not 100 - 500.
        assert service.balance_as_of(events, date(2026, 8, 24)) == Decimal("1400")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_each_account_is_corrected_by_its_own_measured_level():
    """
    Both accounts measured, and neither level may be applied to the other. A single
    `cash_balances` row per date used to be enforced by a unique constraint on
    `report_date` alone, so this shape could not even be stored.
    """
    engine, session = await _make_session()
    try:
        eur = BaseFx("EUR", {})
        session.add_all([
            CashFlow(ib_key="ibkr-d", flow_date=date(2026, 8, 20),
                     flow_type=DEPOSIT_WITHDRAW, amount=Decimal("100"),
                     currency="EUR", amount_eur=Decimal("100"), account=IBKR),
            CashFlow(ib_key="fp-d", flow_date=date(2026, 8, 20),
                     flow_type=DEPOSIT_WITHDRAW, amount=Decimal("100"),
                     currency="EUR", amount_eur=Decimal("100"), account=PILLAR3A),
            CashBalance(report_date=date(2026, 8, 25), currency="EUR",
                        cash=Decimal("1000"), account=IBKR),
            CashBalance(report_date=date(2026, 8, 25), currency="EUR",
                        cash=Decimal("2000"), account=PILLAR3A),
        ])
        await session.flush()

        service = CashService(session)
        events = await service.balance_events(eur)
        assert service.balance_as_of(events, date(2026, 8, 25)) == Decimal("3000")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cash_source_is_mixed_when_one_account_cannot_be_measured():
    """
    `MEASURED` means *read from the broker*. A pillar 3a balance can never be one, so a
    total that is half derived must not be badged `ibkr` — the same provenance overclaim
    that `derived_source()` was split out to avoid, arriving by a new route.
    """
    engine, session = await _make_session()
    try:
        session.add_all([
            CashFlow(ib_key="ibkr-d", flow_date=date(2026, 8, 20),
                     flow_type=DEPOSIT_WITHDRAW, amount=Decimal("100"),
                     currency="EUR", amount_eur=Decimal("100"), account=IBKR),
            CashBalance(report_date=date(2026, 8, 25), currency="EUR",
                        cash=Decimal("100"), account=IBKR),
        ])
        await session.flush()
        assert await CashService(session).cash_source() == MEASURED

        session.add(CashFlow(
            ib_key="fp-d", flow_date=date(2026, 8, 21), flow_type=DEPOSIT_WITHDRAW,
            amount=Decimal("500"), currency="EUR", amount_eur=Decimal("500"),
            account=PILLAR3A,
        ))
        await session.flush()
        assert await CashService(session).cash_source() == MIXED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_pillar3a_only_balance_is_derived_not_measured():
    """No measured rows anywhere means the old two-way answer still applies."""
    engine, session = await _make_session()
    try:
        session.add(CashFlow(
            ib_key="fp-d", flow_date=date(2026, 8, 21), flow_type=DEPOSIT_WITHDRAW,
            amount=Decimal("500"), currency="EUR", amount_eur=Decimal("500"),
            account=PILLAR3A,
        ))
        await session.flush()
        assert await CashService(session).cash_source() == DERIVED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_measured_era_reads_mixed_when_a_second_account_is_only_derived():
    """
    The timeline stamped a literal `ibkr` on every point after the first measured
    balance, while `CashService.cash_source()` — read by the summary card and the
    allocation tab — says `mixed` once a second account holds cash IBKR cannot see.
    Live on production: 14 tail points `ibkr`, the card `mixed`, and the chart's
    caveat gone because it reads the last point. One balance, two provenance claims.
    """
    from app.accounts import PILLAR3A

    engine, session = await _make_session()
    try:
        session.add(_flow(date(2026, 1, 9), "1000", "D1"))
        other = _flow(date(2026, 1, 10), "100", "P1")
        other.account = PILLAR3A                     # derived-only, IBKR never sees it
        session.add(other)
        session.add(MarketPrice(
            security_id=1, date=date(2026, 1, 12), close_price=Decimal("10"),
            currency="EUR", source="test",
        ))
        session.add(_lot(date(2026, 1, 9), "500"))
        session.add(CashBalance(
            report_date=date(2026, 1, 13), currency="EUR", cash=Decimal("500"),
        ))
        await session.flush()

        verdict = await CashService(session).cash_source()
        assert verdict == MIXED

        timeline = await PortfolioService(session).get_portfolio_value_over_time(
            date(2026, 1, 12), date(2026, 1, 14)
        )
        by_date = {r["date"]: r for r in timeline}
        assert by_date["2026-01-12"]["cash_source"] == DERIVED
        assert by_date["2026-01-13"]["cash_source"] == verdict
        assert by_date["2026-01-14"]["cash_source"] == verdict
    finally:
        await engine.dispose()
