"""
Refuse to start the container on a database that is not there.

`backend/Dockerfile`'s CMD runs `python -m app.db_guard` before `alembic upgrade head`,
because alembic — and SQLite itself — will happily create a missing database file, and
an app serving a new, empty database looks exactly like an account that lost everything.
Worse than looking: the next IBKR sync would rebuild trades, cash flows and dividends from
a bounded Flex window into that empty file, and nothing before the window comes back.

The case this was written for is the 2026-10 move of the database from a FILE bind mount
(`./portfolio.db:/app/portfolio.db`) to a directory one (`./data:/app/data`, see
`ops/db-layout.sh`). Any path that brings the new compose file up without having moved the
file first — a deploy script from before the change, a hand-typed `docker compose up` —
would otherwise boot on an empty `data/portfolio.db` beside the real one. With this guard
it crash-loops instead, fails the deploy's health check, and the rollback restores the old
layout. Loud and down beats quiet and wrong.

An EMPTY file is refused too: `touch portfolio.db` is how the old deploy script made a
bind-mount source exist, and a 0-byte database is just as new as a missing one.

A genuinely fresh install sets `ALLOW_NEW_DATABASE=1` (docker-compose passes it through
from the environment `deploy.sh` runs in). Only the container runs this — local
development keeps creating `backend/portfolio.db` on first start, as it always has.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from sqlalchemy.engine import make_url


def sqlite_database_path(database_url: str) -> Optional[Path]:
    """The file a SQLite URL points at, or None for anything that is not a file on disk."""
    url = make_url(database_url)
    if not url.get_backend_name().startswith("sqlite"):
        return None
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database)


def refusal(database_url: str, allow_new: bool) -> Optional[str]:
    """Why the app must not start on this database, or None when it may."""
    path = sqlite_database_path(database_url)
    if path is None or allow_new:
        return None
    if not path.exists():
        return f"database file {path} does not exist"
    if not path.is_file():
        return f"database path {path} is not a regular file"
    if path.stat().st_size == 0:
        return f"database file {path} is empty (0 bytes)"
    return None


def main() -> int:
    from app.config import settings

    reason = refusal(settings.database_url, os.environ.get("ALLOW_NEW_DATABASE") == "1")
    if reason is None:
        return 0
    print(
        f"REFUSING TO START: {reason}. Starting here would create a new, empty database "
        "and serve it as the account. If the real database sits elsewhere, move it "
        "(ops/db-layout.sh, docs/deployment.md \"The database directory\"); if this is a "
        "genuinely fresh install, set ALLOW_NEW_DATABASE=1 for the first start.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
