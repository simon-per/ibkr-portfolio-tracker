"""
bulk_create must be one statement per chunk, not three per row.

It looped upsert(), which does SELECT + flush + refresh for every row despite the
(security_id, date) unique constraint making SQLite's ON CONFLICT DO UPDATE
available. A split purge refill is ~500 rows and a cold 40-security sync ~20,000,
all inside the request that holds the sync-pipeline gate.

Counted rather than timed: a wall-clock assertion would be flaky, and the point is
the number of round trips.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.repositories.market_price_repository import MarketPriceRepository
from tests.memory_db import memory_engine


async def _db():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="A",
                         currency="USD", conid=1, asset_category="STK", exchange="NASDAQ"))
    await session.flush()
    await session.commit()
    return engine, session


def _rows(n, start=date(2026, 1, 1), price="100"):
    return [
        {"security_id": 1, "date": start + timedelta(days=i),
         "close_price": Decimal(price), "currency": "USD", "source": "yahoo_finance"}
        for i in range(n)
    ]


class _Counter:
    """Counts SQL statements actually emitted on the connection."""

    def __init__(self, engine):
        self.engine = engine.sync_engine
        self.statements = []

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "before_cursor_execute", self._on)
        return False

    def _on(self, conn, cursor, statement, params, context, executemany):
        self.statements.append(statement)

    def count(self, keyword):
        kw = keyword.upper()
        return sum(1 for s in self.statements if s.lstrip().upper().startswith(kw))


@pytest.mark.asyncio
async def test_500_rows_cost_a_handful_of_statements_not_1500():
    engine, session = await _db()
    try:
        repo = MarketPriceRepository(session)
        with _Counter(engine) as counted:
            written = await repo.bulk_create(_rows(500))
            await session.commit()

        assert written == 500
        # 500 / UPSERT_CHUNK(100) = 5 INSERTs. The old loop emitted 500 SELECTs
        # plus 500 INSERTs plus 500 refresh SELECTs.
        assert counted.count("INSERT") == 5
        assert counted.count("SELECT") == 0, "no per-row lookup should remain"

        stored = (await session.execute(select(MarketPrice))).scalars().all()
        assert len(stored) == 500
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_re_fetch_updates_in_place_rather_than_duplicating():
    """A split restates history, so a second pass must correct, not collide."""
    engine, session = await _db()
    try:
        repo = MarketPriceRepository(session)
        await repo.bulk_create(_rows(10, price="100"))
        await session.commit()

        assert await repo.bulk_create(_rows(10, price="42")) == 10
        await session.commit()

        stored = (await session.execute(select(MarketPrice))).scalars().all()
        assert len(stored) == 10, "upsert, not insert"
        assert {p.close_price for p in stored} == {Decimal("42")}
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_chunk_boundary_is_not_a_special_case():
    engine, session = await _db()
    try:
        repo = MarketPriceRepository(session)
        n = MarketPriceRepository.UPSERT_CHUNK * 2 + 1
        assert await repo.bulk_create(_rows(n)) == n
        await session.commit()

        stored = (await session.execute(select(MarketPrice))).scalars().all()
        assert len(stored) == n
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_empty_list_writes_nothing_and_emits_nothing():
    engine, session = await _db()
    try:
        repo = MarketPriceRepository(session)
        with _Counter(engine) as counted:
            assert await repo.bulk_create([]) == 0
        assert counted.count("INSERT") == 0
    finally:
        await session.close()
        await engine.dispose()
