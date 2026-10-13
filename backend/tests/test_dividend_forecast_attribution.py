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


def H(shares, next_12m, fx="1"):
    return Holding(Decimal(shares), Decimal(next_12m),
                   Decimal(fx) if fx is not None else None)


def _total(holdings, rate):
    return sum((h.next_12m_eur for h in holdings.values()), Decimal("0")) * rate


@pytest.mark.parametrize("prev, cur, r0, r1", [
    ({1: H("100", "400")}, {1: H("150", "630")}, "1", "1"),
    ({1: H("10", "90", "0.9")}, {1: H("12", "130", "0.8")}, "0.95", "0.91"),
    ({1: H("10", "90"), 2: H("3", "0")}, {2: H("3", "12"), 3: H("8", "40", "0.5")},
     "0.9", "0.8"),
    ({1: H("10", "90", None)}, {1: H("20", "150", "0.8")}, "1", "1.1"),
    ({1: H("10", "40", "0.9")}, {1: H("100", "41", "0.9")}, "1", "1"),
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
        "payout": Decimal("30"),         # 150 x 0.20
        "newly_forecast": Decimal("0"),
        "fx": Decimal("0"),
    }


def test_a_position_opened_or_closed_is_shares_whole():
    parts = attribute_change({1: H("10", "90")}, {2: H("8", "40")}, ONE, ONE)
    assert parts["shares"] == Decimal("-50")
    assert parts["payout"] == parts["newly_forecast"] == parts["fx"] == 0


def test_a_first_forecast_on_shares_already_held_is_not_a_purchase():
    """The second payout lands and a cadence exists: nothing was bought or raised."""
    parts = attribute_change({1: H("5", "0")}, {1: H("5", "50")}, ONE, ONE)
    assert parts["newly_forecast"] == Decimal("50")
    assert parts["shares"] == parts["payout"] == 0


def test_the_securitys_own_currency_is_currency_not_payout():
    """A level USD dividend while the dollar falls: no payer changed anything."""
    parts = attribute_change(
        {1: H("10", "90", "0.9")}, {1: H("10", "80", "0.8")}, ONE, ONE
    )
    assert parts["fx"] == Decimal("-10")
    assert parts["payout"] == parts["shares"] == 0


def test_without_both_rates_the_move_stays_in_payout():
    """Nothing separates a currency from a payout without the rate on both days."""
    parts = attribute_change(
        {1: H("10", "90", None)}, {1: H("10", "80", "0.8")}, ONE, ONE
    )
    assert parts["payout"] == Decimal("-10")
    assert parts["fx"] == 0


def test_a_split_is_neither_a_purchase_nor_a_cut():
    """Ten times the shares for the same money, and a 2.5% raise beside it."""
    parts = attribute_change(
        {1: H("10", "40", "0.9")}, {1: H("100", "41", "0.9")}, ONE, ONE
    )
    assert parts["shares"] == 0
    assert round(parts["payout"], 6) == Decimal("1")    # 40/0.9 does not terminate


def test_a_position_doubled_is_not_mistaken_for_a_split():
    parts = attribute_change({1: H("10", "40")}, {1: H("20", "80")}, ONE, ONE)
    assert parts["shares"] == Decimal("40")
    assert parts["payout"] == 0
