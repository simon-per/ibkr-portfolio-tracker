"""
One market-cap converter (`app/services/market_cap.py`), whoever serves a cap.

The Fundamentals tab's converter lived inside `FundamentalsService` until the Watchlist
needed the same figure (2026-10-17). A second copy would agree today and drift the first
time one of them learnt a new refusal — a minor unit, a stale rate — which is the shape
CLAUDE.md opens with. So the test is "is there a copy at all": every module that serves
`market_cap_base` imports the shared converter, and nothing else defines one.
"""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
HOME = APP / "services" / "market_cap.py"
# Declares the field; serves nothing.
DECLARES_ONLY = {APP / "schemas" / "portfolio.py"}


def _modules():
    for path in sorted(APP.rglob("*.py")):
        yield path, path.read_text(encoding="utf-8")


def test_every_module_serving_a_base_market_cap_uses_the_shared_converter():
    offenders = []
    for path, source in _modules():
        if path == HOME or path in DECLARES_ONLY or "market_cap_base" not in source:
            continue
        imports = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module == "app.services.market_cap"
            for alias in node.names
        }
        if "market_cap_to_base" not in imports:
            offenders.append(str(path.relative_to(APP)))
    assert offenders == []


def test_nothing_else_defines_a_market_cap_converter():
    defined = [
        f"{path.relative_to(APP)}:{node.name}"
        for path, source in _modules() if path != HOME
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "market_cap" in node.name and "base" in node.name
    ]
    assert defined == []


def test_the_family_is_not_empty():
    """Both surfaces are found, so a rename cannot make the scan vacuous."""
    serving = {
        path.relative_to(APP).as_posix() for path, source in _modules()
        if "market_cap_base" in source and path != HOME and path not in DECLARES_ONLY
    }
    assert {"services/fundamentals_service.py", "routers/watchlist.py"} <= serving
