"""
yfinance's on-disk cache lives on the data mount, set before any Ticker is built
(GitHub issue #11, item f). Offline: yfinance's setter and Ticker are recording stubs.
"""
from pathlib import Path

import pytest
import yfinance as yf
from fastapi.testclient import TestClient

import app.services.scheduler_service as scheduler_service
from app import yf_cache
from app.config import settings
from app.yf_cache import YF_CACHE_DIRNAME, configure_yfinance_cache, yfinance_cache_dir


def test_the_cache_is_a_subdirectory_beside_the_database(tmp_path):
    db = tmp_path / "data" / "portfolio.db"
    cache = yfinance_cache_dir(f"sqlite+aiosqlite:///{db.as_posix()}")
    assert cache == (tmp_path / "data" / YF_CACHE_DIRNAME).resolve()
    # Never a file beside the database: db_guard, db-layout.sh and backup-db.sh match
    # `portfolio.db` and its sidecars by name.
    assert not cache.name.startswith("portfolio.db")


def test_the_container_url_lands_on_the_data_mount():
    cache = yfinance_cache_dir("sqlite+aiosqlite:////app/data/portfolio.db")
    assert (cache.parent.name, cache.name) == ("data", YF_CACHE_DIRNAME)


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///:memory:", "postgresql://h/db"])
def test_no_file_database_means_no_cache_move(url, monkeypatch):
    calls = []
    monkeypatch.setattr(yf, "set_tz_cache_location", calls.append)
    monkeypatch.setattr(settings, "database_url", url)
    assert configure_yfinance_cache() is None
    assert calls == []


def test_a_failing_setter_never_costs_startup(monkeypatch):
    def boom(_path):
        raise OSError("read-only")

    monkeypatch.setattr(yf, "set_tz_cache_location", boom)
    assert configure_yfinance_cache() is None


def test_startup_sets_the_cache_before_any_ticker_is_built(monkeypatch):
    import app.main as main

    seen = []
    monkeypatch.setattr(yf, "set_tz_cache_location", lambda path: seen.append(("cache", path)))
    monkeypatch.setattr(yf, "Ticker", lambda symbol: seen.append(("ticker", symbol)))

    class _Scheduler:
        def start(self):
            yf.Ticker("AAPL")  # the first job a started scheduler could run

        def shutdown(self):
            pass

    monkeypatch.setattr(scheduler_service, "get_scheduler", lambda: _Scheduler())
    monkeypatch.setattr(settings, "scheduler_enabled", True)

    with TestClient(main.app):
        pass

    assert seen == [("cache", str(yf_cache.yfinance_cache_dir())), ("ticker", "AAPL")]
    assert Path(seen[0][1]).name == YF_CACHE_DIRNAME
