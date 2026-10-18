"""
No test module carries its own copy of the shared test database (GitHub issues #50, #72).

Twenty-six modules each defined the same in-memory engine + `create_all` + session block
before it moved to `tests/conftest.py` (#50). This file then compared only *decorated
fixtures* with conftest's `db`, so the same block went on living in about ninety modules
as `_make_session()` helpers, fixtures under other names, inline in tests and behind an
import alias (#72). The question is "is there a copy at all", not "do the copies agree"
(CLAUDE.md, *the dominant failure mode*), so the rules below look for the ingredients
rather than for the whole shape:

* `create_all` is written once, in `tests/memory_db.py`. A module wanting fewer tables
  passes `tables=` to `memory_engine` / `create_schema`.
* `create_async_engine` is imported once, in the same module, under any alias.
* conftest's `db` is not re-declared under another name (compared by AST, so re-wrapping
  the arguments does not hide one). A module whose `db` adds rows or patches overrides
  the shared one and is not a copy.
* the shared AST scope predicates in `tests/ast_scope.py` are not redefined.

`ALLOWED` names the modules that genuinely need a database `memory_db` cannot build,
each with its reason. An entry that no longer needs its exemption fails too.
"""
import ast
from pathlib import Path

TESTS = Path(__file__).parent
SHARED = {"conftest.py", "memory_db.py"}

ALLOWED = {
    "create_all": {
        "test_benchmark_fetch_window.py":
            "a file database seeded through a sync engine before the route's TestClient "
            "opens it with NullPool from its own event loop",
    },
    "create_async_engine": {
        "test_api_smoke.py":
            "a named shared-cache memory database, copied per test from a template by "
            "sqlite3 backup (issue #50: the seed was 93% of the module's runtime)",
        "test_benchmark_fetch_window.py":
            "the file database above, opened per request with NullPool",
    },
}


def _modules():
    paths = sorted(p for p in TESTS.rglob("*.py") if p.name not in SHARED)
    assert len(paths) > 150, f"only {len(paths)} test modules found: is TESTS right?"
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in paths]


def _names(tree, symbol: str) -> bool:
    """Whether the module refers to `symbol`: an attribute, a bare name, or an import
    of it under any alias (`create_async_engine as _engine` hid one copy)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == symbol:
            return True
        if isinstance(node, ast.Name) and node.id == symbol:
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name == symbol for a in node.names):
            return True
    return False


def _fixtures(tree):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            "fixture" in ast.unparse(d) for d in node.decorator_list
        ):
            yield node


def _shape(node) -> str:
    node = ast.parse(ast.unparse(node)).body[0]   # a copy, so renaming leaves the original
    node.name = "_"
    return ast.dump(node)


def _offenders(symbol: str):
    return {path.name for path, tree in _modules() if _names(tree, symbol)}


def test_create_all_is_written_only_in_memory_db():
    found = _offenders("create_all")
    allowed = set(ALLOWED["create_all"])
    assert not found - allowed, (
        "build the schema with tests/memory_db.py (`memory_engine`, `create_schema`, "
        f"`tables=` for a subset) instead of a copy: {sorted(found - allowed)}"
    )
    assert not allowed - found, f"stale ALLOWED['create_all'] entries: {sorted(allowed - found)}"


def test_create_async_engine_is_imported_only_in_memory_db():
    found = _offenders("create_async_engine")
    allowed = set(ALLOWED["create_async_engine"])
    assert not found - allowed, (
        "take conftest's `db`, or an engine from tests/memory_db.py "
        f"(`memory_engine`, `new_memory_engine`), instead of a copy: {sorted(found - allowed)}"
    )
    assert not allowed - found, (
        f"stale ALLOWED['create_async_engine'] entries: {sorted(allowed - found)}"
    )


def test_no_module_copies_the_shared_db_fixture():
    [shared] = [f for f in _fixtures(ast.parse((TESTS / "conftest.py").read_text(encoding="utf-8")))
                if f.name == "db"]
    canonical = _shape(shared)
    copies = [
        f"{path.name}::{node.name}"
        for path, tree in _modules()
        for node in _fixtures(tree)
        if _shape(node) == canonical
    ]
    assert not copies, f"use conftest's `db` instead of a copy: {copies}"


def test_no_module_redefines_a_shared_ast_scope():
    scope = ast.parse((TESTS / "ast_scope.py").read_text(encoding="utf-8"))
    shared = {n.name for n in scope.body if isinstance(n, ast.FunctionDef)}
    assert shared, "tests/ast_scope.py defines no predicate"
    copies = [
        f"{path.name}::{node.name}"
        for path, tree in _modules() if path.name != "ast_scope.py"
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.lstrip("_") in shared
    ]
    assert not copies, f"import it from tests/ast_scope.py instead: {copies}"
