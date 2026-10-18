"""
The frontend's TypeScript interfaces must not read fields the backend stopped
sending.

`tests/test_dividend_summary_contract.py` pins one endpoint's key set against its
`response_model`, for a reason that generalises: a `response_model` is a *filter*,
so an undeclared key is dropped silently, and a key the client reads but the
server never sends arrives as `undefined` with no error anywhere. TypeScript
cannot catch either — it describes what we *believe* the wire carries, and
believing wrongly is exactly the failure.

**Paired by route, not by name.** The walk starts from the mounted app: every
route's `response_model`, unwrapped (`List[...]`, `Optional[...]`) and followed
into its nested models, read from the Pydantic classes themselves — wherever they
are defined, `app/schemas` or a router like `SettingsResponse`. Each is matched to
its TS interface by name, or through `TS_NAME_FOR` where the two names differ.
Until GitHub issue #44 this regex-scanned `app/schemas` and paired by exact name,
so `SettingsResponse` (where every figure's base currency comes from),
`PositionResponse` and `WatchlistItemResponse` were never compared, while three
fundamentals models no route used were "covered" — a pass that certified a model,
not the wire. Now a model whose name pairs with a TS interface but governs no
route fails, so a paired name always means the wire.

**The two directions are not symmetric, deliberately.** A field the frontend
declares and the backend does not send is a silent `undefined` and fails here. A
field the backend sends and the frontend does not read is fine and common —
`SecurityAttribution.disposal_proceeds_eur` is on the wire and unused — so it is
reported for information rather than failed on.

Skips entirely when `frontend/` is absent: the Docker image copies only `app/` and
`alembic/`, so the suite has to run there too.
"""

import importlib
import inspect
import pkgutil
import re
import typing
from pathlib import Path

import pytest
from fastapi.routing import iter_route_contexts
from pydantic import BaseModel

import app.routers
import app.schemas
from app.main import app as fastapi_app

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
API_TS = REPO / "frontend" / "src" / "lib" / "api.ts"

pytestmark = pytest.mark.skipif(
    not API_TS.exists(), reason="frontend/ is not present (Docker image ships app/ only)"
)

# Pydantic model -> TS interface, where the names differ. Every entry must name a model
# some route serves and an interface api.ts declares (`test_the_alias_map_has_no_stale_entry`).
TS_NAME_FOR = {
    "SettingsResponse": "AppSettings",
    "PositionResponse": "Position",
    "AnalystRatingInfo": "AnalystRating",
    "WatchlistItemResponse": "WatchlistItem",
    "FundamentalMetricsResponse": "FundamentalMetrics",
}


def _ts_interfaces() -> dict[str, set[str]]:
    """
    Field names of every `export interface X [extends A, B] { ... }` in api.ts, with
    the fields of what it extends folded in (`DecompositionYear extends DecompositionWindow`).
    """
    src = API_TS.read_text(encoding="utf-8")
    own: dict[str, set[str]] = {}
    parents: dict[str, list[str]] = {}
    for match in re.finditer(
        r"export interface (\w+)(?:\s+extends\s+([\w\s,]+?))?\s*\{(.*?)\n\}", src, re.S
    ):
        name, extends, body = match.group(1), match.group(2), match.group(3)
        own[name] = set(re.findall(r"^ {2}(\w+)\??\s*:", body, re.M))
        parents[name] = [p.strip() for p in (extends or "").split(",") if p.strip()]

    def resolved(name: str) -> set[str]:
        fields = set(own.get(name, ()))
        for parent in parents.get(name, ()):
            fields |= resolved(parent)
        return fields

    return {name: resolved(name) for name in own if resolved(name)}


def _models_in(annotation, out: set) -> None:
    """Every BaseModel reachable from a type: through generics and nested fields."""
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        if annotation not in out:
            out.add(annotation)
            for field in annotation.model_fields.values():
                _models_in(field.annotation, out)
        return
    for arg in typing.get_args(annotation):
        _models_in(arg, out)


def _route_models() -> dict[type, set[str]]:
    """
    Model -> the routes it governs, from the live route table. `iter_route_contexts`,
    not `app.routes`: since FastAPI 0.141 an included router is one lazy node there.
    """
    served: dict[type, set[str]] = {}
    for ctx in iter_route_contexts(fastapi_app.routes):
        model = getattr(ctx, "response_model", None)
        if model is None:
            continue
        found: set = set()
        _models_in(model, found)
        for cls in found:
            served.setdefault(cls, set()).update(
                f"{method} {ctx.path}" for method in sorted(ctx.methods or ())
            )
    return served


def _defined_models() -> dict[str, list[type]]:
    """Every BaseModel defined in `app/schemas` or `app/routers`, by class name."""
    out: dict[str, list[type]] = {}
    for package in (app.schemas, app.routers):
        for info in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(f"{package.__name__}.{info.name}")
            for name, cls in vars(module).items():
                if (inspect.isclass(cls) and issubclass(cls, BaseModel)
                        and cls.__module__ == module.__name__):
                    out.setdefault(name, []).append(cls)
    return out


def _wire_fields(model: type) -> set[str]:
    """
    The keys FastAPI serializes: by alias (`response_model_by_alias` defaults to true),
    plus computed fields.
    """
    return {
        field.serialization_alias or field.alias or name
        for name, field in model.model_fields.items()
    } | set(model.model_computed_fields)


def _ts_name(model: type) -> str:
    return TS_NAME_FOR.get(model.__name__, model.__name__)


INTERFACES = _ts_interfaces() if API_TS.exists() else {}
SERVED = _route_models() if API_TS.exists() else {}
PAIRED = sorted(
    (cls for cls in SERVED if _ts_name(cls) in INTERFACES), key=lambda cls: cls.__name__
)


def test_the_walk_finds_something():
    """
    A walk that quietly stops matching would turn every assertion below into a vacuous
    pass, which is worse than no test.
    """
    routes = {route for governed in SERVED.values() for route in governed}
    assert len(routes) > 25, f"only {len(routes)} routes with a response_model: {routes}"
    assert len(INTERFACES) > 20, f"only parsed {len(INTERFACES)} TS interfaces"
    assert len(PAIRED) > 40, f"only {len(PAIRED)} models paired"


def test_every_served_model_has_a_ts_twin():
    """
    Every model a route serves is compared, so none is covered by omission. A model the
    frontend reads under another name goes in `TS_NAME_FOR`.
    """
    names = [cls.__name__ for cls in SERVED]
    assert len(names) == len(set(names)), f"two served models share a name: {sorted(names)}"
    unpaired = {cls.__name__: sorted(SERVED[cls]) for cls in SERVED if cls not in PAIRED}
    assert not unpaired, (
        f"{unpaired} are served but match no TS interface in api.ts. Add the interface, "
        "or map the model to the one that reads it in TS_NAME_FOR."
    )


@pytest.mark.parametrize("name", [
    "SettingsResponse", "PositionResponse", "WatchlistItemResponse",
    "FundamentalMetricsResponse", "EarningsCalendarItem", "EarningsHistoryItem",
    "FundamentalsStatus",
])
def test_the_models_named_in_issue_44_are_compared(name):
    assert name in {cls.__name__ for cls in PAIRED}


@pytest.mark.parametrize("model", PAIRED, ids=lambda cls: cls.__name__)
def test_the_frontend_reads_no_field_the_backend_stopped_sending(model):
    missing = INTERFACES[_ts_name(model)] - _wire_fields(model)
    assert not missing, (
        f"{model.__name__} (TS {_ts_name(model)}): the TS interface declares "
        f"{sorted(missing)}, which the Pydantic model does not send. A `response_model` "
        f"filters undeclared keys, so these arrive as `undefined` with nothing raising — "
        f"add them to the model, or drop them from the interface."
    )


def test_no_paired_model_governs_nothing():
    """
    A model whose name pairs with a TS interface but which no route serves passes the
    comparison above while the route returns a bare dict nobody checks.
    """
    idle = sorted(
        name for name, classes in _defined_models().items()
        for cls in classes
        if _ts_name(cls) in INTERFACES and cls not in SERVED
    )
    assert not idle, (
        f"{idle} match a TS interface but no route uses them as its response_model. "
        "Attach each to its route (after checking the keys both ways), or delete it."
    )


def test_the_alias_map_has_no_stale_entry():
    served = {cls.__name__ for cls in SERVED}
    for model_name, ts_name in TS_NAME_FOR.items():
        assert model_name in served, f"TS_NAME_FOR[{model_name!r}]: no route serves it"
        assert ts_name in INTERFACES, f"TS_NAME_FOR[{model_name!r}]: api.ts has no {ts_name}"
        assert model_name not in INTERFACES, (
            f"TS_NAME_FOR[{model_name!r}]: api.ts declares {model_name} too, so the "
            "pairing is ambiguous"
        )


def test_backend_only_fields_are_reported_but_allowed(capsys):
    """
    Not a failure: the wire may legitimately carry more than any screen renders.
    Printed so an intentionally-unused field stays visible rather than becoming
    invisible dead weight nobody can justify.
    """
    unread = {
        cls.__name__: sorted(_wire_fields(cls) - INTERFACES[_ts_name(cls)])
        for cls in PAIRED
        if _wire_fields(cls) - INTERFACES[_ts_name(cls)]
    }
    if unread:
        print("\nOn the wire but unread by the frontend:")
        for name, fields in sorted(unread.items()):
            print(f"  {name}: {', '.join(fields)}")
    # Only that the check ran; the assertion is the printout.
    assert isinstance(unread, dict)
