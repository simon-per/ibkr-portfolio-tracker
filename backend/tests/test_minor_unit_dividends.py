"""
Yahoo dividends for a minor-unit listing (GBp / ZAc / ILA) are scaled like the prices from
the very same chart response (GitHub issue #35).

`_try_fetch_yahoo` divides `Close` when Yahoo reports `GBp` and labels the row GBP. The
same frame's `Dividends` column (`chart_actions`) used to reach `yahoo_dividend_rows`
unscaled, labelled `security.currency` (GBP) — so a 4.5p dividend was stored as 4.50 GBP
per share, 100x. The scale now travels with the chart actions and the builder divides.
The dividend pass's own `.dividends` read is pinned in `test_chart_dividends.py`.

Offline: yf.Ticker is a stub.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services import market_data_service as mds
from app.services.dividend_service import yahoo_dividend_rows
from app.services.market_data_service import MarketDataService


@pytest.mark.asyncio
async def test_a_pence_dividend_is_scaled_like_the_pence_price(monkeypatch):
    monkeypatch.setattr(mds.random, "uniform", lambda *_: 0)

    class FakeTicker:
        def __init__(self, ticker):
            self._price_history = SimpleNamespace(_history_metadata=None)

        def history(self, **_kw):
            self._price_history._history_metadata = {"currency": "GBp"}
            return pd.DataFrame(
                {"Close": [512.4, 515.0], "Dividends": [0.0, 4.5]},   # pence, both
                index=pd.to_datetime(["2026-07-23", "2026-07-24"]),
            )

    monkeypatch.setattr(mds.yf, "Ticker", FakeTicker)
    service = MarketDataService.__new__(MarketDataService)
    security = SimpleNamespace(id=1, symbol="LLOY", exchange="LSE", currency="GBP")

    prices, _ = await service._try_fetch_yahoo(
        "LLOY.L", security, date(2026, 7, 23), date(2026, 7, 24)
    )
    assert prices[-1]["close_price"] == Decimal("5.15")

    _ticker, (dividends, _splits, scale) = service._last_chart_actions
    rows, _ = yahoo_dividend_rows(security, dividends, keep_from=None, scale=scale)

    assert rows[0]["currency"] == "GBP"
    assert rows[0]["amount_per_share"] == Decimal("0.045"), (
        "a 4.5p dividend stored as GBP must be 0.045, like the price beside it"
    )
