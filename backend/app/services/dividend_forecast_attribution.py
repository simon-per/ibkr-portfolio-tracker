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

**The payout change is served in two parts, `raises` and `timing`.** `n` is whatever
falls inside the 365-day window, so a payment entering or leaving it, or one dropping
out of the forecast while it is overdue, moves `n` without any company having changed
its dividend: measured alone, the payout change read negative on a book whose payers
had all raised. So each line also carries `L`, the payer's *level*
(`dividend_forecast.payout_level`: a year of its own schedule at its current size,
anchored on its newest payment rather than on today, gross of withholding), and

    raises   q1 x n0 x (L1/L0 - 1) x g0        the old window, at the new level
    timing   payout - raises                   everything else that moved n

A holding without a level on either day has no measure of its raises: its whole payout
change stays in `timing`, never guessed. The five parts still sum to the change.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Mapping, Optional

PARTS = ("shares", "raises", "timing", "newly_forecast", "fx")

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
    # `payout_level` per share, gross, in EUR at `fx_to_eur`; None when unknown.
    level_eur: Optional[Decimal] = None


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
        # payout, so its moves stay in the payout parts and only EUR→base counts as
        # currency.
        if a.fx_to_eur and b.fx_to_eur:
            f0, f1 = a.fx_to_eur, b.fx_to_eur
        else:
            f0 = f1 = Decimal("1")
        native0, native1 = a.next_12m_eur / f0, b.next_12m_eur / f1
        # The level is per share, so a split rescales it exactly as it rescales `n`.
        level_scale = Decimal("1")
        if _is_split(q0, q1, native0, native1):
            level_scale = q0 / q1
            q0 = q1
        n0, n1 = native0 / q0, native1 / q1
        g0 = f0 * base_rate_prev

        shares = (q1 - q0) * n0 * g0
        payout = q1 * (n1 - n0) * g0
        raises = Decimal("0")
        if a.level_eur and b.level_eur and a.level_eur > 0 and b.level_eur > 0:
            level0 = a.level_eur / f0 * level_scale
            level1 = b.level_eur / f1
            raises = q1 * n0 * (level1 / level0 - 1) * g0
        out["shares"] += shares
        out["raises"] += raises
        out["timing"] += payout - raises
        out["fx"] += (value1 - value0) - shares - payout
    return out
