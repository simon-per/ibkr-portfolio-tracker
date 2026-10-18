"""
No test module carries its own copy of conftest's `db` fixture (GitHub issue #50).

Twenty-six modules each defined the same in-memory engine + `create_all` + session block
before it moved to `tests/conftest.py`. The question is "is there a copy at all", not "do
the copies agree" (CLAUDE.md, *the dominant failure mode*): compared by AST, under any
fixture name, so re-wrapping the arguments does not hide one. A module whose `db` adds
rows or patches overrides the shared one and is not a copy.
"""
import ast
from pathlib import Path

TESTS = Path(__file__).parent


def _fixtures(path: Path):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            "fixture" in ast.unparse(d) for d in node.decorator_list
        ):
            yield node


def _shape(node) -> str:
    node = ast.parse(ast.unparse(node)).body[0]   # a copy, so renaming leaves the original
    node.name = "_"
    return ast.dump(node)


def test_no_module_copies_the_shared_db_fixture():
    [shared] = [f for f in _fixtures(TESTS / "conftest.py") if f.name == "db"]
    canonical = _shape(shared)
    copies = [
        f"{path.name}::{node.name}"
        for path in sorted(TESTS.rglob("*.py")) if path.name != "conftest.py"
        for node in _fixtures(path)
        if _shape(node) == canonical
    ]
    assert not copies, f"use conftest's `db` instead of a copy: {copies}"
