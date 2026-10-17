"""
Shared test setup.

The middleware added in `app/main.py` keeps **process-lifetime** state — the rate
limiter's per-client window counters — and reads production defaults from `settings`.
Both leak across tests: a module that drives the HTTP stack hundreds of times inside
one minute would start seeing 429s from requests an earlier module made, and the
failure would move around as tests were reordered.

So every test runs with the limiter off and write auth off, i.e. the behaviour the
suite was written against. `tests/test_api_hardening.py` re-enables them explicitly,
which is the only place their behaviour is the thing under test.

The scheduler's job store is neutralised for the same reason: its default path is
relative to the working directory, so a test that starts a scheduler would drop a
`scheduler_jobs.db` into the repo and carry state into the next run.

The CoinStats credentials are blanked so a developer's real key and share token in
`backend/.env` can never make a test spend credits: with them empty, every crypto path
short-circuits as "not configured" unless a test deliberately configures fakes and
injects a mock transport.

**The suite gets a database of its own, one per process** (2026-10-17). Most tests build
an in-memory engine, but anything that enters the real lifespan (`with TestClient(app)`:
`create_all`, the default ticker-mapping seed, the WAL checkpoint) or reaches the module
`AsyncSessionLocal` wrote into whatever `DATABASE_URL` named — in a developer's checkout
that is `backend/.env`'s, which may be a production snapshot they still need, and under
xdist every worker wrote into the same file at once. `settings` is built once, at import,
so the variable is set here, before anything below imports `app.config`; an environment
variable outranks `.env` in pydantic-settings. A test that wants a particular file still
patches `settings.database_url` itself (`test_migrations.py`). Each process — the xdist
controller and every worker — gets its own directory, removed when it exits.
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="ibkr-tests-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TEST_DB_DIR / 'test.db').as_posix()}"
atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)

import pytest  # noqa: E402

from app import rate_limit  # noqa: E402
from app.config import settings  # noqa: E402


def pytest_configure(config):
    # Registered here because there is deliberately no pytest.ini (.github/workflows/ci.yml).
    config.addinivalue_line(
        "markers",
        "deploy_scripts: runs deploy.sh / ops/*.sh under bash. Seconds each under Git Bash, "
        "so the documented local command deselects it; CI runs it and gates the deploy.",
    )


@pytest.fixture(autouse=True)
def _neutral_process_state(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0, raising=False)
    monkeypatch.setattr(settings, "api_admin_token", "", raising=False)
    monkeypatch.setattr(settings, "scheduler_jobstore_url", "", raising=False)
    monkeypatch.setattr(settings, "coin_stats_api_key", "", raising=False)
    monkeypatch.setattr(settings, "coin_stats_share_token", "", raising=False)
    monkeypatch.setattr(settings, "coin_stats_share_passcode", "", raising=False)
    monkeypatch.setattr(settings, "coingecko_api_key", "", raising=False)
    rate_limit.reset()
    yield
    rate_limit.reset()
