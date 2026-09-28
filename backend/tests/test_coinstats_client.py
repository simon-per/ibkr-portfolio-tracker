"""
The CoinStats client: what it sends, what it refuses, and what it never does.

Every test drives the client through `httpx.MockTransport`, so no request leaves the
machine and no credit is spent. The properties pinned here are the ones a failure would
make expensive or public: secrets only in headers, the share token only where needed,
every failure a typed error with no retry, and a coin list that is either complete or
refused.
"""
import httpx
import pytest

from app.config import settings
from app.services import coinstats_client as cs
from app.services.coinstats_client import (
    COINS_PAGE_LIMIT,
    MAX_COIN_PAGES,
    CoinStatsAuthError,
    CoinStatsClient,
    CoinStatsNotSynced,
    CoinStatsQuotaError,
    CoinStatsTooManyPages,
    CoinStatsTransientError,
)

KEY = "test-key-0123456789abcdefghij"
TOKEN = "test-share-token-abcdef012345"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "coin_stats_api_key", KEY)
    monkeypatch.setattr(settings, "coin_stats_share_token", TOKEN)
    monkeypatch.setattr(settings, "coin_stats_share_passcode", "")


async def _no_sleep(_seconds):
    return None


def _client(handler, **kwargs):
    return CoinStatsClient(
        transport=httpx.MockTransport(handler), min_interval_s=0, sleep=_no_sleep, **kwargs
    )


def test_is_configured_needs_both_the_key_and_the_share_token(monkeypatch):
    monkeypatch.setattr(settings, "coin_stats_api_key", KEY)
    monkeypatch.setattr(settings, "coin_stats_share_token", "")
    assert not cs.is_configured()
    monkeypatch.setattr(settings, "coin_stats_share_token", "   ")
    assert not cs.is_configured()
    monkeypatch.setattr(settings, "coin_stats_share_token", TOKEN)
    assert cs.is_configured()


@pytest.mark.asyncio
async def test_secrets_travel_in_headers_and_never_in_the_url(configured, monkeypatch):
    monkeypatch.setattr(settings, "coin_stats_share_passcode", "123456")
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path.endswith("/usage/credits"):
            return httpx.Response(200, json={"remainingCredits": 1})
        return httpx.Response(200, json={"totalValue": 1})

    async with _client(handler) as client:
        await client.credits()
        await client.portfolio_value()

    credits_req, value_req = seen
    for request in seen:
        assert KEY not in str(request.url) and TOKEN not in str(request.url)
        assert request.headers["X-API-KEY"] == KEY
    # The account-level endpoint never sees the portfolio's credential.
    assert "sharetoken" not in credits_req.headers
    assert "passcode" not in credits_req.headers
    assert value_req.headers["sharetoken"] == TOKEN
    assert value_req.headers["passcode"] == "123456"


@pytest.mark.asyncio
async def test_no_passcode_header_when_none_is_configured(configured):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"totalValue": 1})

    async with _client(handler) as client:
        await client.portfolio_value()
    assert "passcode" not in seen[0].headers


@pytest.mark.parametrize("status,error", [
    (401, CoinStatsAuthError),
    (403, CoinStatsAuthError),
    (409, CoinStatsNotSynced),
    (429, CoinStatsQuotaError),
    (500, CoinStatsTransientError),
    (503, CoinStatsTransientError),
])
@pytest.mark.asyncio
async def test_each_failure_is_a_typed_error_and_is_asked_exactly_once(configured, status, error):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "Insufficient credits for this request"})

    async with _client(handler) as client:
        with pytest.raises(error) as raised:
            await client.portfolio_value()
        # A refused call spends nothing — and is never retried inside the run.
        assert client.credits_spent == 0
    assert len(calls) == 1
    assert raised.value.reason == error.reason
    # CoinStats' own wording rides along, because it says more than the status does.
    assert "Insufficient credits" in str(raised.value)


@pytest.mark.asyncio
async def test_a_transport_failure_is_transient(configured):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(CoinStatsTransientError):
            await client.portfolio_value()


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_transient(configured):
    def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    async with _client(handler) as client:
        with pytest.raises(CoinStatsTransientError):
            await client.portfolio_value()


def _coins_handler(pages, fail_on_page=None):
    """Serve `pages` (a list of item counts) as successive `/portfolio/coins` pages."""
    def handler(request):
        page = int(request.url.params["page"])
        assert int(request.url.params["limit"]) == COINS_PAGE_LIMIT
        if page == fail_on_page:
            return httpx.Response(503, json={})
        count = pages[page - 1] if page <= len(pages) else 0
        return httpx.Response(200, json={"result": [
            {"count": 1, "coin": {"identifier": f"c{page}-{i}"}} for i in range(count)
        ]})
    return handler


@pytest.mark.asyncio
async def test_coins_are_paged_until_a_short_page(configured):
    async with _client(_coins_handler([COINS_PAGE_LIMIT, 5])) as client:
        coins = await client.portfolio_coins()
        assert len(coins) == COINS_PAGE_LIMIT + 5
        assert client.credits_spent == 2 * cs.CREDIT_COST["coins"]


@pytest.mark.asyncio
async def test_a_failed_page_refuses_the_whole_list(configured):
    """A partial list stored as the portfolio would be indistinguishable from a real
    sale of everything on the missing pages."""
    async with _client(_coins_handler([COINS_PAGE_LIMIT, COINS_PAGE_LIMIT, 3],
                                      fail_on_page=2)) as client:
        with pytest.raises(CoinStatsTransientError):
            await client.portfolio_coins()


@pytest.mark.asyncio
async def test_more_pages_than_the_cap_are_refused_not_truncated(configured):
    full = [COINS_PAGE_LIMIT] * (MAX_COIN_PAGES + 1)
    async with _client(_coins_handler(full)) as client:
        with pytest.raises(CoinStatsTooManyPages):
            await client.portfolio_coins()
        assert client.credits_spent == MAX_COIN_PAGES * cs.CREDIT_COST["coins"]


@pytest.mark.asyncio
async def test_calls_are_paced_under_the_rate_limit(configured):
    slept = []
    now = [100.0]

    async def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    def handler(request):
        return httpx.Response(200, json={"totalValue": 1})

    client = CoinStatsClient(
        transport=httpx.MockTransport(handler), min_interval_s=0.6,
        sleep=fake_sleep, clock=lambda: now[0],
    )
    async with client:
        await client.portfolio_value()
        await client.portfolio_value()
        await client.portfolio_value()
    # The first call goes straight out; each later one waits out the interval.
    assert slept == [pytest.approx(0.6), pytest.approx(0.6)]


@pytest.mark.asyncio
async def test_credits_spent_follows_the_documented_costs(configured):
    def handler(request):
        path = request.url.path
        if path.endswith("/usage/credits"):
            return httpx.Response(200, json={"remainingCredits": 1})
        if path.endswith("/portfolio/chart"):
            return httpx.Response(200, json={"result": [[1_700_000_000, 1.0, 0.0, 0.0]]})
        if path.endswith("/portfolio/pl/history"):
            return httpx.Response(200, json={"result": [], "meta": {}})
        return httpx.Response(200, json={"totalValue": 1})

    async with _client(handler) as client:
        await client.credits()
        await client.portfolio_value()
        await client.portfolio_chart()
        await client.portfolio_pl_history()
        assert client.credits_spent == (
            cs.CREDIT_COST["value"] + cs.CREDIT_COST["chart"] + cs.CREDIT_COST["pl_history"]
        )
