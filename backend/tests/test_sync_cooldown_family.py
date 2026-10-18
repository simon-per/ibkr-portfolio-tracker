"""
Every public route's `single_flight` cooldown comes from `app/single_flight.py`.

The bulk-sync cooldown of 300 s was one policy written nine ways: a literal at seven
call sites across six routers, plus `SYNC_COOLDOWN_SECONDS = 300` defined separately in
`routers/dividends.py` and `routers/fundamentals.py` — and `fundamentals.py` still used
the literal beside its own constant. Nothing had drifted yet, which is the cheapest
moment to remove the copies (issue #45).

Written against the family: any router, including one added later, that hands
`single_flight` or `cooldown_remaining` a number, or defines its own `*COOLDOWN_SECONDS`,
fails here.
"""

import ast
from pathlib import Path

ROUTERS = Path(__file__).resolve().parent.parent / "app" / "routers"

GATE_FUNCTIONS = {"single_flight", "cooldown_remaining"}


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_number(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
        and not isinstance(node.value, bool)


def cooldown_offenders(source: str, label: str) -> list[str]:
    """Literal cooldowns and module-level cooldown constants in one router's source."""
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _called_name(node) in GATE_FUNCTIONS:
            literal = [kw.value for kw in node.keywords if kw.arg == "cooldown_seconds"]
            literal += node.args[1:2]  # (name, cooldown_seconds) positionally
            if any(_is_number(v) for v in literal):
                offenders.append(f"{label}:{node.lineno}: literal cooldown")
    for stmt in tree.body:
        targets = (
            stmt.targets if isinstance(stmt, ast.Assign)
            else [stmt.target] if isinstance(stmt, ast.AnnAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id.endswith("COOLDOWN_SECONDS"):
                offenders.append(f"{label}:{stmt.lineno}: defines {target.id}")
    return offenders


def _routers() -> list[Path]:
    return sorted(p for p in ROUTERS.glob("*.py") if p.name != "__init__.py")


def test_no_router_writes_its_own_cooldown():
    offenders = []
    for path in _routers():
        offenders += cooldown_offenders(path.read_text(encoding="utf-8"), path.name)
    assert not offenders, (
        "Import the cooldown from app/single_flight.py (PUBLIC_SYNC_COOLDOWN_SECONDS, or a "
        "named constant there with its reason) instead of writing it in a router:\n"
        + "\n".join(offenders)
    )


def test_the_scan_sees_the_gated_routes():
    """Not vacuous: the routers that gate a public sync are the ones scanned."""
    gated = {
        path.name for path in _routers()
        if any(
            isinstance(n, ast.Call) and _called_name(n) == "single_flight"
            for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }
    assert {"sync.py", "market_data.py", "fundamentals.py", "dividends.py",
            "scheduler.py", "watchlist.py"} <= gated


def test_the_scan_catches_each_shape():
    snippet = (
        "from app.single_flight import single_flight, cooldown_remaining\n"
        "X_COOLDOWN_SECONDS = 300\n"
        "def f():\n"
        "    with single_flight('g', cooldown_seconds=300):\n"
        "        pass\n"
        "    cooldown_remaining('g', 300)\n"
        "    with single_flight('g', cooldown_seconds=X_COOLDOWN_SECONDS):\n"
        "        pass\n"
    )
    found = cooldown_offenders(snippet, "snippet.py")
    assert sorted(found) == [
        "snippet.py:2: defines X_COOLDOWN_SECONDS",
        "snippet.py:4: literal cooldown",
        "snippet.py:6: literal cooldown",
    ]
