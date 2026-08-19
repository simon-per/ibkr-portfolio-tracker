"""
The migrations themselves, run rather than read.

`alembic upgrade head` is the first thing `backend/Dockerfile`'s CMD does, gating
uvicorn with `&&`, so it is the one production step whose failure is a container that
never serves — and until now nothing in the suite ran it. `grep -rn alembic tests/`
matched two docstrings. `test_model_metadata.py` builds the schema from
`Base.metadata`, which validates the *models* and says nothing about whether the
migration chain that actually produces production's schema still works.

Three concrete ways that gap bites, all of them boot failures rather than wrong
answers:

* **Two heads.** A migration written on a branch, merged, and `alembic upgrade head`
  fails with "Multiple head revisions are present". Live risk rather than theory:
  `git worktree list` currently shows two checkouts sitting on branches whose alembic
  head is two revisions behind this one.
* **A broken `down_revision`,** which silently detaches part of the chain.
* **Drift between the models and the migrations.** A model added without a migration
  works everywhere, because `app/main.py` used to run `Base.metadata.create_all()` on
  every boot and quietly created it — until the migration written for it later failed
  on production with "table already exists". That `create_all` is gone now, and
  `test_the_migrations_produce_the_schema_the_models_describe` is what keeps it
  unnecessary.

Runs against a temporary sqlite file, so it touches nothing real and needs no network.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.database import Base

BACKEND = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND / "alembic.ini"

# Columns whose absence from one side is expected rather than drift. Empty today, and
# it should stay that way: an entry here is a place where the schema production runs
# and the schema the code believes in have been allowed to differ on purpose.
KNOWN_DIVERGENCES: dict[str, set[str]] = {}


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    """A database built the way production builds one: by running the migrations."""
    from app.config import settings

    db_path = tmp_path / "migrated.db"
    # env.py rewrites the URL from `settings.database_url`, so setting it on the Config
    # alone is not enough. Patched through monkeypatch rather than by assigning to
    # os.environ, which is what the first version did — and `settings` is a singleton
    # built at import, so that assignment did nothing here and leaked a DATABASE_URL
    # into every test that ran afterwards. It surfaced as an unrelated scheduler test
    # failing in the full suite while passing on its own.
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    return db_path, cfg


def test_there_is_exactly_one_head():
    """
    Two heads is a boot failure, and it is produced by ordinary merging rather than by
    anybody doing something wrong — which is why it needs a test rather than care.
    """
    script = ScriptDirectory(str(BACKEND / "alembic"))
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"alembic has {len(heads)} heads ({heads}); `alembic upgrade head` will refuse "
        f"and the container will not start. Merge them with `alembic merge`."
    )


def test_every_revision_is_reachable_from_the_head():
    """
    A typo'd `down_revision` detaches part of the chain: the head still resolves, the
    orphaned revisions simply never run, and the tables they create are missing on a
    fresh install while existing on every migrated one.
    """
    script = ScriptDirectory(str(BACKEND / "alembic"))
    head = script.get_current_head()
    reachable = {rev.revision for rev in script.iterate_revisions(head, "base")}
    everything = {rev.revision for rev in script.walk_revisions()}
    orphans = everything - reachable
    assert not orphans, f"revisions unreachable from head: {sorted(orphans)}"


def test_upgrade_head_succeeds_on_an_empty_database(migrated):
    """The literal production boot step, on a database that has never seen one."""
    db_path, _ = migrated
    assert db_path.exists()
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "alembic_version" in tables, "alembic did not stamp its version table"
    assert len(tables) > 5, f"suspiciously few tables after upgrade head: {sorted(tables)}"


def test_the_migrations_produce_the_schema_the_models_describe(migrated):
    """
    The check that lets `Base.metadata.create_all()` stay out of the application.

    Both directions matter and they fail differently. A table or column the models
    declare and the migrations do not create is an `OperationalError` on the first query
    that touches it — loud, and only in production. One the migrations create and the
    models no longer declare is silent: it just sits there, and the next person to read
    the model believes the column is gone.
    """
    db_path, _ = migrated
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        migrated_tables = set(inspector.get_table_names()) - {"alembic_version"}
        model_tables = set(Base.metadata.tables)

        assert not (model_tables - migrated_tables), (
            "these tables are declared by a model but no migration creates them: "
            f"{sorted(model_tables - migrated_tables)}"
        )
        assert not (migrated_tables - model_tables), (
            "these tables exist after `upgrade head` but no model declares them: "
            f"{sorted(migrated_tables - model_tables)}"
        )

        for table in sorted(model_tables):
            migrated_cols = {c["name"] for c in inspector.get_columns(table)}
            model_cols = set(Base.metadata.tables[table].columns.keys())
            allowed = KNOWN_DIVERGENCES.get(table, set())
            missing = model_cols - migrated_cols - allowed
            extra = migrated_cols - model_cols - allowed
            assert not missing, (
                f"{table}: declared by the model, never created by a migration: "
                f"{sorted(missing)}"
            )
            assert not extra, (
                f"{table}: created by a migration, no longer declared by the model: "
                f"{sorted(extra)}"
            )
    finally:
        engine.dispose()


def test_the_chain_downgrades_all_the_way_back(migrated):
    """
    Every revision here already has a real `downgrade()`, which is what makes a bad
    migration recoverable rather than a restore-from-backup. Asserting it keeps the next
    one from shipping with a `pass`.

    Note `o8d5f2a9b3c4` refuses up front when cross-source same-day dividend rows exist,
    rather than half-downgrading — on an empty database there are none, so it proceeds.
    """
    _, cfg = migrated
    command.downgrade(cfg, "base")


def test_the_migration_engine_sets_a_busy_timeout():
    """
    Source-read rather than behavioural, and worth saying why: the pragma is applied to
    a connection alembic opens and closes inside `command.upgrade`, so by the time a
    test could ask, there is nothing left to ask.

    What it protects: `backend/Dockerfile`'s CMD is
    `alembic upgrade head && uvicorn ...`. Without a timeout, a write lock held by
    something else makes alembic fail instantly, the `&&` short-circuits, and the
    container never serves — an outage rather than a pause. That overlap is not
    hypothetical: `resolve_identities` commits once at the end of a ~37-minute run, and
    auto-deploy ticks every ten minutes knowing nothing about CLI runs.

    Comments are stripped first. A paragraph explaining the pragma satisfies a naive
    substring check, which is how the deploy tests in this repository first passed
    against their own mutants.
    """
    source = (BACKEND / "alembic" / "env.py").read_text(encoding="utf-8")
    code = chr(10).join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "busy_timeout" in code, (
        "alembic/env.py must set PRAGMA busy_timeout on the migration connection — "
        "see app/database.py, which does the same for the application"
    )
    assert 'listens_for(connectable, "connect")' in code, (
        "the pragma has to run per connection, via a connect listener"
    )


def test_alembic_does_not_disable_the_application_loggers():
    """
    `fileConfig` defaults to `disable_existing_loggers=True`, which switches off every
    logger already configured in the process. Invisible in production, where alembic is
    its own process — and it silently removed the application's logging from any
    in-process caller. Found by this file: adding a test that runs `upgrade head` made
    an unrelated scheduler test fail, because the warning it asserts on was no longer
    emitted at all.
    """
    source = (BACKEND / "alembic" / "env.py").read_text(encoding="utf-8")
    code = chr(10).join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "disable_existing_loggers=False" in code, (
        "alembic/env.py's fileConfig must pass disable_existing_loggers=False, or "
        "running a migration in-process silently kills the app's logging"
    )
