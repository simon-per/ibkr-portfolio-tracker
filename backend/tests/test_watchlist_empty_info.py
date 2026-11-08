"""
An empty `.info` is refused by the watchlist as by the fundamentals passes (review item Y3,
2026-11-08).

yfinance answers a symbol it momentarily cannot serve with `{'trailingPegRatio': None}`.
`extract_metrics` refused it; the watchlist wrote it — a fresh `last_synced`, price, P/E and
target blank, and a buy score from the carried growth plus neutral fallbacks. Now one test
(`yahoo_estimates.info_is_empty`) refuses it in both, and the row stays as it was.
Offline: `yf.Ticker` is a stub.
"""
from datetime import timedelta

import pytest

import app.models  # noqa: F401
import app.services.watchlist_service as watchlist_module
from app.clock import utcnow
from app.models.watchlist_item import WatchlistItem
from app.services.fundamentals_service import extract_metrics
from app.services.watchlist_service import WatchlistService


class _Ticker:
    def __init__(self, ticker):
        self.ticker = ticker

    @property
    def info(self):
        return {"trailingPegRatio": None}

    def history(self, period=None):
        return None

    @property
    def quarterly_financials(self):
        raise AssertionError("not asked on a fast run")

    @property
    def revenue_estimate(self):
        raise AssertionError("not asked on a fast run")


@pytest.mark.asyncio
async def test_an_empty_info_leaves_the_row_as_it_was(db, monkeypatch):
    monkeypatch.setattr(watchlist_module.yf, "Ticker", _Ticker)
    monkeypatch.setattr(watchlist_module.random, "uniform", lambda *_a, **_kw: 0)
    synced = utcnow() - timedelta(days=1)
    item = WatchlistItem(
        yahoo_ticker="NVDA", estimates_synced_at=synced, ratings_synced_at=synced,
        last_synced=synced, current_price=186.64, trailing_pe=23.6, analyst_target=327.1,
        buy_score=86.0, rsi14=45.0,
    )
    db.add(item)
    await db.commit()

    result = await WatchlistService(db).sync_item("NVDA", force=True)
    await db.refresh(item)

    assert extract_metrics({"trailingPegRatio": None}) is None
    assert "error" in result
    assert (item.last_synced, item.current_price, item.trailing_pe, item.buy_score) == (
        synced, 186.64, 23.6, 86.0)
