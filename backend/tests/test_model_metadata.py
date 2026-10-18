"""
Guards on the SQLAlchemy metadata itself.

Alembic owns the real schema, so nothing in production ever calls
`Base.metadata.create_all()` — which meant the metadata could quietly become invalid
without anyone noticing. It had: three models declared an index in `__table_args__` using
the *same name* SQLAlchemy already auto-generates for a `mapped_column(index=True)`, so the
metadata held two identically-named Index objects and create_all() died with
"index ix_... already exists". That only surfaced when a test tried to build the full
schema at once.
"""
import collections

import pytest

from app.database import Base
import app.models  # noqa: F401  - registers every model on the metadata
from tests.memory_db import create_schema, new_memory_engine


def test_no_table_declares_the_same_index_name_twice():
    offenders = {}
    for table in Base.metadata.tables.values():
        counts = collections.Counter(ix.name for ix in table.indexes)
        duplicates = {name: n for name, n in counts.items() if n > 1}
        if duplicates:
            offenders[table.name] = duplicates

    assert not offenders, (
        f"Duplicate index names in metadata: {offenders}. A mapped_column(index=True) "
        f"already creates ix_<table>_<column>; don't also declare it in __table_args__."
    )


@pytest.mark.asyncio
async def test_full_schema_can_be_created_from_metadata():
    """The whole schema must build in one go, so integration tests can use it."""
    engine = new_memory_engine()
    try:
        await create_schema(engine)
    finally:
        await engine.dispose()
