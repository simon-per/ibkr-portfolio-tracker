"""
Vanguard US's holdings route since 2026-10 (issue #7), parsed from a real body and fetched
through `httpx.MockTransport`.

The old `/investment-products/etfs/profile/api/` path began redirecting to the HTML fund page,
which the parser refused correctly, so VT's basket aged until it was stale. The route Vanguard's
own page calls is `/vmf/api/<ticker>/portfolio-holding/stock.json`, and with the `count=20000`
that page sends it returns the whole fund in one request.

`fixtures/vanguard_us_vt_start1_count5.json` is a verbatim capture of
`.../vmf/api/vt/portfolio-holding/stock.json?start=1&count=5` taken 2026-10-17 — five rows of
Vanguard's public disclosure, kept that small because the file is theirs to publish, not ours.
It declares `size` 10170 and carries a `next` pointing at an internal host.

Offline: no network, no database.
"""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import app.services.etf_basket_fetch as fetch_mod
from app.etf_sources import FUND_SOURCES
from app.services.etf_basket_fetch import FetchError, fetch_bodies
from app.services.etf_basket_parsers import BasketParseError, parse_vanguard_us

VT = "US9220427424"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "vanguard_us_vt_start1_count5.json"
CAPTURED = FIXTURE.read_bytes()
ROUTE = "/vmf/api/vt/portfolio-holding/stock.json"


def _one_page_variant() -> bytes:
    """The capture as a complete fund: `size` 5 and no `next`."""
    payload = json.loads(CAPTURED)
    payload["size"] = 5
    payload.pop("next")
    return json.dumps(payload).encode("utf-8")


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(fetch_mod, "BETWEEN_REQUESTS_S", 0)


async def _fetch(handler):
    requests = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as client:
        bodies = await fetch_bodies(client, VT, FUND_SOURCES[VT])
    return bodies, requests


# ----------------------------------------------------------------------------- the parser

def test_the_captured_page_alone_is_refused_as_incomplete():
    """5 of a declared 10,170 is the missing-page shape, and the refusal names it."""
    with pytest.raises(BasketParseError, match="declares 10170 holdings but 5 were assembled"):
        parse_vanguard_us([CAPTURED], VT)


def test_the_new_route_body_parses_unchanged():
    basket = parse_vanguard_us([_one_page_variant()], VT)
    assert basket.as_of_date == date(2026, 9, 30)
    assert [r.isin for r in basket.rows] == [
        "US67066G1040", "US0378331005", "US5949181045", "US0231351067", "US02079K3059",
    ]
    assert [r.ticker for r in basket.rows] == ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL"]
    assert [r.weight_pct for r in basket.rows] == [
        Decimal("4.52"), Decimal("4.09"), Decimal("3.23"), Decimal("2.05"), Decimal("1.72"),
    ]


# ---------------------------------------------------------------------------- the fetcher

@pytest.mark.asyncio
async def test_one_request_fetches_the_whole_fund():
    """VT was 21 requests on the old route; with no `next` it must now be exactly one."""
    body = _one_page_variant()
    bodies, requests = await _fetch(lambda r: httpx.Response(200, content=body))

    assert bodies == [body]
    assert len(requests) == 1
    url = requests[0].url
    assert url.host == "investor.vanguard.com"
    assert url.path == ROUTE
    assert url.params["start"] == "1"
    assert url.params["count"] == str(fetch_mod.VANGUARD_COUNT) == "20000"


@pytest.mark.asyncio
async def test_a_paging_api_is_walked_from_local_offsets_never_from_next_href():
    """
    Should the API cap pages again, the loop follows `next` — but `start` advances by the rows
    received, and the internal host `next.href` names is never asked.
    """
    rows = json.loads(CAPTURED)["fund"]["entity"]

    def capped_at_two(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        chunk = rows[start - 1:start + 1]
        payload = json.loads(CAPTURED)
        payload["size"] = len(rows)
        payload["fund"]["entity"] = chunk
        if start + len(chunk) > len(rows):
            payload.pop("next")
        return httpx.Response(200, json=payload)

    bodies, requests = await _fetch(capped_at_two)

    assert [r.url.params["start"] for r in requests] == ["1", "3", "5"]
    assert {(r.url.host, r.url.path) for r in requests} == {("investor.vanguard.com", ROUTE)}
    basket = parse_vanguard_us(bodies, VT)
    assert [r.ticker for r in basket.rows] == ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL"]


@pytest.mark.asyncio
async def test_a_runaway_walk_stops_at_the_page_ceiling(monkeypatch):
    monkeypatch.setattr(fetch_mod, "VANGUARD_MAX_PAGES", 3)
    with pytest.raises(FetchError, match="still paginating after 3 pages"):
        await _fetch(lambda r: httpx.Response(200, content=CAPTURED))


@pytest.mark.asyncio
async def test_the_retired_routes_web_page_is_fetched_once_and_refused():
    """What production saw on 2026-10-15: HTTP 200, `text/html`, the Angular fund page."""
    page = b'<!doctype html><html><head><base href="/"></head><body><app-root></app-root>'
    bodies, requests = await _fetch(
        lambda r: httpx.Response(200, content=page, headers={"Content-Type": "text/html"})
    )
    assert len(requests) == 1
    with pytest.raises(BasketParseError, match="web page"):
        parse_vanguard_us(bodies, VT)
