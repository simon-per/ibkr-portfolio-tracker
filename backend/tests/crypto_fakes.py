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
from app.services.coinstats_client import COINS_PAGE_LIMIT, CoinStatsClient

KEY = "test-coinstats-key-0123456789"
TOKEN = "test-share-token-0123456789"


def configure(monkeypatch) -> None:
    """Point the settings at fake credentials (conftest blanks them for every test)."""
    monkeypatch.setattr(settings, "coin_stats_api_key", KEY)
    monkeypatch.setattr(settings, "coin_stats_share_token", TOKEN)
    monkeypatch.setattr(settings, "coin_stats_share_passcode", "")


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


def history_bodies(days: int, end: datetime) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """A chart body and a P&L body covering `days` consecutive UTC days ending at `end`."""
    rows, points = [], []
    for offset in range(days):
        stamp = end - timedelta(days=days - 1 - offset)
        rows.append([int(stamp.timestamp()), 1000.0 + offset, 0.0, 0.0])
        points.append({"date": stamp.isoformat(), "profitLoss": 10.0 + offset,
                       "profitLossPercent": 1.0})
    return {"result": rows}, {"result": points, "meta": {"interval": "daily"}}


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
        history_days: int = 30,
    ):
        end = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
        chart, pl = history_bodies(history_days, end)
        self.coins = default_coins() if coins is None else coins
        self.responses: Dict[str, Tuple[int, Any]] = {
            "/v1/usage/credits": (200, credits or credits_body()),
            "/v1/portfolio/value": (200, value or value_body()),
            "/v1/portfolio/chart": (200, chart),
            "/v1/portfolio/pl/history": (200, pl),
        }
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
