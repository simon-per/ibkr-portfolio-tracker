"""
A London or Johannesburg watchlist name is shown in pounds or rand, not pence or cents
(review item Y4, 2026-11-08).

Yahoo quotes those listings in the minor unit (`GBp`, `ZAc`) and the watchlist stored the
figures as reported, so a 5-pound share read 500 under a pound sign. Its prices are now
divided by `minor_unit_scale` and labelled in the major code; the reported code stays as
`data_currency`, which keeps the market cap's refusal. Offline: `yf.Ticker` is a stub.
"""
import pytest

import app.models  # noqa: F401
import app.services.watchlist_service as watchlist_module
from app.models.watchlist_item import WatchlistItem
from app.routers.watchlist import get_watchlist
from app.services.watchlist_service import WatchlistService


class _Ticker:
    def __init__(self, ticker):
        self.ticker = ticker

    @property
    def info(self):
        return {"quoteType": "EQUITY", "symbol": self.ticker, "currency": "GBp",
                "currentPrice": 500.0, "fiftyTwoWeekHigh": 600.0, "fiftyTwoWeekLow": 400.0,
                "twoHundredDayAverage": 480.0, "fiftyDayAverage": 510.0,
                "targetMeanPrice": 700.0, "marketCap": 9_000_000_000}

    def history(self, period=None):
        return None

    @property
    def quarterly_financials(self):
        return None

    @property
    def revenue_estimate(self):
        return None

    @property
    def recommendations(self):
        return None


@pytest.mark.asyncio
async def test_a_pence_quote_is_shown_in_pounds(db, monkeypatch):
    monkeypatch.setattr(watchlist_module.yf, "Ticker", _Ticker)
    monkeypatch.setattr(watchlist_module.random, "uniform", lambda *_a, **_kw: 0)
    db.add(WatchlistItem(yahoo_ticker="ULVR.L"))
    await db.commit()

    await WatchlistService(db).sync_item("ULVR.L", force=True)
    (row,) = await get_watchlist(db=db)

    assert (row.current_price, row.week52_high, row.week52_low, row.ma200, row.ma50,
            row.analyst_target) == (5.0, 6.0, 4.0, 4.8, 5.1, 7.0)
    assert (row.currency, row.data_currency) == ("GBP", "GBp")
    # The percentages are ratios of one unit, so they are untouched.
    assert row.pct_from_52w_high == pytest.approx(-16.67, abs=0.01)
    # The cap's scale is not verified: still refused in the base currency.
    assert row.market_cap_base is None
