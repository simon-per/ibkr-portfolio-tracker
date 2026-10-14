"""
The analyst-estimate half of a Yahoo fetch, shared by `FundamentalsService` and
`WatchlistService`.

Both read `.info` and then the same three secondary endpoints — `quarterly_financials`,
`revenue_estimate`, `growth_estimates` — each in a `try` of its own, and both parsed the
forward estimates out of the last two. Those were two copies, and they had drifted the way
copies here do: only the watchlist's checked `.empty`, and only the watchlist's derived
forward EPS growth from `.info` when the estimate frame was missing, so the same security
could carry a forward EPS growth (and a PEG built on it) on one screen and none on the
other.

Worse, **both closures swallowed every exception from the secondary endpoints, a 429
included**. `.info` itself was unguarded, so a rate limit there did reach the loop's
`is_rate_limit` breaker — but one that landed on any of the four later reads was logged
and the pass went on asking the same IP about the next security. The watchlist runs
nightly, so that was rule 1 (CLAUDE.md) broken on a schedule.
`tests/test_yahoo_estimates.py` holds the family to it.

Pure helpers: no yfinance import, no I/O of their own. `ticker` is whatever
`yf.Ticker(...)` the caller already built inside its worker thread.
"""
import logging
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from app.services.safe_numbers import safe_float
from app.services.ttm_growth import growth_over_base
from app.services.yahoo_rate_limit import is_rate_limit

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The secondary endpoints both loops read after `.info`, in the order they are asked.
ESTIMATE_ENDPOINTS = ("quarterly_financials", "revenue_estimate", "growth_estimates")

#: Yahoo `quoteType`s with no income statement and no analyst estimates. Asking for them
#: anyway is three requests per fund per pass that can only come back empty.
FUND_QUOTE_TYPES = frozenset({"ETF", "MUTUALFUND"})


def read_optional(read: Callable[[], T], endpoint: str, label: str) -> Optional[T]:
    """
    One secondary Yahoo read: its value, or None when it failed for this ticker alone.

    **A rate limit is re-raised**, because it is not this ticker's problem — the caller's
    loop must see it to abandon the pass. Anything else (a 404, an empty JSON body, a
    frame yfinance could not parse) costs this endpoint and is logged.
    """
    try:
        return read()
    except Exception as e:
        if is_rate_limit(e):
            raise
        logger.warning(f"{endpoint} failed for {label}: {type(e).__name__}: {e}")
        return None


def is_fund(info: Optional[Dict]) -> bool:
    return (info or {}).get("quoteType") in FUND_QUOTE_TYPES


def fetch_estimate_frames(ticker: Any, info: Optional[Dict], label: str) -> Dict[str, Any]:
    """
    `ESTIMATE_ENDPOINTS` for one ticker, keyed by name; each None when unavailable.

    Not asked at all for a fund (`FUND_QUOTE_TYPES`), which `.info` — already fetched —
    identifies for free.
    """
    if is_fund(info):
        return dict.fromkeys(ESTIMATE_ENDPOINTS)
    return {
        name: read_optional(lambda name=name: getattr(ticker, name), name, label)
        for name in ESTIMATE_ENDPOINTS
    }


def _plus_one_year(frame, column: str, label: str) -> Optional[float]:
    """The `+1y` row of an estimate frame, or None when it has none."""
    try:
        if frame is None or getattr(frame, "empty", True) or "+1y" not in frame.index:
            return None
        return safe_float(frame.loc["+1y", column])
    except Exception as e:
        # Parsing only — the frame is already in hand, nothing here reaches Yahoo.
        logger.warning(f"+1y {column} unreadable for {label}: {type(e).__name__}: {e}")
        return None


def forward_growth(
    revenue_estimate, growth_estimates, info: Optional[Dict], label: str = "",
) -> Tuple[Optional[float], Optional[float]]:
    """
    ``(fwd_revenue_growth, fwd_eps_growth)`` as fractions (0.15 = +15%), each or None.

    Forward EPS growth falls back to ``forwardEps`` against ``trailingEps`` from `.info`
    (already fetched, no extra request) when the consensus frame has no `+1y` row — under
    the same rule as trailing growth: no positive base, no percentage
    (`ttm_growth.growth_over_base`).
    """
    fwd_rev = _plus_one_year(revenue_estimate, "growth", label)
    fwd_eps = _plus_one_year(growth_estimates, "stockTrend", label)
    if fwd_eps is None:
        info = info or {}
        fwd_eps = growth_over_base(
            safe_float(info.get("forwardEps")), safe_float(info.get("trailingEps"))
        )
    return fwd_rev, fwd_eps
