"""
Every integer a route takes from the URL is bounded to what SQLite can bind (GitHub #64).

`GET /api/logos/99999999999999999999` reached the primary-key lookup, where the driver
raised `OverflowError` (SQLite's INTEGER is signed 64-bit): a 500 with its traceback
logged twice, on a route the rate limiter exempts. `/api/analyst-ratings/{id}` did the same.
The question is not "is this route bounded" but "is there an unbounded int at all", so the
walk is over the live route table and a new route inherits the rule or fails here:

- statically, every int path or query parameter (`Optional[int]` included) carries a lower
  and an upper bound inside SQLite's range, and every int path parameter is a row id >= 1
  (`app/routers/_params.py`'s `DbId`);
- at runtime, every route with an int path parameter answers 422, not 500, to an id past
  the range and to 0, before any handler runs, and logs nothing at ERROR.
"""
import logging
import typing

import annotated_types
import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.routers._params import DB_INT_MAX

DB_INT_MIN = -DB_INT_MAX - 1


def _walk(dependant):
    yield dependant
    for sub in dependant.dependencies:
        yield from _walk(sub)


def _is_int(annotation) -> bool:
    if annotation is int:
        return True
    args = typing.get_args(annotation)
    return int in args and all(a in (int, type(None)) for a in args)


def _bounds(field_info):
    lower = upper = None
    for m in field_info.metadata:
        if isinstance(m, annotated_types.Ge):
            lower = m.ge
        elif isinstance(m, annotated_types.Gt):
            lower = m.gt + 1
        elif isinstance(m, annotated_types.Le):
            upper = m.le
        elif isinstance(m, annotated_types.Lt):
            upper = m.lt - 1
    return lower, upper


def _int_params():
    """(methods, path, kind, name, lower, upper) for every int parameter on every route."""
    out = []
    for ctx in iter_route_contexts(app.routes):
        dependant = getattr(ctx, "dependant", None)
        if dependant is None:
            continue
        for dep in _walk(dependant):
            for kind, params in (("path", dep.path_params), ("query", dep.query_params)):
                for p in params:
                    if _is_int(p.field_info.annotation):
                        out.append((
                            sorted(ctx.methods or ()), ctx.path, kind, p.name,
                            *_bounds(p.field_info),
                        ))
    return out


def test_the_walk_reaches_the_known_int_parameters():
    """A floor, so an API change that hides the parameters cannot pass this vacuously."""
    seen = {(path, name) for _, path, _, name, _, _ in _int_params()}
    assert {
        ("/api/logos/{security_id}", "security_id"),
        ("/api/analyst-ratings/{security_id}", "security_id"),
        ("/api/watchlist/{item_id}", "item_id"),
        ("/api/scheduler/history", "limit"),
        ("/api/portfolio/activity", "offset"),
    } <= seen


def test_every_int_parameter_is_bounded_to_what_sqlite_can_bind():
    unbounded = [
        f"{methods} {path} {kind} {name}: [{lower}, {upper}]"
        for methods, path, kind, name, lower, upper in _int_params()
        if lower is None or upper is None or lower < DB_INT_MIN or upper > DB_INT_MAX
    ]
    assert not unbounded, (
        "bound these (app/routers/_params.py: `DbId` for a path id, "
        f"`le=DB_INT_MAX` at most for a query int): {unbounded}"
    )


def test_every_int_path_parameter_is_a_positive_row_id():
    wrong = [
        f"{methods} {path} {name}: [{lower}, {upper}]"
        for methods, path, kind, name, lower, upper in _int_params()
        if kind == "path" and (lower != 1 or upper != DB_INT_MAX)
    ]
    assert not wrong, f"use `DbId` from app/routers/_params.py: {wrong}"


def _int_path_routes():
    routes = {}
    for methods, path, kind, name, _, _ in _int_params():
        if kind == "path":
            for method in methods:
                routes.setdefault((method, path), set()).add(name)
    return sorted(routes.items())


@pytest.fixture
def probe():
    """No database: every request here must be refused by validation before a handler."""

    async def _no_db():
        yield None

    app.dependency_overrides[get_db] = _no_db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("value", [str(2**64), str(DB_INT_MAX + 1), "0", "-1"])
@pytest.mark.parametrize("method_path", [r for r, _ in _int_path_routes()],
                         ids=lambda r: f"{r[0]} {r[1]}")
def test_an_id_out_of_range_is_a_422_without_a_traceback(probe, caplog, method_path, value):
    method, path = method_path
    url = path
    for name in dict(_int_path_routes())[method_path]:
        url = url.replace("{" + name + "}", value)
    with caplog.at_level(logging.ERROR):
        r = probe.request(method, url, json={})
    assert r.status_code == 422, f"{method} {url}: {r.status_code}"
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
