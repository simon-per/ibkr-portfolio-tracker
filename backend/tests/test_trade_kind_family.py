"""
"Is this trade a sale?" is answered in one place, and a cancelled sale is no sale (#58).

`trades.buy_sell` holds BUY/SELL/CANCELBUY/CANCELSELL (ibflex's member names; CANCELSELL
is "SELL (Ca.)"). The question was written inline seven times on the server as
`(t.buy_sell or "").upper() == "SELL"` and once more on the client as
`subtype.includes('SELL')`, and no copy knew a cancellation: a busted sale's P&L stayed
in the realized headline, the tax report and the closed positions, and reconcile closed
lots with it. The loop converting the SELLs' realized P&L was written three times, with
three failure policies.

The family half fails any `buy_sell` comparison outside `app/models/trade.py`, and any
`.realized_pnl` read outside `app/services/realized_trades.py`. The behaviour half runs
a sale with its cancellation through every reader.

The sign convention the netting rests on -- a cancellation row carries its execution's
amounts negated -- is IBKR's and is stated beside `sale_sign`; the fixtures here follow it.
"""
import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.clock import utc_today
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import (
    Trade, is_cancellation, is_sale_side, sale_sign, trade_kind, uncancelled_sales,
)
from app.services.activity_service import ActivityService
from app.services.performance_analytics_service import PerformanceAnalyticsService
from app.services.portfolio_service import BaseFx, PortfolioService
from app.services.tax_service import TaxService

APP = Path(__file__).resolve().parents[1] / "app"
HELPER = APP / "models" / "trade.py"
REALIZED = APP / "services" / "realized_trades.py"
KIND_WORDS = {"BUY", "SELL", "CANCELBUY", "CANCELSELL", "BUY (CA.)", "SELL (CA.)"}
TEXT_TESTS = {"startswith", "endswith", "find", "index", "count", "contains", "like",
              "ilike", "in_", "notin_"}


def _modules():
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(APP.rglob("*.py"))]


def _reads_buy_sell(node) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == "buy_sell" for n in ast.walk(node))


def _kind_literal(node) -> bool:
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value.strip().upper() in KIND_WORDS
        for n in ast.walk(node)
    )


def _imports_trade_model(tree) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and n.module == "app.models.trade" for n in ast.walk(tree)
    )


# --------------------------------------------------------------------------- family


def test_no_module_decides_a_trade_kind_from_buy_sell_itself():
    offenders = []
    scanned = buy_sell_reads = 0
    for path, tree in _modules():
        if path == HELPER:
            continue
        scanned += 1
        # A literal like "SELL" compared to anything is the same test through a local
        # variable -- in a module that reads trades. Other books (crypto) have their own
        # buy/sell vocabulary that has nothing to do with this column.
        reads_trades = _imports_trade_model(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "buy_sell":
                buy_sell_reads += 1
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                if any(_reads_buy_sell(o) for o in operands) or (
                    reads_trades and any(_kind_literal(o) for o in operands)
                ):
                    offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in TEXT_TESTS and _reads_buy_sell(node.func.value)
            ):
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    # Floors: the walk really opened the tree and really sees the attribute (the
    # activity ledger prints `buy_sell` as its subtype, which is a read, not a test).
    assert scanned > 50 and buy_sell_reads >= 1
    assert not offenders, (
        "decide a trade's kind through app.models.trade (trade_kind / sale_sign / "
        f"is_sale_side / uncancelled_sales), not by comparing buy_sell: {offenders}"
    )


def test_only_the_shared_loop_reads_realized_pnl():
    offenders = []
    for path, tree in _modules():
        if path in (REALIZED, HELPER):
            continue
        for node in ast.walk(tree):
            attr = isinstance(node, ast.Attribute) and node.attr == "realized_pnl"
            via_getattr = (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == "realized_pnl"
            )
            if attr or via_getattr:
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert not offenders, (
        "realized P&L off `trades` is converted by realized_trades.realized_from_sell_trades "
        f"and aggregated by each reader under its own policy: {offenders}"
    )


def test_the_shared_loop_exists_and_the_helper_reads_the_column():
    realized = ast.parse(REALIZED.read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.AsyncFunctionDef) and n.name == "realized_from_sell_trades"
               for n in ast.walk(realized))
    assert _reads_buy_sell(ast.parse(HELPER.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- the helper


def _t(side, qty, d=date(2026, 6, 10), price="10", conid="1", key=None):
    return SimpleNamespace(buy_sell=side, quantity=Decimal(qty), trade_date=d,
                           price=Decimal(price), conid=conid, ib_key=key or f"{side}{qty}{d}")


@pytest.mark.parametrize("stored, kind, sign, cancel", [
    ("BUY", "BUY", 0, False),
    ("SELL", "SELL", 1, False),
    ("CANCELBUY", "CANCELBUY", 0, True),
    ("CANCELSELL", "CANCELSELL", -1, True),
    ("SELL (Ca.)", "CANCELSELL", -1, True),
    ("sell", "SELL", 1, False),
    ("UNKNOWN", None, 0, False),
    (None, None, 0, False),
])
def test_trade_kind(stored, kind, sign, cancel):
    t = SimpleNamespace(buy_sell=stored)
    assert trade_kind(t) == kind
    assert sale_sign(t) == sign
    assert is_sale_side(t) is (sign != 0)
    assert is_cancellation(t) is cancel


def test_a_cancellation_strikes_out_the_sale_it_reverses():
    d = date(2026, 6, 10)
    keep = _t("SELL", "-5", d - timedelta(days=3), key="keep")
    busted = _t("SELL", "-10", d, key="busted")
    other_size = _t("SELL", "-7", d, key="other")
    other_contract = _t("SELL", "-10", d, conid="2", key="elsewhere")
    cancel = _t("CANCELSELL", "10", d, key="cancel")
    rows = [keep, busted, other_size, other_contract, cancel, _t("BUY", "3", d)]
    assert [t.ib_key for t in uncancelled_sales(rows)] == ["keep", "other", "elsewhere"]


def test_a_cancellation_prefers_its_own_date_then_price():
    d = date(2026, 6, 10)
    earlier = _t("SELL", "-10", d - timedelta(days=1), key="earlier")
    same_day_other_price = _t("SELL", "-10", d, price="11", key="same-day-11")
    same_day_same_price = _t("SELL", "-10", d, price="10", key="same-day-10")
    cancel = _t("CANCELSELL", "10", d, price="10")
    kept = uncancelled_sales([earlier, same_day_other_price, same_day_same_price, cancel])
    assert [t.ib_key for t in kept] == ["earlier", "same-day-11"]


def test_a_cancellation_whose_sale_is_elsewhere_strikes_out_nothing():
    sale = _t("SELL", "-4", key="sale")
    assert uncancelled_sales([sale, _t("CANCELSELL", "10")]) == [sale]


# --------------------------------------------------------------------------- the readers


def _trade(key, side, d, qty, proceeds, pnl, currency="EUR"):
    return Trade(ib_key=key, conid="1", security_id=1, symbol="AAA", trade_date=d,
                 buy_sell=side, quantity=Decimal(qty), price=Decimal("10"),
                 proceeds=Decimal(proceeds), commission=Decimal("0"),
                 currency=currency, realized_pnl=Decimal(pnl), asset_category="STK")


async def _seed(db, *trades, closed_lot_on=None):
    db.add(Security(id=1, isin="DE0000000001", symbol="AAA", description="A",
                    currency="EUR", conid=1, exchange="XETRA"))
    await db.flush()
    if closed_lot_on is not None:
        db.add(TaxLot(security_id=1, open_date=closed_lot_on - timedelta(days=60),
                      close_date=closed_lot_on, is_open=False, quantity=Decimal("10"),
                      cost_basis=Decimal("60"), cost_basis_eur=Decimal("60"),
                      price_per_unit=Decimal("6"), currency="EUR", close_source="trade"))
        db.add(MarketPrice(security_id=1, date=closed_lot_on, close_price=Decimal("12"),
                           currency="EUR", source="test"))
    for t in trades:
        db.add(t)
    await db.commit()


@pytest.mark.asyncio
async def test_a_cancelled_sale_is_not_realized(db):
    """The issue's repro: a SELL with P&L 40 and its CANCELSELL read 0, not 40."""
    d = utc_today() - timedelta(days=20)
    await _seed(db, _trade("T1", "SELL", d, -10, 100, 40),
                _trade("T1c", "CANCELSELL", d, 10, -100, -40))
    realized = await PortfolioService(db).get_realized_totals(BaseFx("EUR", {}))
    report = await TaxService(db).get_tax_report(d.year)
    assert realized["total_realized_gain_loss_eur"] == 0
    assert realized["num_closed_positions"] == 0
    assert report["realized_totals"]["gain_loss"] == 0


@pytest.mark.asyncio
async def test_every_realized_reader_nets_a_cancelled_sale_beside_a_standing_one(db):
    d = utc_today() - timedelta(days=20)
    await _seed(
        db,
        _trade("S1", "SELL", d - timedelta(days=5), -10, 100, 40),
        _trade("S2", "SELL", d, -10, 120, 60),
        _trade("S2c", "CANCELSELL", d, 10, -120, -60),
        closed_lot_on=d - timedelta(days=5),
    )

    totals = await PortfolioService(db).get_realized_totals(BaseFx("EUR", {}))
    assert totals["total_realized_gain_loss_eur"] == 40
    assert totals["total_realized_proceeds_eur"] == 100
    assert totals["num_closed_positions"] == 1
    assert totals["unpriced_disposals"] == 0

    report = await TaxService(db).get_tax_report(d.year)
    assert report["realized_source"] == "trades"
    assert report["realized_totals"]["gain_loss"] == 40
    # The reversal is listed as what it is: every figure on its row negated.
    rows = sorted(report["realized_gains"], key=lambda r: (r["trade_date"], r["quantity"]))
    assert [(r["quantity"], r["proceeds"], r["gain_loss"]) for r in rows] == [
        (10.0, 100.0, 40.0), (-10.0, -120.0, -60.0), (10.0, 120.0, 60.0),
    ]

    closed = await PerformanceAnalyticsService(db).closed_positions()
    [position] = closed["positions"]
    assert position["realized_source"] == "trade"
    assert position["realized_pnl_eur"] == 40


@pytest.mark.asyncio
async def test_a_security_whose_only_sale_was_cancelled_has_no_ibkr_figure(db):
    """Its closed lot (closed by some other path) is served from the lot, not as 'trade' 0."""
    d = utc_today() - timedelta(days=20)
    await _seed(db, _trade("S1", "SELL", d, -10, 100, 40),
                _trade("S1c", "CANCELSELL", d, 10, -100, -40), closed_lot_on=d)
    closed = await PerformanceAnalyticsService(db).closed_positions()
    [position] = closed["positions"]
    assert position["realized_source"] == "closed_lots"
    assert position["realized_pnl_eur"] == 60  # 10 x 12 - 60, the lot's own estimate
    assert not any("FX" in w for w in closed["warnings"])


@pytest.mark.asyncio
async def test_the_ledger_flags_the_sale_side_and_nets_a_cancellation(db):
    d = utc_today() - timedelta(days=20)
    await _seed(db, _trade("B1", "BUY", d - timedelta(days=9), 10, -60, 0),
                _trade("S1", "SELL", d, -10, 100, 40),
                _trade("S1c", "CANCELSELL", d, 10, -100, -40),
                _trade("B1c", "CANCELBUY", d, -10, 60, 0))
    act = await ActivityService(db).get_activity(d - timedelta(days=30), d)
    by_key = {r["ib_key"]: r for r in act["items"]}
    flags = {k: (r["is_sale"], r["is_cancellation"]) for k, r in by_key.items()}
    assert flags == {"B1": (False, False), "S1": (True, False),
                     "S1c": (True, True), "B1c": (False, True)}
    # The cancellation carries its sale's figure negated, so the sale side sums to zero.
    assert by_key["S1"]["realized_pnl_base"] == 40
    assert by_key["S1c"]["realized_pnl_base"] == -40
    # A buy realizes nothing, cancelled or not: blank, never 0.00.
    assert by_key["B1"]["realized_pnl_base"] is None
    assert by_key["B1c"]["realized_pnl_base"] is None


@pytest.mark.asyncio
async def test_rows_of_other_kinds_carry_no_trade_flags(db):
    from app.models.cash_flow import CashFlow

    d = utc_today() - timedelta(days=20)
    await _seed(db)
    db.add(CashFlow(ib_key="C1", flow_date=d, flow_type="DEPOSITWITHDRAW",
                    amount=Decimal("100"), currency="EUR", amount_eur=Decimal("100")))
    await db.commit()
    act = await ActivityService(db).get_activity(d - timedelta(days=1), d)
    [row] = act["items"]
    assert row["is_sale"] is None and row["is_cancellation"] is None
