"""
"Can the backend value this holding?" has one server-side answer: `cash_service.is_valuable`.

Allocation, look-through and the forward yield in `dividend_service` each wrote
`market_value_eur > 0` themselves — identical, each docstring citing another as where the
rule came from, and look-through's reading a missing value as 0 where allocation's would
raise (GitHub #71). Two scans keep it one rule:

- every module that reads `get_positions_breakdown()` must reach `is_valuable`, since it is
  the readers that partition the rows into valued and counted-as-unpriced; the router that
  serves the rows unfiltered is the one reasoned exemption;
- no module but `cash_service` compares anything naming `market_value_eur` with a number.
"""
import ast
from decimal import Decimal
from pathlib import Path

from app.services.cash_service import is_valuable

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: Readers of `get_positions_breakdown()` that do not partition the rows, each with why.
NOT_A_PARTITION = {
    "routers/portfolio.py": "serves the rows as they are; the client asks `isUnpriced` "
    "(frontend/src/lib/positionValuation.ts), which sees price and FX separately.",
}


def _modules():
    for path in sorted(APP_DIR.rglob("*.py")):
        yield path.relative_to(APP_DIR).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _reads_the_breakdown(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_positions_breakdown"
        for node in ast.walk(tree)
    )


def _names_is_valuable(tree: ast.AST) -> bool:
    return any(isinstance(node, ast.Name) and node.id == "is_valuable" for node in ast.walk(tree))


def _names_market_value(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value == "market_value_eur":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "market_value_eur":
            return True
        if isinstance(sub, ast.Name) and sub.id == "market_value_eur":
            return True
    return False


def _is_number(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    return isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Decimal"


def _value_tests(tree: ast.AST) -> list[int]:
    """Lines comparing something that names `market_value_eur` with a number."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for a, b in zip(operands, operands[1:]):
            if (_names_market_value(a) and _is_number(b)) or (
                _names_market_value(b) and _is_number(a)
            ):
                lines.append(node.lineno)
                break
    return lines


def test_the_value_scan_recognises_the_form():
    """A detector that quietly stopped matching would pass everything below."""
    for form in (
        "x = [p for p in ps if p['market_value_eur'] > 0]",
        "x = p['market_value_eur'] <= 0",
        "x = Decimal(str(pos.get('market_value_eur') or 0)) > 0",
        "x = 0 < pos.market_value_eur",
        "x = p['market_value_eur'] > Decimal('0')",
    ):
        assert _value_tests(ast.parse(form)) == [1], form
    assert _value_tests(ast.parse("x = is_valuable(p['market_value_eur'])")) == []
    assert _reads_the_breakdown(ast.parse("rows = await svc.get_positions_breakdown()"))


def test_every_reader_of_the_breakdown_partitions_through_the_one_predicate():
    readers = [name for name, tree in _modules() if _reads_the_breakdown(tree)]
    # A floor: allocation, look-through and the dividend breakdown read it today.
    assert len(readers) >= 4, readers
    offenders = [
        name
        for name, tree in _modules()
        if _reads_the_breakdown(tree)
        and name not in NOT_A_PARTITION
        and not _names_is_valuable(tree)
    ]
    assert offenders == [], f"partition the breakdown with cash_service.is_valuable: {offenders}"
    assert set(NOT_A_PARTITION) <= set(readers), "a stale NOT_A_PARTITION entry"


def test_no_module_tests_a_market_value_itself():
    offenders = [
        f"{name}:{line}"
        for name, tree in _modules()
        if name != "services/cash_service.py"
        for line in _value_tests(tree)
    ]
    assert offenders == [], f"use cash_service.is_valuable: {offenders}"


def test_the_predicate():
    assert is_valuable(Decimal("0.01")) and is_valuable(12.5) and is_valuable(1)
    # Both ways the backend fails to value a holding arrive as 0.00.
    assert not is_valuable(Decimal("0")) and not is_valuable(0.0)
    assert not is_valuable(None)
    assert not is_valuable(Decimal("-3"))
