"""
The public scheduler endpoints stay the stock pipeline's (docs/crypto.md).

The crypto sync records `crypto_sync` rows and registers jobs at the same slots as the
stock pipeline. Left alone, three public surfaces would change meaning: the dashboard's
"Last sync" after a restart (the newest row of any type), its "Next:" (`jobs[0]`), and
`/api/scheduler/history`, which is public while the crypto book is not.
"""
import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app import rate_limit
from app.database import Base, get_db
from app.main import app
from app.models.sync_run import SyncRun
from app.services.crypto_service import SYNC_TYPE
from app.services.scheduler_service import SCHEDULED_JOB_TYPES


@pytest.fixture()
def client():
    rate_limit.reset()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    session = AsyncSession(engine, expire_on_commit=False)
    loop = asyncio.new_event_loop()

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session.add_all([
            SyncRun(sync_type="market_data_only", status="success", message="stock run",
                    started_at=datetime(2026, 9, 20, 13), finished_at=datetime(2026, 9, 20, 13, 5)),
            SyncRun(sync_type=SYNC_TYPE, status="success", message="Crypto snapshot stored",
                    started_at=datetime(2026, 9, 20, 13), finished_at=datetime(2026, 9, 20, 13, 9)),
        ])
        await session.commit()

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


def test_last_sync_is_the_stock_pipelines_even_when_a_crypto_run_is_newer(client):
    body = client.get("/api/scheduler/status").json()
    assert body["last_sync"]["type"] == "market_data_only"
    assert "crypto_sync" not in SCHEDULED_JOB_TYPES


def test_the_public_history_leaves_the_crypto_runs_out(client):
    runs = client.get("/api/scheduler/history").json()["runs"]
    assert [r["type"] for r in runs] == ["market_data_only"]
