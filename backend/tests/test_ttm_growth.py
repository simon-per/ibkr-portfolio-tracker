"""
The TTM growth helper shared by FundamentalsService and WatchlistService.

It was duplicated and the copies had diverged, so the same security could report
different earnings growth on /api/fundamentals/portfolio and /api/watchlist. These
tests pin the tiers and the fallback-chain semantics so the copies cannot come back.
"""
import numpy as np
import pandas as pd
import pytest

from app.services.ttm_growth import (
    EARNINGS_ROWS,
    growth_over_base,
    trailing_growth,
    ttm_growth_from_quarterly,
)


def frame(rows: dict, n_cols: int | None = None) -> pd.DataFrame:
    """
    A yfinance-shaped quarterly frame: line items as the index, quarters as columns,
    newest first. Values are padded with NaN to a common width, which is what a real
    frame looks like when one line item has a shorter history than another.
    """
    width = n_cols or max(len(v) for v in rows.values())
    padded = {k: list(v) + [np.nan] * (width - len(v)) for k, v in rows.items()}
    cols = pd.date_range("2026-06-30", periods=width, freq="-91D")
    return pd.DataFrame.from_dict(padded, orient="index", columns=cols)


def test_eight_quarters_compare_trailing_twelve_months():
    # Newest four sum to 440, prior four to 400 → +10%.
    df = frame({"Total Revenue": [110, 110, 110, 110, 100, 100, 100, 100]})
    assert ttm_growth_from_quarterly(df, ["Total Revenue"]) == 0.1


def test_more_than_eight_quarters_uses_only_the_most_recent_eight():
    # The trailing 100s must not dilute the comparison.
    df = frame({"Total Revenue": [110, 110, 110, 110, 100, 100, 100, 100, 1, 1, 1]})
    assert ttm_growth_from_quarterly(df, ["Total Revenue"]) == 0.1


def test_five_to_seven_quarters_fall_back_to_the_same_quarter_a_year_earlier():
    """
    The tier FundamentalsService lacked. A recently-listed company has nothing better,
    and returning None here sends the caller to yfinance's laggier .info figure.
    """
    df = frame({"Total Revenue": [120, 0, 0, 0, 100]})
    assert ttm_growth_from_quarterly(df, ["Total Revenue"]) == 0.2

    # Still the same-quarter tier at 7, and the TTM tier the moment an 8th arrives.
    df7 = frame({"Total Revenue": [120, 5, 5, 5, 100, 5, 5]})
    assert ttm_growth_from_quarterly(df7, ["Total Revenue"]) == 0.2


def test_fewer_than_five_quarters_yields_nothing():
    df = frame({"Total Revenue": [110, 100, 90, 80]})
    assert ttm_growth_from_quarterly(df, ["Total Revenue"]) is None


def test_a_column_count_does_not_decide_it_a_rows_non_nan_length_does():
    """
    FundamentalsService bailed on `shape[1] < 8` — a *column* count, while the tiers
    measure a row's non-NaN length. A wide frame with a sparse row and a narrow frame
    with a full row were judged by different rules; both now go by the row.
    """
    # 12 columns, but only 6 real quarters → the same-quarter tier, not None.
    sparse = frame({"Total Revenue": [120, 5, 5, 5, 100, 5]}, n_cols=12)
    assert sparse.shape[1] == 12
    assert ttm_growth_from_quarterly(sparse, ["Total Revenue"]) == 0.2

    # 6 columns, all real → the same answer. The old fundamentals copy returned None.
    narrow = frame({"Total Revenue": [120, 5, 5, 5, 100, 5]})
    assert narrow.shape[1] == 6
    assert ttm_growth_from_quarterly(narrow, ["Total Revenue"]) == 0.2


def test_nans_are_dropped_before_the_quarters_are_counted():
    df = frame({"Total Revenue": [110, np.nan, 110, 110, 110, 100, 100, 100, 100]})
    assert ttm_growth_from_quarterly(df, ["Total Revenue"]) == 0.1


def test_a_zero_base_is_undefined_growth_not_infinite_growth():
    df = frame({"Diluted EPS": [10, 10, 10, 10, 0, 0, 0, 0]})
    assert ttm_growth_from_quarterly(df, ["Diluted EPS"]) is None


def test_a_recovery_from_a_loss_has_no_growth_rate():
    """
    The base was a loss, so there is no meaningful percentage. It used to divide by
    ``abs(prior)``: -0.01 to +0.50 EPS read as +5,100%, green and top-scored.
    """
    df = frame({"Net Income": [25, 25, 25, 25, -25, -25, -25, -25]})
    assert ttm_growth_from_quarterly(df, ["Net Income"]) is None

    tiny_loss = frame({"Diluted EPS": [0.5, 0.0, 0.0, 0.0, -0.01]})
    assert ttm_growth_from_quarterly(tiny_loss, ["Diluted EPS"]) is None


def test_a_loss_widening_or_narrowing_has_no_growth_rate_either():
    for current in (-50, -10):
        df = frame({"Net Income": [current] * 4 + [-25] * 4})
        assert ttm_growth_from_quarterly(df, ["Net Income"]) is None


def test_a_profit_turning_into_a_loss_is_still_negative_growth():
    df = frame({"Net Income": [-5, -5, 0, 0, 25, 25, 25, 25]})
    assert ttm_growth_from_quarterly(df, ["Net Income"]) == -1.1


@pytest.mark.parametrize("current, prior, expected", [
    (110, 100, 0.1),
    (90, 100, -0.1),
    (0.5, -0.01, None),     # loss to profit
    (-1, -2, None),         # a narrowing loss
    (5, 0, None),           # from zero
    (None, 100, None),
    (100, None, None),
])
def test_growth_over_base(current, prior, expected):
    assert growth_over_base(current, prior) == expected


def test_trailing_growth_falls_back_to_info_only_when_the_statements_are_silent():
    """
    Too few quarters: the statements cannot answer, so yfinance's own figure stands in.
    A loss base: the statements *did* answer — the growth is undefined — and Yahoo's
    figure must not paper over that.
    """
    short = frame({"Diluted EPS": [1, 1, 1]})
    assert trailing_growth(short, EARNINGS_ROWS, 0.42) == 0.42
    assert trailing_growth(None, EARNINGS_ROWS, 0.42) == 0.42
    assert trailing_growth(None, EARNINGS_ROWS, float("nan")) is None

    loss_base = frame({"Diluted EPS": [1, 1, 1, 1, -1, -1, -1, -1]})
    assert trailing_growth(loss_base, EARNINGS_ROWS, 3.5) is None

    usable = frame({"Diluted EPS": [110, 110, 110, 110, 100, 100, 100, 100]})
    assert trailing_growth(usable, EARNINGS_ROWS, 3.5) == 0.1


def test_a_non_positive_base_still_falls_through_to_the_next_candidate():
    """An EPS that rounds to 0.00 can have a Net Income behind it."""
    df = frame({
        "Diluted EPS": [0.01, 0.01, 0.01, 0.01, 0, 0, 0, 0],
        "Net Income": [110, 110, 110, 110, 100, 100, 100, 100],
    })
    assert trailing_growth(df, EARNINGS_ROWS, 9.9) == 0.1


def test_candidates_are_a_fallback_chain_not_a_first_match_win():
    """
    A present-but-unusable candidate falls through, which is the whole reason callers
    pass ['Diluted EPS', 'Basic EPS', 'Net Income'].
    """
    df = frame({
        "Diluted EPS": [1, 1],                              # too short
        "Basic EPS": [10, 10, 10, 10, 0, 0, 0, 0],          # zero base
        "Net Income": [110, 110, 110, 110, 100, 100, 100, 100],
    })
    assert ttm_growth_from_quarterly(df, ["Diluted EPS", "Basic EPS", "Net Income"]) == 0.1


def test_candidate_order_decides_which_line_item_answers():
    df = frame({
        "Diluted EPS": [110, 110, 110, 110, 100, 100, 100, 100],
        "Net Income": [400, 400, 400, 400, 100, 100, 100, 100],
    })
    assert ttm_growth_from_quarterly(df, ["Diluted EPS", "Net Income"]) == 0.1
    assert ttm_growth_from_quarterly(df, ["Net Income", "Diluted EPS"]) == 3.0


@pytest.mark.parametrize("empty", [None, pd.DataFrame()])
def test_a_missing_or_empty_frame_is_survivable(empty):
    assert ttm_growth_from_quarterly(empty, ["Total Revenue"]) is None


def test_an_absent_line_item_is_survivable():
    df = frame({"Total Revenue": [110, 110, 110, 110, 100, 100, 100, 100]})
    assert ttm_growth_from_quarterly(df, ["Diluted EPS"]) is None


def test_an_all_nan_row_is_survivable():
    df = frame({"Diluted EPS": [np.nan] * 8})
    assert ttm_growth_from_quarterly(df, ["Diluted EPS"]) is None


def test_both_services_call_the_one_implementation():
    """
    The regression guard. Neither service may carry its own copy again — that is how
    the 5-7-quarter tier came to exist on one side only.
    """
    from app.services import fundamentals_service, watchlist_service

    assert not hasattr(fundamentals_service.FundamentalsService, "_ttm_growth_from_quarterly")
    assert not hasattr(watchlist_service.WatchlistService, "_ttm_growth_from_quarterly")
    assert fundamentals_service.trailing_growth is trailing_growth
    assert watchlist_service.trailing_growth is trailing_growth
    assert fundamentals_service.EARNINGS_ROWS is watchlist_service.EARNINGS_ROWS
