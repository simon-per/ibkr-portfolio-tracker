"""
`CryptoService`'s key sets against the `/api/crypto` response models, in both
directions.

A `response_model` is a filter (CLAUDE.md, *Conventions*): a key the service adds and the
model does not declare is dropped without a word, and a declared key the service stops
producing silently becomes its default. Pinned on a populated book — produced by a real
sync through the fake CoinStats — and on an empty one, since the empty shapes are built
separately and drift separately.
"""
import pytest
import pytest_asyncio

from app.schemas.crypto import (
    CryptoHistoryPoint,
    CryptoHistoryResponse,
    CryptoHoldingItem,
    CryptoLastRun,
    CryptoPortfolioResponse,
    CryptoStatusResponse,
    CryptoSyncResponse,
)
from app.services.crypto_service import CryptoService, CryptoSyncService
from tests.crypto_fakes import (
    FakeCoinStats,
    RecordingCurrencyService,
    configure,
    memory_session_factory,
)


def _fields(model) -> set:
    return set(model.model_fields)


@pytest_asyncio.fixture
async def factory():
    engine, factory = await memory_session_factory()
    try:
        yield factory
    finally:
        await engine.dispose()


async def _synced(factory, monkeypatch):
    configure(monkeypatch)
    return await CryptoSyncService(
        session_factory=factory,
        client_factory=FakeCoinStats().client_factory(),
        currency_service_factory=RecordingCurrencyService,
    ).sync()


@pytest.mark.asyncio
async def test_the_sync_result_matches_its_model(factory, monkeypatch):
    result = await _synced(factory, monkeypatch)
    assert set(result) == _fields(CryptoSyncResponse)
    CryptoSyncResponse.model_validate(result)


@pytest.mark.asyncio
async def test_a_populated_book_matches_every_model(factory, monkeypatch):
    await _synced(factory, monkeypatch)
    async with factory() as db:
        service = CryptoService(db)
        portfolio = await service.portfolio()
        history = await service.history()
        status = await service.status(next_run=None, sync_in_progress=False,
                                      retry_after_seconds=0)

    assert set(portfolio) == _fields(CryptoPortfolioResponse)
    assert portfolio["holdings"], "the fake book has holdings"
    for holding in portfolio["holdings"]:
        assert set(holding) == _fields(CryptoHoldingItem)
    CryptoPortfolioResponse.model_validate(portfolio)

    assert set(history) == _fields(CryptoHistoryResponse)
    assert history["points"]
    for point in history["points"]:
        assert set(point) == _fields(CryptoHistoryPoint)
    CryptoHistoryResponse.model_validate(history)

    assert set(status) == _fields(CryptoStatusResponse)
    assert set(status["last_run"]) == _fields(CryptoLastRun)
    CryptoStatusResponse.model_validate(status)


@pytest.mark.asyncio
async def test_an_empty_book_matches_every_model(factory):
    async with factory() as db:
        service = CryptoService(db)
        portfolio = await service.portfolio()
        history = await service.history()
        status = await service.status(next_run=None, sync_in_progress=False,
                                      retry_after_seconds=0)
    assert set(portfolio) == _fields(CryptoPortfolioResponse)
    assert set(history) == _fields(CryptoHistoryResponse)
    assert set(status) == _fields(CryptoStatusResponse)
    for body, model in ((portfolio, CryptoPortfolioResponse),
                        (history, CryptoHistoryResponse),
                        (status, CryptoStatusResponse)):
        model.model_validate(body)
