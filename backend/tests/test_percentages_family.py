"""
A share of a whole has one rule (`app/services/percentages.share_pct`).

`lookthrough_service` and `performance_analytics_service` each had a `_pct(part, whole)`
and disagreed about a whole of zero (0.0 against None). The family test is the scan: no
module under `app/` may define its own part-of-whole percentage again.

The function scan missed the inline form: seven `x / whole * 100 if whole > 0 else 0`
expressions in portfolio_service and benchmark_service published 0% for a share of
nothing (GitHub issue #29). The second scan fails a conditional whose one branch is a
`<a> / <b> * 100` and whose other is a numeric constant.

That scan still saw only a numeric fallback, so nine `round(float(a / b * 100), n)` in
dividend_service and crypto_service went on rounding a float after the division — a tie
such as 1.015 published 1.01 there and 1.02 through `share_pct` (GitHub issue #71). The
third scan fails every `<a> / <b> * 100` (and `100 * <a> / <b>`) under `app/` outside
`percentages.py` that `NOT_A_SHARE` does not name with its reason — all of `app/`, not
`app/services` alone, since a family test must not choose its scope by directory.
"""
import ast
from decimal import Decimal
from pathlib import Path

from app.services.percentages import share_pct

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: `(module under app/, the expression as ast.unparse prints it)` → why it is not a share
#: of a whole that `share_pct` should publish. Each is a change against a reference, a
#: figure kept at another precision, or a number an operator reads off a console.
NOT_A_SHARE = {
    ("services/dividend_service.py", "(current - base) / base * 100"):
        "`DividendService._pct` is a growth rate, mirrored by `lib/dividendGrowth.ts` "
        "across the language boundary (docs/failure-modes.md: both ends name the rules).",
    ("services/crypto_service.py", "unrealized / cost * 100"):
        "re-derives CoinStats' `profitPercent` for a coin listed twice; stored unrounded "
        "beside the upstream's own figure for every other coin, never published from here.",
    ("services/etf_basket_parsers.py", "with_isin / invested * Decimal('100')"):
        "`identifier_coverage_pct` is stored in a NOT NULL Numeric(9, 6) column; a 2-dp "
        "float, or None, would not fit it.",
    ("services/finpension_ingest.py", "abs(price - nav) / nav * 100"):
        "a derived price's distance from the published NAV, compared with a tolerance.",
    ("services/watchlist_service.py", "(target - price) / price * 100"):
        "upside to the analyst target, bucketed into a score and never published.",
    ("services/watchlist_service.py",
     "(current_price - data['week52_high']) / data['week52_high'] * 100"):
        "distance from the 52-week high: a change against a reference level.",
    ("services/watchlist_service.py", "(current_price - data['ma200']) / data['ma200'] * 100"):
        "distance from the 200-day average: a change against a reference level.",
    ("cli/backfill_dividend_forecast.py", "(replayed - recorded) / recorded * 100"):
        "a replay's deviation from the recorded forecast, printed by an operator CLI.",
    ("cli/backtest_dividend_forecast.py", "num / den * 100"):
        "a forecast's error relative to the actual, printed by an operator CLI.",
    ("cli/manage_mappings.py", "abs(c - row.close_price) / row.close_price * 100"):
        "a candidate ticker's price gap against the stored close, printed by an operator CLI.",
}


def _is_hundred(node: ast.AST) -> bool:
    """`100`, `100.0`, `Decimal("100")` / `Decimal(100)`, or a constant named `HUNDRED`."""
    if isinstance(node, ast.Constant):
        return not isinstance(node.value, bool) and node.value == 100
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        arg = node.args[0]
        return name == "Decimal" and isinstance(arg, ast.Constant) and str(arg.value) == "100"
    return isinstance(node, ast.Name) and node.id == "HUNDRED"


def _is_numeric_constant(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _is_div(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)


def _percentage_nodes(tree: ast.AST) -> list[ast.BinOp]:
    """
    Every `<a> / <b> * 100` and `100 * (<a> / <b>)`, plus `100 * <a> / <b>` and
    `<a> * 100 / <b>`, which parse as a division of a product.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        if isinstance(node.op, ast.Mult) and (
            (_is_hundred(node.right) and _is_div(node.left))
            or (_is_hundred(node.left) and _is_div(node.right))
        ):
            found.append(node)
        elif (
            _is_div(node)
            and isinstance(node.left, ast.BinOp)
            and isinstance(node.left.op, ast.Mult)
            and (_is_hundred(node.left.left) or _is_hundred(node.left.right))
        ):
            found.append(node)
    return found


def _holds_a_percentage_of(node: ast.AST) -> bool:
    """Whether `node` contains `<a> / <b> * 100` (or `100 * (<a> / <b>)`)."""
    return bool(_percentage_nodes(node))


def _inline_fallbacks(source: str) -> list[int]:
    """Lines of every `<a> / <b> * 100 if ... else <number>` (either branch order)."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.IfExp)
        and (
            (_holds_a_percentage_of(node.body) and _is_numeric_constant(node.orelse))
            or (_holds_a_percentage_of(node.orelse) and _is_numeric_constant(node.body))
        )
    ]


def _inline_shares(source: str) -> list[tuple[int, str]]:
    """`(line, expression)` of every inline percentage of a division."""
    return [(node.lineno, ast.unparse(node)) for node in _percentage_nodes(ast.parse(source))]


def _app_modules():
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name != "percentages.py":
            yield path.relative_to(APP_DIR).as_posix(), path.read_text(encoding="utf-8")


def test_the_inline_scan_recognises_the_form():
    """A detector that quietly stopped matching would pass everything below."""
    assert _inline_fallbacks("p = (g / c * 100) if c > 0 else 0") == [1]
    assert _inline_fallbacks("p = float(round(g / c * 100, 2) if c else 0.0)") == [1]
    assert _inline_fallbacks("p = 0 if not c else 100 * (g / c)") == [1]
    assert _inline_fallbacks("p = g / c * 100 if c else None") == []
    assert _inline_fallbacks("p = share_pct(g, c)") == []


def test_the_share_scan_recognises_every_spelling():
    for spelling in (
        "p = round(float(a / b * 100), 2)",
        "p = 100 * (a / b)",
        "p = 100 * a / b",
        "p = a * 100 / b",
        "p = a / b * Decimal('100')",
        "p = a / b * HUNDRED",
        "p = a / b * 100.0",
    ):
        assert len(_inline_shares(spelling)) == 1, spelling
    for not_one in ("p = share_pct(a, b)", "p = (a / b - 1) * 100", "p = a * 100", "p = a / 100"):
        assert _inline_shares(not_one) == [], not_one


def test_no_module_computes_an_inline_share_with_a_numeric_fallback():
    offenders = []
    for name, source in _app_modules():
        for line in _inline_fallbacks(source):
            offenders.append(f"{name}:{line}")
    assert offenders == [], f"use percentages.share_pct (None for a share of nothing): {offenders}"


def test_no_module_computes_a_share_of_a_whole_inline():
    offenders = [
        f"{name}:{line} {expression}"
        for name, source in _app_modules()
        for line, expression in _inline_shares(source)
        if (name, expression) not in NOT_A_SHARE
    ]
    assert offenders == [], (
        "use percentages.share_pct (its `digits` for another precision), or add the "
        f"expression to NOT_A_SHARE with the reason it is not a share of a whole: {offenders}"
    )


def test_every_exemption_still_names_an_expression():
    """A stale entry would exempt the next expression that happens to be spelled the same."""
    present = {
        (name, expression)
        for name, source in _app_modules()
        for _, expression in _inline_shares(source)
    }
    assert set(NOT_A_SHARE) - present == set()


def test_no_module_defines_its_own_share_of_a_whole():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name == "percentages.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = [a.arg for a in node.args.args if a.arg != "self"]
            if params == ["part", "whole"]:
                offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno} {node.name}")
    assert offenders == [], f"use percentages.share_pct: {offenders}"


def test_a_share_of_nothing_is_absent_not_zero():
    assert share_pct(Decimal("5"), Decimal("0")) is None
    assert share_pct(Decimal("0"), Decimal("10")) == 0.0
    assert share_pct(Decimal("1"), Decimal("3")) == 33.33
    assert share_pct(Decimal("-1"), Decimal("8")) == -12.5
    assert share_pct(1.0, None) is None
    assert share_pct(1.0, 0.0) is None


def test_a_tie_rounds_in_decimal_whatever_the_operands_are():
    """
    The divergence #71 found: `round(float(Decimal("1.015")), 2)` is 1.01, because the
    float is 1.01499…; `share_pct` rounds the Decimal, half to even. A float operand
    enters by its repr, so the crypto view's floats round the same way.
    """
    assert round(float(Decimal("1.015") / 100 * 100), 2) == 1.01
    assert share_pct(Decimal("1.015"), Decimal("100")) == 1.02
    assert share_pct(1.015, 100.0) == 1.02
    assert share_pct(Decimal("1.25"), Decimal("100"), digits=1) == 1.2
    assert share_pct(Decimal("1.35"), Decimal("100"), digits=1) == 1.4


def test_a_figure_that_is_not_finite_is_absent():
    assert share_pct(float("inf"), 2.0) is None
    assert share_pct(1.0, float("nan")) is None
    assert share_pct(Decimal("NaN"), Decimal("2")) is None
