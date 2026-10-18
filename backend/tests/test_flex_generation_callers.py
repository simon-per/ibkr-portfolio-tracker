"""
Who may generate a Flex statement is a reviewed list (CLAUDE.md rule 2, docs/flex-and-sync.md).

`IBKRService.fetch_flex_data` is a statement *generation*: one SendRequest that IBKR counts
towards the `1025` lockout when it fails. Every caller is a path that can spend the token, so
a new one must be a decision, not an accident — the dead `get_portfolio_summary` called it
with no gate around it until 2026-10-18 (issue #31). This walks the AST of `app/` and fails
on any reference outside the allowlist: a call, a bare attribute (a callback handed on), or
the name as a string (`getattr`). It also fails when an allowlisted module stops referencing
it, so the list cannot go stale.
"""
import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
METHOD = "fetch_flex_data"

# Modules allowed to generate a statement, each for a named reason.
ALLOWED_CALLERS = {
    "app.routers.sync": "POST /api/sync/ibkr, the interactive sync (single_flight, auth-gated)",
    "app.services.scheduler_service": "the scheduled IBKR slots (flex_generation, ET-day skip)",
}


def _module_name(path: Path) -> str:
    parts = path.relative_to(APP_DIR.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _references(source: str) -> list:
    """Line numbers where `source` reaches the method: attribute, name, or string literal."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == METHOD:
            hits.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == METHOD:
            hits.append(node.lineno)
        elif isinstance(node, ast.Constant) and node.value == METHOD:
            hits.append(node.lineno)
    return hits


def _referrers() -> dict:
    found = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        hits = _references(path.read_text(encoding="utf-8"))
        if hits:
            found[_module_name(path)] = hits
    return found


def test_only_allowlisted_modules_generate_a_flex_statement():
    outside = {m: lines for m, lines in _referrers().items() if m not in ALLOWED_CALLERS}
    assert not outside, (
        f"{METHOD} is referenced outside the allowlist: {outside}. Each caller can spend the "
        f"Flex token (rule 2); route through POST /api/sync/ibkr or the scheduler, or add the "
        f"module to ALLOWED_CALLERS with its reason after review."
    )


def test_every_allowlisted_module_still_calls_it():
    stale = set(ALLOWED_CALLERS) - set(_referrers())
    assert not stale, f"no longer reference {METHOD}; drop them from ALLOWED_CALLERS: {stale}"


def test_the_walk_catches_every_shape_of_reference():
    """Pins the detector itself, so a refactor of it cannot quietly pass everything."""
    assert _references("async def f(s):\n    await s.fetch_flex_data()\n") == [2]
    assert _references("cb = IBKRService().fetch_flex_data\n") == [1]
    assert _references("fn = getattr(svc, 'fetch_flex_data')\n") == [1]
    # Defining it is not calling it, and prose naming it is not a reference.
    assert _references("class S:\n    async def fetch_flex_data(self):\n        pass\n") == []
    assert _references('"""Parsed data from fetch_flex_data()."""\n') == []
