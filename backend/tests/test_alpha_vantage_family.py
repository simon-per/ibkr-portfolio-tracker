"""
One door to Alpha Vantage, one daily budget (2026-11-16, docs/flex-and-sync.md).

The price fallback and the Company tab's statement fill spend one free-tier key. Each could
spend all of it on its own, and two copies of "how to ask Alpha Vantage" would drift. So
`app/services/alpha_vantage.py` is the only module that names its host, and every call
goes through its budget: counted before the call and persisted, the price fallback's
reserve kept, and a `Note`/`Information` notice held for the rest of the UTC day.
Offline: a fake HTTP client.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.repositories.app_settings_repository import AppSettingsRepository
from app.services import alpha_vantage as av

APP = Path(__file__).resolve().parent.parent / "app"


def test_no_other_module_asks_alpha_vantage():
    """The family test: a second caller must go through `alpha_vantage.ask`."""
    naming = sorted(
        p.relative_to(APP).as_posix()
        for p in APP.rglob("*.py")
        if "alphavantage.co" in p.read_text("utf-8")
    )
    assert naming == ["services/alpha_vantage.py"]


class FakeClient:
    """httpx.AsyncClient as Alpha Vantage answers: HTTP 200 and a JSON body, always."""

    calls = []
    body = {"Time Series (Daily)": {}}

    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, params=None):
        FakeClient.calls.append(dict(params))
        body = FakeClient.body
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


@pytest.fixture
def fake(monkeypatch):
    FakeClient.calls = []
    FakeClient.body = {"annualReports": []}
    monkeypatch.setattr(av.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(av.settings, "alpha_vantage_api_key", "test-key-123456")
    return FakeClient


async def _no_sleep(_seconds):
    return None


async def _ask(db, purpose=av.STATEMENTS, function="CASH_FLOW"):
    return await av.ask(db, function, purpose, sleep=_no_sleep, symbol="ABC")


@pytest.mark.asyncio
async def test_without_a_key_nothing_is_asked(db, fake, monkeypatch):
    monkeypatch.setattr(av.settings, "alpha_vantage_api_key", "")
    assert await _ask(db) is None and fake.calls == []


@pytest.mark.asyncio
async def test_every_call_is_counted_before_it_is_made_and_persisted(db, fake):
    assert await _ask(db) == {"annualReports": []}
    assert fake.calls[0]["apikey"] == "test-key-123456"
    assert (await av.budget(db, av.STATEMENTS))["spent"] == 1
    assert (await AppSettingsRepository(db).get(av.SPENT_KEY)).endswith(" 1")


@pytest.mark.asyncio
async def test_the_statement_fill_leaves_the_price_fallback_its_reserve(db, fake):
    for _ in range(av.LIMITS[av.STATEMENTS]):
        await _ask(db)
    with pytest.raises(av.AlphaVantageCapped):
        await _ask(db)
    assert len(fake.calls) == av.DAILY_LIMIT - av.PRICE_RESERVE
    # Prices go first: their reserve is still there.
    for _ in range(av.PRICE_RESERVE):
        await _ask(db, av.PRICES, "TIME_SERIES_DAILY")
    with pytest.raises(av.AlphaVantageCapped):
        await _ask(db, av.PRICES, "TIME_SERIES_DAILY")
    assert len(fake.calls) == av.DAILY_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("notice", ["Note", "Information"])
async def test_a_notice_holds_every_caller_for_the_rest_of_the_day(db, fake, notice):
    """HTTP 200 and a notice is Alpha Vantage's limit: persisted, never retried today."""
    fake.body = {notice: "Our standard API rate limit is 25 requests per day."}
    with pytest.raises(av.AlphaVantageCapped):
        await _ask(db)
    fake.body = {"annualReports": []}
    for purpose in (av.STATEMENTS, av.PRICES):
        with pytest.raises(av.AlphaVantageCapped):
            await _ask(db, purpose)
    assert len(fake.calls) == 1
    assert (await av.budget(db, av.PRICES))["capped"] is True


@pytest.mark.asyncio
async def test_a_new_utc_day_has_a_new_budget(db, fake, monkeypatch):
    repo = AppSettingsRepository(db)
    await repo.set(av.SPENT_KEY, "2000-01-01 25")
    await repo.set(av.CAPPED_KEY, "2000-01-01")
    await db.commit()
    assert await av.budget(db, av.STATEMENTS) == {
        "spent": 0, "left": av.LIMITS[av.STATEMENTS], "capped": False,
    }


@pytest.mark.asyncio
async def test_an_error_message_is_an_answer_and_spent(db, fake):
    fake.body = {"Error Message": "Invalid API call."}
    assert await _ask(db) is None
    assert (await av.budget(db, av.STATEMENTS))["spent"] == 1


@pytest.mark.asyncio
async def test_calls_are_paced_at_five_a_minute(db, fake):
    slept, now = [], [100.0]

    async def sleep(seconds):
        slept.append(seconds)

    for _ in range(2):
        await av.ask(db, "CASH_FLOW", av.STATEMENTS, sleep=sleep, clock=lambda: now[0],
                     symbol="ABC")
    assert slept == [av.MIN_INTERVAL_S]


@pytest.mark.asyncio
async def test_a_transport_error_never_logs_the_url(db, fake, caplog):
    class Exploding(FakeClient):
        async def get(self, url, params=None):
            raise RuntimeError(f"{url}?apikey={params['apikey']} timed out")

    av.httpx.AsyncClient = Exploding
    assert await _ask(db) is None
    assert "test-key-123456" not in caplog.text and "apikey" not in caplog.text
