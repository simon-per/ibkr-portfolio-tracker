"""
What one summary, one closed-positions read and one decomposition cost in queries, and
proof that making them cheaper moved no figure (GitHub #47).

Before the fix, against the fixture below (EUR base and rows, a measured cash balance,
counted with a cursor listener):

    summary, 1 / 20 spinoff actions:   14 / 33 queries (trades x3, cash_balances x3,
                                       cash_flows x2, one SUM per corporate action)
    closed positions, 5 / 50 sold:     16 / 106 queries (two price reads per security)
    decomposition, window + 1 year:    48 queries (tax lots x8: every window reloaded
                                       lots, price and FX caches and base_fx)

and after that fix, the decomposition still read the cash ledgers per reader (GitHub
#73): 27 queries, `cash_flows` x11, `trades`, `dividend_payments`, `cash_balances` and
`app_settings` x3 each — `_contribution_inputs` ran twice (directly and inside the
timeline) at four `cash_flows` queries each, and a second `CashService` re-read every
ledger for the corrections. The value chart alone read `cash_flows` five times.

The bounds below are what the fixes measured; a change that needs more queries has to
raise them on purpose. The equivalence tests compare each new path against the old one
it replaced — re-implemented here where the old code is gone, or switched back on by
disabling the preload where it is not. #73 moved no figure either; its proof is a golden
recorded before it (`tests/test_decomposition_golden.py`).
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.accounts import IBKR, PILLAR3A
from app.clock import utc_today
from app.database import Base
from app.models.app_settings import AppSetting
from app.models.cash_balance import CashBalance
from app.models.cash_flow import CashFlow
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.repositories import market_price_repository
from app.repositories.corporate_action_repository import CorporateActionRepository
from app.repositories.market_price_repository import MarketPriceRepository
from app.repositories.security_repository import held_at
from app.services.cash_service import CashService
from app.services.performance_analytics_service import PerformanceAnalyticsService
from app.services.portfolio_service import PortfolioService
from app.services.sync_helper import POSITION_CREATING_ACTIONS
from tests.test_performance_analytics import END, _session as analytics_session

TODAY = utc_today()

# What the fix measured (see the module docstring for before).
SUMMARY_MAX_QUERIES = 9
CLOSED_POSITIONS_MAX_QUERIES = 8
DECOMPOSITION_MAX_QUERIES = 11


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, AsyncSession(engine, expire_on_commit=False)


async def _cost_session(n_actions: int, n_sold: int = 5):
    engine, s = await _engine()
    n_secs = max(n_actions, 1) + n_sold
    for i in range(1, n_secs + 1):
        s.add(Security(id=i, isin=f"US{i:010d}", symbol=f"S{i}", description="x",
                       currency="EUR", conid=500 + i, asset_category="STK", exchange="X"))
    await s.flush()
    opened = TODAY - timedelta(days=200)
    for i in range(1, n_secs + 1):
        sold = i > max(n_actions, 1)
        closed = TODAY - timedelta(days=30) if sold else None
        s.add(TaxLot(security_id=i, open_date=opened, close_date=closed, is_open=not sold,
                     quantity=Decimal("10"), cost_basis=Decimal("100"),
                     cost_basis_eur=Decimal("100"), price_per_unit=Decimal("10"),
                     currency="EUR"))
        for d in sorted({closed or TODAY, TODAY}):
            s.add(MarketPrice(security_id=i, date=d, close_price=Decimal("11"),
                              currency="EUR", source="test"))
        if sold:
            s.add(Trade(ib_key=f"T{i}", conid=str(500 + i), security_id=i, symbol=f"S{i}",
                        trade_date=closed, buy_sell="SELL", quantity=Decimal("-10"),
                        price=Decimal("11"), proceeds=Decimal("110"), commission=Decimal("0"),
                        currency="EUR", realized_pnl=Decimal("10")))
    for k in range(1, n_actions + 1):
        s.add(CorporateAction(ib_key=f"CA{k}", conid=str(500 + k), security_id=k,
                              action_date=opened + timedelta(days=10), action_type="SPINOFF",
                              quantity=Decimal("10")))
    s.add(CashFlow(ib_key="CF1", flow_date=opened, flow_type="DEPOSITWITHDRAW",
                   currency="EUR", amount=Decimal("5000"), amount_eur=Decimal("5000")))
    # A measured balance: production's state, and what sends cash_source() through
    # _accounts_with_activity.
    s.add(CashBalance(report_date=TODAY - timedelta(days=1), cash=Decimal("100"),
                      currency="EUR"))
    await s.commit()
    return engine, s


async def _count(engine, coro_factory):
    statements = []

    def count(conn, cursor, statement, *_):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        await coro_factory()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    return statements


def _from(statements, table):
    return sum(f"FROM {table}" in st for st in statements)


# ------------------------------------------------------------------ query counts


@pytest.mark.asyncio
async def test_a_summary_reads_each_ledger_once_whatever_the_number_of_corporate_actions():
    report = {}
    for n in (1, 20):
        engine, s = await _cost_session(n_actions=n)
        try:
            st = await _count(engine, lambda: PortfolioService(s).get_current_portfolio_summary())
            report[n] = st
        finally:
            await s.close()
            await engine.dispose()
    for n, st in report.items():
        assert len(st) <= SUMMARY_MAX_QUERIES, (n, len(st))
        assert _from(st, "trades") == 1, n
        assert _from(st, "cash_flows") == 1, n
        assert _from(st, "cash_balances") == 1, n
    assert len(report[20]) == len(report[1]), "one query per corporate action is back"


@pytest.mark.asyncio
async def test_closed_positions_cost_does_not_grow_with_the_number_of_sold_securities():
    report = {}
    for n_sold in (5, 50):
        engine, s = await _cost_session(n_actions=1, n_sold=n_sold)
        try:
            report[n_sold] = await _count(
                engine, lambda: PerformanceAnalyticsService(s).closed_positions()
            )
        finally:
            await s.close()
            await engine.dispose()
    assert len(report[5]) <= CLOSED_POSITIONS_MAX_QUERIES
    assert len(report[50]) == len(report[5]), "price queries per sold security are back"
    assert _from(report[50], "market_prices") <= 3


@pytest.mark.asyncio
async def test_a_decomposition_reads_lots_and_prices_once_for_every_window():
    engine, s = await _cost_session(n_actions=1, n_sold=5)
    try:
        st = await _count(engine, lambda: PerformanceAnalyticsService(s).decomposition(
            TODAY - timedelta(days=60), TODAY
        ))
        assert len(st) <= DECOMPOSITION_MAX_QUERIES, len(st)
        # The money-in splice's own lot read, and the one shared load. (A non-EUR base
        # adds a third, `_load_base_fx`'s MIN(open_date) for its rate window.)
        assert _from(st, "taxlots") <= 2
        assert _from(st, "market_prices") == 1
        # One CashService for the request, the splice's inputs handed to the timeline
        # (GitHub #73): each cash ledger once, whatever reads it.
        for table in ("cash_flows", "trades", "dividend_payments", "cash_balances"):
            assert _from(st, table) == 1, (table, _from(st, table))
        # Two settings, two keys: the base currency and the deposit-coverage date.
        assert _from(st, "app_settings") == 2
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_value_chart_and_the_contributions_read_each_cash_ledger_once():
    """The timeline route and the contributions strip without the decomposition: the
    splice's deposits and its three ledger dates come off the one `cash_flows` read the
    balance makes (they were four queries of their own, the chart's five in all)."""
    engine, s = await _cost_session(n_actions=1, n_sold=5)
    try:
        chart = await _count(engine, lambda: PortfolioService(s).get_portfolio_value_over_time(
            TODAY - timedelta(days=60), TODAY
        ))
        for table in ("cash_flows", "trades", "dividend_payments", "cash_balances"):
            assert _from(chart, table) == 1, (table, _from(chart, table))
        strip = await _count(engine, lambda: PortfolioService(s).get_contributions())
        assert _from(strip, "cash_flows") == 1
    finally:
        await s.close()
        await engine.dispose()


# ------------------------------------------------------------------ summary: same figures


async def _two_account_session():
    """An IBKR account with a measured balance, USD trades and a SELL, beside a pillar 3a
    account whose cash is derived only — so `cash_source()` is MIXED and every branch
    of the ledger reads is exercised."""
    engine, s = await _engine()
    s.add(AppSetting(key="base_currency", value="CHF"))
    s.add_all([
        Security(id=1, isin="US0000000001", symbol="AAA", description="AAA", currency="USD",
                 conid=1, asset_category="STK", exchange="X"),
        Security(id=2, isin="CH0000000002", symbol="P3A", description="P3A", currency="CHF",
                 conid=2, asset_category="STK", exchange="X", account=PILLAR3A),
    ])
    await s.flush()
    opened = TODAY - timedelta(days=90)
    sold = TODAY - timedelta(days=20)
    s.add_all([
        TaxLot(security_id=1, open_date=opened, is_open=True, quantity=Decimal("3"),
               cost_basis=Decimal("300.37"), cost_basis_eur=Decimal("277.1234"),
               price_per_unit=Decimal("100.1233"), currency="USD"),
        TaxLot(security_id=1, open_date=opened, close_date=sold, is_open=False,
               quantity=Decimal("2"), cost_basis=Decimal("200.11"),
               cost_basis_eur=Decimal("184.3311"), price_per_unit=Decimal("100.055"),
               currency="USD"),
        TaxLot(security_id=2, open_date=opened, is_open=True, quantity=Decimal("7"),
               cost_basis=Decimal("700"), cost_basis_eur=Decimal("745.12"),
               price_per_unit=Decimal("100"), currency="CHF"),
    ])
    s.add_all([
        Trade(ib_key="B1", conid="1", security_id=1, symbol="AAA", trade_date=opened,
              buy_sell="BUY", quantity=Decimal("5"), price=Decimal("100.0963"),
              proceeds=Decimal("-500.4815"), commission=Decimal("-1.0731"), currency="USD"),
        Trade(ib_key="S1", conid="1", security_id=1, symbol="AAA", trade_date=sold,
              buy_sell="SELL", quantity=Decimal("-2"), price=Decimal("113.17"),
              proceeds=Decimal("226.34"), commission=Decimal("-1.0213"), currency="USD",
              realized_pnl=Decimal("25.1987")),
        CashFlow(ib_key="D1", flow_date=opened - timedelta(days=3), flow_type="DEPOSITWITHDRAW",
                 currency="USD", amount=Decimal("1000"), amount_eur=Decimal("923.17")),
        CashFlow(ib_key="D2", flow_date=opened, flow_type="DEPOSITWITHDRAW",
                 currency="CHF", amount=Decimal("800"), amount_eur=Decimal("851.33"),
                 account=PILLAR3A),
        CashBalance(report_date=sold + timedelta(days=2), account=IBKR, currency="CHF",
                    cash=Decimal("670.41")),
        CashBalance(report_date=TODAY - timedelta(days=1), account=IBKR, currency="CHF",
                    cash=Decimal("671.02")),
    ])
    d = opened - timedelta(days=20)
    while d <= TODAY:
        s.add(MarketPrice(security_id=1, date=d, close_price=Decimal("111.37"),
                          currency="USD", source="test"))
        s.add(MarketPrice(security_id=2, date=d, close_price=Decimal("103.9"),
                          currency="CHF", source="test"))
        s.add(ExchangeRate(date=d, from_currency="USD", to_currency="EUR",
                           rate=Decimal("0.921731"), source="test"))
        s.add(ExchangeRate(date=d, from_currency="CHF", to_currency="EUR",
                           rate=Decimal("1.063377"), source="test"))
        s.add(ExchangeRate(date=d, from_currency="EUR", to_currency="CHF",
                           rate=Decimal("0.940387"), source="test"))
        d += timedelta(days=1)
    await s.commit()
    return engine, s


@pytest.mark.asyncio
async def test_the_shared_reads_give_the_summary_the_figures_separate_reads_give():
    engine, s = await _two_account_session()
    try:
        summary = await PortfolioService(s).get_current_portfolio_summary()

        # Each figure re-derived the old way: realized totals reading `trades` on their
        # own, a cold CashService per question (distinct-account query, count queries).
        portfolio = PortfolioService(s)
        base_fx = await portfolio._load_base_fx()
        realized = await PortfolioService(s).get_realized_totals(base_fx=base_fx)
        for key, value in realized.items():
            assert summary[key] == value, key
        events = await CashService(s).balance_events(base_fx)
        assert summary["total_cash_eur"] == float(CashService.balance_as_of(events, TODAY))
        assert summary["cash_source"] == await CashService(s).cash_source() == "mixed"
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_account_names_and_derived_source_agree_cold_and_off_the_memo():
    engine, s = await _two_account_session()
    try:
        cold = CashService(s)
        warm = CashService(s)
        await warm.all_trades()
        await warm._all_flows()
        assert await cold._accounts_with_activity() == await warm._accounts_with_activity() \
            == {IBKR, PILLAR3A}
        assert await CashService(s).derived_source() == await warm.derived_source() == "derived"
    finally:
        await s.close()
        await engine.dispose()

    engine, s = await _engine()
    try:
        cold, warm = CashService(s), CashService(s)
        await warm.all_trades()
        await warm._all_flows()
        assert await cold._accounts_with_activity() == await warm._accounts_with_activity() == set()
        assert await cold.derived_source() == await warm.derived_source() == "unknown"
    finally:
        await s.close()
        await engine.dispose()


async def _start_dates_the_old_way(s) -> dict:
    """`_load_position_start_dates` as it was before GitHub #47: one SUM per action."""
    starts = {}
    for action in await CorporateActionRepository(s).get_by_types(POSITION_CREATING_ACTIONS):
        if action.security_id is None or not action.quantity or action.quantity <= 0:
            continue
        held = (await s.execute(
            select(func.sum(TaxLot.quantity)).where(
                TaxLot.security_id == action.security_id,
                held_at(action.action_date),
            )
        )).scalar() or Decimal("0")
        if held > action.quantity:
            continue
        previous = starts.get(action.security_id)
        if previous is None or action.action_date < previous:
            starts[action.security_id] = action.action_date
    return starts


@pytest.mark.asyncio
async def test_one_grouped_query_floors_exactly_the_positions_the_per_action_sums_did():
    engine, s = await _engine()
    try:
        base = date(2026, 1, 5)
        for i in range(1, 7):
            s.add(Security(id=i, isin=f"US{i:010d}", symbol=f"S{i}", description="x",
                           currency="EUR", conid=i, asset_category="STK", exchange="X"))
        await s.flush()

        def lot(sid, opened, qty, closed=None):
            return TaxLot(security_id=sid, open_date=opened, close_date=closed,
                          is_open=closed is None, quantity=Decimal(qty),
                          cost_basis=Decimal("1"), cost_basis_eur=Decimal("1"),
                          price_per_unit=Decimal("1"), currency="EUR")

        def action(key, sid, when, qty, kind="SPINOFF"):
            return CorporateAction(ib_key=key, conid=str(sid), security_id=sid,
                                   action_date=when, action_type=kind,
                                   quantity=None if qty is None else Decimal(qty))

        s.add_all([
            # 1: the received line is the whole position -> floored.
            lot(1, base, "4.5"), action("A1", 1, base + timedelta(days=30), "4.5"),
            # 2: more was held before the spinoff -> not floored.
            lot(2, base, "10"), lot(2, base, "3"), action("A2", 2, base + timedelta(days=30), "3"),
            # 3: the parent-side row carries no quantity -> skipped.
            lot(3, base, "8"), action("A3", 3, base + timedelta(days=30), None),
            action("A3b", 3, base + timedelta(days=31), "0"),
            # 4: two actions on one security -> the earlier wins.
            lot(4, base, "2"), action("A4", 4, base + timedelta(days=40), "2"),
            action("A4b", 4, base + timedelta(days=20), "2"),
            # 5: a lot closed ON the action date is not held at it (exclude-on-close),
            #    one opened after it does not count either -> held 0 -> floored.
            lot(5, base, "9", closed=base + timedelta(days=30)),
            lot(5, base + timedelta(days=31), "9"),
            action("A5", 5, base + timedelta(days=30), "1"),
            # 6: no lots at all -> held 0 -> floored.
            action("A6", 6, base + timedelta(days=30), "1"),
        ])
        await s.commit()

        expected = await _start_dates_the_old_way(s)
        assert set(expected) == {1, 4, 5, 6}   # the fixture covers both outcomes
        assert await PortfolioService(s)._load_position_start_dates() == expected
    finally:
        await s.close()
        await engine.dispose()


# ------------------------------------------------------------------ closed positions


@pytest.mark.asyncio
async def test_bulk_price_reads_return_what_the_per_security_reads_did(monkeypatch):
    engine, s = await _cost_session(n_actions=1, n_sold=7)
    try:
        # A gap and a currency switch, so ranges are not uniform.
        s.add(MarketPrice(security_id=3, date=TODAY - timedelta(days=35),
                          close_price=Decimal("9"), currency="USD", source="test"))
        await s.commit()
        repo = MarketPriceRepository(s)
        ids = list(range(1, 10)) + [999]   # 999 has no prices
        latest = await repo.get_latest_prices(ids)
        for sid in ids:
            one = await repo.get_latest_price(sid)
            assert latest.get(sid) is one, sid

        windows = {
            sid: (TODAY - timedelta(days=30 + sid), TODAY - timedelta(days=sid % 3))
            for sid in ids
        }
        monkeypatch.setattr(market_price_repository, "_WINDOWS_PER_QUERY", 3)
        ranges = await repo.get_price_ranges(windows)
        assert set(ranges) == set(windows)
        for sid, (start, end) in windows.items():
            assert ranges[sid] == await repo.get_price_range(sid, start, end), sid
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_closed_positions_are_unchanged_by_the_bulk_price_reads(monkeypatch):
    engine, s = await _cost_session(n_actions=1, n_sold=5)
    try:
        last_close = TODAY - timedelta(days=30)
        # 2: rose after the sale. 3: no later price. 4: reference in another currency.
        # 5: a zero reference close. 6: the reference sits days before the close.
        await s.execute(MarketPrice.__table__.update()
                        .where(MarketPrice.security_id == 2, MarketPrice.date == TODAY)
                        .values(close_price=Decimal("13.37")))
        await s.execute(MarketPrice.__table__.delete()
                        .where(MarketPrice.security_id == 3, MarketPrice.date == TODAY))
        await s.execute(MarketPrice.__table__.update()
                        .where(MarketPrice.security_id == 4, MarketPrice.date == TODAY)
                        .values(currency="USD"))
        await s.execute(MarketPrice.__table__.update()
                        .where(MarketPrice.security_id == 5, MarketPrice.date == last_close)
                        .values(close_price=Decimal("0")))
        s.add(MarketPrice(security_id=5, date=last_close - timedelta(days=2),
                          close_price=Decimal("11"), currency="EUR", source="test"))
        await s.execute(MarketPrice.__table__.update()
                        .where(MarketPrice.security_id == 6, MarketPrice.date == last_close)
                        .values(date=last_close - timedelta(days=4), close_price=Decimal("10.5")))
        await s.commit()

        new = await PerformanceAnalyticsService(s).closed_positions()

        # The old reads: one latest price and one range per security.
        async def latest_one_by_one(self, ids):
            out = {}
            for sid in ids:
                p = await self.get_latest_price(sid)
                if p is not None:
                    out[sid] = p
            return out

        async def ranges_one_by_one(self, windows):
            return {sid: await self.get_price_range(sid, a, b) for sid, (a, b) in windows.items()}

        monkeypatch.setattr(MarketPriceRepository, "get_latest_prices", latest_one_by_one)
        monkeypatch.setattr(MarketPriceRepository, "get_price_ranges", ranges_one_by_one)
        old = await PerformanceAnalyticsService(s).closed_positions()

        assert new == old
        by_sid = {p["security_id"]: p for p in new["positions"]}
        assert by_sid[2]["post_sale_pct"] is not None and by_sid[2]["post_sale_pct"] > 0
        assert by_sid[6]["post_sale_pct"] is not None
        assert all(by_sid[sid]["post_sale_pct"] is None for sid in (3, 4, 5))
    finally:
        await s.close()
        await engine.dispose()


# ------------------------------------------------------------------ decomposition


async def _decomposition_session():
    """The analytics fixture (CHF base, USD holding, a sale, an unpriced holding, a
    measured balance) plus the two shapes a wider cache would get wrong:

    - MIXED: a 2025 lot whose prices are EUR in 2025 and USD from 30 Dec, so the newest
      row over the whole span (USD) is not the newest row of the 2025 window (EUR);
    - a 2026 window (from 31 Dec) whose effective start falls on 30 Dec, the last day
      anything is priced, so a fallback from there reaches 14 days back to 16 Dec — one
      day before the window's own loads begin. GAP's price needs that reach (no rows
      17 Dec - 4 Jan); MIXED's USD rate does (no USD rate over the same days).
    """
    engine, s = await analytics_session("CHF")
    s.add_all([
        Security(id=7, isin="US0000000007", symbol="MIXED", description="MIXED",
                 currency="EUR", conid=107, asset_category="STK", exchange="X"),
        Security(id=8, isin="NL0000000008", symbol="GAP", description="GAP",
                 currency="EUR", conid=108, asset_category="STK", exchange="X"),
    ])
    await s.flush()
    s.add_all([
        TaxLot(security_id=7, open_date=date(2025, 6, 2), is_open=True, quantity=Decimal("4"),
               cost_basis=Decimal("400"), cost_basis_eur=Decimal("400"),
               price_per_unit=Decimal("100"), currency="EUR"),
        TaxLot(security_id=8, open_date=date(2025, 6, 2), is_open=True, quantity=Decimal("1"),
               cost_basis=Decimal("50"), cost_basis_eur=Decimal("50"),
               price_per_unit=Decimal("50"), currency="EUR"),
    ])
    gap = (date(2025, 12, 17), date(2026, 1, 4))
    d = date(2025, 6, 2)
    while d <= END:
        if d <= date(2025, 12, 16):
            s.add(MarketPrice(security_id=7, date=d, close_price=Decimal("101.5"),
                              currency="EUR", source="test"))
        elif d == date(2025, 12, 30) or d > gap[1]:
            s.add(MarketPrice(security_id=7, date=d, close_price=Decimal("120.25"),
                              currency="USD", source="test"))
        if not gap[0] <= d <= gap[1]:
            s.add(MarketPrice(security_id=8, date=d, close_price=Decimal("51.75"),
                              currency="EUR", source="test"))
        if d < date(2026, 2, 1):
            s.add(ExchangeRate(date=d, from_currency="EUR", to_currency="CHF",
                               rate=Decimal("0.9377"), source="test"))
            if not gap[0] <= d <= gap[1]:
                s.add(ExchangeRate(date=d, from_currency="USD", to_currency="EUR",
                                   rate=Decimal("0.8813"), source="test"))
        d += timedelta(days=1)
    await s.commit()
    return engine, s


def _without_base_fx(computed):
    return {k: v for k, v in computed.items() if k != "base_fx"}


@pytest.mark.asyncio
async def test_each_window_reads_from_the_shared_load_exactly_what_its_own_load_read():
    engine, s = await _decomposition_session()
    try:
        portfolio = PortfolioService(s)
        span_start = date(2024, 12, 31)
        inputs = await portfolio.load_attribution_inputs(span_start, END)

        windows = [
            (span_start, date(2025, 12, 31)),        # the 2025 year: MIXED was EUR then
            (date(2025, 12, 31), END),               # 2026: effective start a day early
            (date(2026, 3, 2), END),
            (date(2026, 4, 1), date(2026, 4, 30)),
            (date(2024, 6, 1), date(2025, 1, 31)),   # outside the span: queries itself
        ]
        for ws, we in windows:
            alone = await portfolio.attribution_rows(ws, we)
            alone_skipped = dict(portfolio.last_realized_skipped)
            shared = await portfolio.attribution_rows(ws, we, preloaded=inputs)
            assert _without_base_fx(shared) == _without_base_fx(alone), (ws, we)
            assert portfolio.last_realized_skipped == alone_skipped, (ws, we)

        base_fx = inputs.base_fx
        for start, end in [(None, None), (date(2026, 4, 1), None), (None, date(2026, 4, 14)),
                           (date(2026, 4, 15), date(2026, 4, 15)), (date(2023, 1, 1), END)]:
            alone = await portfolio.realized_rows_from_closed_lots(base_fx, start=start, end=end)
            alone_skipped = dict(portfolio.last_realized_skipped)
            shared = await portfolio.realized_rows_from_closed_lots(
                base_fx, start=start, end=end, preloaded=inputs
            )
            assert shared == alone, (start, end)
            assert portfolio.last_realized_skipped == alone_skipped, (start, end)

        assert await portfolio.get_portfolio_value_over_time(span_start, END, preloaded=inputs) \
            == await portfolio.get_portfolio_value_over_time(span_start, END)
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_decomposition_is_identical_with_the_shared_load_switched_off(monkeypatch):
    engine, s = await _decomposition_session()
    try:
        shared = await PerformanceAnalyticsService(s).decomposition(date(2026, 3, 2), END)

        async def no_preload(self, *args, **kwargs):
            return None

        monkeypatch.setattr(PortfolioService, "load_attribution_inputs", no_preload)
        alone = await PerformanceAnalyticsService(s).decomposition(date(2026, 3, 2), END)

        assert shared == alone
        assert [y["year"] for y in shared["years"]] == [2025, 2026]
    finally:
        await s.close()
        await engine.dispose()
