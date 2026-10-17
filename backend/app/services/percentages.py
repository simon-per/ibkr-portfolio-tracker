"""
A part as a percentage of a whole — the one rule for every "share of" figure.

`lookthrough_service` and `performance_analytics_service` each had a `_pct(part, whole)`,
and they disagreed about a whole of zero: look-through said 0.0, analytics said None. A
share of nothing is not 0% — that reads as "none of it", which is a claim — so the rule
is None (an unknown is absent, never a stand-in). It rounds to two decimals in Decimal
before the float, so a half-cent cannot round one way on one endpoint and the other way
on the next. `tests/test_percentages_family.py` fails a module that brings a copy back.
"""
from decimal import Decimal
from typing import Optional

HUNDRED = Decimal("100")


def share_pct(part: Decimal, whole: Decimal) -> Optional[float]:
    """`part` as a percentage of `whole`, to two decimals; None when `whole` is zero."""
    if not whole:
        return None
    return float(round(part / whole * HUNDRED, 2))
