"""
A base-currency switch the app cannot convert into is refused, and refused whole.

`BaseFx.convert` hands back the EUR amount when it has no EUR->base rate at all, so a
switch accepted while the rate provider was down labelled every figure in the app with
the new currency while the numbers stayed EUR — right numbers, wrong currency, nothing on
screen inviting doubt. `PUT /api/settings/base-currency` now validates first, backfills,
asks the read path's own loader whether it would find a rate, and only then writes the
setting (STATUS.md *Worth doing next*, the `BaseFx.convert` item).

The rate provider is always replaced here: the real `_batch_fetch_rates` calls
Frankfurter, and a test must never reach the network.
"""
import asyncio
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (before `app.main`, whose `app` must win the name)
from app import rate_limit
from app.auth import API_KEY_HEADER
from app.config import settings
from app.database import Base, get_db
from app.models.exchange_rate import ExchangeRate
from app.services.currency_service import CurrencyService
from app.main import app

TOKEN = "test-admin-token-long-enough"
AUTH = {API_KEY_HEADER: TOKEN}


@pytest.fixture()
def env(monkeypatch):
    rate_limit.reset()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0, raising=False)
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

    calls = []

    def provider(behaviour):
        """Replace the Frankfurter backfill: 'empty' answers nothing, 'down' raises,
        'ok' stores one EUR->target rate for today, as a successful fetch would."""
        async def fake(self, from_currency, target_date, to_currency, days_back=30):
            calls.append(to_currency)
            if behaviour == "down":
                raise ConnectionError("frankfurter unreachable")
            if behaviour == "ok":
                self.session.add(ExchangeRate(
                    date=target_date, from_currency=from_currency,
                    to_currency=to_currency, rate=Decimal("0.93"),
                ))
                await self.session.flush()
                return True
            return False
        monkeypatch.setattr(CurrencyService, "_batch_fetch_rates", fake)

    def seed_rate(to_currency, on=None):
        session.add(ExchangeRate(
            date=on or date.today(), from_currency="EUR",
            to_currency=to_currency, rate=Decimal("1.08"),
        ))
        loop.run_until_complete(session.commit())

    async def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app), provider, seed_rate, calls
    finally:
        app.dependency_overrides.clear()
        loop.run_until_complete(session.close())
        loop.run_until_complete(engine.dispose())
        loop.close()
        rate_limit.reset()


def _base(client):
    return client.get("/api/settings").json()["base_currency"]


@pytest.mark.parametrize("behaviour", ["empty", "down"])
def test_a_switch_with_no_rate_anywhere_is_refused_and_changes_nothing(env, behaviour):
    client, provider, _seed, calls = env
    provider(behaviour)
    assert _base(client) == "EUR"

    r = client.put("/api/settings/base-currency", json={"base_currency": "CHF"}, headers=AUTH)

    assert r.status_code == 503
    assert "not changed" in r.json()["detail"]
    assert calls == ["CHF"], "the backfill must still have been tried"
    assert _base(client) == "EUR"


def test_a_switch_whose_rates_arrive_succeeds(env):
    client, provider, _seed, _calls = env
    provider("ok")

    r = client.put("/api/settings/base-currency", json={"base_currency": "CHF"}, headers=AUTH)

    assert r.status_code == 200, r.text
    assert r.json()["base_currency"] == "CHF"
    assert _base(client) == "CHF"


def test_one_cached_rate_is_enough_while_the_provider_is_down(env):
    # `BaseFx` carries the nearest rate forward and back, so a stored rate converts every
    # day — stale at worst, never a mislabelled EUR figure.
    client, provider, seed_rate, _calls = env
    seed_rate("USD")
    provider("down")

    r = client.put("/api/settings/base-currency", json={"base_currency": "USD"}, headers=AUTH)

    assert r.status_code == 200, r.text
    assert _base(client) == "USD"


def test_a_rate_for_another_currency_does_not_count(env):
    client, provider, seed_rate, _calls = env
    seed_rate("USD")
    provider("empty")

    r = client.put("/api/settings/base-currency", json={"base_currency": "CHF"}, headers=AUTH)

    assert r.status_code == 503
    assert _base(client) == "EUR"


def test_eur_needs_no_rate_and_asks_for_none(env):
    client, provider, seed_rate, calls = env
    seed_rate("CHF")
    provider("empty")
    assert client.put(
        "/api/settings/base-currency", json={"base_currency": "CHF"}, headers=AUTH,
    ).status_code == 200
    calls.clear()
    provider("down")

    r = client.put("/api/settings/base-currency", json={"base_currency": "eur"}, headers=AUTH)

    assert r.status_code == 200, r.text
    assert _base(client) == "EUR"
    assert calls == []


def test_an_unsupported_currency_is_refused_before_anything_runs(env):
    client, provider, _seed, calls = env
    provider("ok")

    r = client.put("/api/settings/base-currency", json={"base_currency": "GBP"}, headers=AUTH)

    assert r.status_code == 400
    assert "Unsupported base currency" in r.json()["detail"]
    assert calls == []
    assert _base(client) == "EUR"
