"""
A part as a percentage of a whole — the one rule for every "share of" figure.

`lookthrough_service` and `performance_analytics_service` each had a `_pct(part, whole)`,
and they disagreed about a whole of zero: look-through said 0.0, analytics said None. A
share of nothing is not 0% — that reads as "none of it", which is a claim — so the rule
is None (an unknown is absent, never a stand-in). It rounds to two decimals in Decimal
before the float, so a half-cent cannot round one way on one endpoint and the other way
on the next. `tests/test_percentages_family.py` fails a module that brings a copy back.

Consumers: look-through coverage and weights, the analytics gain percentages, and the
valuation paths in `portfolio_service` (timeline, summary and position gain %, the
attribution's weight and contribution shares) and `benchmark_service` (the benchmark
point's gain %). Those last two carried inline `x / whole * 100 if whole > 0 else 0`
copies that published 0% for a share of nothing (GitHub issue #29); the family test now
fails the inline form too.

Then the dividend breakdown (each row's trailing yield, forward yield, yield on cost and
`share_pct`, the forward-yield card's `pct` and `on_cost_pct`, the book growth's
`covered_pct`) and the crypto view (`weight_pct`, the total's `change_today_pct`). Those
rounded a float after the division, so a tie such as 1.015 published 1.01 there and 1.02
here (GitHub issue #71). Two of them publish one decimal, hence `digits`; the crypto
figures are floats, hence the float entry. The family test now fails any
`<a> / <b> * 100` outside this module that its allow-list does not name with a reason.
"""
import math
from decimal import Decimal
from typing import Optional, Union

HUNDRED = Decimal("100")

Number = Union[Decimal, float, int]


def _as_decimal(value: Number) -> Optional[Decimal]:
    """
    A float enters by its shortest repr, so 1.015 rounds as the 1.015 it prints as rather
    than as its binary neighbour 1.01499…; None for anything not finite.
    """
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    number = Decimal(value)
    return number if number.is_finite() else None


def share_pct(part: Number, whole: Number, digits: int = 2) -> Optional[float]:
    """`part` as a percentage of `whole`, to `digits` decimals; None when `whole` is zero."""
    if not whole:
        return None
    part_d, whole_d = _as_decimal(part), _as_decimal(whole)
    if part_d is None or whole_d is None:
        return None
    return float(round(part_d / whole_d * HUNDRED, digits))
