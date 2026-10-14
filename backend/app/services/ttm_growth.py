"""
Trailing-twelve-month year-over-year growth from a yfinance quarterly-financials frame.

Shared by ``FundamentalsService`` and ``WatchlistService`` (and, through
`growth_over_base`, by the forward-estimate fallback in ``yahoo_estimates``). It lived as a private method
on **both**, and the copies had drifted: the watchlist's carried a 5-7-quarter fallback
tier the fundamentals' lacked, and the fundamentals' additionally bailed out on
``quarterly_financials.shape[1] < 8`` — a *column* count, while the tiers measure a
*row's* non-NaN length, so a frame with 8 columns but a sparse row was judged by one
rule and a frame with 6 columns by another. The consequence was visible: the same
security could report different earnings growth on ``/api/fundamentals/portfolio`` and
``/api/watchlist``.

Neither copy touched ``self``, so this is a plain function.

**No positive base, no percentage** (2026-10-14). The division used to be by
``abs(prior)``, guarding only ``prior == 0``, so a company moving from a loss to a profit
reported growth: -0.01 to +0.50 EPS read as **+5,100%**, painted green and scored as
the strongest quality signal the watchlist has. A sign change has no meaningful growth
rate — the same reason `peg_ratio` refuses a negative P/E — so a non-positive base is now
undefined, and so is the `.info` figure that would otherwise stand in for it
(`trailing_growth`).
"""
from typing import Optional, Tuple

from app.services.safe_numbers import safe_float

#: The line-item chains both services pass, named once so they cannot drift apart.
REVENUE_ROWS = ('Total Revenue',)
EARNINGS_ROWS = ('Diluted EPS', 'Basic EPS', 'Net Income')


def growth_over_base(current: Optional[float], prior: Optional[float]) -> Optional[float]:
    """
    ``(current - prior) / prior`` as a fraction rounded to 4 dp, or None.

    None when either side is unknown or the base is not positive: from zero the ratio is
    infinite, and from a loss its sign says nothing about direction.
    """
    if current is None or prior is None or prior <= 0:
        return None
    return round((current - prior) / prior, 4)


def _ttm_growth(quarterly_financials, row_candidates: list) -> Tuple[Optional[float], bool]:
    """``(growth, saw_a_non_positive_base)`` — see `ttm_growth_from_quarterly`."""
    if quarterly_financials is None or quarterly_financials.empty:
        return None, False

    saw_non_positive_base = False
    for row_name in row_candidates:
        if row_name not in quarterly_financials.index:
            continue

        # Columns are newest-first in yfinance's frame; dropna() keeps that order.
        row = quarterly_financials.loc[row_name].dropna()

        if len(row) >= 8:
            current = float(row.iloc[:4].sum())
            prior = float(row.iloc[4:8].sum())
        elif len(row) >= 5:
            current = float(row.iloc[0])
            prior = float(row.iloc[4])
        else:
            continue

        growth = growth_over_base(current, prior)
        if growth is None:
            saw_non_positive_base = True
            continue
        return growth, saw_non_positive_base

    return None, saw_non_positive_base


def ttm_growth_from_quarterly(quarterly_financials, row_candidates: list) -> Optional[float]:
    """
    YoY growth of a line item, with a tiered fallback:

    - **>= 8 quarters** — ``sum(Q1..Q4)`` against ``sum(Q5..Q8)``. The real TTM
      comparison, and the only tier immune to seasonality.
    - **5-7 quarters** — the same quarter a year earlier (``Q[0]`` vs ``Q[4]``).
      Seasonal, but a recently-listed company has nothing better, and reporting
      *nothing* sends the caller to yfinance's own laggier ``.info`` figure.
    - **< 5 quarters** — no answer from this candidate.

    ``row_candidates`` is a fallback *chain*, tried in order, and a candidate that is
    present but unusable (too few quarters, or a base that is not positive) falls through
    to the next — which is why callers pass e.g. ``['Diluted EPS', 'Basic EPS',
    'Net Income']``: an EPS that rounds to 0.00 can still have a Net Income behind it.

    Returns a fraction (0.15 = +15%), rounded to 4 dp, or None.
    """
    return _ttm_growth(quarterly_financials, row_candidates)[0]


def trailing_growth(quarterly_financials, row_candidates: list, info_value) -> Optional[float]:
    """
    The stored trailing growth figure: the statements' TTM growth, else ``info_value``
    (yfinance's own ``revenueGrowth`` / ``earningsGrowth``) — **unless the statements
    showed a non-positive base**. Then the growth is known to be undefined, not merely
    unmeasured, and Yahoo's figure must not stand in for an answer the data refuses.
    """
    growth, saw_non_positive_base = _ttm_growth(quarterly_financials, row_candidates)
    if growth is not None:
        return growth
    if saw_non_positive_base:
        return None
    return safe_float(info_value)
