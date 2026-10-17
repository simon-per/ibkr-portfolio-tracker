"""
The suite never writes into the developer's database (tests/conftest.py).

Until 2026-10-17 the lifespan tests and the module `AsyncSessionLocal` wrote into whatever
`DATABASE_URL` named — `backend/.env`'s in a checkout, which may be a production snapshot
the developer still needs. `conftest.py` now points the process at a temporary file before
`settings` exists; these pin that the engine the app actually uses is that file.
"""
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import settings
from app.database import engine


def _db_path(url) -> Path:
    return Path(make_url(str(url)).database).resolve()


def test_the_settings_point_at_a_throwaway_file():
    path = _db_path(settings.database_url)
    assert path.parent.name.startswith("ibkr-tests-"), path
    assert Path(tempfile.gettempdir()).resolve() in path.parents, path


def test_the_app_engine_is_bound_to_that_file_not_to_dot_env():
    # The engine is built at import from `settings`; were the variable set too late, the
    # settings could read the temp file while the engine still held `.env`'s path.
    assert _db_path(engine.url) == _db_path(settings.database_url)
