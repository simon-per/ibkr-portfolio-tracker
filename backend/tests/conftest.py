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
injects a mock transport. The logo.dev key likewise: blank, the logo pass is `skipped`.

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
import logging
import logging.config
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="ibkr-tests-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TEST_DB_DIR / 'test.db').as_posix()}"
atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)

import pytest  # noqa: E402

from app import rate_limit  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.currency_service import reset_frankfurter_rejections  # noqa: E402


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
    monkeypatch.setattr(settings, "logo_dev_secret_key", "", raising=False)
    rate_limit.reset()
    reset_frankfurter_rejections()
    yield
    rate_limit.reset()
    reset_frankfurter_rejections()


@pytest.fixture
def production_log_handlers():
    """
    The handlers that write the container log, configured the way production does it:
    uvicorn's own `LOGGING_CONFIG` first (uvicorn applies it before importing the app),
    then `main._configure_logging()`. Yields the root handler and uvicorn's two, so a
    test can format captured records through exactly what `docker compose logs` shows.

    Global logging is put back afterwards — `force=True` would otherwise strip pytest's
    capture handler, and uvicorn's `propagate=False` would hide its loggers from caplog
    for every later test.
    """
    from _pytest.logging import LogCaptureHandler
    from uvicorn.config import LOGGING_CONFIG

    import app.main as main

    names = ("uvicorn", "uvicorn.error", "uvicorn.access", *main._NOISY_AT_INFO)
    root = logging.getLogger()
    saved_root = (root.level, list(root.handlers))
    saved = {
        n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers),
            logging.getLogger(n).propagate)
        for n in names
    }
    logging.config.dictConfig(LOGGING_CONFIG)
    main._configure_logging()
    installed = [h for h in root.handlers if h not in saved_root[1]]
    assert len(installed) == 1, "_configure_logging should install exactly one root handler"
    try:
        yield SimpleNamespace(
            root=installed[0],
            uvicorn=logging.getLogger("uvicorn").handlers[0],
            access=logging.getLogger("uvicorn.access").handlers[0],
        )
    finally:
        root.setLevel(saved_root[0])
        # pytest's capture handlers are per test phase and already gone; putting the
        # setup phase's back would leave it collecting every record for the whole run.
        root.handlers[:] = [
            h for h in root.handlers if h is not installed[0]
        ] + [
            h for h in saved_root[1]
            if not isinstance(h, LogCaptureHandler) and h not in root.handlers
        ]
        for n, (level, handlers, propagate) in saved.items():
            logger = logging.getLogger(n)
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate = propagate
