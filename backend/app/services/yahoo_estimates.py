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

**`growth_estimates` is no longer read** (GitHub issue #11, item b). Its `stockTrend`
column — the only one used — is `earningsTrend.trend[i].growth`, which `revenue_estimate`
has already fetched and yfinance caches on the ticker; the property then always added a
quoteSummary call for `industryTrend,sectorTrend,indexTrend` that nothing read.
`eps_growth_from_trend` takes the same value from that cache: one request a company saved
per pass, identical by construction (`tests/test_yahoo_estimates.py` pins the cache's
location against the installed yfinance, so a move fails loudly rather than silently
falling back to `.info`).

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
#: `revenue_estimate` fetches `earningsTrend`, which `eps_growth_from_trend` reads after it.
ESTIMATE_ENDPOINTS = ("quarterly_financials", "revenue_estimate")

#: The key `fetch_estimate_frames` adds for the `+1y` EPS growth read off the cached trend.
EPS_TREND_GROWTH = "eps_trend_growth"

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


def is_fund_quote_type(quote_type: Optional[str]) -> bool:
    """The one quoteType test for "a fund": also read off the stored
    `fundamental_metrics.quote_type` (`FundamentalMetrics.is_fund`, the ratings pass)."""
    return quote_type in FUND_QUOTE_TYPES


def is_fund(info: Optional[Dict]) -> bool:
    return is_fund_quote_type((info or {}).get("quoteType"))


def eps_growth_from_trend(ticker: Any, label: str = "") -> Optional[float]:
    """
    ``earningsTrend.trend['+1y'].growth`` from the trend yfinance cached on ``ticker``.

    Exactly what ``growth_estimates['+1y']['stockTrend']`` reported (yfinance builds that
    column from ``item['growth']['raw']`` of the same list), without the extra request.
    Read from ``ticker._analysis._earnings_trend``, which ``revenue_estimate`` fills;
    None when it never ran (a fund), failed, or the trend has no ``+1y`` period. A plain
    attribute read — never a request. The private path is pinned by
    ``test_the_trend_cache_is_where_yfinance_keeps_it``.
    """
    try:
        trend = ticker._analysis._earnings_trend
    except AttributeError:
        return None
    for item in trend or ():
        if isinstance(item, dict) and item.get("period") == "+1y":
            return safe_float((item.get("growth") or {}).get("raw"))
    return None


def fetch_estimate_frames(ticker: Any, info: Optional[Dict], label: str) -> Dict[str, Any]:
    """
    `ESTIMATE_ENDPOINTS` for one ticker, keyed by name, plus `EPS_TREND_GROWTH`; each None
    when unavailable.

    Not asked at all for a fund (`FUND_QUOTE_TYPES`), which `.info` — already fetched —
    identifies for free.
    """
    if is_fund(info):
        return dict.fromkeys((*ESTIMATE_ENDPOINTS, EPS_TREND_GROWTH))
    frames = {
        name: read_optional(lambda name=name: getattr(ticker, name), name, label)
        for name in ESTIMATE_ENDPOINTS
    }
    frames[EPS_TREND_GROWTH] = eps_growth_from_trend(ticker, label)
    return frames


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
    revenue_estimate, eps_trend_growth: Optional[float], info: Optional[Dict], label: str = "",
) -> Tuple[Optional[float], Optional[float]]:
    """
    ``(fwd_revenue_growth, fwd_eps_growth)`` as fractions (0.15 = +15%), each or None.

    ``eps_trend_growth`` is `eps_growth_from_trend`'s answer. Forward EPS growth falls
    back to ``forwardEps`` against ``trailingEps`` from `.info` (already fetched, no extra
    request) when the consensus has no `+1y` growth — under the same rule as trailing
    growth: no positive base, no percentage (`ttm_growth.growth_over_base`).
    """
    fwd_rev = _plus_one_year(revenue_estimate, "growth", label)
    fwd_eps = safe_float(eps_trend_growth)
    if fwd_eps is None:
        info = info or {}
        fwd_eps = growth_over_base(
            safe_float(info.get("forwardEps")), safe_float(info.get("trailingEps"))
        )
    return fwd_rev, fwd_eps
