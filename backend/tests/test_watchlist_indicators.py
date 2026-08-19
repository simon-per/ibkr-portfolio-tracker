"""
RSI must refuse a flat series rather than call it maximally overbought.

`_compute_rsi` returned 100.0 whenever `avg_loss == 0`, which conflates two very
different things. Gains with no losses is a real, maximal RSI — an unbroken advance.
*Nothing moving* is not: RSI is undefined on a flat series, and 100 is the strongest
overbought reading the scale has.

The consequence is not cosmetic, because `_compute_buy_score` reads it:

    rsi = 100   ->  rsi_sub = 0   (worst technical timing)
    rsi = None  ->  rsi_sub = 5   (explicitly neutral)

So the fabricated value scored a full ten points *worse* than admitting the metric could
not be measured — and the neutral branch already existed, which is what makes this a
substitution rather than a missing case.

This is the lens CLAUDE.md draws from the Sharpe and concentration fixes: when a metric
can be unknown, ask what its stand-in value would **claim**. Severity tracks
plausibility, and an RSI of 100 is entirely plausible.

Reachable on a halted or delisted listing, a fixed-NAV fund, or a very illiquid one —
and the watchlist is where arbitrary tickers get added, so it is more exposed to those
than the portfolio is.

Offline: pure arithmetic on seeded arrays, no DB and no network.
"""

import numpy as np

from app.services.watchlist_service import WatchlistService

# Built with __new__ on purpose: _compute_rsi and _compute_buy_score are pure, and
# constructing the service would demand a session for no reason. This is also why the
# `rate_limited` latch is declared on the class as well as in __init__.
service = WatchlistService.__new__(WatchlistService)


def test_a_flat_series_has_no_rsi():
    """Nothing moved, so there is nothing to measure — not an extreme reading."""
    assert service._compute_rsi(np.full(60, 100.0)) is None


def test_an_unbroken_advance_is_still_a_real_hundred():
    """The case the old branch was written for must keep working."""
    rising = np.arange(1.0, 61.0)  # every day a gain, never a loss
    assert service._compute_rsi(rising) == 100.0


def test_an_unbroken_decline_is_zero_not_none():
    """The mirror: all losses is a genuine floor, and 0 must not become None."""
    falling = np.arange(100.0, 40.0, -1.0)
    assert service._compute_rsi(falling) == 0.0


def test_a_mixed_series_lands_between_the_extremes():
    closes = np.array([100.0 + (1 if i % 2 else -1) * (i % 5) for i in range(60)])
    rsi = service._compute_rsi(closes)
    assert rsi is not None
    assert 0.0 < rsi < 100.0


def test_too_short_a_series_still_refuses():
    assert service._compute_rsi(np.full(5, 100.0)) is None


# Every case below carries `profit_margins` alongside the RSI it is actually about.
# `_compute_buy_score` now refuses outright when not one of its twelve inputs is
# present, so a dict holding only `{"rsi14": None}` means "nothing is known about this
# security" and correctly scores None — which would make these two assertions about the
# *RSI branch* untestable. One measurable input keeps the score defined so the branch
# under test is the thing being compared.
_OTHERWISE_KNOWN = {"profit_margins": 0.20}


def test_a_flat_series_no_longer_costs_ten_points_of_buy_score():
    """
    The reason the refusal matters. Scored through the real function, an unmeasurable
    RSI must land on the neutral branch rather than the worst one.
    """
    flat = {**_OTHERWISE_KNOWN, "rsi14": service._compute_rsi(np.full(60, 100.0))}
    overbought = {**_OTHERWISE_KNOWN, "rsi14": 100.0}

    assert flat["rsi14"] is None
    # 5 (neutral, unknown) vs 0 (measured and maximally overbought)
    assert service._compute_buy_score(flat) > service._compute_buy_score(overbought)


def test_the_neutral_branch_is_genuinely_the_midpoint():
    """
    Guard on the guard: the fix only helps because `rsi is None` scores 5 of 10. If
    that branch ever changed to 0, refusing would stop being better than guessing and
    this whole change would be inert.
    """
    unknown = service._compute_buy_score({**_OTHERWISE_KNOWN, "rsi14": None})
    worst = service._compute_buy_score({**_OTHERWISE_KNOWN, "rsi14": 100.0})
    best = service._compute_buy_score({**_OTHERWISE_KNOWN, "rsi14": 10.0})
    assert worst < unknown < best


def test_a_security_nothing_is_known_about_scores_nothing():
    """
    The empty dict used to score **48.0** — every dimension's neutral fallback summed.
    That is not a cautious estimate: `watchlistColumns.tsx` paints anything from 40 up
    yellow, and `WatchlistTab` sorts by this column by default, so a ticker with no data
    at all outranked every genuinely measured 35-47 and read as a middling opportunity.

    Reachable in one click — add a halted, delisted or thinly covered ticker and
    `yf.Ticker().info` comes back sparse.
    """
    assert service._compute_buy_score({}) is None
    assert service._compute_buy_score({field: None for field in service._SCORE_INPUTS}) is None
    # Fields that are not scoring inputs must not rescue it.
    assert service._compute_buy_score({"symbol": "XYZ", "current_price": 12.0}) is None


def test_one_measurable_input_is_enough_to_produce_a_score():
    """
    The refusal is "nothing is known", not "not everything is known". Demanding a full
    set would blank the column for most of a real watchlist, which is the opposite
    failure.
    """
    assert service._compute_buy_score({"profit_margins": 0.30}) is not None
