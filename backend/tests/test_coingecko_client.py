"""
The CoinGecko client: what it sends, what it refuses, and what it never does.

Driven through `httpx.MockTransport`, so no request leaves the machine. Pinned: the key
only in its header, pacing measured from the previous call's completion, every failure a
typed error asked exactly once (a 429 by its status code), and unpriced ids absent rather
than 0.
"""
import httpx
import pytest

from app.config import settings
from app.services import coingecko_client as cg
from app.services.coingecko_client import (
    KEY_HEADER,
    CoinGeckoAuthError,
    CoinGeckoClient,
    CoinGeckoQuotaError,
    CoinGeckoRequestError,
    CoinGeckoTransientError,
)

KEY = "test-gecko-key-0123456789abcdef"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "coingecko_api_key", KEY)


async def _no_sleep(_seconds):
    return None


def _client(handler):
    return CoinGeckoClient(transport=httpx.MockTransport(handler), min_interval_s=0,
                           sleep=_no_sleep)


def test_is_configured_follows_the_key(monkeypatch):
    assert not cg.is_configured()
    monkeypatch.setattr(settings, "coingecko_api_key", KEY)
    assert cg.is_configured()


@pytest.mark.asyncio
async def test_the_key_travels_in_its_header_and_never_in_the_url(configured):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"bitcoin": {"usd": 60000}, "ghost": {}})

    async with _client(handler) as client:
        prices = await client.simple_price(["bitcoin", "ghost"])
    assert prices == {"bitcoin": 60000.0}  # an unpriced id is absent, not 0
    assert seen[0].headers[KEY_HEADER] == KEY
    assert KEY not in str(seen[0].url)
    assert client.calls == 1


@pytest.mark.parametrize("status,error", [
    (401, CoinGeckoAuthError),
    (429, CoinGeckoQuotaError),
    (400, CoinGeckoRequestError),
    (500, CoinGeckoTransientError),
])
@pytest.mark.asyncio
async def test_each_failure_is_a_typed_error_asked_exactly_once(configured, status, error):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"status": {"error_message": "nope"}})

    async with _client(handler) as client:
        with pytest.raises(error):
            await client.coins_list()
        assert client.calls == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_body_that_is_not_the_documented_shape_is_transient(configured):
    async with _client(lambda r: httpx.Response(200, json={"oops": 1})) as client:
        with pytest.raises(CoinGeckoTransientError):
            await client.market_chart_range("bitcoin", 0, 1)


@pytest.mark.asyncio
async def test_calls_are_paced_from_the_previous_completion(configured):
    slept, now = [], [100.0]

    async def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    client = CoinGeckoClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])),
        min_interval_s=2.5, sleep=fake_sleep, clock=lambda: now[0],
    )
    async with client:
        await client.coins_list()
        await client.coins_list()
        await client.coins_list()
    assert slept == [pytest.approx(2.5), pytest.approx(2.5)]


@pytest.mark.asyncio
async def test_market_chart_keeps_valid_points_only(configured):
    body = {"prices": [[1_700_000_000_000, 10.0], [1_700_086_400_000, None], ["x", 1.0],
                       [1_700_172_800_000, 12.5]]}
    async with _client(lambda r: httpx.Response(200, json=body)) as client:
        points = await client.market_chart_range("bitcoin", 0, 1)
    assert points == [(1_700_000_000_000, 10.0), (1_700_172_800_000, 12.5)]
