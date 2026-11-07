"""
The watchlist's analyst rating is five counts labelled by `consensus_of`, like the positions
table and the Company overview, never Yahoo's own `recommendationKey` (owner, 2026-11-07:
NVDA read "Strong Buy" on the watchlist and "Buy" everywhere else).

A held listing's counts are `analyst_ratings`' and a Company-tab company's
`company_yahoo_profiles`', read at response time; only a ticker in neither costs the
watchlist a `recommendations` request of its own, on its weekly slow run or when never
asked. Offline: `yf.Ticker` is a stub that records every read.
"""

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

import app.models  # noqa: F401
import app.services.watchlist_service as watchlist_module
from app.clock import utcnow
from app.models.analyst_rating import AnalystRating
from app.models.company_financials import CompanyYahooProfile, SecCompany
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.watchlist_item import WatchlistItem
from app.routers.watchlist import get_watchlist
from app.services.watchlist_service import RATING_POINTS, WatchlistService

YAHOO_COUNTS = {"strongBuy": 25, "buy": 10, "hold": 3, "sell": 1, "strongSell": 0}


@pytest.fixture
def reads(monkeypatch):
    seen = _Seen()
    quote_type = {"value": "EQUITY"}

    class _Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        @property
        def info(self):
            seen.append("info")
            return {
                "quoteType": quote_type["value"],
                "symbol": self.ticker,
                "currentPrice": 100.0,
                "recommendationKey": "strong_buy",
            }

        def history(self, period=None):
            return pd.DataFrame({"Close": [200.0 + i for i in range(30)]})

        @property
        def quarterly_financials(self):
            return None

        @property
        def revenue_estimate(self):
            return None

        @property
        def recommendations(self):
            seen.append("recommendations")
            return pd.DataFrame([{"period": "0m", **YAHOO_COUNTS}])

    monkeypatch.setattr(watchlist_module.yf, "Ticker", _Ticker)
    monkeypatch.setattr(watchlist_module.random, "uniform", lambda *_a, **_kw: 0)
    seen.quote_type = quote_type
    return seen


class _Seen(list):
    """The reads, plus the quote type the stub's `.info` answers (a test sets "ETF")."""

    quote_type: dict


async def _item(db, ticker="TCK", estimates_age=None, ratings_age=None, **counts):
    item = WatchlistItem(
        yahoo_ticker=ticker,
        estimates_synced_at=None if estimates_age is None else utcnow() - estimates_age,
        ratings_synced_at=None if ratings_age is None else utcnow() - ratings_age,
        **counts,
    )
    db.add(item)
    await db.commit()
    return item


async def _hold(db, symbol, **counts):
    security = Security(
        isin="US0000000099",
        symbol=symbol,
        description=symbol,
        currency="USD",
        conid=9999,
        asset_category="STK",
        exchange="NASDAQ",
    )
    db.add(security)
    await db.flush()
    db.add(
        TaxLot(
            security_id=security.id,
            open_date=date(2025, 1, 2),
            is_open=True,
            quantity=Decimal("1"),
            cost_basis=Decimal("10"),
            price_per_unit=Decimal("10"),
            currency="USD",
            cost_basis_eur=Decimal("9"),
        )
    )
    if counts:
        db.add(AnalystRating(security_id=security.id, last_updated=utcnow(), **counts))
    await db.commit()


async def _company(db, ticker, **counts):
    db.add(SecCompany(cik=320193, ticker=ticker, name=ticker))
    db.add(CompanyYahooProfile(cik=320193, fetched_at=utcnow(), **counts))
    await db.commit()


async def _row(db, ticker):
    rows = await get_watchlist(db=db)
    return next(r for r in rows if r.yahoo_ticker == ticker)


@pytest.mark.asyncio
async def test_a_ticker_no_pass_keeps_counts_for_is_asked_once_and_labelled_by_the_counts(
    db,
    reads,
):
    item = await _item(db, estimates_age=timedelta(days=1))

    await WatchlistService(db).sync_item("TCK", force=True)
    await db.refresh(item)

    assert reads.count("recommendations") == 1
    assert (item.strong_buy, item.buy, item.hold, item.sell, item.strong_sell) == (
        25,
        10,
        3,
        1,
        0,
    )
    assert item.ratings_synced_at is not None
    # 25×1 + 10×2 + 3×3 + 1×4 over 39 = 1.49: Strong Buy by the counts — and the label the
    # score reads, never `recommendationKey`.
    assert item.analyst_rating == "Strong Buy"
    rating = (await _row(db, "TCK")).analyst_rating
    assert rating.model_dump() == {
        "strong_buy": 25,
        "buy": 10,
        "hold": 3,
        "sell": 1,
        "strong_sell": 0,
        "consensus": "Strong Buy",
    }


@pytest.mark.asyncio
async def test_a_fast_run_carries_the_counts_and_a_slow_run_asks_again(db, reads):
    item = await _item(
        db,
        estimates_age=timedelta(days=2),
        ratings_age=timedelta(days=2),
        strong_buy=1,
        buy=5,
        hold=5,
        sell=0,
        strong_sell=0,
    )

    await WatchlistService(db).sync_item("TCK", force=True)
    await db.refresh(item)
    assert "recommendations" not in reads
    assert (item.strong_buy, item.buy, item.hold) == (1, 5, 5)
    assert item.analyst_rating == "Buy"

    item.estimates_synced_at = (
        utcnow() - WatchlistService.ESTIMATES_REFRESH_AFTER - timedelta(minutes=1)
    )
    await db.commit()
    await WatchlistService(db).sync_item("TCK", force=True)
    assert reads.count("recommendations") == 1


@pytest.mark.asyncio
async def test_a_held_ticker_reads_the_positions_tables_counts_and_asks_nothing(
    db, reads
):
    await _hold(db, "HELD", strong_buy=2, buy=20, hold=4, sell=0, strong_sell=0)
    item = await _item(db, "HELD")

    await WatchlistService(db).sync_item("HELD", force=True)
    await db.refresh(item)

    assert "recommendations" not in reads
    assert item.strong_buy is None and item.ratings_synced_at is None
    assert item.analyst_rating == "Buy"
    rating = (await _row(db, "HELD")).analyst_rating
    assert (rating.strong_buy, rating.buy, rating.hold, rating.consensus) == (
        2,
        20,
        4,
        "Buy",
    )


@pytest.mark.asyncio
async def test_a_held_ticker_shows_the_held_counts_even_over_its_own_older_ones(
    db, reads
):
    await _hold(db, "HELD", strong_buy=0, buy=0, hold=9, sell=0, strong_sell=0)
    await _item(db, "HELD", strong_buy=30, buy=0, hold=0, sell=0, strong_sell=0)

    rating = (await _row(db, "HELD")).analyst_rating

    assert rating.consensus == "Hold"


@pytest.mark.asyncio
async def test_a_company_tab_company_reads_its_overviews_counts_and_asks_nothing(
    db, reads
):
    await _company(db, "IDX", strong_buy=0, buy=3, hold=10, sell=2, strong_sell=0)
    item = await _item(db, "IDX")

    await WatchlistService(db).sync_item("IDX", force=True)
    await db.refresh(item)

    assert "recommendations" not in reads
    assert item.analyst_rating == "Hold"
    assert (await _row(db, "IDX")).analyst_rating.hold == 10


@pytest.mark.asyncio
async def test_a_fund_is_never_asked_and_shows_no_rating(db, reads):
    reads.quote_type["value"] = "ETF"
    item = await _item(db, "FUND")

    await WatchlistService(db).sync_item("FUND", force=True)
    await db.refresh(item)

    assert "recommendations" not in reads
    assert item.strong_buy == 0 and item.ratings_synced_at is not None
    assert item.analyst_rating is None
    assert (await _row(db, "FUND")).analyst_rating is None


@pytest.mark.asyncio
async def test_a_rate_limited_ask_stops_the_item_like_info_does(db, reads, monkeypatch):
    item = await _item(db)

    async def refused(self, ticker, label):
        raise Exception("Too Many Requests. Rate limited. Try after a while.")

    monkeypatch.setattr(watchlist_module.AnalystRatingService, "fetch_counts", refused)
    result = await WatchlistService(db).sync_item("TCK", force=True)
    await db.refresh(item)

    assert "error" in result
    assert item.last_synced is None and item.ratings_synced_at is None


def test_the_score_reads_the_consensus_label():
    service = WatchlistService.__new__(WatchlistService)
    base = {"current_price": 100.0, "rsi14": 40.0}
    unrated = service._compute_buy_score({**base, "analyst_rating": None})
    for label, points in RATING_POINTS.items():
        score = service._compute_buy_score({**base, "analyst_rating": label})
        assert score - unrated == points - 6, label
