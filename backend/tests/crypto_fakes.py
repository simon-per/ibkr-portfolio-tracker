"""
Synthetic CoinStats answers and an in-memory database for the crypto tests.

**Every figure here is invented.** This repository is public, so nothing below is copied
from a real portfolio; the shapes follow CoinStats' documentation and the probe
(`app/cli/coinstats_probe.py`), the numbers are round and made up.

`FakeCoinStats` is an `httpx.MockTransport` handler, so the real `CoinStatsClient` —
headers, pacing, error mapping, pagination — runs unmodified and no request can leave
the machine.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register every mapper
from app.config import settings
from app.database import Base
from app.services.coingecko_client import CoinGeckoClient
from app.services.coinstats_client import COINS_PAGE_LIMIT, CoinStatsClient

KEY = "test-coinstats-key-0123456789"
TOKEN = "test-share-token-0123456789"
GECKO_KEY = "test-coingecko-key-0123456789"


def configure(monkeypatch) -> None:
    """Point the settings at fake credentials (conftest blanks them for every test)."""
    monkeypatch.setattr(settings, "coin_stats_api_key", KEY)
    monkeypatch.setattr(settings, "coin_stats_share_token", TOKEN)
    monkeypatch.setattr(settings, "coin_stats_share_passcode", "")


def configure_gecko(monkeypatch) -> None:
    """Point the CoinGecko setting at a fake key (conftest blanks it for every test)."""
    monkeypatch.setattr(settings, "coingecko_api_key", GECKO_KEY)


def money(usd: Optional[float]) -> Optional[Dict[str, float]]:
    return None if usd is None else {"USD": usd, "BTC": 0.0, "ETH": 0.0}


def coin(
    identifier: str,
    symbol: str,
    count: float,
    price: Optional[float],
    *,
    rank: Optional[int] = None,
    fake: bool = False,
    fiat: bool = False,
    cost: Optional[float] = None,
    avg_buy: Optional[float] = None,
    unrealized: Optional[float] = None,
    unrealized_pct: Optional[float] = None,
    realized: Optional[float] = None,
    hour24: Optional[float] = None,
    change_24h: Optional[float] = None,
) -> Dict[str, Any]:
    """One `/portfolio/coins` item, shaped as CoinStats documents it."""
    return {
        "count": count,
        "coin": {
            "identifier": identifier,
            "symbol": symbol,
            "name": symbol.title(),
            "rank": rank,
            "isFake": fake,
            "isFiat": fiat,
            "priceChange24h": change_24h,
        },
        "price": money(price),
        "profit": {
            "allTime": money(None if unrealized is None else unrealized + (realized or 0)),
            "hour24": money(hour24),
            "unrealized": money(unrealized),
            "realized": money(realized),
        },
        "profitPercent": {"unrealized": money(unrealized_pct)},
        "averageBuy": {"allTime": money(avg_buy)},
        "totalCost": money(cost),
    }


def default_coins() -> List[Dict[str, Any]]:
    """Two valued coins, a flagged spam token, an unpriced token and a fiat balance."""
    return [
        coin("bitcoin", "BTC", 0.01, 60_000.0, rank=1, cost=400.0, avg_buy=40_000.0,
             unrealized=200.0, unrealized_pct=50.0, realized=10.0, hour24=12.0, change_24h=2.0),
        coin("solana", "SOL", 5.0, 100.0, rank=5, cost=400.0, avg_buy=80.0,
             unrealized=100.0, unrealized_pct=25.0, realized=0.0, hour24=-5.0, change_24h=-1.0),
        coin("scam-token", "VISIT-SCAM.EXAMPLE", 1_000_000.0, 0.5, fake=True),
        coin("mystery-token", "MYST", 42.0, None),
        coin("FiatCoinEUR", "EUR", 100.0, 1.1, rank=None, fiat=True, cost=110.0,
             avg_buy=1.1, unrealized=0.0, realized=0.0, hour24=0.0, change_24h=0.0),
    ]


def value_body(total: float = 1310.0, **overrides: Any) -> Dict[str, Any]:
    body = {
        "totalValue": total,
        "defiValue": 0.0,
        "totalCost": 910.0,
        "unrealizedProfitLoss": 300.0,
        "unrealizedProfitLossPercent": 32.97,
        "realizedProfitLoss": 10.0,
        "realizedProfitLossPercent": 1.1,
        "allTimeProfitLoss": 310.0,
        "allTimeProfitLossPercent": 34.07,
    }
    body.update(overrides)
    return body


def credits_body(remaining: int = 19_900, total: int = 20_000) -> Dict[str, Any]:
    return {
        "subscription": "free",
        "totalCredits": total,
        "usedCredits": total - remaining,
        "remainingCredits": remaining,
    }


async def _no_sleep(_seconds: float) -> None:
    return None


class FakeCoinStats:
    """
    Serves each CoinStats path from `responses[path] = (status, body)` and logs every
    request. `coins` is served in pages the way the real endpoint pages them.
    """

    def __init__(
        self,
        coins: Optional[List[Dict[str, Any]]] = None,
        value: Optional[Dict[str, Any]] = None,
        credits: Optional[Dict[str, Any]] = None,
    ):
        self.coins = default_coins() if coins is None else coins
        self.responses: Dict[str, Tuple[int, Any]] = {
            "/v1/usage/credits": (200, credits or credits_body()),
            "/v1/portfolio/value": (200, value or value_body()),
        }
        self.transaction_pages: List[Any] = []
        self.coin_page_status: Dict[int, int] = {}
        self.calls: List[str] = []

    def paths(self) -> List[str]:
        return [c.replace("/v1", "", 1) for c in self.calls]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/v1/portfolio/coins":
            page = int(request.url.params.get("page", 1))
            status = self.coin_page_status.get(page, 200)
            if status != 200:
                return httpx.Response(status, json={"message": "page failed"})
            start = (page - 1) * COINS_PAGE_LIMIT
            return httpx.Response(
                200, json={"result": self.coins[start:start + COINS_PAGE_LIMIT]}
            )
        if path == "/v1/portfolio/transactions":
            page = int(request.url.params.get("page", 1))
            body = self.transaction_pages[page - 1] if page <= len(self.transaction_pages)                 else {"result": []}
            return httpx.Response(200, json=body)
        status, body = self.responses[path]
        return httpx.Response(status, json=body)

    def client_factory(self):
        return lambda: CoinStatsClient(
            transport=httpx.MockTransport(self.handler), min_interval_s=0, sleep=_no_sleep
        )


class RecordingCurrencyService:
    """Stands in for `CurrencyService` in the sync's FX warm-up: records, never fetches."""

    calls: List[Tuple[Tuple[str, ...], str, int]] = []

    def __init__(self, db):
        self.db = db

    async def warm_rates(self, currencies, target_date=None, days_back=30, to_currency="EUR"):
        RecordingCurrencyService.calls.append((tuple(sorted(currencies)), to_currency, days_back))
        return {"frankfurter": 1, "fallback": 0, "currencies": sorted(currencies)}


async def memory_session_factory():
    """An in-memory database with the full schema, and a session factory bound to it."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class FakeCoinGecko:
    """
    CoinGecko's Demo API as an `httpx.MockTransport` handler. `listing` is `/coins/list`;
    `price(gid, day)` is the invented close on a UTC day (and the spot price today);
    `responses[path] = (status, body)` overrides a path. Every request is logged.
    """

    def __init__(self, prices: Optional[Dict[str, float]] = None, listing=None):
        self.base = prices if prices is not None else {
            "bitcoin": 60_000.0, "solana": 100.0, "usd-coin": 1.0,
        }
        self.listing = listing if listing is not None else [
            {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
            {"id": "solana", "symbol": "sol", "name": "Solana"},
            {"id": "ethereum", "symbol": "eth", "name": "Ethereum"},
        ]
        self.responses: Dict[str, Tuple[int, Any]] = {}
        self.calls: List[str] = []
        self.headers: List[Dict[str, str]] = []

    def price(self, gid: str, day) -> Optional[float]:
        return self.base.get(gid)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/v3", "", 1)
        self.calls.append(path)
        self.headers.append(dict(request.headers))
        if path in self.responses:
            status, body = self.responses[path]
            return httpx.Response(status, json=body)
        if path == "/coins/list":
            return httpx.Response(200, json=self.listing)
        if path == "/simple/price":
            ids = request.url.params["ids"].split(",")
            return httpx.Response(200, json={
                i: {"usd": self.base[i]} for i in ids if i in self.base
            })
        if path.startswith("/coins/") and path.endswith("/market_chart/range"):
            gid = path.split("/")[2]
            start = int(request.url.params["from"])
            end = int(request.url.params["to"])
            points = []
            stamp = start + 86_400  # daily points at 00:00 UTC close the day before
            while stamp <= end:
                day = datetime.fromtimestamp(stamp - 1, tz=timezone.utc).date()
                price = self.price(gid, day)
                if price is not None:
                    points.append([stamp * 1000, price])
                stamp += 86_400
            return httpx.Response(200, json={"prices": points})
        return httpx.Response(404, json={"error": "not found"})

    def client_factory(self):
        return lambda: CoinGeckoClient(
            transport=httpx.MockTransport(self.handler), min_interval_s=0, sleep=_no_sleep
        )
