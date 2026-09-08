"""
The write-ahead log must be folded into portfolio.db before the container can disappear.

`docker-compose.yml` bind-mounts `./portfolio.db` as a FILE, so SQLite's `-wal`/`-shm`
sidecars live in the container's writable layer and vanish with `docker compose down`. Until
2026-09-08 every deploy silently discarded whatever had been committed since the last
auto-checkpoint — measured: a `sync_runs` row from 18:23 UTC gone after the 18:30 deploy,
one from 18:07 still there, and a 2.7 MB WAL inside the container against 0 bytes on the host.

Three places now checkpoint, and each is pinned here because each covers a case the others
cannot: the app on a clean shutdown (a plain `docker stop`), `deploy.sh` from outside the
container before `down` (a shutdown that never runs the lifespan), and `backup-db.sh` before
the host-side copy (the host cannot see the container's WAL at all).
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "deploy.sh"
BACKUP_SH = REPO_ROOT / "ops" / "backup-db.sh"

CHECKPOINT = "wal_checkpoint(TRUNCATE)"


class _EngineSpy:
    """
    Stands in for `app.main.engine`, whose attributes are read-only. `connect()` is
    delegated (or made to fail) and `dispose()` is recorded, so the real PRAGMA still runs
    against the test database while the ordering stays observable.
    """

    def __init__(self, real, seen, fail_connect=False):
        self._real = real
        self.seen = seen
        self._fail_connect = fail_connect

    def connect(self):
        if self._fail_connect:
            class _Boom:
                async def __aenter__(self):
                    raise RuntimeError("no connection")

                async def __aexit__(self, *exc):
                    return False
            return _Boom()
        return self._real.connect()

    async def dispose(self):
        self.seen.append("dispose")
        await self._real.dispose()


def test_shutdown_checkpoints_the_wal_and_disposes_the_engine(monkeypatch):
    """Entering and leaving the TestClient runs the lifespan; both halves must run, in order."""
    import app.main as main

    seen = []
    real_checkpoint = main.checkpoint_and_dispose_engine

    async def spy_checkpoint():
        seen.append("checkpoint")
        await real_checkpoint()

    monkeypatch.setattr(main, "engine", _EngineSpy(main.engine, seen))
    # Wrap rather than replace, so the real PRAGMA still executes against the test database.
    monkeypatch.setattr(main, "checkpoint_and_dispose_engine", spy_checkpoint)

    with TestClient(main.app):
        pass

    assert seen == ["checkpoint", "dispose"], seen


def test_the_shutdown_helper_survives_a_failing_checkpoint(monkeypatch):
    """A checkpoint that cannot run must not stop the pool from being disposed — dispose is
    where SQLite's own final checkpoint happens, so it is the more important half."""
    import app.main as main

    seen = []
    monkeypatch.setattr(main, "engine", _EngineSpy(main.engine, seen, fail_connect=True))

    import asyncio
    asyncio.run(main.checkpoint_and_dispose_engine())

    assert seen == ["dispose"]


@pytest.mark.skipif(not DEPLOY_SH.exists(), reason="deploy.sh not present")
def test_deploy_checkpoints_inside_the_running_container_before_down():
    """
    Between `build` and `down`, so the old container is still up to run it and the new image
    is already built — the WAL it folds in is everything committed since the last
    auto-checkpoint, which is exactly what `down` used to destroy.
    """
    text = DEPLOY_SH.read_text(encoding="utf-8")
    build = text.index("docker compose build --no-cache")
    checkpoint = text.index(CHECKPOINT)
    down = text.index("docker compose down")
    assert build < checkpoint < down, "the WAL checkpoint must sit between build and down"
    # It has to run INSIDE the container: the host cannot see the container's WAL.
    line = next(l for l in text.splitlines() if CHECKPOINT in l)
    assert re.search(r"docker compose exec\b", line), line


@pytest.mark.skipif(not BACKUP_SH.exists(), reason="ops/backup-db.sh not present")
def test_the_backup_checkpoints_before_copying_the_host_file():
    text = BACKUP_SH.read_text(encoding="utf-8")
    checkpoint = text.index(CHECKPOINT)
    copy = text.index("src.backup(dest)")
    assert checkpoint < copy, "the host-side copy would otherwise lack the container's WAL"
    line = next(l for l in text.splitlines() if CHECKPOINT in l)
    assert "docker exec" in line, line
