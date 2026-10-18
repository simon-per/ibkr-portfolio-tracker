"""
Every route that takes `start_date`/`end_date` goes through the one window guard.

The guard was hand-written four times in the routers and missing from two routes, which
answered 500 to `start_date=0001-01-01` while their siblings answered 400 (issue #30).
This pins the family, so the next window route inherits the guard or fails here:

- statically, every router function taking the window calls `validate_window` or
  `default_window` from `app/routers/_windows.py`, and no other router module carries its
  own copy of the cap;
- at runtime, every mounted route with a `start_date` or `end_date` query parameter
  refuses an out-of-range, an inverted and a too-long window with a 400, before any
  service runs.
"""
import ast
import asyncio
from pathlib import Path

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app import rate_limit
from app.config import settings
from app.database import Base, get_db
from app.main import app

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"
GUARD_MODULE = ROUTERS / "_windows.py"
GUARDS = {"validate_window", "default_window"}
WINDOW_PARAMS = {"start_date", "end_date"}


def _called_names(func: ast.AST) -> set:
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def _router_modules():
    return [p for p in sorted(ROUTERS.glob("*.py")) if p != GUARD_MODULE]


def test_every_router_function_taking_the_window_calls_the_shared_guard():
    offenders, seen = [], []
    for path in _router_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg for a in node.args.args + node.args.kwonlyargs}
            if not params & WINDOW_PARAMS:
                continue
            seen.append(f"{path.name}:{node.name}")
            if not _called_names(node) & GUARDS:
                offenders.append(f"{path.name}:{node.name}")
    assert seen, "no router function takes start_date/end_date — this test lost its subject"
    assert not offenders, (
        f"{offenders} take start_date/end_date without calling validate_window or "
        "default_window (app/routers/_windows.py)"
    )


def test_no_router_carries_its_own_copy_of_the_cap():
    copies = []
    for path in _router_modules():
        source = path.read_text(encoding="utf-8")
        for marker in ("365 * 5", "Date range too large"):
            if marker in source:
                copies.append(f"{path.name}: {marker!r}")
    assert not copies, f"a second window guard: {copies} — use app/routers/_windows.py"


def _window_routes():
    # `iter_route_contexts`, not `app.routes`: an included router is one lazy node there
    # (see test_api_hardening.py), so the flat walk would find nothing.
    routes = []
    for ctx in iter_route_contexts(app.routes):
        dependant = getattr(ctx.route, "dependant", None)
        if dependant is None or "GET" not in (ctx.methods or ()):
            continue
        if {p.name for p in dependant.query_params} & WINDOW_PARAMS:
            routes.append(ctx.path)
    return sorted(routes)


WINDOW_ROUTES = _window_routes()


def test_the_runtime_family_is_the_one_expected():
    """A sanity floor, so a refactor that hides the params cannot empty the parametrisation."""
    for path in (
        "/api/portfolio/value-over-time",
        "/api/portfolio/annualized-return",
        "/api/portfolio/attribution",
        "/api/portfolio/activity",
        "/api/portfolio/benchmark",
        "/api/performance/decomposition",
    ):
        assert path in WINDOW_ROUTES


@pytest.fixture
def client(monkeypatch):
    # No `with`: the lifespan (scheduler, sync) never starts. An empty in-memory schema,
    # so a route whose guard is missing fails on its own terms rather than on a table.
    rate_limit.reset()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0, raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(setup())

    async def override():
        async with AsyncSession(engine, expire_on_commit=False) as s:
            yield s

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


@pytest.mark.parametrize("path", WINDOW_ROUTES)
@pytest.mark.parametrize("query", [
    "start_date=0001-01-01&end_date=0001-01-10",   # within the span cap, outside the calendar
    "start_date=2026-03-02&end_date=9999-12-31",
    "start_date=2026-03-02&end_date=2025-03-02",   # inverted
    "start_date=2015-03-02&end_date=2026-03-02",   # longer than five years
])
def test_every_window_route_refuses_a_bad_window_with_400(client, path, query):
    r = client.get(f"{path}?{query}")
    assert r.status_code == 400, f"{path}?{query} answered {r.status_code}: {r.text[:200]}"
