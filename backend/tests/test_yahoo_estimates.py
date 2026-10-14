"""
The secondary Yahoo reads both fetch closures share — `app/services/yahoo_estimates.py`.

Two families pinned here, both written against every closure rather than one:

- **A 429 on any secondary endpoint reaches the loop.** `FundamentalsService` and
  `WatchlistService` each wrapped their four later reads in `except Exception`, so a rate
  limit after `.info` was logged and the pass carried on. Driven from the `yf.Ticker`
  boundary, so the raise has to travel out through the real closure.
- **One parser for the forward estimates.** The copies had drifted (`.empty`, the
  `forwardEps` fallback on one side only).

Offline: `yf.Ticker` is replaced by a fake for every test.
"""
import pandas as pd
import pytest

import app.services.fundamentals_service as fundamentals_module
import app.services.watchlist_service as watchlist_module
from app.services.fundamentals_service import FundamentalsService
from app.services.watchlist_service import WatchlistService
from app.services.yahoo_estimates import (
    ESTIMATE_ENDPOINTS,
    fetch_estimate_frames,
    forward_growth,
    read_optional,
)


class RateLimited(Exception):
    def __init__(self):
        super().__init__("429 Client Error: Too Many Requests for url: https://query2.finance.yahoo.com")


def _fake_ticker(failing: str = "", error: Exception = None, quote_type: str = "EQUITY"):
    """A `yf.Ticker` stand-in whose `failing` endpoint raises `error`; records reads."""
    reads = []

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def _read(self, name):
            reads.append(name)
            if name == failing:
                raise error
            return None

        @property
        def info(self):
            reads.append("info")
            return {"quoteType": quote_type, "symbol": self.symbol}

        @property
        def earnings_dates(self):
            return self._read("earnings_dates")

        @property
        def quarterly_financials(self):
            return self._read("quarterly_financials")

        @property
        def revenue_estimate(self):
            return self._read("revenue_estimate")

        @property
        def growth_estimates(self):
            return self._read("growth_estimates")

        def history(self, period=None):
            return self._read("history")

    return FakeTicker, reads


async def _fundamentals_fetch(ticker):
    return await FundamentalsService.__new__(FundamentalsService)._fetch_yahoo_data(ticker)


async def _watchlist_fetch(ticker):
    return await WatchlistService.__new__(WatchlistService)._fetch_ticker_data(ticker)


# Every closure, with every secondary endpoint it reads. A new closure joins here.
CLOSURES = [
    (fundamentals_module, _fundamentals_fetch, ("earnings_dates", *ESTIMATE_ENDPOINTS)),
    (watchlist_module, _watchlist_fetch, ("history", *ESTIMATE_ENDPOINTS)),
]
CASES = [
    pytest.param(module, fetch, endpoint, id=f"{module.__name__.rsplit('.', 1)[-1]}:{endpoint}")
    for module, fetch, endpoints in CLOSURES
    for endpoint in endpoints
]


@pytest.mark.asyncio
@pytest.mark.parametrize("module, fetch, endpoint", CASES)
async def test_a_rate_limit_on_a_secondary_endpoint_reaches_the_loop(monkeypatch, module, fetch, endpoint):
    fake, _reads = _fake_ticker(failing=endpoint, error=RateLimited())
    monkeypatch.setattr(module.yf, "Ticker", fake)

    with pytest.raises(RateLimited):
        await fetch("TCK")


@pytest.mark.asyncio
@pytest.mark.parametrize("module, fetch, endpoint", CASES)
async def test_an_ordinary_failure_costs_that_endpoint_only(monkeypatch, module, fetch, endpoint):
    """The mirror case: a 404 on one endpoint must not lose `.info` or the others."""
    fake, reads = _fake_ticker(failing=endpoint, error=ValueError("404 Not Found"))
    monkeypatch.setattr(module.yf, "Ticker", fake)

    result = await fetch("TCK")

    assert result[0]["symbol"] == "TCK"
    assert set(reads) >= {"info", endpoint}


@pytest.mark.asyncio
@pytest.mark.parametrize("module, fetch, endpoints", CLOSURES)
async def test_a_fund_is_asked_for_info_and_nothing_it_cannot_have(monkeypatch, module, fetch, endpoints):
    """An ETF has no income statement, earnings dates or analyst estimates: one request,
    not five. (The watchlist still reads history for its RSI.)"""
    fake, reads = _fake_ticker(quote_type="ETF")
    monkeypatch.setattr(module.yf, "Ticker", fake)

    await fetch("FUND")

    assert not set(reads) & {"earnings_dates", *ESTIMATE_ENDPOINTS}


def test_read_optional_re_raises_only_a_rate_limit():
    def _raise(error):
        def read():
            raise error
        return read

    with pytest.raises(RateLimited):
        read_optional(_raise(RateLimited()), "growth_estimates", "TCK")
    assert read_optional(_raise(ValueError("Expecting value")), "growth_estimates", "TCK") is None
    assert read_optional(lambda: 42, "growth_estimates", "TCK") == 42


def test_fetch_estimate_frames_keys_every_endpoint():
    fake, _ = _fake_ticker()
    frames = fetch_estimate_frames(fake("TCK"), {"quoteType": "EQUITY"}, "TCK")
    assert set(frames) == set(ESTIMATE_ENDPOINTS)


# ── one parser ─────────────────────────────────────────────────────────────────

def _estimate(column, value):
    return pd.DataFrame({column: [0.05, value]}, index=["0y", "+1y"])


def test_forward_growth_reads_the_plus_one_year_rows():
    fwd_rev, fwd_eps = forward_growth(
        _estimate("growth", 0.12), _estimate("stockTrend", 0.2), {}
    )
    assert (fwd_rev, fwd_eps) == (0.12, 0.2)


@pytest.mark.parametrize("frame", [None, pd.DataFrame(), pd.DataFrame({"growth": [0.1]}, index=["0y"])])
def test_an_absent_or_empty_estimate_is_none(frame):
    assert forward_growth(frame, frame, {}) == (None, None)


@pytest.mark.parametrize("forward, trailing, expected", [
    (2.2, 2.0, 0.1),
    (2.0, -0.5, None),    # no positive base: no percentage
    (2.0, 0, None),
    (None, 2.0, None),
    (0.0, 2.0, -1.0),     # a forecast of zero earnings is a real -100%, not "missing"
])
def test_the_info_fallback_follows_the_growth_rule(forward, trailing, expected):
    info = {"forwardEps": forward, "trailingEps": trailing}
    assert forward_growth(None, None, info) == (None, expected)


def test_the_consensus_frame_wins_over_the_info_fallback():
    info = {"forwardEps": 3.0, "trailingEps": 1.0}
    assert forward_growth(None, _estimate("stockTrend", 0.2), info)[1] == 0.2


def test_both_services_use_the_one_parser():
    """No service may parse `+1y` itself again — that is how the copies drifted."""
    import inspect

    for module in (fundamentals_module, watchlist_module):
        source = inspect.getsource(module)
        assert "'+1y'" not in source and '"+1y"' not in source, module.__name__
        assert module.forward_growth is forward_growth
