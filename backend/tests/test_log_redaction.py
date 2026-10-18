"""
Secrets must not reach the container log (GitHub issue #14).

`redact_secrets` guarded what is stored and served, not what is logged. Three paths
leaked: an f-string interpolating a transport error (the Flex `t=` URL), the traceback
`exc_info` appends — a sanitised `RuntimeError(...) from e` still prints the chained
`e` — and uvicorn's own handlers, which `basicConfig` never touches. The fix is one
choke point, `RedactingFormatter`, installed by `app/logging_setup.configure_logging`.

So every assertion here is on a record **formatted by the handler production
installs** (`production_log_handlers` in conftest), i.e. what `docker compose logs`
shows — never on `record.getMessage()`, which is how the traceback leak passed review.

Offline: every transport error is built by hand in the shapes requests/urllib3/httpx
really produce.
"""
import ast
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401
from app.config import settings
from app.redact import RedactingFormatter
from app.services import ibkr_service as ibkr_module
from app.services import market_data_service as mds
from tests.memory_db import memory_engine

APP_DIR = Path(__file__).resolve().parent.parent / "app"

TOKEN = "SUPERSECRETFLEXTOKEN123456"
SEND_URL = (
    "https://gdcdyn.interactivebrokers.com/Universal/servlet/"
    f"FlexStatementService.SendRequest?v=3&t={TOKEN}&q=1"
)


def _formatted(handler, records):
    return "\n".join(handler.format(r) for r in records)


async def _run_scheduled_ibkr_sync(monkeypatch, exc):
    monkeypatch.setattr(settings, "ibkr_token", TOKEN, raising=False)

    async def no_sleep(_seconds):
        return None

    def boom(token, query_id):
        raise exc

    monkeypatch.setattr(ibkr_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(ibkr_module, "send_flex_request", boom)

    engine = await memory_engine()
    monkeypatch.setattr(
        "app.services.scheduler_service.AsyncSessionLocal",
        async_sessionmaker(engine, expire_on_commit=False),
    )
    from app.services.scheduler_service import SchedulerService
    try:
        return await SchedulerService().sync_ibkr_data(force=True)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_dns_failure_logged_by_the_scheduler_is_redacted(
    monkeypatch, caplog, production_log_handlers
):
    """A never-reached error is retried and the last one re-raised raw; the scheduler
    logs f"Failed to sync IBKR data: {str(e)}", so the message itself carries the URL."""
    import requests
    from urllib3.exceptions import MaxRetryError, NameResolutionError

    exc = requests.exceptions.ConnectionError(
        MaxRetryError(None, SEND_URL, NameResolutionError("gdcdyn.interactivebrokers.com", None, "DNS"))
    )
    with caplog.at_level(logging.ERROR):
        result = await _run_scheduled_ibkr_sync(monkeypatch, exc)

    assert result["status"] == "error"
    logged = _formatted(production_log_handlers.root, caplog.records)
    assert "Failed to sync IBKR data" in logged
    assert TOKEN not in logged
    assert "t=[REDACTED]" in logged


@pytest.mark.asyncio
async def test_an_ssl_failure_traceback_is_redacted(
    monkeypatch, caplog, production_log_handlers
):
    """A may-have-reached error becomes `RuntimeError(type name only) from e`: a clean
    message, but `exc_info=True` appends the chained requests exception — the URL."""
    import requests
    from urllib3.exceptions import MaxRetryError, SSLError as Urllib3SSLError

    exc = requests.exceptions.SSLError(
        MaxRetryError(None, SEND_URL, Urllib3SSLError("EOF occurred in violation of protocol"))
    )
    with caplog.at_level(logging.ERROR):
        result = await _run_scheduled_ibkr_sync(monkeypatch, exc)

    assert result["status"] == "error"
    logged = _formatted(production_log_handlers.root, caplog.records)
    assert "Traceback" in logged, "the traceback is the thing under test"
    assert TOKEN not in logged


@pytest.mark.asyncio
async def test_an_alpha_vantage_http_error_is_redacted(
    monkeypatch, caplog, production_log_handlers
):
    """market_data_service logs f"Error fetching Alpha Vantage data: {e}"; an httpx
    HTTPStatusError stringifies with the full URL, `apikey=` included."""
    key = "ALPHAVANTAGEKEY1234567"
    monkeypatch.setattr(mds.settings, "alpha_vantage_api_key", key)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(503, request=request))
    monkeypatch.setattr(
        mds.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)
    )

    service = mds.MarketDataService.__new__(mds.MarketDataService)
    service.alpha_vantage_capped = False
    security = SimpleNamespace(
        symbol="AMZN", exchange="NASDAQ", currency="USD", alpha_vantage_asked_at=None
    )
    with caplog.at_level(logging.ERROR):
        assert await service.fetch_prices_from_alpha_vantage(security) == []

    logged = _formatted(production_log_handlers.root, caplog.records)
    assert "Alpha Vantage" in logged
    assert key not in logged


def test_the_formatter_redacts_chained_tracebacks_and_stack_info(monkeypatch):
    monkeypatch.setattr(settings, "ibkr_token", TOKEN, raising=False)
    try:
        try:
            raise ConnectionError(f"Max retries exceeded with url: {SEND_URL}")
        except ConnectionError as e:
            raise RuntimeError("SendRequest failed (SSLError)") from e
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "app.test", logging.ERROR, __file__, 1, "sync failed", None, exc_info,
        sinfo=f"Stack (most recent call last):\n  token={TOKEN}",
    )

    out = RedactingFormatter("%(levelname)s %(name)s: %(message)s").format(record)

    assert "ConnectionError" in out and "Stack" in out
    assert TOKEN not in out


def test_uvicorns_handlers_redact_and_keep_their_layout(monkeypatch, production_log_handlers):
    """uvicorn, uvicorn.error and uvicorn.access have their own handlers with
    propagate=False; uvicorn.error logs the 500's traceback a second time after
    Starlette re-raises it. Their formatters are wrapped, not replaced."""
    monkeypatch.setattr(settings, "ibkr_token", TOKEN, raising=False)
    try:
        raise RuntimeError(f"SendRequest failed: {SEND_URL}")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application",
        None, exc_info,
    )

    assert logging.getLogger("uvicorn.error").propagate  # reaches the "uvicorn" handler
    out = production_log_handlers.uvicorn.format(record)
    assert out.startswith("ERROR:"), "uvicorn's own levelprefix layout is kept"
    assert "Traceback" in out
    assert TOKEN not in out
    assert isinstance(production_log_handlers.access.formatter, RedactingFormatter)


def test_nothing_in_app_calls_basicconfig_except_the_shared_helper():
    """A new CLI calling `logging.basicConfig` itself would get an unredacted handler."""
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "logging_setup.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Attribute, ast.Name))
                and getattr(node.func, "attr", getattr(node.func, "id", None)) == "basicConfig"
            ):
                offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
    assert not offenders, f"use app.logging_setup.configure_logging: {offenders}"
