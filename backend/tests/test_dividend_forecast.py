"""
Unit tests for the pure dividend forecast inference (app/services/dividend_forecast.py).

The forecast invents nothing: cadence comes from the spacing of past ex-dates,
the size from the recent dividend per share scaled to the holding we have now,
and every "can't know" case must refuse ([]), not guess.

Working per share is what lets a recently bought payer be forecast at all — the
schedule belongs to the company, not to how long we have owned it.
"""
from datetime import date
from decimal import Decimal

from app.services.dividend_forecast import (
    ForecastPayment,
    HistPayment,
    infer_gap_days,
    project_dividends,
)


def _hist(dates, per_share="1"):
    return [
        HistPayment(
            on_date=d,
            per_share_eur=Decimal(per_share) if per_share is not None else None,
        )
        for d in dates
    ]


QUARTERLY = [date(2025, 10, 15), date(2026, 1, 15), date(2026, 4, 15)]


def test_quarterly_cadence_is_inferred():
    assert infer_gap_days(QUARTERLY) == 91


def test_a_single_payment_has_no_cadence():
    assert infer_gap_days([date(2026, 1, 15)]) is None


def test_slower_than_annual_is_not_a_cadence():
    assert infer_gap_days([date(2024, 1, 1), date(2025, 6, 1), date(2026, 12, 1)]) is None


def test_subweekly_noise_is_not_a_cadence():
    dates = [date(2026, 1, d) for d in (1, 8, 15, 22)]
    assert infer_gap_days(dates) is None


def test_projection_lands_inside_the_horizon_only():
    out = project_dividends(
        _hist(QUARTERLY), Decimal("10"),
        horizon_start=date(2026, 5, 2), horizon_end=date(2026, 12, 31),
    )
    # ~91 days snaps to quarterly, so it keeps the 15th rather than drifting
    assert [fp.on_date for fp in out] == [date(2026, 7, 15), date(2026, 10, 15)]
    assert all(fp.net_eur == Decimal("10") for fp in out)   # 1/share x 10 shares


def test_a_stopped_payer_is_not_resurrected():
    out = project_dividends(
        _hist(QUARTERLY), Decimal("10"),
        horizon_start=date(2027, 2, 1), horizon_end=date(2027, 12, 31),
    )
    assert out == []


def test_the_amount_follows_the_current_holding():
    out = project_dividends(
        _hist(QUARTERLY, per_share="2"), Decimal("25"),
        horizon_start=date(2026, 5, 2), horizon_end=date(2026, 8, 1),
    )
    assert out == [ForecastPayment(on_date=date(2026, 7, 15), net_eur=Decimal("50"))]


def test_median_resists_a_special_dividend():
    history = _hist(QUARTERLY) + [
        HistPayment(on_date=date(2026, 4, 20), per_share_eur=Decimal("40"))
    ]
    out = project_dividends(
        history, Decimal("10"),
        horizon_start=date(2026, 5, 2), horizon_end=date(2026, 9, 1),
    )
    assert out and all(fp.net_eur == Decimal("10") for fp in out)


def test_nothing_held_projects_nothing():
    assert project_dividends(_hist(QUARTERLY), Decimal("0"),
                             date(2026, 5, 2), date(2026, 12, 31)) == []


def test_dates_without_amounts_still_establish_the_cadence():
    """
    A payment we can't price still proves the schedule; only one priced payment
    in the sample is needed to size the projection.
    """
    history = [
        HistPayment(on_date=QUARTERLY[0], per_share_eur=None),
        HistPayment(on_date=QUARTERLY[1], per_share_eur=None),
        HistPayment(on_date=QUARTERLY[2], per_share_eur=Decimal("3")),
    ]
    out = project_dividends(history, Decimal("10"),
                            date(2026, 5, 2), date(2026, 8, 1))
    assert out == [ForecastPayment(on_date=date(2026, 7, 15), net_eur=Decimal("30"))]


def test_a_schedule_with_no_priced_payment_refuses():
    history = _hist(QUARTERLY, per_share=None)
    assert project_dividends(history, Decimal("10"),
                             date(2026, 5, 2), date(2026, 12, 31)) == []


def test_a_full_future_year_is_projected():
    """
    The whole of next year, not just the remainder of this one — and asking
    about a distant horizon must not make a current payer look stopped.
    """
    out = project_dividends(
        _hist(QUARTERLY), Decimal("10"),
        horizon_start=date(2027, 1, 1), horizon_end=date(2027, 12, 31),
        as_of=date(2026, 5, 1),
    )
    assert [fp.on_date.year for fp in out] == [2027] * len(out)
    assert len(out) == 4   # quarterly


def test_staleness_is_judged_from_now_not_from_the_horizon():
    """A payer that really has stopped stays stopped, however far out we look."""
    out = project_dividends(
        _hist(QUARTERLY), Decimal("10"),
        horizon_start=date(2027, 1, 1), horizon_end=date(2027, 12, 31),
        as_of=date(2027, 1, 1),   # ~9 months since the last ex-date
    )
    assert out == []


def test_a_monthly_payer_gets_twelve_payments_a_year():
    """
    Real schedules pay on a day of the month. Stepping by a fixed 31 days drifts
    against the calendar and gave SBI 11 payouts in 2027 instead of 12.
    """
    monthly = [date(2026, 2, 20), date(2026, 3, 20), date(2026, 4, 20)]
    out = project_dividends(
        _hist(monthly), Decimal("10"),
        horizon_start=date(2027, 1, 1), horizon_end=date(2027, 12, 31),
        as_of=date(2026, 5, 1),
    )
    assert len(out) == 12
    assert [fp.on_date.month for fp in out] == list(range(1, 13))
    assert all(fp.on_date.day == 20 for fp in out)   # the schedule's own day


def test_a_quarterly_payer_keeps_its_day_of_month():
    out = project_dividends(
        _hist(QUARTERLY), Decimal("10"),
        horizon_start=date(2027, 1, 1), horizon_end=date(2027, 12, 31),
        as_of=date(2026, 5, 1),
    )
    assert len(out) == 4
    assert [fp.on_date.month for fp in out] == [1, 4, 7, 10]
    assert all(fp.on_date.day == 15 for fp in out)


def test_month_end_dates_are_clamped_not_overflowed():
    """A 31st-of-the-month payer must land on the 28th/30th, not spill over."""
    monthly = [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    out = project_dividends(
        _hist(monthly), Decimal("1"),
        horizon_start=date(2026, 4, 1), horizon_end=date(2026, 7, 1),
        as_of=date(2026, 4, 1),
    )
    assert [str(fp.on_date) for fp in out] == ["2026-04-30", "2026-05-31", "2026-06-30"]


def test_an_irregular_cadence_still_steps_in_days():
    """Nothing near a calendar period keeps the old day-stepping behaviour."""
    irregular = [date(2026, 1, 1), date(2026, 2, 20), date(2026, 4, 10)]  # 50d, 49d
    out = project_dividends(
        _hist(irregular), Decimal("1"),
        horizon_start=date(2026, 4, 11), horizon_end=date(2026, 8, 1),
        as_of=date(2026, 4, 11),
    )
    # median 49 days, and 49 matches no calendar period, so it steps in days
    assert [str(fp.on_date) for fp in out] == ["2026-05-29", "2026-07-17"]


# ---- Sizing: "same payment one year later", and the latest for a steady payer ------
#
# Until 2026-10-04 every projected payment was one flat median of the last 8, which
# read low twice over: it averaged away a fund's large December, and it trailed every
# raise by up to two years (NVDA had raised to 0.25 a quarter and projected ~0.01).

from app.services.dividend_forecast import (  # noqa: E402
    METHOD_LATEST,
    METHOD_SAME_PAYMENT,
    SIZING_MEDIAN8,
    is_steady,
    special_payments,
)


def _series(points):
    return [HistPayment(on_date=d, per_share_eur=Decimal(a)) for d, a in points]


def _quarters(years, amounts, day=15):
    """Quarterly payments in Mar/Jun/Sep/Dec of each year, amounts cycled per year."""
    out = []
    for y, row in zip(years, amounts):
        for m, a in zip((3, 6, 9, 12), row):
            out.append((date(y, m, day), a))
    return out


SEASONAL = _series(_quarters([2024, 2025], [
    ["0.40", "0.70", "0.45", "0.90"],
    ["0.42", "0.72", "0.47", "0.95"],
]))


def test_a_seasonal_fund_keeps_its_large_december():
    out = project_dividends(SEASONAL, Decimal("1"),
                            date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 1, 1))
    assert [(fp.on_date.month, fp.net_eur) for fp in out] == [
        (3, Decimal("0.42")), (6, Decimal("0.72")), (9, Decimal("0.47")), (12, Decimal("0.95")),
    ]
    assert {fp.method for fp in out} == {METHOD_SAME_PAYMENT}
    # The old flat median would have read 0.585 x 4 = 2.34 against 2.56 paid.
    assert sum(fp.net_eur for fp in out) == Decimal("2.56")


def test_the_flat_median_is_still_available_to_the_backtest():
    out = project_dividends(SEASONAL, Decimal("1"),
                            date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 1, 1),
                            sizing=SIZING_MEDIAN8)
    assert {fp.net_eur for fp in out} == {Decimal("0.585")}


def test_a_projection_past_a_year_out_repeats_a_real_payment_not_a_projection():
    """The horizon reaches the end of next year; a slot two years back is a real one."""
    out = project_dividends(SEASONAL, Decimal("1"),
                            date(2027, 1, 1), date(2027, 12, 31), as_of=date(2026, 1, 1))
    assert [fp.net_eur for fp in out] == [
        Decimal("0.42"), Decimal("0.72"), Decimal("0.47"), Decimal("0.95"),
    ]


def test_a_raise_already_paid_carries_into_every_later_payment():
    """NVDA: level at 0.01 for years, then 0.25. The new level is the forecast."""
    history = _series(_quarters([2025, 2026], [
        ["0.01", "0.01", "0.01", "0.01"],
        ["0.01", "0.01", "0.25"],
    ]))
    assert is_steady(history)
    out = project_dividends(history, Decimal("8"),
                            date(2026, 10, 1), date(2027, 9, 30), as_of=date(2026, 10, 1))
    assert len(out) == 4
    assert all(fp.net_eur == Decimal("2.00") for fp in out)
    assert {fp.method for fp in out} == {METHOD_LATEST}


def test_a_small_raise_inside_the_tolerance_is_carried_too():
    history = _series(_quarters([2025, 2026], [
        ["0.20", "0.20", "0.20", "0.21"],
        ["0.21", "0.21", "0.22"],
    ]))
    out = project_dividends(history, Decimal("1"),
                            date(2026, 10, 1), date(2027, 9, 30), as_of=date(2026, 10, 1))
    assert {fp.net_eur for fp in out} == {Decimal("0.22")}


def test_a_cut_already_paid_is_carried_as_well():
    """Facts both ways: a company that has cut is forecast at the cut level."""
    history = _series(_quarters([2025, 2026], [
        ["1.00", "1.00", "1.00", "1.00"],
        ["1.00", "1.00", "0.50"],
    ]))
    out = project_dividends(history, Decimal("1"),
                            date(2026, 10, 1), date(2027, 9, 30), as_of=date(2026, 10, 1))
    assert {fp.net_eur for fp in out} == {Decimal("0.50")}


def test_a_year_end_payment_that_always_jumps_is_not_a_raise():
    """
    SK Hynix's shape: level quarters and a large year-end payment. The latest being
    the big one must not size every quarter at the big amount — the year before
    jumped the same way, so the payer is varying and each slot repeats its own.
    """
    history = _series(_quarters([2024, 2025], [
        ["300", "300", "300", "1200"],
        ["375", "375", "375", "1500"],
    ]))
    regular = sorted(history, key=lambda p: p.on_date)
    assert not is_steady(regular)
    out = project_dividends(history, Decimal("1"),
                            date(2026, 1, 1), date(2026, 12, 31), as_of=date(2026, 1, 1))
    assert [fp.net_eur for fp in out] == [
        Decimal("375"), Decimal("375"), Decimal("375"), Decimal("1500"),
    ]


def test_a_special_is_never_repeated_and_its_slot_falls_back_to_the_regular_level():
    """
    Samsung's early-2021 shape: one quarter many times the rest, never again — judged
    against the same quarter a year earlier, which was ordinary.
    """
    history = _series(_quarters([2024, 2025, 2026], [
        ["354", "354", "354", "354"],
        ["361", "361", "361", "1932"],
        ["361", "361", "361"],
    ]))
    assert [p.per_share_eur for p in special_payments(history, 91)] == [Decimal("1932")]
    out = project_dividends(history, Decimal("1"),
                            date(2026, 10, 1), date(2027, 9, 30), as_of=date(2026, 10, 1))
    assert {fp.net_eur for fp in out} == {Decimal("361")}


def test_ibkr_labelling_a_payment_special_is_final():
    """Even a modest special is excluded when IBKR's cash line says so."""
    history = _series(_quarters([2025, 2026], [
        ["1.00", "1.00", "1.00", "1.00"],
        ["1.00", "1.00"],
    ])) + [HistPayment(on_date=date(2026, 6, 20), per_share_eur=Decimal("1.50"),
                       special=True)]
    out = project_dividends(history, Decimal("1"),
                            date(2026, 7, 1), date(2027, 6, 30), as_of=date(2026, 7, 1))
    assert {fp.net_eur for fp in out} == {Decimal("1.00")}


def test_announced_evidence_sizes_without_bending_the_schedule():
    """
    An accrual (cadence=False) a few days after the last Yahoo ex-date is the newest
    amount, but its date must not halve the inferred cycle.
    """
    history = _series(_quarters([2025, 2026], [
        ["4.50", "4.50", "4.50", "4.50"],
        ["5.00", "5.00"],
    ])) + [HistPayment(on_date=date(2026, 9, 16), per_share_eur=Decimal("7.00"),
                       cadence=False)]
    out = project_dividends(history, Decimal("1"),
                            date(2026, 10, 1), date(2027, 9, 30), as_of=date(2026, 10, 1))
    assert [fp.on_date for fp in out] == [
        date(2026, 12, 15), date(2027, 3, 15), date(2027, 6, 15), date(2027, 9, 15),
    ]
    assert {fp.net_eur for fp in out} == {Decimal("7.00")}


def test_a_first_large_december_with_no_year_before_it_is_not_called_special():
    """
    Without the same payment a year apart there is no telling a special from a
    seasonal fund's first December — and calling it special reads low on every fund
    whose history starts in a winter. It stays regular.
    """
    history = _series([(date(2025, 12, 15), "0.90"), (date(2026, 3, 15), "0.40"),
                       (date(2026, 6, 15), "0.70")])
    assert special_payments(history, 91) == []


def test_under_a_year_of_history_uses_the_latest_payment():
    history = _series([(date(2026, 3, 15), "0.40"), (date(2026, 6, 15), "0.90")])
    out = project_dividends(history, Decimal("1"),
                            date(2026, 7, 1), date(2026, 12, 31), as_of=date(2026, 7, 1))
    assert {fp.net_eur for fp in out} == {Decimal("0.90")}
    assert {fp.method for fp in out} == {METHOD_LATEST}


def test_an_unknown_sizing_is_refused_loudly():
    import pytest
    with pytest.raises(ValueError):
        project_dividends(SEASONAL, Decimal("1"), date(2026, 1, 1), date(2026, 12, 31),
                          sizing="guess")
