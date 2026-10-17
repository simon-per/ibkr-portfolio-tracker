"""
A share of a whole has one rule (`app/services/percentages.share_pct`).

`lookthrough_service` and `performance_analytics_service` each had a `_pct(part, whole)`
and disagreed about a whole of zero (0.0 against None). The family test is the scan: no
module under `app/` may define its own part-of-whole percentage again.
"""
import ast
from decimal import Decimal
from pathlib import Path

from app.services.percentages import share_pct

APP_DIR = Path(__file__).resolve().parents[1] / "app"


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
