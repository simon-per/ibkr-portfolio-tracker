"""
The activity ledger: trades, dividends, cash flows and corporate actions unioned.

The behaviours worth pinning are the ones that carry meaning beyond "it returns rows":
the deposit-vs-transfer distinction the money-added figure hinges on, the pay-date
windowing that keeps the two dividend sources comparable, and the fact that paging
happens after the union rather than per table.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.app_settings import AppSetting
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW, TRANSFER_IN
from app.models.corporate_action import CorporateAction
from app.models.dividend_payment import DividendPayment
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.models.trade import Trade
from app.services.activity_service import (
    ActivityService,
    CASH_FLOW,
    CORPORATE_ACTION,
    DIVIDEND,
    MAX_LIMIT,
    TRADE,
)

TODAY = date(2026, 7, 31)
WINDOW_START = TODAY - timedelta(days=365)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    yield session
    await session.close()
    await engine.dispose()


async def seed(session: AsyncSession, base_currency: str = "EUR") -> None:
    session.add(AppSetting(key="base_currency", value=base_currency))

    # `trades` and `corporate_actions` store money in the trade's OWN currency — there
    # is no _eur column on either — so every non-EUR row needs a native->EUR rate before
    # the base-currency projection. A flat 0.5 makes the two steps individually visible
    # in the assertions below.
    d = WINDOW_START - timedelta(days=5)
    while d <= TODAY:
        session.add(ExchangeRate(date=d, from_currency="USD", to_currency="EUR",
                                 rate=Decimal("0.5"), source="test"))
        d += timedelta(days=1)
    session.add(Security(id=1, isin="US0378331005", symbol="AAPL", description="Apple",
                         currency="USD", conid=103, asset_category="STK", exchange="NASDAQ"))
    session.add(Security(id=2, isin="NL0010273215", symbol="ASML", description="ASML",
                         currency="EUR", conid=101, asset_category="STK", exchange="AEB"))

    session.add(Trade(
        ib_key="T-BUY", conid="103", security_id=1, symbol="AAPL",
        trade_date=TODAY - timedelta(days=100), buy_sell="BUY",
        quantity=Decimal("10"), price=Decimal("100"), proceeds=Decimal("-1000"),
        commission=Decimal("-1"), currency="USD", realized_pnl=None,
    ))
    session.add(Trade(
        ib_key="T-SELL", conid="103", security_id=1, symbol="AAPL",
        trade_date=TODAY - timedelta(days=40), buy_sell="SELL",
        quantity=Decimal("-10"), price=Decimal("120"), proceeds=Decimal("1200"),
        commission=Decimal("-1"), currency="USD", realized_pnl=Decimal("199"),
    ))
    # A fully-sold security leaves OpenPositions, so security_id can be NULL.
    session.add(Trade(
        ib_key="T-ORPHAN", conid="999", security_id=None, symbol="GONE",
        trade_date=TODAY - timedelta(days=30), buy_sell="SELL",
        quantity=Decimal("-5"), price=Decimal("10"), proceeds=Decimal("50"),
        commission=Decimal("-1"), currency="EUR", realized_pnl=Decimal("5"),
    ))

    session.add(CashFlow(
        ib_key="CF-DEP", flow_date=TODAY - timedelta(days=90),
        flow_type=DEPOSIT_WITHDRAW, amount=Decimal("2000"),
        amount_eur=Decimal("2000"), currency="EUR", description="deposit",
    ))
    session.add(CashFlow(
        ib_key="CF-WDR", flow_date=TODAY - timedelta(days=60),
        flow_type=DEPOSIT_WITHDRAW, amount=Decimal("-300"),
        amount_eur=Decimal("-300"), currency="EUR", description="withdrawal",
    ))
    # In-kind: no cash, and never money in.
    session.add(CashFlow(
        ib_key="CF-XFER", flow_date=TODAY - timedelta(days=200),
        flow_type=TRANSFER_IN, amount=Decimal("0"),
        amount_eur=Decimal("0"), currency="EUR", description="broker transfer",
    ))

    session.add(CorporateAction(
        ib_key="CA-1", conid="101", security_id=2, symbol="ASML",
        action_date=TODAY - timedelta(days=70), action_type="FORWARDSPLIT",
        quantity=Decimal("10"), value=None, proceeds=None, currency="EUR",
        description="ASML(NL0010273215) SPLIT 2 FOR 1",
    ))

    session.add(DividendPayment(
        security_id=2, ex_date=TODAY - timedelta(days=80),
        pay_date=TODAY - timedelta(days=50), shares_held=Decimal("10"),
        gross_amount_eur=Decimal("12"), withholding_tax_eur=Decimal("2"),
        net_amount_eur=Decimal("10"), currency="EUR", source="ibkr",
    ))
    # Pre-ownership zero row from yfinance's full history — must not appear.
    session.add(DividendPayment(
        security_id=2, ex_date=date(1999, 4, 4), pay_date=date(1999, 4, 4),
        shares_held=Decimal("0"), gross_amount_eur=Decimal("0"),
        withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("0"),
        currency="EUR", source="yfinance_estimate",
    ))
    await session.flush()
    await session.commit()


async def fetch(session, **kwargs):
    kwargs.setdefault("start_date", WINDOW_START)
    kwargs.setdefault("end_date", TODAY)
    return await ActivityService(session).get_activity(**kwargs)


@pytest.mark.asyncio
async def test_all_four_sources_appear_in_one_list(db):
    await seed(db)
    result = await fetch(db)

    kinds = {row["kind"] for row in result["items"]}
    assert kinds == {TRADE, DIVIDEND, CASH_FLOW, CORPORATE_ACTION}


@pytest.mark.asyncio
async def test_rows_are_newest_first(db):
    await seed(db)
    dates = [row["date"] for row in (await fetch(db))["items"]]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.asyncio
async def test_a_transfer_is_shown_but_never_counts_as_money_in(db):
    """
    The audit CLAUDE.md prescribes before trusting any money-added figure, which was
    only runnable as `manage_cash_flows list` over ssh. An unexcluded transfer shows a
    portfolio-sized fake contribution, so the flag has to be on the row.
    """
    await seed(db)
    flows = {r["ib_key"]: r for r in (await fetch(db))["items"] if r["kind"] == CASH_FLOW}

    assert flows["CF-XFER"]["counts_as_money_in"] is False
    assert flows["CF-DEP"]["counts_as_money_in"] is True
    # A withdrawal is still a DEPOSITWITHDRAW row; the sign is what distinguishes it.
    assert flows["CF-WDR"]["counts_as_money_in"] is True
    assert flows["CF-WDR"]["amount_base"] == -300.0


@pytest.mark.asyncio
async def test_only_cash_flows_carry_the_money_in_flag(db):
    """None, not False: a trade is not a non-deposit, the question does not apply."""
    await seed(db)
    for row in (await fetch(db))["items"]:
        if row["kind"] != CASH_FLOW:
            assert row["counts_as_money_in"] is None


@pytest.mark.asyncio
async def test_a_trades_amount_is_its_net_cash_effect(db):
    await seed(db)
    trades = {r["ib_key"]: r for r in (await fetch(db))["items"] if r["kind"] == TRADE}

    # proceeds already carry IBKR's sign, commission is separately negative — and both
    # are USD here, so they convert at 0.5 before the (identity) EUR base projection.
    assert trades["T-BUY"]["amount_base"] == pytest.approx(-1001.0 * 0.5)
    assert trades["T-SELL"]["amount_base"] == pytest.approx(1199.0 * 0.5)
    assert trades["T-SELL"]["realized_pnl_base"] == pytest.approx(199.0 * 0.5)
    # A buy realizes nothing; that is absent, not zero.
    assert trades["T-BUY"]["realized_pnl_base"] is None
    # A EUR trade needs no conversion at all.
    assert trades["T-ORPHAN"]["amount_base"] == pytest.approx(49.0)


@pytest.mark.asyncio
async def test_a_native_currency_amount_is_converted_before_it_is_projected(db):
    """
    The bug this caught in production. `trades.proceeds` and `realized_pnl` are in the
    trade's OWN currency — unlike `cash_flows.amount_eur`, which ingest pre-converts —
    so applying only the EUR->base factor both skips native->EUR and then scales a
    number that was never EUR. A CAD 30.27 realized gain was reported as CHF 27.85 (the
    EUR->CHF factor) instead of roughly CHF 18.

    Two independent factors here, so a single-step conversion cannot pass by accident:
    USD->EUR at 0.5 and EUR->CHF at 2.0.
    """
    await seed(db, base_currency="CHF")
    d = WINDOW_START - timedelta(days=5)
    while d <= TODAY:
        db.add(ExchangeRate(date=d, from_currency="EUR", to_currency="CHF",
                            rate=Decimal("2.0"), source="test"))
        d += timedelta(days=1)
    await db.commit()

    trades = {r["ib_key"]: r for r in (await fetch(db))["items"] if r["kind"] == TRADE}

    # 1199 USD -> 599.5 EUR -> 1199 CHF. Numerically back where it started, which is
    # exactly why the factors were chosen: the buggy one-step path gives 2398.
    assert trades["T-SELL"]["amount_base"] == pytest.approx(1199.0)
    assert trades["T-SELL"]["realized_pnl_base"] == pytest.approx(199.0)
    # The EUR trade only takes the second step.
    assert trades["T-ORPHAN"]["amount_base"] == pytest.approx(49.0 * 2.0)


@pytest.mark.asyncio
async def test_an_unconvertible_amount_is_blank_rather_than_mis_scaled(db):
    """
    No rate for the pair: the row still appears with the figure absent. Dropping it
    would hide a trade from the ledger, and passing the native number through would
    report a TWD figure as CHF — the shape that once made a sale read ~35x high.
    """
    await seed(db)
    db.add(Trade(
        ib_key="T-TWD", conid="777", security_id=None, symbol="2330",
        trade_date=TODAY - timedelta(days=10), buy_sell="SELL",
        quantity=Decimal("-12"), price=Decimal("1000"), proceeds=Decimal("12000"),
        commission=Decimal("-20"), currency="TWD", realized_pnl=Decimal("500"),
    ))
    await db.commit()

    row = next(r for r in (await fetch(db))["items"] if r["ib_key"] == "T-TWD")
    assert row["amount_base"] is None
    assert row["realized_pnl_base"] is None
    # But the non-money columns still carry what is known.
    assert row["quantity"] == -12.0 and row["currency"] == "TWD"


@pytest.mark.asyncio
async def test_a_buy_realizes_nothing_and_says_so(db):
    """
    IBKR sends fifoPnlRealized=0 on every BUY. Rendering that as 0.00 asserts a realized
    result where there is none — the same rule that keeps a corporate action's price
    blank. On this account it was 67 rows of noise against 4 real ones.
    """
    await seed(db)
    trades = [r for r in (await fetch(db))["items"] if r["kind"] == TRADE]

    buys = [r for r in trades if r["subtype"] == "BUY"]
    assert buys and all(r["realized_pnl_base"] is None for r in buys)
    assert any(r["realized_pnl_base"] is not None for r in trades if r["subtype"] == "SELL")


@pytest.mark.asyncio
async def test_a_sell_with_no_surviving_security_still_appears(db):
    """security_id is NULL once a holding is fully sold — the row must not vanish."""
    await seed(db)
    keys = {r["ib_key"] for r in (await fetch(db))["items"]}
    assert "T-ORPHAN" in keys


@pytest.mark.asyncio
async def test_a_dividend_is_dated_by_when_the_cash_moved(db):
    """
    yfinance stores under the ex-date and IBKR under the pay date. A ledger is about
    when money moved, so pay_date wins — 30 days apart in the fixture, which is more
    than a monthly payer's whole cycle.
    """
    await seed(db)
    dividends = [r for r in (await fetch(db))["items"] if r["kind"] == DIVIDEND]

    assert len(dividends) == 1
    assert dividends[0]["date"] == (TODAY - timedelta(days=50)).isoformat()
    # Net, not gross: the withholding was really deducted.
    assert dividends[0]["amount_base"] == 10.0
    assert dividends[0]["source"] == "ibkr"


@pytest.mark.asyncio
async def test_zero_dividend_rows_are_excluded(db):
    """
    yfinance returns a security's whole history — 1355 of 1446 rows on this account
    were zero rows reaching back to 1985. Both dividend readers filter them; so does this.
    """
    await seed(db)
    result = await ActivityService(db).get_activity(
        start_date=date(1990, 1, 1), end_date=TODAY,
    )
    assert all(r["date"][:4] != "1999" for r in result["items"])


@pytest.mark.asyncio
async def test_a_corporate_action_asserts_no_price_it_does_not_have(db):
    await seed(db)
    action = next(r for r in (await fetch(db))["items"] if r["kind"] == CORPORATE_ACTION)

    assert action["price"] is None
    assert action["amount_base"] is None      # a split moves no cash
    assert action["quantity"] == 10.0
    assert "SPLIT 2 FOR 1" in action["description"]


@pytest.mark.asyncio
async def test_the_kind_filter_narrows_the_union(db):
    await seed(db)
    result = await fetch(db, kinds=[TRADE])
    assert {r["kind"] for r in result["items"]} == {TRADE}
    assert result["total"] == 3


@pytest.mark.asyncio
async def test_several_kinds_can_be_combined(db):
    await seed(db)
    result = await fetch(db, kinds=[TRADE, CASH_FLOW])
    assert {r["kind"] for r in result["items"]} == {TRADE, CASH_FLOW}


@pytest.mark.asyncio
async def test_the_symbol_filter_is_exact_and_case_insensitive(db):
    await seed(db)
    assert {r["symbol"] for r in (await fetch(db, symbol="aapl"))["items"]} == {"AAPL"}
    # Not a prefix match: AAP must not return AAPL.
    assert (await fetch(db, symbol="AAP"))["total"] == 0


@pytest.mark.asyncio
async def test_the_window_excludes_what_falls_outside_it(db):
    await seed(db)
    # The transfer sits 200 days back; a 100-day window must not reach it.
    result = await fetch(db, start_date=TODAY - timedelta(days=100))
    assert "CF-XFER" not in {r["ib_key"] for r in result["items"]}

    wide = await fetch(db, start_date=TODAY - timedelta(days=300))
    assert "CF-XFER" in {r["ib_key"] for r in wide["items"]}


@pytest.mark.asyncio
async def test_paging_happens_after_the_union_not_per_table(db):
    """
    The reason limits are applied to the merged list: the four sources are separately
    ordered, so a per-table limit would silently drop every dividend in a busy trading
    month. A page of 1 must therefore be the single newest event overall.
    """
    await seed(db)
    everything = (await fetch(db))["items"]

    first = await fetch(db, limit=1)
    assert first["items"] == everything[:1]
    assert first["total"] == len(everything)

    second = await fetch(db, limit=1, offset=1)
    assert second["items"] == everything[1:2]


@pytest.mark.asyncio
async def test_paging_covers_every_row_exactly_once(db):
    await seed(db)
    total = (await fetch(db))["total"]

    seen = []
    for offset in range(0, total, 2):
        seen.extend(r["ib_key"] for r in (await fetch(db, limit=2, offset=offset))["items"])

    assert len(seen) == total
    assert len(set(seen)) == total


@pytest.mark.asyncio
async def test_an_oversized_limit_is_clamped(db):
    await seed(db)
    assert (await fetch(db, limit=99_999))["limit"] == MAX_LIMIT


@pytest.mark.asyncio
async def test_empty_tables_yield_an_empty_ledger_not_an_error(db):
    db.add(AppSetting(key="base_currency", value="EUR"))
    await db.commit()

    result = await fetch(db)
    assert result["items"] == []
    assert result["total"] == 0
    assert result["base_currency"] == "EUR"


@pytest.mark.asyncio
async def test_amounts_are_projected_into_the_base_currency(db):
    """
    Production runs CHF while the tests otherwise run EUR, and each row converts at
    **its own date** — a two-year-old trade must not be restated at today's rate.
    """
    await seed(db, base_currency="CHF")
    d = WINDOW_START - timedelta(days=5)
    while d <= TODAY:
        # A deliberately moving rate: a single factor would hide a date mix-up.
        rate = "0.90" if d < TODAY - timedelta(days=50) else "1.10"
        db.add(ExchangeRate(date=d, from_currency="EUR", to_currency="CHF",
                            rate=Decimal(rate), source="test"))
        d += timedelta(days=1)
    await db.commit()

    rows = {r["ib_key"]: r for r in (await fetch(db))["items"]}
    # The deposit sits 90 days back, in the 0.90 era. amount_eur is pre-converted at
    # ingest, so this one takes the EUR->base step only.
    assert rows["CF-DEP"]["amount_base"] == pytest.approx(2000 * 0.90)
    # The sale sits 40 days back, in the 1.10 era, and is USD — both steps.
    assert rows["T-SELL"]["amount_base"] == pytest.approx(1199 * 0.5 * 1.10)
    assert rows["T-SELL"]["realized_pnl_base"] == pytest.approx(199 * 0.5 * 1.10)
    # The 200-day-old transfer picks up the older rate, not today's.
    assert rows["CF-XFER"]["amount_base"] == pytest.approx(0)


# ── CSV ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_csv_has_a_header_and_a_row_per_item(db):
    await seed(db)
    result = await fetch(db)
    lines = ActivityService.to_csv(result).strip().split("\n")

    assert lines[0].startswith("date,kind,subtype,symbol,description")
    assert len(lines) == len(result["items"]) + 1


@pytest.mark.asyncio
async def test_csv_quotes_a_description_containing_a_comma(db):
    """
    IBKR's actionDescription contains commas routinely. Hand-rolled joining would
    corrupt exactly the rows that matter most, so this goes through csv.writer.
    """
    await seed(db)
    result = await fetch(db, kinds=[CORPORATE_ACTION])
    result["items"][0]["description"] = 'SPLIT 2 FOR 1, ex "old" shares'

    line = ActivityService.to_csv(result).strip().split("\n")[1]
    assert '"SPLIT 2 FOR 1, ex ""old"" shares"' in line


@pytest.mark.asyncio
async def test_csv_leaves_an_inapplicable_figure_blank(db):
    """Not 0.00 — a corporate action has no price, and a zero would assert one."""
    await seed(db)
    result = await fetch(db, kinds=[CORPORATE_ACTION])
    fields = ActivityService.to_csv(result).strip().split("\n")[1].split(",")

    header = ActivityService.to_csv(result).strip().split("\n")[0].split(",")
    assert fields[header.index("price")] == ""


@pytest.mark.asyncio
async def test_csv_names_the_base_currency_in_its_amount_columns(db):
    await seed(db, base_currency="CHF")
    # The app reports in CHF only while an EUR->CHF rate exists (`get_base_currency`).
    db.add(ExchangeRate(date=TODAY, from_currency="EUR", to_currency="CHF",
                        rate=Decimal("0.94"), source="test"))
    await db.commit()
    header = ActivityService.to_csv(await fetch(db)).split("\n")[0]
    assert "amount_chf" in header


@pytest.mark.asyncio
async def test_a_fractional_share_count_is_not_rounded_away(db):
    """
    The column is Numeric(18, 6) and this account trades fractional shares constantly
    (0.3 MU, 0.1 CSU), so `str(Decimal)` pads every description to six decimals and
    rounding the quantity to whole shares renders the real trades as 0 — or, for a
    sell, as -0.
    """
    await seed(db)
    db.add(Trade(
        ib_key="T-FRAC", conid="103", security_id=1, symbol="AAPL",
        trade_date=TODAY - timedelta(days=5), buy_sell="SELL",
        quantity=Decimal("-0.100000"), price=Decimal("2742"),
        proceeds=Decimal("274.2"), commission=Decimal("-1"), currency="USD",
        realized_pnl=Decimal("27.85"),
    ))
    await db.commit()

    row = next(r for r in (await fetch(db))["items"] if r["ib_key"] == "T-FRAC")
    assert row["quantity"] == -0.1
    # The padding is gone, and the whole-share rows stay whole.
    assert row["description"] == "SELL 0.1 AAPL"
    whole = next(r for r in (await fetch(db))["items"] if r["ib_key"] == "T-BUY")
    assert whole["description"] == "BUY 10 AAPL"


@pytest.mark.asyncio
async def test_the_fx_rate_is_looked_up_once_per_currency_and_date(db):
    """
    `get_exchange_rate` queries the rate table on every call and, on a miss, reaches
    Frankfurter — so a 500-row page would issue up to 500 queries and, cold, 500
    provider requests. Rows cluster on a handful of pairs, so the memo has to collapse
    them.
    """
    await seed(db)
    # Several USD trades on ONE date: one lookup, not one per row.
    for i in range(5):
        db.add(Trade(
            ib_key=f"T-SAMEDAY-{i}", conid="103", security_id=1, symbol="AAPL",
            trade_date=TODAY - timedelta(days=7), buy_sell="BUY",
            quantity=Decimal("1"), price=Decimal("100"), proceeds=Decimal("-100"),
            commission=Decimal("-1"), currency="USD", realized_pnl=None,
        ))
    await db.commit()

    service = ActivityService(db)
    calls = []
    real = service.currency_service.get_exchange_rate

    async def counting(from_currency, target_date, to_currency="EUR"):
        calls.append((from_currency, target_date))
        return await real(from_currency, target_date, to_currency)

    service.currency_service.get_exchange_rate = counting
    result = await service.get_activity(start_date=WINDOW_START, end_date=TODAY)

    assert result["total"] > 5
    # One per distinct (currency, date), and never for the EUR rows.
    assert len(calls) == len(set(calls))
    assert all(c[0] != "EUR" for c in calls)
    same_day = [c for c in calls if c[1] == TODAY - timedelta(days=7)]
    assert len(same_day) == 1, f"5 trades on one date caused {len(same_day)} lookups"


@pytest.mark.asyncio
async def test_a_missing_rate_is_memoized_too(db):
    """Otherwise every unconvertible row retries the provider that just failed."""
    await seed(db)
    for i in range(3):
        db.add(Trade(
            ib_key=f"T-TWD-{i}", conid="777", security_id=None, symbol="2330",
            trade_date=TODAY - timedelta(days=9), buy_sell="BUY",
            quantity=Decimal("1"), price=Decimal("1000"), proceeds=Decimal("-1000"),
            commission=Decimal("-1"), currency="TWD", realized_pnl=None,
        ))
    await db.commit()

    service = ActivityService(db)
    calls = []
    real = service.currency_service.get_exchange_rate

    async def counting(from_currency, target_date, to_currency="EUR"):
        calls.append((from_currency, target_date))
        return await real(from_currency, target_date, to_currency)

    service.currency_service.get_exchange_rate = counting
    result = await service.get_activity(start_date=WINDOW_START, end_date=TODAY)

    twd = [r for r in result["items"] if r["currency"] == "TWD"]
    assert len(twd) == 3 and all(r["amount_base"] is None for r in twd)
    assert len([c for c in calls if c[0] == "TWD"]) == 1


# ---------------------------------------------------------------------------
# The era splice. The same dividend is stored twice — yfinance under its ex-date and
# IBKR under its pay date, a week or two apart — and every reader that COMPUTES a figure
# drops the superseded estimate (`DividendService._splice_by_era`). The ledger only
# DISPLAYS, and it had adopted one of the readers' two rules (the income test) while
# missing this one, so it listed both rows and overstated dividend income by 72% on the
# real account: 31 duplicated rows, 47 of 113 CHF.
# ---------------------------------------------------------------------------

IBKR_ERA_START = TODAY - timedelta(days=120)


async def _seed_both_eras(session: AsyncSession) -> None:
    """One dividend paid twice over: the estimate under its ex-date, IBKR under its pay
    date. Plus an estimate from before the ledger existed, which must survive."""
    session.add(AppSetting(key="base_currency", value="EUR"))
    session.add(Security(id=1, isin="US02079K3059", symbol="GOOGL", description="Alphabet",
                         currency="EUR", conid=100, asset_category="STK", exchange="NASDAQ"))
    # The era boundary: the first IBKR row anywhere in the table.
    session.add(DividendPayment(
        security_id=1, ex_date=IBKR_ERA_START, pay_date=IBKR_ERA_START,
        shares_held=Decimal("0"), gross_amount_eur=Decimal("12"),
        withholding_tax_eur=Decimal("2"), net_amount_eur=Decimal("10"),
        currency="EUR", source="ibkr",
    ))
    # A later quarter, present as BOTH: estimate on the ex-date, IBKR on the pay date.
    ex, pay = TODAY - timedelta(days=40), TODAY - timedelta(days=26)
    session.add(DividendPayment(
        security_id=1, ex_date=ex, pay_date=ex, shares_held=Decimal("10"),
        amount_per_share=Decimal("1.3"), gross_amount_eur=Decimal("13"),
        withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("13"),
        currency="EUR", source="yfinance_estimate",
    ))
    session.add(DividendPayment(
        security_id=1, ex_date=ex, pay_date=pay, shares_held=Decimal("0"),
        gross_amount_eur=Decimal("12"), withholding_tax_eur=Decimal("2"),
        net_amount_eur=Decimal("10"), currency="EUR", source="ibkr",
    ))
    # Before the ledger reached back to: the only source for that era.
    session.add(DividendPayment(
        security_id=1, ex_date=IBKR_ERA_START - timedelta(days=90),
        pay_date=IBKR_ERA_START - timedelta(days=90), shares_held=Decimal("8"),
        amount_per_share=Decimal("1.1"), gross_amount_eur=Decimal("9"),
        withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("9"),
        currency="EUR", source="yfinance_estimate",
    ))
    await session.flush()
    await session.commit()


@pytest.mark.asyncio
async def test_the_ledger_does_not_show_both_eras_for_the_same_dividend(db):
    await _seed_both_eras(db)
    result = await ActivityService(db).get_activity(
        start_date=WINDOW_START, end_date=TODAY, kinds=[DIVIDEND], limit=MAX_LIMIT,
    )
    divs = result["items"]
    est = [r for r in divs if r["source"] == "yfinance_estimate"]
    ibkr = [r for r in divs if r["source"] == "ibkr"]

    # The duplicate is gone: no estimate on or after the boundary.
    assert not [r for r in est if r["date"] >= IBKR_ERA_START.isoformat()], est
    # Both IBKR rows survive, and so does the estimate that predates the ledger — the
    # mirror-image bug is dropping those, which once blanked every pre-IBKR month.
    assert len(ibkr) == 2
    assert len(est) == 1
    assert est[0]["date"] < IBKR_ERA_START.isoformat()
    # ...still badged, because it really is a gross guess with no withholding.
    assert est[0]["source"] == "yfinance_estimate"


@pytest.mark.asyncio
async def test_the_boundary_comes_from_the_whole_history_not_the_window(db):
    """
    The trap the obvious form of this fix falls into. `_splice_by_era` derives the
    boundary from the rows handed to it, so `_splice_by_era(get_between(...))` would
    recompute it from the window — and a window opening after the era began would treat
    its own first IBKR row as the start and resurrect superseded estimates.

    Here the window starts AFTER the true boundary, so the estimate inside it must still
    be dropped even though the window contains a later IBKR row.
    """
    await _seed_both_eras(db)
    late_start = TODAY - timedelta(days=45)
    result = await ActivityService(db).get_activity(
        start_date=late_start, end_date=TODAY, kinds=[DIVIDEND], limit=MAX_LIMIT,
    )
    assert not [r for r in result["items"] if r["source"] == "yfinance_estimate"], result["items"]
    assert [r["source"] for r in result["items"]] == ["ibkr"]


@pytest.mark.asyncio
async def test_the_ledger_dividend_total_agrees_with_the_dividends_reader(db):
    """
    Two readers of one table, and the anti-divergence test worth keeping: the ledger's
    dividend rows must sum to what `DividendService` reports as received. They differed
    by 72% in production precisely because nothing compared them.
    """
    from app.services.dividend_service import DividendService

    await _seed_both_eras(db)
    ledger = await ActivityService(db).get_activity(
        start_date=WINDOW_START, end_date=TODAY, kinds=[DIVIDEND], limit=MAX_LIMIT,
    )
    ledger_total = sum(Decimal(str(r["amount_base"] or 0)) for r in ledger["items"])

    spliced, _ = DividendService._splice_by_era(
        await __import__(
            "app.repositories.dividend_repository", fromlist=["DividendRepository"]
        ).DividendRepository(db).get_computed_dividends()
    )
    # Every reader's net since issue #61: an estimate's is its gross less the ladder
    # withholding, so the ledger lists the amount the Dividends tab counts.
    income = await DividendService(db).income_split()
    reader_total = sum(
        income.net(p)
        for p in spliced
        if WINDOW_START <= (p.pay_date or p.ex_date) <= TODAY
    )
    assert ledger_total == reader_total
    assert any(p.source != "ibkr" for p in spliced)


@pytest.mark.asyncio
async def test_the_repository_boundary_agrees_with_the_splice_helper(db):
    """
    Two ways of finding the same era boundary, and the ledger depends on them agreeing.

    `_splice_by_era` takes `min(pay_date or ex_date)` over the IBKR rows it is given, and
    every reader hands it rows **before** the income filter runs (`dividend_service`
    splices then filters, in both readers). `earliest_ibkr_payment_date()` must therefore
    also count IBKR rows regardless of income — add an income filter to one side only and
    a zero-value IBKR row would move one boundary and not the other, dropping estimates
    that no real IBKR income replaces.

    Asserted over data that contains exactly that trap: an income-free IBKR row dated
    before any paying one.
    """
    from app.repositories.dividend_repository import DividendRepository
    from app.services.dividend_service import DividendService

    await _seed_both_eras(db)
    # The trap: an IBKR row carrying nothing, earlier than every paying IBKR row.
    db.add(DividendPayment(
        security_id=1, ex_date=IBKR_ERA_START - timedelta(days=7),
        pay_date=IBKR_ERA_START - timedelta(days=7), shares_held=Decimal("0"),
        gross_amount_eur=Decimal("0"), withholding_tax_eur=Decimal("0"),
        net_amount_eur=Decimal("0"), currency="EUR", source="ibkr",
    ))
    await db.flush()
    await db.commit()

    repo = DividendRepository(db)
    from_repo = await repo.earliest_ibkr_payment_date()
    _, from_helper = DividendService._splice_by_era(await repo.get_computed_dividends())

    assert from_repo == from_helper == IBKR_ERA_START - timedelta(days=7)


# ── The ledger follows the splice, including the parts added after it ──────────

async def _seed_boundary_pair(session: AsyncSession) -> None:
    """
    The production shape: ASML's dividend recorded twice, an estimate under its
    ex-date and the IBKR actual under its pay-date nine days later, with the IBKR
    row being the first of the era.
    """
    session.add(AppSetting(key="base_currency", value="EUR"))
    session.add(Security(
        id=1, isin="NL0010273215", symbol="ASML", description="ASML Holding",
        currency="EUR", conid=9001, asset_category="STK", exchange="AEB",
    ))
    # The same dividend, seen twice.
    session.add(DividendPayment(
        security_id=1, ex_date=date(2026, 2, 9), pay_date=date(2026, 2, 9), currency="EUR",
        shares_held=Decimal("10"), gross_amount_eur=Decimal("14.60"),
        withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("14.60"),
        source="yfinance_estimate",
    ))
    session.add(DividendPayment(
        # IBKR rows carry the PAY date in ex_date too — the column holds an ex-date
        # for yfinance rows and the pay date for IBKR ones (see CLAUDE.md on the
        # (security_id, source, ex_date) identity).
        security_id=1, ex_date=date(2026, 2, 18), pay_date=date(2026, 2, 18), currency="EUR",
        shares_held=Decimal("10"), gross_amount_eur=Decimal("12.00"),
        withholding_tax_eur=Decimal("1.80"), net_amount_eur=Decimal("10.20"),
        source="ibkr",
    ))
    # Genuine earlier income, well outside the lag window — must survive.
    session.add(DividendPayment(
        security_id=1, ex_date=date(2025, 11, 5), pay_date=date(2025, 11, 5), currency="EUR",
        shares_held=Decimal("10"), gross_amount_eur=Decimal("9.00"),
        withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("9.00"),
        source="yfinance_estimate",
    ))
    await session.commit()


@pytest.mark.asyncio
async def test_the_ledger_drops_the_boundary_duplicate_like_every_other_reader(db):
    await _seed_boundary_pair(db)

    result = await ActivityService(db).get_activity(
        start_date=date(2026, 1, 1), end_date=date(2026, 3, 31), kinds=[DIVIDEND],
    )
    dates = sorted(r["date"] for r in result["items"])

    assert dates == ["2026-02-18"], (
        "the ledger still shows the estimate its IBKR twin supersedes — it reimplemented "
        "the boundary rule instead of calling the shared helper, so it did not inherit "
        "the duplicate match"
    )


@pytest.mark.asyncio
async def test_a_window_that_excludes_the_ibkr_twin_still_drops_the_estimate(db):
    """
    The reason the fetch is widened. Asking for 1-15 February contains the estimate
    but not the IBKR row it duplicates, so a naive implementation has nothing to match
    against and shows the superseded estimate as income.
    """
    await _seed_boundary_pair(db)

    result = await ActivityService(db).get_activity(
        start_date=date(2026, 2, 1), end_date=date(2026, 2, 15), kinds=[DIVIDEND],
    )

    assert result["items"] == [], (
        "a narrow window resurrected the duplicate because its IBKR twin fell outside it"
    )


@pytest.mark.asyncio
async def test_genuine_pre_era_income_is_still_listed(db):
    """The mirror-image bug: dropping real pre-IBKR months once blanked the card."""
    await _seed_boundary_pair(db)

    result = await ActivityService(db).get_activity(
        start_date=date(2025, 1, 1), end_date=date(2026, 3, 31), kinds=[DIVIDEND],
    )
    dates = sorted(r["date"] for r in result["items"])

    assert dates == ["2025-11-05", "2026-02-18"]
    estimate = next(r for r in result["items"] if r["date"] == "2025-11-05")
    assert estimate["subtype"] == "yfinance_estimate", "the badge must survive the splice"


@pytest.mark.asyncio
async def test_the_ledger_total_matches_the_breakdown_after_the_dedup(db):
    """
    The anti-divergence check, re-run against the corrected rule. Two readers of one
    table that nothing compares is how the 72% overstatement got there, and comparing
    them is also what would have caught the boundary pair — had both been wrong in the
    same direction, which they now are not.
    """
    from app.services.dividend_service import DividendService

    await _seed_boundary_pair(db)
    span = dict(start_date=date(2025, 1, 1), end_date=date(2026, 12, 31))

    ledger = await ActivityService(db).get_activity(kinds=[DIVIDEND], **span)
    ledger_total = sum(r["amount_base"] for r in ledger["items"])

    breakdown = await DividendService(db).get_dividend_breakdown(year=None, include_forecast=False)
    assert ledger_total == pytest.approx(float(breakdown["total_net_eur"]), abs=0.01)
