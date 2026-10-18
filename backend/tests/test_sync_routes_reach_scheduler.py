"""
Every public `POST …/sync` route runs its pass through a `SchedulerService` method
(GitHub issue #65).

`POST /api/market-data/sync` carried its own copy of the scheduler's market-data step —
status rule, result dict, rate-limit warning, diagnostics — and it had drifted: it never
passed `refresh_provisional`, so a weekend manual sync re-downloaded every cached close
that could no longer change (the scheduled path keys that on the calendar, issue #9), and
its rate-limit warning said something else. `POST /api/dividends/sync` ran sync-then-compute
like `SchedulerService.sync_dividends` but dropped `rate_limited` and the warnings and
recorded no `sync_runs` row. Both routes now call the scheduler's bodies; the shared gate
and the public cooldown are the only things a route adds.

The family check is route-level, from the app's own route table, so a new `…/sync` route
is caught the day it exists: it reaches a `SchedulerService` method or it is named in
NO_SCHEDULER_BODY with the reason it has none. Offline: every Yahoo-facing call is stubbed.
"""
import ast
import inspect
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException
from fastapi.routing import iter_route_contexts
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register all mappers
from app import single_flight as sf
from app.database import Base
from app.main import app as fastapi_app
from app.models.security import Security
from app.models.sync_run import SyncRun
from app.routers import dividends as div_router
from app.routers import market_data as md_router
from app.services import scheduler_service as sched
from app.services.dividend_service import DividendService
from app.services.scheduler_service import BERLIN, SchedulerService

# The `POST …/sync` routes with no scheduler body to call, and why. A route in neither
# this table nor reaching a SchedulerService method fails the family test below.
NO_SCHEDULER_BODY = {
    "/api/allocation/sync": "nothing schedules the allocation pass; the route is its only caller",
    "/api/analyst-ratings/sync": "the full ratings pass; the scheduler runs only the stale one",
    "/api/fundamentals/sync": "the full (or forced) fundamentals pass; the scheduler runs only "
                              "the stale one",
    "/api/watchlist/sync": "its body is `WatchlistService.sync_all` itself, called with the "
                           "caller's `force`; the scheduled job adds only its run record",
    "/api/crypto/sync": "the crypto book's own gates (never SYNC_PIPELINE); its body is "
                        "`CryptoSyncService.sync`, the same call `crypto_sync_job` makes",
}


def _post_sync_routes() -> dict:
    # `iter_route_contexts`, not `app.routes`: an included router is one lazy node there
    # (FastAPI 0.141+), and the flat walk would find no route at all.
    return {
        ctx.path: ctx.route.endpoint
        for ctx in iter_route_contexts(fastapi_app.routes)
        if "POST" in (ctx.methods or ()) and ctx.path.endswith("/sync")
    }


def _scheduler_methods_reached(endpoint) -> set:
    """
    The `get_scheduler().<name>(...)` calls reachable from a route, following the
    module's own functions it calls or hands over (`background_tasks.add_task(_run, …)`).
    """
    tree = ast.parse(inspect.getsource(inspect.getmodule(endpoint)))
    defs = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reached, seen, todo = set(), set(), [endpoint.__name__]
    while todo:
        name = todo.pop()
        if name in seen or name not in defs:
            continue
        seen.add(name)
        for node in ast.walk(defs[name]):
            if isinstance(node, ast.Name):
                todo.append(node.id)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Call)
                and getattr(node.func.value.func, "id", None) == "get_scheduler"
            ):
                reached.add(node.func.attr)
    return {name for name in reached if callable(getattr(SchedulerService, name, None))}


def test_every_post_sync_route_reaches_a_scheduler_method():
    routes = _post_sync_routes()
    assert "/api/market-data/sync" in routes and "/api/dividends/sync" in routes

    offenders = [
        path for path, endpoint in sorted(routes.items())
        if path not in NO_SCHEDULER_BODY and not _scheduler_methods_reached(endpoint)
    ]
    assert not offenders, (
        f"{offenders} run their pass without a SchedulerService method: a second copy of "
        "a job body drifts from the scheduled one (issue #65). Call the scheduler's body "
        "and add only the gate and the cooldown, or name the route in NO_SCHEDULER_BODY "
        "with the reason it has none."
    )


def test_the_exemptions_are_still_true():
    routes = _post_sync_routes()
    for path in NO_SCHEDULER_BODY:
        assert path in routes, f"{path} is no longer a POST …/sync route; drop it"
        assert not _scheduler_methods_reached(routes[path]), (
            f"{path} reaches the scheduler now; drop its exemption"
        )


def test_the_two_routes_reach_the_bodies_they_copied():
    routes = _post_sync_routes()
    assert "sync_market_data" in _scheduler_methods_reached(routes["/api/market-data/sync"])
    assert "dividend_sync_locked" in _scheduler_methods_reached(routes["/api/dividends/sync"])


# --- behaviour -----------------------------------------------------------------------


@pytest_asyncio.fixture
async def isolated(monkeypatch):
    """A fresh scheduler singleton over an in-memory database with one security, and a
    clean gate."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as seed:
        seed.add(Security(
            id=1, isin="US0000000001", symbol="S1", description="S1",
            currency="USD", conid=1001, asset_category="STK", exchange="NASDAQ",
        ))
        await seed.commit()

    svc = SchedulerService()
    monkeypatch.setattr(sched, "_scheduler_service", svc)
    monkeypatch.setattr(sched, "AsyncSessionLocal", maker)
    monkeypatch.setattr(sf, "_running", set())
    monkeypatch.setattr(sf, "_last_start", {})
    try:
        yield svc, maker
    finally:
        await engine.dispose()


def _pin_berlin(monkeypatch, day: date, hour: int = 10) -> None:
    moment = (
        datetime(day.year, day.month, day.day, hour, tzinfo=BERLIN)
        .astimezone(timezone.utc).replace(tzinfo=None)
    )
    monkeypatch.setattr(sched, "utcnow", lambda: moment)


def _stub_sweep(monkeypatch, swept=None, rate_limited_after=None):
    async def _sweep(self, securities, **kwargs):
        if swept is not None:
            swept.append(kwargs)
        return {"total_prices": 0, "processed": 1, "planned": 3, "errors": [],
                "rate_limited_after": rate_limited_after,
                "skipped_sold": 0, "skipped_backoff": 0}

    monkeypatch.setattr(
        "app.services.market_data_service.MarketDataService.sync_securities", _sweep,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("day, trading", [
    (date(2026, 10, 16), True),    # Friday
    (date(2026, 10, 17), False),   # Saturday
    (date(2026, 10, 18), False),   # Sunday
])
async def test_a_weekend_manual_market_sync_asks_only_for_missing_dates(
    isolated, monkeypatch, day, trading,
):
    """The acceptance criterion: `refresh_provisional=False` on a Berlin weekend, as the
    scheduled full sync does — and the scheduler's diagnostics on the response."""
    svc, _maker = isolated
    swept = []
    _stub_sweep(monkeypatch, swept)

    async def _diagnostics(db, **_kw):
        return ["a diagnostic"]

    monkeypatch.setattr(svc, "collect_market_data_diagnostics", _diagnostics)
    _pin_berlin(monkeypatch, day)

    result = await md_router.sync_market_data(days_back=730)

    assert swept == [{"days_back": 730, "refresh_provisional": trading}]
    assert result["status"] == "success"
    assert result["warnings"] == ["a diagnostic"]


@pytest.mark.asyncio
async def test_a_rate_limited_manual_sync_answers_what_the_scheduled_step_records(
    isolated, monkeypatch,
):
    """One body, so one status rule and one rate-limit warning: the route's answer is the
    step's result, timestamp aside."""
    svc, _maker = isolated
    _stub_sweep(monkeypatch, rate_limited_after="S1")

    async def _diagnostics(db, **_kw):
        return []

    monkeypatch.setattr(svc, "collect_market_data_diagnostics", _diagnostics)

    routed = await md_router.sync_market_data(days_back=7)
    direct = await svc.sync_market_data(days_back=7)
    routed.pop("timestamp")
    direct.pop("timestamp")

    assert routed == direct
    assert routed["status"] == "partial_success" and routed["rate_limited"] is True
    assert len(routed["warnings"]) == 1 and "rate-limited" in routed["warnings"][0]


@pytest.mark.asyncio
async def test_a_failed_market_step_is_still_a_500(isolated, monkeypatch):
    svc, _maker = isolated

    async def _failed(**_kw):
        return {"status": "error", "message": "Failed to sync market data: boom"}

    monkeypatch.setattr(svc, "sync_market_data", _failed)

    with pytest.raises(HTTPException) as exc:
        await md_router.sync_market_data(days_back=7)
    assert exc.value.status_code == 500
    assert "boom" in exc.value.detail


@pytest.mark.asyncio
async def test_the_market_route_keeps_the_gate_and_the_cooldown(isolated, monkeypatch):
    _stub_sweep(monkeypatch)
    svc, _maker = isolated

    async def _diagnostics(db, **_kw):
        return []

    monkeypatch.setattr(svc, "collect_market_data_diagnostics", _diagnostics)

    await md_router.sync_market_data(days_back=7)
    with pytest.raises(HTTPException) as exc:
        await md_router.sync_market_data(days_back=7)
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_a_manual_dividend_sync_records_a_run_with_its_warnings(isolated, monkeypatch):
    """The acceptance criterion: a `sync_runs` row, carrying the rate limit and both
    children's warnings that the route used to drop."""
    _svc, maker = isolated

    async def _sync(self):
        return {
            "securities_processed": 1, "dividends_added": 0, "errors": 0,
            "message": "Synced dividends: 0 records from 1 securities",
            "rate_limited": True,
            "warnings": ["Yahoo Finance rate limit reached; the rest was abandoned."],
        }

    async def _compute(self):
        return {"computed": 0, "warnings": ["1 dividend skipped: no FX rate"]}

    monkeypatch.setattr(DividendService, "sync_dividend_data", _sync)
    monkeypatch.setattr(DividendService, "compute_dividend_income", _compute)
    monkeypatch.setattr(div_router, "_sync_in_progress", False)

    tasks = BackgroundTasks()
    answer = await div_router.sync_dividends(tasks)
    assert answer["status"] == "started"
    await tasks()

    async with maker() as db:
        runs = (await db.execute(select(SyncRun))).scalars().all()
    assert [r.sync_type for r in runs] == ["dividend_sync"]
    run = runs[0]
    assert run.status == "success"
    assert run.message == "Synced dividends: 0 records from 1 securities"
    assert run.warnings == [
        "Yahoo Finance rate limit reached; the rest was abandoned.",
        "1 dividend skipped: no FX rate",
    ]
    assert run.details["rate_limited"] is True
    assert div_router._sync_in_progress is False


@pytest.mark.asyncio
async def test_a_dividend_run_is_not_the_portfolios_last_sync(isolated, monkeypatch):
    svc, _maker = isolated

    async def _empty(self):
        return {}

    monkeypatch.setattr(DividendService, "sync_dividend_data", _empty)
    monkeypatch.setattr(DividendService, "compute_dividend_income", _empty)

    result = await svc.dividend_sync_locked()

    assert result["type"] == "dividend_sync"
    assert result["type"] not in sched.SCHEDULED_JOB_TYPES
    assert svc.last_sync_result is None
