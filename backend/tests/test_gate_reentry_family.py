"""
A gate re-entered by its own holder (issue #4).

`single_flight` is not re-entrant. The scheduled benchmark warm-up ran inside
`_gated_job`, which holds SYNC_PIPELINE, and called `_ensure_prices_available`, which
entered SYNC_PIPELINE again around its fetch — so every ticker was refused with
`SyncBusy`, logged as "pipeline busy; serving cached prices", and counted as synced. The
warm-up never reached Yahoo, for weeks, behind a green suite: every test stubbed one side
of the pair.

Nothing about either half is wrong on its own. The defect is the *path*, so this test
walks it: from everything that runs while SYNC_PIPELINE is held — the body of each job
`_gated_job` runs, and the body of every `with single_flight(SYNC_PIPELINE ...)` block —
through the call graph, to any function that enters SYNC_PIPELINE itself. An edge that
passes `gate_held=True` is the declared way through and ends the walk there; the callee
checks the claim at run time (`BenchmarkService._ensure_prices_available`).

The call graph is keyed on names, so it over-approximates: two methods sharing a name are
one node. That errs towards a false alarm, never a missed path. Route handlers are left out
of it, because only FastAPI calls them — and several share a name with the scheduler method
behind them (`sync_market_data`), which would otherwise make every job reach a route's gate.
If it ever fires on another collision, rename one of the two rather than teaching this test
an exception.
"""
import ast
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

APP = Path(__file__).resolve().parent.parent / "app"
GATE = "SYNC_PIPELINE"


def _callee(call: ast.Call) -> Optional[str]:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _enters_gate(call: ast.Call) -> bool:
    return (
        _callee(call) == "single_flight"
        and bool(call.args)
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == GATE
    )


def _declares_gate_held(call: ast.Call) -> bool:
    return any(
        kw.arg == "gate_held" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in call.keywords
    )


_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "api_route"}


def _is_route_handler(fn: ast.AST) -> bool:
    return any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
        and d.func.attr in _ROUTE_METHODS
        for d in fn.decorator_list
    )


def _calls(nodes) -> List[ast.Call]:
    return [n for node in nodes for n in ast.walk(node) if isinstance(n, ast.Call)]


def reentry_paths(sources: Dict[str, str]) -> List[Tuple[str, ...]]:
    """
    Every call path from code running under SYNC_PIPELINE to code that enters it again.

    ``sources`` maps a module label to its source text, so the walk can be pointed at a
    synthetic module as easily as at `app/`.
    """
    defs: Dict[str, List[ast.AST]] = {}
    roots: List[Tuple[str, ast.Call]] = []
    for label, text in sources.items():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not _is_route_handler(node)):
                defs.setdefault(node.name, []).append(node)
        for node in ast.walk(tree):
            # A block that holds the gate: everything it calls runs under it.
            if isinstance(node, (ast.With, ast.AsyncWith)) and any(
                isinstance(item.context_expr, ast.Call) and _enters_gate(item.context_expr)
                for item in node.items
            ):
                roots += [(f"{label}: with single_flight({GATE})", c) for c in _calls(node.body)]
            # A job `_gated_job` runs: its whole body runs under the gate.
            if (isinstance(node, ast.Call) and _callee(node) == "_gated_job"
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Attribute)):
                job = node.args[1].attr
                for fn in defs_in(tree, job):
                    roots += [(f"{label}: _gated_job -> {job}", c) for c in _calls(fn.body)]

    found: List[Tuple[str, ...]] = []
    for origin, first in roots:
        queue = deque([(first, (origin,))])
        seen = set()
        while queue:
            call, path = queue.popleft()
            name = _callee(call)
            if name is None or _declares_gate_held(call):
                continue
            path = path + (name,)
            if _enters_gate(call):
                found.append(path)
                continue
            if name in seen or name not in defs:
                continue
            seen.add(name)
            for fn in defs[name]:
                queue += [(c, path) for c in _calls(fn.body)]
    return found


def defs_in(tree: ast.AST, name: str) -> List[ast.AST]:
    return [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    ]


def _app_sources() -> Dict[str, str]:
    return {
        str(p.relative_to(APP)): p.read_text(encoding="utf-8")
        for p in sorted(APP.rglob("*.py"))
    }


def test_nothing_running_under_the_pipeline_gate_enters_it_again():
    paths = reentry_paths(_app_sources())
    assert paths == [], (
        "single_flight is not re-entrant: these paths enter SYNC_PIPELINE while already "
        "holding it, so they always raise SyncBusy and never do their work. Pass "
        "gate_held=True at the edge (see BenchmarkService._ensure_prices_available):\n"
        + "\n".join(" -> ".join(p) for p in paths)
    )


# The walk is only worth something if it finds the shape it exists for. The pre-fix
# warm-up, reduced to its call path:
_ISSUE_4 = '''
class Scheduler:
    async def market_data_only_sync_job(self):
        return await self._gated_job("market_data_only", self._market_data_only_locked)

    async def _market_data_only_locked(self):
        return await self.sync_benchmark_prices()

    async def sync_benchmark_prices(self):
        return await bench._ensure_prices_available("^GSPC", s, e{kw})


class Bench:
    async def _ensure_prices_available(self, ticker, s, e, gate_held=False):
        with _upstream_request(ticker, gate_held=gate_held):
            pass


def _upstream_request(key, gate_held=False):
    with single_flight(SYNC_PIPELINE):
        yield
'''


def test_the_walk_finds_the_issue_4_shape():
    paths = reentry_paths({"pre_fix.py": _ISSUE_4.format(kw="")})
    assert paths == [(
        "pre_fix.py: _gated_job -> _market_data_only_locked",
        "sync_benchmark_prices", "_ensure_prices_available", "_upstream_request",
        "single_flight",
    )]


def test_declaring_the_held_gate_is_the_way_through():
    assert reentry_paths({"fixed.py": _ISSUE_4.format(kw=", gate_held=True")}) == []


def test_a_with_block_holding_the_gate_is_a_root_too():
    src = '''
def route():
    with single_flight(SYNC_PIPELINE, cooldown_seconds=300):
        helper()

def helper():
    with single_flight(SYNC_PIPELINE):
        pass
'''
    assert reentry_paths({"r.py": src}) == [
        ("r.py: with single_flight(SYNC_PIPELINE)", "helper", "single_flight")
    ]
