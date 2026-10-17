"""
Where yfinance keeps its on-disk cache: beside the database, so it outlives a deploy.

yfinance caches each ticker's exchange timezone, the A3 cookie from fc.yahoo.com and its
ISIN lookups in three small SQLite files under `platformdirs.user_cache_dir()/py-yfinance`
— `/root/.cache` in the container, which has no `USER`. That is the image's writable
layer, and `deploy.sh` rebuilds with `--no-cache` and runs `down` / `up -d`, so every
deploy started with an empty cache: each ticker's first `history()` then paid an extra
`chart?range=1d` request to learn its timezone, and the cookie was fetched again
(GitHub issue #11, item f). The crumb is per process and is unaffected.

`yf.set_tz_cache_location` (the public name; it moves all three databases together)
points them at `yf-cache/` in the database's own directory: `/app/data/yf-cache` in the
container, which is the `./data` DIRECTORY bind mount, so it lands on the host and
survives `down`. Derived from `DATABASE_URL` rather than configured, so there is no new
setting to keep in step with the mount. It is a subdirectory, never a file beside
`portfolio.db`: `app/db_guard.py`, `ops/db-layout.sh` and `ops/backup-db.sh` each look at
`portfolio.db` and its `-wal`/`-shm`/`-journal` sidecars by name and nothing else, and
the backup copies only the database, so the cache is ephemeral in the one sense that
matters — losing it costs a request per ticker once, never data.

Best effort by construction: yfinance creates the directory on first use and, if it
cannot, falls back to a dummy cache and logs it. Call this before the first `Ticker` is
built — the lifespan does, ahead of the scheduler, and so does every CLI that reaches
Yahoo.
"""
import logging
from pathlib import Path
from typing import Optional

from app.config import settings
from app.db_guard import sqlite_database_path

logger = logging.getLogger(__name__)

YF_CACHE_DIRNAME = "yf-cache"


def yfinance_cache_dir(database_url: Optional[str] = None) -> Optional[Path]:
    """`yf-cache/` beside the SQLite database, absolute; None for a non-file database."""
    db_path = sqlite_database_path(database_url or settings.database_url)
    if db_path is None:
        return None
    return (db_path.parent / YF_CACHE_DIRNAME).resolve()


def configure_yfinance_cache() -> Optional[Path]:
    """Point yfinance's caches at `yfinance_cache_dir()`. Returns the directory, or None."""
    cache_dir = yfinance_cache_dir()
    if cache_dir is None:
        return None
    try:
        import yfinance as yf

        yf.set_tz_cache_location(str(cache_dir))
    except Exception as e:  # never cost the site its startup over a cache
        logger.warning(f"Could not move the yfinance cache to {cache_dir}: {e}")
        return None
    logger.info(f"yfinance cache at {cache_dir}")
    return cache_dir
