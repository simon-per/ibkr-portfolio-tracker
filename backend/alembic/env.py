from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import event
from sqlalchemy import pool

from alembic import context

# Import the base and all models
from app.database import Base
from app.models import Security, TaxLot, ExchangeRate, MarketPrice
from app.config import settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Override the sqlalchemy.url from settings - use sync version for Alembic
sync_url = settings.database_url.replace("+aiosqlite", "")
config.set_main_option("sqlalchemy.url", sync_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # `disable_existing_loggers=False` matters, and it is not cosmetic.
    #
    # fileConfig defaults to True, which switches off every logger already configured in
    # the process. In production that is invisible — alembic runs as its own process
    # before uvicorn starts — but anything running a migration *in process* silently
    # loses the application's logging from that point on. Found by
    # tests/test_migrations.py: adding it made an unrelated scheduler test fail, because
    # the warning it asserts on ("running in memory") was no longer being emitted at
    # all. A test suite is the first in-process caller; it will not be the last.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # The same busy timeout the application sets in app/database.py, and for a sharper
    # reason here.
    #
    # This runs from the container CMD as `alembic upgrade head && uvicorn ...`, so if
    # it cannot get the write lock it fails instantly, the `&&` short-circuits, and
    # uvicorn never starts — the container is simply down. A CLI holding the lock is not
    # hypothetical: STATUS.md warns that `resolve_identities` "commits once at the very
    # end" of a ~37-minute run, and auto-deploy ticks every ten minutes with no
    # knowledge of CLI runs. Without a timeout that overlap is an outage; with one it is
    # a pause.
    #
    # Deliberately not WAL: the application owns that setting and it is persistent in
    # the database file, so re-declaring it here would be a second place to change it.
    @event.listens_for(connectable, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - driver callback
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
