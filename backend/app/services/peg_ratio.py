"""
PEG from a P/E and an analyst growth estimate.

Shared by ``FundamentalsService`` and ``WatchlistService`` for the same reason
``ttm_growth`` is: it lived inline in **both**, and the two had already diverged.

The formula was identical; the *resolution order* was not. Both tried Yahoo's own
``pegRatio`` first, but the watchlist then tried forward-EPS growth — its own
comment calls that tier "preferred" — before the analyst 5-year CAGR, while
fundamentals went straight to the CAGR and had **no forward-EPS tier at all**. So
a security Yahoo gives no ``pegRatio`` for could show one PEG on
``/api/watchlist`` and a different one, or none, on
``/api/fundamentals/portfolio`` — the same divergence, between the same two
services, that ``ttm_growth`` was extracted to end.

Closing it cost nothing: ``FundamentalsService`` already fetches
``growth_estimates`` and already computes ``fwd_eps`` from it for its own
``fwd_eps_growth`` column — it simply never fed it to the PEG fallback. Both
services now run the same three tiers in the same order over the same inputs.

**The order itself now lives here too** (GitHub issue #11, item b): `resolve_peg` is the
one place that knows it, and both services call it — it was written out in each, plus a
third copy of one tier in `routers/watchlist.py`, which now calls `read_time_peg`. Yahoo's
``trailingPegRatio`` became the first tier: `.info` pays a fundamentals-timeseries request
for that one key on every call, and nothing read it. `tests/test_peg_ratio.py` keeps the
two `.info` key names in this module alone.
"""
from typing import Dict, Optional

from app.services.safe_numbers import safe_float


def growth_pct_from_estimate(
    growth: Optional[float], *, is_fraction: bool = False
) -> Optional[float]:
    """
    Normalise an analyst growth estimate to a **percentage**.

    yfinance is inconsistent about whether these arrive as a decimal (``0.30``) or
    already as a percent (``30``), so where the convention is genuinely unknown it is
    inferred: below 1 it is read as a decimal and scaled.

    **Pass ``is_fraction=True`` when the caller knows it is a decimal**, because the
    inference is wrong in the direction that matters. It reads anything ``>= 1`` as
    already-a-percent, but ``growth_estimates['+1y']['stockTrend']`` is a decimal that
    legitimately exceeds 1 whenever a company is expected to grow more than 100% — and
    this account has three such rows today (SNDK 2.2245, LITE 1.23, MU 1.0933). Inferred,
    SNDK's 222% growth was read as 2.2245%, turning a PEG of **0.25 into 24.56**: not a
    rounding difference but a factor of 100, in the direction that makes a fast-growing
    company look catastrophically overvalued. The buy-score PEG ladder scores the first
    10/10 and the second 0/10.

    That the column is a decimal is not a judgement call — the same value is stored as
    ``fwd_eps_growth`` and rendered by the UI as ``(v * 100).toFixed(1)%``, so the app
    reads it as a fraction everywhere except here.

    This docstring used to call the ambiguity "real but narrow — it misreads only a value
    that is *already* a percent and below 1". That is the harmless direction. The harmful
    one is a fraction above 1, which it did not mention and which is live.

    Both call sites guard on ``growth > 0`` before reaching here, so a negative estimate
    never takes the ``< 1`` branch and cannot be scaled by 100; that guard is load-bearing
    and is asserted in the tests rather than left implicit.

    Returns None for a missing, zero or negative estimate — a PEG divided by one
    of those is not a small number, it is meaningless.
    """
    if growth is None or growth <= 0:
        return None
    if is_fraction:
        return growth * 100
    return growth * 100 if growth < 1 else growth


def peg_from_growth(
    trailing_pe: Optional[float], growth: Optional[float], *, is_fraction: bool = False
) -> Optional[float]:
    """
    ``trailing_pe / growth%``, or None when either side is unusable.

    Refuses rather than guessing on a missing P/E, a non-positive P/E (a loss-making
    company has no meaningful PEG) or an unusable growth estimate.

    ``is_fraction`` forwards to {@link growth_pct_from_estimate}: set it for the
    forward-EPS tier, whose input is a known decimal, and leave it off for the long-term
    CAGR tier, where yfinance's convention really is unknown.
    """
    if trailing_pe is None or trailing_pe <= 0:
        return None
    growth_pct = growth_pct_from_estimate(growth, is_fraction=is_fraction)
    if growth_pct is None:
        return None
    return trailing_pe / growth_pct


#: Yahoo's own PEG figures in `.info`, most preferred first. ``trailingPegRatio`` is the
#: one `.info` pays its third request for; ``pegRatio`` is often absent.
YAHOO_PEG_KEYS = ("trailingPegRatio", "pegRatio")


def forward_eps_peg(trailing_pe: Optional[float], fwd_eps_growth: Optional[float]) -> Optional[float]:
    """The forward-EPS tier: P/E over the `+1y` EPS growth, a known fraction; 4 dp."""
    return safe_float(peg_from_growth(trailing_pe, fwd_eps_growth, is_fraction=True))


def resolve_peg(
    info: Optional[Dict], trailing_pe: Optional[float], fwd_eps_growth: Optional[float],
) -> Optional[float]:
    """
    The PEG both services store, by the one tier order:

    1. Yahoo's ``trailingPegRatio``, then its ``pegRatio`` (`YAHOO_PEG_KEYS`);
    2. trailing P/E over forward EPS growth (`forward_eps_peg`) — the watchlist's
       "preferred" derived tier, which fundamentals once lacked;
    3. trailing P/E over the analyst long-term EPS CAGR, whose unit is inferred.

    None when no tier answers.
    """
    info = info or {}
    for key in YAHOO_PEG_KEYS:
        peg = safe_float(info.get(key))
        if peg is not None:
            return peg
    peg = forward_eps_peg(trailing_pe, fwd_eps_growth)
    if peg is not None:
        return peg
    lt_growth = safe_float(info.get("longTermGrowth") or info.get("longTermEpsGrowth"))
    return safe_float(peg_from_growth(trailing_pe, lt_growth))


def read_time_peg(
    stored: Optional[float], trailing_pe: Optional[float], fwd_eps_growth: Optional[float],
) -> Optional[float]:
    """A stored PEG, else the forward-EPS tier over the stored inputs (`routers/watchlist`)."""
    if stored is not None:
        return stored
    return forward_eps_peg(trailing_pe, fwd_eps_growth)
