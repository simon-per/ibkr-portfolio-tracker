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
"""
import pytest

from app import rate_limit
from app.config import settings


@pytest.fixture(autouse=True)
def _neutral_process_state(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0, raising=False)
    monkeypatch.setattr(settings, "api_admin_token", "", raising=False)
    monkeypatch.setattr(settings, "scheduler_jobstore_url", "", raising=False)
    monkeypatch.setattr(settings, "coin_stats_api_key", "", raising=False)
    monkeypatch.setattr(settings, "coin_stats_share_token", "", raising=False)
    monkeypatch.setattr(settings, "coin_stats_share_passcode", "", raising=False)
    rate_limit.reset()
    yield
    rate_limit.reset()
