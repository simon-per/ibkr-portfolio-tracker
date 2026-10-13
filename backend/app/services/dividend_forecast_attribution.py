"""
Why the *Next 12 months* figure moved between two days.

The forecast of one holding is `shares x net per share x exchange rate`, and the three
move for unrelated reasons: the owner buys, a payer raises, a currency drifts. On a book
that is still being funded nearly all of the growth is the first, which is worth
knowing before reading the total as "my dividends grew".

Pure arithmetic over two days of `dividend_forecast_snapshot_lines`; no database, no
forecast rules. Per holding, with `q` shares, `n` the next twelve months per share in
the security's own currency and `g` that currency in the base currency:

    shares   (q1 - q0) x n0 x g0      the shares bought or sold, at the old payout
    payout   q1 x (n1 - n0) x g0      the payout change, on the shares now held
    fx       q1 x n1 x (g1 - g0)      the rate change, on today's forecast

which sum to `q1 n1 g1 - q0 n0 g0` exactly — the order is a choice, the total is not.
A position opened or closed is `shares` whole. A holding kept through both days whose
forecast appears or disappears is neither a purchase nor a raise, so it has its own
part, `newly_forecast`.

**`payout` is per share AND timing.** `n` is whatever falls inside the 365-day window,
so a payment entering or leaving it, or one dropping out of the forecast while it is
overdue, moves `n` without any company having changed its dividend. The per-payer
growth figure on the breakdown (`per_share_growth_pct`) is the measure of raises; this
one answers "what was not shares and not currency".
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Mapping, Optional

PARTS = ("shares", "payout", "newly_forecast", "fx")

# A share count that moves by this factor while the holding's forecast in its own
# currency stays within SPLIT_VALUE_TOLERANCE is a split, not a trade: the same money
# over a different number of shares. Read as a trade it would report a large purchase
# and an equally large payout cut that cancel in the total and mislead in the parts.
SPLIT_MIN_RATIO = Decimal("1.5")
SPLIT_VALUE_TOLERANCE = Decimal("0.05")


@dataclass(frozen=True)
class Holding:
    """One security on one day: a `dividend_forecast_snapshot_lines` row."""
    shares: Decimal
    next_12m_eur: Decimal
    fx_to_eur: Optional[Decimal] = None


def _is_split(q0: Decimal, q1: Decimal, native0: Decimal, native1: Decimal) -> bool:
    ratio = q1 / q0
    if Decimal(1) / SPLIT_MIN_RATIO < ratio < SPLIT_MIN_RATIO:
        return False
    return abs(native1 / native0 - 1) <= SPLIT_VALUE_TOLERANCE


def attribute_change(
    prev: Mapping[int, Holding],
    cur: Mapping[int, Holding],
    base_rate_prev: Decimal,
    base_rate_cur: Decimal,
) -> Dict[str, Decimal]:
    """
    The change in the forecast from ``prev`` to ``cur``, in the base currency, split
    into `PARTS`. ``base_rate_*`` is EUR→base on each day (1 for a EUR base).

    The parts sum to ``sum(cur) x base_rate_cur - sum(prev) x base_rate_prev``.
    """
    out = {part: Decimal("0") for part in PARTS}
    for sid in prev.keys() | cur.keys():
        a, b = prev.get(sid), cur.get(sid)
        q0 = a.shares if a else Decimal("0")
        q1 = b.shares if b else Decimal("0")
        value0 = a.next_12m_eur * base_rate_prev if a else Decimal("0")
        value1 = b.next_12m_eur * base_rate_cur if b else Decimal("0")

        if q0 <= 0 or q1 <= 0:
            out["shares"] += value1 - value0     # opened or closed
            continue
        if a.next_12m_eur <= 0 or b.next_12m_eur <= 0:
            out["newly_forecast"] += value1 - value0
            continue

        # Without both rates the security's own currency cannot be told from its
        # payout, so its moves stay in `payout` and only EUR→base counts as currency.
        if a.fx_to_eur and b.fx_to_eur:
            f0, f1 = a.fx_to_eur, b.fx_to_eur
        else:
            f0 = f1 = Decimal("1")
        native0, native1 = a.next_12m_eur / f0, b.next_12m_eur / f1
        if _is_split(q0, q1, native0, native1):
            q0 = q1
        n0, n1 = native0 / q0, native1 / q1
        g0 = f0 * base_rate_prev

        shares = (q1 - q0) * n0 * g0
        payout = q1 * (n1 - n0) * g0
        out["shares"] += shares
        out["payout"] += payout
        out["fx"] += (value1 - value0) - shares - payout
    return out
