"""
The test suite's one in-memory database recipe (GitHub issues #50 and #72).

The engine + `create_all` block was copied into about ninety test modules, in fixtures,
in `_make_session()` helpers and inline in tests. `tests/conftest.py`'s `db` fixture is
built from this module, and so is every module that needs something `db` does not give
it: the engine itself (a session factory, a patched `AsyncSessionLocal`), only some of
the tables, or a schema created inside an event loop of its own (`TestClient`
fixtures). `tests/test_fixture_copies.py` fails a `create_all` or an in-memory
`create_async_engine(` anywhere else.

`StaticPool` is what makes `:memory:` one database: every checkout gets the same
connection, so the schema created here is the one the session sees.
"""
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (every table on Base.metadata)
from app.database import Base


def new_memory_engine() -> AsyncEngine:
    """An empty in-memory database, no schema. Synchronous, so a fixture that drives its
    own event loop can create the engine first and the schema inside that loop."""
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def create_schema(engine: AsyncEngine, tables=None) -> None:
    """The schema from the models: every table, or only `tables` (`Model.__table__`s)."""
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))


async def memory_engine(tables=None) -> AsyncEngine:
    """An in-memory database with the schema created: every table, or only `tables`."""
    engine = new_memory_engine()
    await create_schema(engine, tables=tables)
    return engine
