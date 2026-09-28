"""
The crypto routes are private and fail closed (docs/crypto.md).

The rest of `/api/` publishes its reads; `/api/crypto` does not, at the owner's request.
So every method there except OPTIONS needs the admin key, and with no key configured
the routes refuse outright rather than opening up — a lock that opens when it is
misconfigured is not a lock. Walked over the live route table, like the mutating-route
walk in `test_api_hardening.py`, so a crypto route added later is covered the moment it
exists.
"""
import asyncio

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app import rate_limit
from app.auth import API_KEY_HEADER, PRIVATE_PREFIXES, is_private_path
from app.config import settings
from app.database import Base, get_db
from app.main import app

TOKEN = "test-admin-token-long-enough"


@pytest.fixture()
def client(monkeypatch):
    rate_limit.reset()
    monkeypatch.setattr(settings, "api_admin_token", TOKEN, raising=False)
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    session = AsyncSession(engine, expire_on_commit=False)
    loop = asyncio.new_event_loop()

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    loop.run_until_complete(_setup())

    async def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        loop.run_until_complete(session.close())
        loop.run_until_complete(engine.dispose())
        loop.close()
        rate_limit.reset()


def _crypto_routes():
    for ctx in iter_route_contexts(app.routes):
        if is_private_path(ctx.path):
            for method in sorted(ctx.methods or ()):
                yield method, ctx.path


def test_the_prefix_covers_the_crypto_routes_and_nothing_else():
    routes = list(_crypto_routes())
    assert {path for _, path in routes} >= {
        "/api/crypto/portfolio", "/api/crypto/history", "/api/crypto/status", "/api/crypto/sync",
    }
    assert PRIVATE_PREFIXES == ("/api/crypto",)
    assert is_private_path("/api/crypto") and is_private_path("/api/crypto/x")
    assert not is_private_path("/api/cryptox") and not is_private_path("/api/portfolio")


def test_every_crypto_route_refuses_a_request_without_the_key(client):
    checked = 0
    for method, path in _crypto_routes():
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"
        assert "private data" in response.json()["detail"]
        checked += 1
    assert checked >= 4


def test_a_wrong_key_is_refused_too(client):
    assert client.get("/api/crypto/portfolio", headers={API_KEY_HEADER: "nope"}).status_code == 401


def test_the_reads_answer_with_the_key_and_are_never_cached(client):
    for path in ("/api/crypto/portfolio", "/api/crypto/history", "/api/crypto/status"):
        response = client.get(path, headers={API_KEY_HEADER: TOKEN})
        assert response.status_code == 200, path
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.json()["configured"] is False


def test_head_is_gated_and_options_is_not(client):
    assert client.head("/api/crypto/portfolio").status_code == 401
    # A CORS preflight carries no credentials by design; it must reach CORS handling.
    preflight = client.options(
        "/api/crypto/portfolio",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code != 401


def test_with_no_key_configured_the_crypto_routes_fail_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "api_admin_token", "", raising=False)
    response = client.get("/api/crypto/portfolio")
    assert response.status_code == 403
    assert "API_ADMIN_TOKEN" in response.json()["detail"]
    # …while the stock book behaves exactly as it always has with auth off.
    assert client.get("/api/settings").status_code == 200


def test_the_stock_reads_stay_open_beside_it(client):
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/cryptox").status_code == 404  # routed, not refused


def test_the_sync_route_says_not_configured_rather_than_spending_its_cooldown(client):
    response = client.post("/api/crypto/sync", headers={API_KEY_HEADER: TOKEN})
    assert response.status_code == 409
    assert "not configured" in response.json()["detail"]
