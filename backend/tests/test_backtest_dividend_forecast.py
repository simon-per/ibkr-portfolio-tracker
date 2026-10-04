"""
The replay backtest scores sizing rules through `project_dividends` itself.

Pinned here: the scoring is honest on shapes whose answer is known by hand — a
seasonal fund (the same payment one year later is exact, the flat median is not)
and a raiser (the latest payment is exact once the raise is paid).
"""
from datetime import date
from decimal import Decimal

from app.cli.backtest_dividend_forecast import score_security
from app.services.dividend_forecast import HistPayment


def _quarterly(years, row):
    return [
        HistPayment(on_date=date(y, m, 15), per_share_eur=Decimal(a))
        for y in years for m, a in zip((3, 6, 9, 12), row)
    ]


def test_a_repeating_seasonal_pattern_is_scored_exact_for_same_payment_and_low_for_the_median():
    history = _quarterly(range(2021, 2026), ["0.40", "0.70", "0.45", "0.90"])
    result, specials, points = score_security(history)
    assert points > 0 and specials == []
    pred, act, err = result["same_payment_last_year"]
    assert pred == act and err == 0
    pred, act, _ = result["median8"]
    assert pred < act        # the flat median averages the December away


def test_a_level_payer_is_exact_under_every_rule():
    history = _quarterly(range(2022, 2026), ["0.50"] * 4)
    result, _, _ = score_security(history)
    for method, (pred, act, err) in result.items():
        assert err == 0, method


def test_a_special_is_listed_and_scored_out_of_the_realized_side():
    history = _quarterly(range(2022, 2026), ["1.00"] * 4)
    history[9] = HistPayment(on_date=history[9].on_date, per_share_eur=Decimal("6.00"))
    _, specials, _ = score_security(history)
    assert [p.per_share_eur for p in specials] == [Decimal("6.00")]


def test_too_short_a_history_scores_nothing():
    result, _, points = score_security(_quarterly([2025], ["1"] * 4))
    assert points == 0
