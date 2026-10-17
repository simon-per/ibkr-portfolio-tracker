"""
`attribute_change` — why the forecast moved between two days
(app/services/dividend_forecast_attribution.py).

The one property everything else rests on is that the parts sum to the change. The
rest are the cases where a plausible-looking split would be the wrong one: a position
opened is not a raise, a first forecast on shares already held is not a purchase, and
a stock split is neither.
"""
from decimal import Decimal

import pytest

from app.services.dividend_forecast_attribution import PARTS, Holding, attribute_change

ONE = Decimal("1")


def H(shares, next_12m, fx="1", level=None):
    return Holding(Decimal(shares), Decimal(next_12m),
                   Decimal(fx) if fx is not None else None,
                   Decimal(level) if level is not None else None)


def _total(holdings, rate):
    return sum((h.next_12m_eur for h in holdings.values()), Decimal("0")) * rate


@pytest.mark.parametrize("prev, cur, r0, r1", [
    ({1: H("100", "400")}, {1: H("150", "630")}, "1", "1"),
    ({1: H("10", "90", "0.9")}, {1: H("12", "130", "0.8")}, "0.95", "0.91"),
    ({1: H("10", "90"), 2: H("3", "0")}, {2: H("3", "12"), 3: H("8", "40", "0.5")},
     "0.9", "0.8"),
    ({1: H("10", "90", None)}, {1: H("20", "150", "0.8")}, "1", "1.1"),
    ({1: H("10", "40", "0.9")}, {1: H("100", "41", "0.9")}, "1", "1"),
    ({1: H("100", "400", "1", "4")}, {1: H("120", "315", "1", "4.2")}, "1", "1"),
    ({1: H("10", "90", "0.9", "9.5")}, {1: H("12", "130", "0.8", "11")}, "0.95", "0.91"),
    ({1: H("10", "40", "0.9", "4")}, {1: H("100", "41", "0.9", "0.41")}, "1", "1.2"),
])
def test_the_parts_always_sum_to_the_change(prev, cur, r0, r1):
    r0, r1 = Decimal(r0), Decimal(r1)
    parts = attribute_change(prev, cur, r0, r1)
    assert set(parts) == set(PARTS)
    assert sum(parts.values()) == _total(cur, r1) - _total(prev, r0)


def test_bought_shares_are_valued_at_the_payout_before_the_raise():
    """50 more shares and a raise from 4.00 to 4.20 on the same day."""
    parts = attribute_change({1: H("100", "400")}, {1: H("150", "630")}, ONE, ONE)
    assert parts == {
        "shares": Decimal("200"),        # 50 x 4.00
        "raises": Decimal("0"),          # no level: nothing measures a raise
        "timing": Decimal("30"),         # 150 x 0.20
        "newly_forecast": Decimal("0"),
        "fx": Decimal("0"),
    }


def test_a_position_opened_or_closed_is_shares_whole():
    parts = attribute_change({1: H("10", "90")}, {2: H("8", "40")}, ONE, ONE)
    assert parts["shares"] == Decimal("-50")
    assert parts["timing"] == parts["newly_forecast"] == parts["fx"] == 0


def test_a_first_forecast_on_shares_already_held_is_not_a_purchase():
    """The second payout lands and a cadence exists: nothing was bought or raised."""
    parts = attribute_change({1: H("5", "0")}, {1: H("5", "50")}, ONE, ONE)
    assert parts["newly_forecast"] == Decimal("50")
    assert parts["shares"] == parts["timing"] == 0


def test_the_securitys_own_currency_is_currency_not_payout():
    """A level USD dividend while the dollar falls: no payer changed anything."""
    parts = attribute_change(
        {1: H("10", "90", "0.9")}, {1: H("10", "80", "0.8")}, ONE, ONE
    )
    assert parts["fx"] == Decimal("-10")
    assert parts["timing"] == parts["shares"] == 0


def test_without_both_rates_the_move_stays_in_payout():
    """Nothing separates a currency from a payout without the rate on both days."""
    parts = attribute_change(
        {1: H("10", "90", None)}, {1: H("10", "80", "0.8")}, ONE, ONE
    )
    assert parts["timing"] == Decimal("-10")
    assert parts["fx"] == 0


def test_a_split_is_neither_a_purchase_nor_a_cut():
    """Ten times the shares for the same money, and a 2.5% raise beside it."""
    parts = attribute_change(
        {1: H("10", "40", "0.9")}, {1: H("100", "41", "0.9")}, ONE, ONE
    )
    assert parts["shares"] == 0
    assert round(parts["timing"], 6) == Decimal("1")    # 40/0.9 does not terminate


def test_a_raise_is_raises_not_timing():
    """A quarterly payer lifts 1.00 to 1.05 a quarter: its level goes 4.00 -> 4.20."""
    parts = attribute_change(
        {1: H("100", "400", "1", "4")}, {1: H("100", "420", "1", "4.2")}, ONE, ONE
    )
    assert parts["raises"] == Decimal("20")
    assert parts["timing"] == parts["shares"] == parts["fx"] == 0


def test_a_payment_leaving_the_window_is_timing_not_a_cut():
    """Four payments in the window become three; the payer's level did not move."""
    parts = attribute_change(
        {1: H("100", "400", "1", "4")}, {1: H("100", "300", "1", "4")}, ONE, ONE
    )
    assert parts["timing"] == Decimal("-100")
    assert parts["raises"] == 0


def test_a_raise_and_a_payment_leaving_on_one_day_are_told_apart():
    """The raise is the old window at the new level; the payment that left is timing,
    at the level it would have been paid at."""
    parts = attribute_change(
        {1: H("100", "400", "1", "4")}, {1: H("100", "315", "1", "4.2")}, ONE, ONE
    )
    assert parts["raises"] == Decimal("20")         # 100 x 4.00 x 5%
    assert parts["timing"] == Decimal("-105")       # one 1.05 payment on 100 shares


def test_without_a_level_on_both_days_nothing_is_called_a_raise():
    """Lines stored before the level existed: the payout change stays in timing."""
    parts = attribute_change(
        {1: H("100", "400", "1", None)}, {1: H("100", "420", "1", "4.2")}, ONE, ONE
    )
    assert parts["raises"] == 0
    assert parts["timing"] == Decimal("20")


def test_a_level_in_another_currency_is_measured_in_its_own():
    """A level USD payer while the dollar falls raised nothing."""
    parts = attribute_change(
        {1: H("10", "90", "0.9", "9")}, {1: H("10", "80", "0.8", "8")}, ONE, ONE
    )
    assert parts["raises"] == parts["timing"] == 0
    assert parts["fx"] == Decimal("-10")


def test_a_split_does_not_read_as_a_cut_in_the_level():
    """Ten times the shares: the level per share falls tenfold, and a 2.5% raise
    beside it is the only raise."""
    parts = attribute_change(
        {1: H("10", "40", "0.9", "4")}, {1: H("100", "41", "0.9", "0.41")}, ONE, ONE
    )
    assert parts["shares"] == 0
    assert round(parts["raises"], 6) == Decimal("1")
    assert round(parts["timing"], 6) == 0


def test_a_position_doubled_is_not_mistaken_for_a_split():
    parts = attribute_change({1: H("10", "40")}, {1: H("20", "80")}, ONE, ONE)
    assert parts["shares"] == Decimal("40")
    assert parts["timing"] == 0
