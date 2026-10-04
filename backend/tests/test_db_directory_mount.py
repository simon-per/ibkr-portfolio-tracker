"""
The database lives inside a DIRECTORY bind mount, and nothing can start the app on an empty one.

Until 2026-10-04 `docker-compose.yml` mounted `./portfolio.db:/app/portfolio.db` — a file —
so SQLite's `-wal`/`-shm` sidecars lived in the container's writable layer and every
`docker compose down` destroyed the commits since the last auto-checkpoint
(`test_wal_checkpoint_on_shutdown.py` pins the three checkpoints that mitigated it). The fix
moves the database to `backend/data/portfolio.db` and mounts `./data:/app/data`.

The move runs inside an unattended deploy, so what these tests guard above all is the one
outcome that must never happen: the app starting on a new, EMPTY database while the real one
sits at the old path. Three layers, each pinned here:

* `ops/db-layout.sh` — run by `deploy.sh` — recognises exactly three layouts (migrated,
  legacy, fresh-with-permission), refuses everything else without moving anything, performs
  the one-time move idempotently, and leaves the old path as a symlink so a rollback to an
  older commit, or the stale `/root/backup-db.sh` copy, still finds the real file.
* `app/db_guard.py`, first in the container's CMD, refuses a missing or empty database, so
  even a deploy that never ran the script (an older deploy.sh with this compose file) fails
  its health check instead of serving an empty account.
* compose pins `DATABASE_URL` itself, so the host `.env` cannot keep pointing at the old path.

The script is RUN against throwaway directories rather than read: a test satisfied by the
words in a shell script is how this repository's deploy tests first passed their mutants.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

import pytest
import yaml

from app.db_guard import refusal, sqlite_database_path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
COMPOSE_FILE = BACKEND_DIR / "docker-compose.yml"
DOCKERFILE = BACKEND_DIR / "Dockerfile"
DEPLOY_SH = REPO_ROOT / "deploy.sh"
BACKUP_SH = REPO_ROOT / "ops" / "backup-db.sh"
DB_LAYOUT = REPO_ROOT / "ops" / "db-layout.sh"

CHECKPOINT = "wal_checkpoint(TRUNCATE)"


def _code_only(source: str) -> str:
    """Comment lines removed, so an explanation cannot satisfy an assertion about code."""
    return "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("#"))


def _service() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]["portfolio-backend"]


# --- compose, Dockerfile, ignore files --------------------------------------------------


def test_compose_mounts_the_database_directory_not_the_file():
    service = _service()
    targets = [PurePosixPath(v.split(":")[1]) for v in service["volumes"]]
    assert not [t for t in targets if t.name.endswith(".db")], (
        f"a .db file is bind-mounted directly ({targets}); its -wal would live in the "
        "container layer and die with `docker compose down`"
    )

    url = service["environment"]["DATABASE_URL"]
    path = sqlite_database_path(url)
    assert path is not None, f"compose's DATABASE_URL is not a sqlite file: {url!r}"
    db = PurePosixPath(path.as_posix())
    assert db.is_absolute(), (
        f"compose's DATABASE_URL must be absolute (four slashes), got {url!r}: a relative "
        "one depends on the working directory of whatever process reads it"
    )
    assert db.parent in targets, (
        f"{db} must sit directly inside a mounted directory ({targets}) so its -wal "
        "lands on the host beside it"
    )


def test_compose_owns_the_database_url_and_the_fresh_install_flag():
    """
    `environment:` overrides `env_file:`. The host's .env predates the move and may still
    name ./portfolio.db, so the URL has to be set where the mount is. The fresh-install
    flag defaults OFF: the guard is only worth anything if nobody has to remember it.
    """
    env = _service()["environment"]
    assert "DATABASE_URL" in env
    assert env.get("ALLOW_NEW_DATABASE") == "${ALLOW_NEW_DATABASE:-0}", env.get("ALLOW_NEW_DATABASE")


def test_the_container_runs_the_guard_before_alembic():
    """`&&`, because alembic creates a missing database file: the guard has to stop the
    chain, not merely complain before it."""
    cmd = next(l for l in DOCKERFILE.read_text(encoding="utf-8").splitlines() if l.startswith("CMD"))
    assert re.search(r"python -m app\.db_guard\s*&&\s*alembic upgrade head", cmd), cmd


def test_the_data_directory_is_never_committed_or_baked_into_the_image():
    """The repository is public, and data/ also holds the sidecars db-layout.sh sets aside
    under names (`portfolio.db-wal.pre-switchover-...`) no `*.db*` pattern catches."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "backend/data/" in [l.strip() for l in gitignore]
    dockerignore = (BACKEND_DIR / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "data/" in [l.strip() for l in dockerignore]


# --- deploy.sh and backup-db.sh ---------------------------------------------------------


def test_deploy_checks_before_building_and_moves_between_down_and_up():
    code = _code_only(DEPLOY_SH.read_text(encoding="utf-8"))
    preflight = code.index('ops/db-layout.sh" preflight')
    build = code.index("docker compose build")
    down = code.index("docker compose down")
    switch = code.index('ops/db-layout.sh" switch')
    up = code.index("docker compose up -d")
    assert preflight < build, "the read-only check must run while the old containers still serve"
    assert down < switch < up, "the file must not move under a running container"
    assert "touch portfolio.db" not in code, (
        "deploy.sh must not create an empty database — that is what made an app on an "
        "empty file look like a normal start"
    )


@pytest.mark.parametrize("script", [DEPLOY_SH, BACKUP_SH], ids=["deploy.sh", "backup-db.sh"])
def test_the_checkpoints_ask_the_container_where_its_database_is(script):
    """
    Either side of the move a running container may be on either layout — the deploy that
    performs the move stops one still on the file mount — so neither checkpoint may name a
    path. `mode=rw` so a wrong path fails instead of sqlite creating an empty file there.
    """
    line = next(l for l in script.read_text(encoding="utf-8").splitlines() if CHECKPOINT in l)
    assert "settings.database_url" in line, line
    assert "mode=rw" in line, line
    assert "/app/portfolio.db" not in line, line


def _bash() -> str | None:
    found = shutil.which("bash")
    if found and "system32" in found.lower():  # WSL's launcher, not a POSIX bash for these paths
        found = None
    if not found and sys.platform == "win32":
        for candidate in (r"C:\Program Files\Git\usr\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
    return found


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


def _backup_db_resolution() -> str:
    """The DB-default block of ops/backup-db.sh, lifted out so it can be run."""
    match = re.search(r'^if \[ -z "\$\{DB:-\}" \]; then\n.*?^fi$',
                      BACKUP_SH.read_text(encoding="utf-8"), re.S | re.M)
    assert match, "the DB default block was not found in ops/backup-db.sh"
    return match.group(0)


@needs_bash
@pytest.mark.parametrize(
    "layout,explicit,expected",
    [
        ("legacy", None, "backend/portfolio.db"),         # before the move: the live file
        ("migrated", None, "backend/data/portfolio.db"),
        ("neither", None, "backend/data/portfolio.db"),   # then "not found", loudly
        ("legacy", "/elsewhere/x.db", "/elsewhere/x.db"),  # an explicit DB is never rewritten
    ],
)
def test_backup_defaults_to_the_data_directory(tmp_path, layout, explicit, expected):
    backend = tmp_path / "backend"
    backend.mkdir()
    if layout == "legacy":
        (backend / "portfolio.db").write_bytes(b"x")
    if layout == "migrated":
        (backend / "data").mkdir()
        (backend / "data" / "portfolio.db").write_bytes(b"x")
    repo = tmp_path.as_posix()
    env = {**os.environ, "REPO_DIR": repo}
    env.pop("DB", None)
    if explicit:
        env["DB"] = explicit
    out = subprocess.run([BASH, "-c", _backup_db_resolution() + '\necho "$DB"'],
                         env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    want = expected if explicit else f"{repo}/{expected}"
    assert out.stdout.strip() == want


# --- app/db_guard.py --------------------------------------------------------------------


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def test_guard_refuses_a_missing_database(tmp_path):
    assert "does not exist" in refusal(_url(tmp_path / "data" / "portfolio.db"), allow_new=False)


def test_guard_refuses_an_empty_database(tmp_path):
    """`touch portfolio.db` was how the old deploy.sh made a mount source exist."""
    db = tmp_path / "portfolio.db"
    db.touch()
    assert "empty" in refusal(_url(db), allow_new=False)


def test_guard_refuses_a_directory(tmp_path):
    """What Docker creates for a missing file bind-mount source."""
    db = tmp_path / "portfolio.db"
    db.mkdir()
    assert "not a regular file" in refusal(_url(db), allow_new=False)


def test_guard_accepts_a_real_database(tmp_path):
    db = tmp_path / "portfolio.db"
    sqlite3.connect(db).execute("create table t (a int)").connection.close()
    assert refusal(_url(db), allow_new=False) is None


def test_guard_accepts_a_fresh_install_only_when_told(tmp_path):
    missing = _url(tmp_path / "portfolio.db")
    assert refusal(missing, allow_new=True) is None
    assert refusal(missing, allow_new=False) is not None


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///:memory:", "sqlite://", "postgresql://u@h/db"])
def test_guard_has_nothing_to_say_about_non_file_databases(url):
    assert refusal(url, allow_new=False) is None


def test_guard_main_exits_non_zero_so_the_cmd_chain_stops(tmp_path, monkeypatch, capsys):
    from app import db_guard
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", _url(tmp_path / "data" / "portfolio.db"))
    monkeypatch.delenv("ALLOW_NEW_DATABASE", raising=False)
    assert db_guard.main() == 1
    assert "REFUSING TO START" in capsys.readouterr().err

    monkeypatch.setenv("ALLOW_NEW_DATABASE", "1")
    assert db_guard.main() == 0


def test_guard_resolves_a_relative_url_the_way_sqlite_does():
    """Relative to the working directory, which is how local dev's ./portfolio.db works."""
    assert sqlite_database_path("sqlite+aiosqlite:///./portfolio.db") == Path("./portfolio.db")
    assert sqlite_database_path("sqlite+aiosqlite:////app/data/portfolio.db").as_posix() == "/app/data/portfolio.db"


# --- ops/db-layout.sh, run ---------------------------------------------------------------


def _layout_env(backend: Path, allow: bool = False) -> dict:
    env = {**os.environ, "BACKEND_DIR": backend.as_posix()}
    env.pop("ALLOW_NEW_DATABASE", None)
    if allow:
        env["ALLOW_NEW_DATABASE"] = "1"
    # Git Bash copies a file for `ln -s` unless told to make a real link; the VPS is Linux.
    env["MSYS"] = "winsymlinks:nativestrict"
    return env


def _layout(backend: Path, sub: str, allow: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, DB_LAYOUT.as_posix(), sub], env=_layout_env(backend, allow),
                          capture_output=True, text=True, timeout=60)


def _symlinks_work() -> bool:
    if BASH is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        probe = subprocess.run([BASH, "-c", "echo x > t && ln -s t l && [ -L l ]"], cwd=tmp,
                               env=_layout_env(Path(tmp)), capture_output=True, timeout=30)
    return probe.returncode == 0


# The VPS is Linux and can always link; Windows without developer mode cannot. CI (Ubuntu)
# runs every one of these.
needs_links = pytest.mark.skipif(not _symlinks_work(), reason="this machine cannot create symlinks")


@pytest.fixture
def backend(tmp_path):
    """A throwaway backend/ directory for ops/db-layout.sh to act on."""
    if BASH is None:
        pytest.skip("bash not available")
    d = tmp_path / "backend"
    d.mkdir()
    return d


def _make_db(path: Path, value: str = "the account") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("create table t (v text)")
    c.execute("insert into t values (?)", (value,))
    c.commit()
    c.close()


def _read(path: Path) -> list:
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return [r[0] for r in c.execute("select v from t")]
    finally:
        c.close()


def _snapshot(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): (p.is_symlink(), p.read_bytes() if p.is_file() else None)
            for p in sorted(d.rglob("*"))}


@needs_links
def test_the_legacy_layout_is_moved_once_and_linked_back(backend):
    legacy = backend / "portfolio.db"
    _make_db(legacy)

    before = _snapshot(backend)
    pre = _layout(backend, "preflight")
    assert pre.returncode == 0, pre.stderr
    assert _snapshot(backend) == before, "preflight must not touch anything"

    first = _layout(backend, "switch")
    assert first.returncode == 0, first.stderr
    target = backend / "data" / "portfolio.db"
    assert target.is_file() and not target.is_symlink()
    assert legacy.is_symlink(), "the old path must become a link, not a hole"
    assert legacy.resolve() == target.resolve()
    assert _read(target) == ["the account"]
    assert _read(legacy) == ["the account"], "a reader of the old path must reach the real data"

    # Idempotent: every later deploy runs it too.
    again = _layout(backend, "switch")
    assert again.returncode == 0, again.stderr
    assert "nothing to move" in again.stdout
    assert _layout(backend, "preflight").returncode == 0


@pytest.mark.skipif(_symlinks_work(), reason="needs a machine where `ln -s` fails")
def test_a_move_that_cannot_leave_its_link_is_undone(backend):
    """
    Half a switchover is the dangerous state: database moved, old path empty. A rollback's
    older deploy.sh would `touch portfolio.db` there and start the old build on an empty
    file. So if the link cannot be made, the file goes back. Runs where links cannot be
    made (Windows without developer mode); on Linux `ln -s` does not fail on demand.
    """
    _make_db(backend / "portfolio.db")
    result = _layout(backend, "switch")
    assert result.returncode != 0
    assert "moved the database back" in result.stderr
    assert (backend / "portfolio.db").is_file()
    assert not (backend / "data" / "portfolio.db").exists()
    assert _read(backend / "portfolio.db") == ["the account"]


@needs_links
def test_host_side_sidecars_are_set_aside_not_replayed(backend):
    """Under the file mount the container never saw the host's -wal; carrying it along
    would let SQLite replay it into the database at its new home."""
    _make_db(backend / "portfolio.db")
    (backend / "portfolio.db-wal").write_bytes(b"stale host wal")
    (backend / "portfolio.db-shm").write_bytes(b"stale host shm")

    result = _layout(backend, "switch")
    assert result.returncode == 0, result.stderr
    data = backend / "data"
    assert not (data / "portfolio.db-wal").exists()
    assert not (data / "portfolio.db-shm").exists()
    kept = sorted(p.name for p in data.glob("portfolio.db-wal.pre-switchover-*"))
    assert len(kept) == 1 and (data / kept[0]).read_bytes() == b"stale host wal"
    assert _read(data / "portfolio.db") == ["the account"]


def test_an_already_migrated_layout_without_the_link_is_fine(backend):
    _make_db(backend / "data" / "portfolio.db")
    assert _layout(backend, "preflight").returncode == 0
    result = _layout(backend, "switch")
    assert result.returncode == 0, result.stderr
    assert not (backend / "portfolio.db").exists()


def _assert_refused_untouched(backend: Path, sub: str, allow: bool = False):
    before = _snapshot(backend)
    result = _layout(backend, sub, allow)
    assert result.returncode != 0, f"{sub} accepted a layout it must refuse:\n{result.stdout}"
    assert "REFUSE" in result.stderr
    assert _snapshot(backend) == before, f"{sub} changed something while refusing"


@pytest.mark.parametrize("sub", ["preflight", "switch"])
def test_two_databases_are_refused(backend, sub):
    _make_db(backend / "portfolio.db", "old path")
    _make_db(backend / "data" / "portfolio.db", "new path")
    _assert_refused_untouched(backend, sub)


@needs_links
@pytest.mark.parametrize("sub", ["preflight", "switch"])
def test_a_link_to_some_other_file_is_refused(backend, sub):
    _make_db(backend / "data" / "portfolio.db")
    _make_db(backend / "elsewhere.db")
    subprocess.run([BASH, "-c", "ln -s elsewhere.db portfolio.db"], cwd=backend,
                   env=_layout_env(backend), check=True, timeout=30)
    _assert_refused_untouched(backend, sub)


@pytest.mark.parametrize("sub", ["preflight", "switch"])
def test_no_database_at_all_is_refused_without_permission(backend, sub):
    """The case `touch portfolio.db` used to paper over: the app then started empty."""
    _assert_refused_untouched(backend, sub)
    assert not (backend / "data").exists()


def test_a_fresh_install_proceeds_only_when_told(backend):
    assert _layout(backend, "preflight", allow=True).returncode == 0
    result = _layout(backend, "switch", allow=True)
    assert result.returncode == 0, result.stderr
    assert (backend / "data").is_dir()
    assert not (backend / "data" / "portfolio.db").exists(), "the app creates it, not the script"


@needs_links
@pytest.mark.parametrize("sub", ["preflight", "switch"])
def test_a_dangling_link_is_refused(backend, sub):
    subprocess.run([BASH, "-c", "mkdir -p data && echo x > data/portfolio.db && "
                                "ln -s data/portfolio.db portfolio.db && rm data/portfolio.db"],
                   cwd=backend, env=_layout_env(backend), check=True, timeout=30)
    _assert_refused_untouched(backend, sub)


@pytest.mark.parametrize("sub", ["preflight", "switch"])
def test_the_directory_docker_creates_for_a_missing_file_mount_is_refused(backend, sub):
    (backend / "portfolio.db").mkdir()
    _assert_refused_untouched(backend, sub)


@pytest.mark.parametrize("sub", ["preflight", "switch"])
def test_a_stray_wal_at_the_destination_is_refused(backend, sub):
    _make_db(backend / "portfolio.db")
    (backend / "data").mkdir()
    (backend / "data" / "portfolio.db-wal").write_bytes(b"someone else's pages")
    _assert_refused_untouched(backend, sub)


@pytest.mark.parametrize("sub", ["preflight", "switch"])
def test_data_that_is_not_a_directory_is_refused(backend, sub):
    _make_db(backend / "portfolio.db")
    (backend / "data").write_bytes(b"")
    _assert_refused_untouched(backend, sub)


@needs_links
def test_an_empty_data_directory_from_dockers_auto_create_is_not_an_obstacle(backend):
    """What an older deploy.sh bringing this compose file up leaves behind: Docker creates
    ./data, the guard refuses, the rollback restores the old layout — and the next deploy
    must still be able to move the file."""
    _make_db(backend / "portfolio.db")
    (backend / "data").mkdir()
    result = _layout(backend, "switch")
    assert result.returncode == 0, result.stderr
    assert _read(backend / "data" / "portfolio.db") == ["the account"]


@needs_links
@pytest.mark.skipif(sys.platform == "win32", reason="SQLite's unix VFS is what resolves the link")
def test_sqlite_through_the_legacy_link_writes_its_wal_beside_the_real_file(backend):
    """
    Why the link is safe for the stale /root/backup-db.sh and for anything else still
    naming the old path: SQLite resolves a symlinked database to its real name before
    naming the -wal (since 3.10.0), so a reader through the link and the container
    writing data/portfolio.db share one WAL instead of each seeing half the database.
    """
    _make_db(backend / "portfolio.db")
    assert _layout(backend, "switch").returncode == 0
    c = sqlite3.connect(backend / "portfolio.db")
    try:
        c.execute("insert into t values ('through the link')")
        c.commit()
        assert (backend / "data" / "portfolio.db-wal").exists()
        assert not (backend / "portfolio.db-wal").exists()
    finally:
        c.close()
    assert _read(backend / "data" / "portfolio.db") == ["the account", "through the link"]
