"""
A share of a whole has one rule (`app/services/percentages.share_pct`).

`lookthrough_service` and `performance_analytics_service` each had a `_pct(part, whole)`
and disagreed about a whole of zero (0.0 against None). The family test is the scan: no
module under `app/` may define its own part-of-whole percentage again.

The function scan missed the inline form: seven `x / whole * 100 if whole > 0 else 0`
expressions in portfolio_service and benchmark_service published 0% for a share of
nothing (GitHub issue #29). The second scan fails a conditional whose one branch is a
`<a> / <b> * 100` and whose other is a numeric constant.
"""
import ast
from decimal import Decimal
from pathlib import Path

from app.services.percentages import share_pct

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _is_hundred(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and not isinstance(node.value, bool)
        and node.value == 100
    )


def _is_numeric_constant(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _holds_a_percentage_of(node: ast.AST) -> bool:
    """Whether `node` contains `<a> / <b> * 100` (or `100 * (<a> / <b>)`)."""
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mult)):
            continue
        for factor, other in ((sub.right, sub.left), (sub.left, sub.right)):
            if (
                _is_hundred(factor)
                and isinstance(other, ast.BinOp)
                and isinstance(other.op, ast.Div)
            ):
                return True
    return False


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


def test_the_inline_scan_recognises_the_form():
    """A detector that quietly stopped matching would pass everything below."""
    assert _inline_fallbacks("p = (g / c * 100) if c > 0 else 0") == [1]
    assert _inline_fallbacks("p = float(round(g / c * 100, 2) if c else 0.0)") == [1]
    assert _inline_fallbacks("p = 0 if not c else 100 * (g / c)") == [1]
    assert _inline_fallbacks("p = g / c * 100 if c else None") == []
    assert _inline_fallbacks("p = share_pct(g, c)") == []


def test_no_module_computes_an_inline_share_with_a_numeric_fallback():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name == "percentages.py":
            continue
        for line in _inline_fallbacks(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(APP_DIR)}:{line}")
    assert offenders == [], f"use percentages.share_pct (None for a share of nothing): {offenders}"


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
