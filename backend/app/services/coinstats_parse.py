"""
The pure readers of CoinStats payloads — one copy, shared by the sync
(`crypto_service`), the rebuild CLI and the probe (issue #34).

The probe exists to decide how the sync should read a payload, so it must read the payload
exactly as the sync does: three copies of these helpers had drifted apart (the probe took
`True` for 1.0 and for 1970-01-01, refused numeric strings, and rendered lists without
their item shape). `tests/test_coinstats_parse_family.py` fails on any crypto module that
defines one of these again.

No I/O, no database, no imports beyond the standard library.
"""
import math
from datetime import datetime, timezone
from typing import Any, Optional

# An epoch number above this is milliseconds (10_000_000_000 seconds is the year 2286).
_EPOCH_MS_THRESHOLD = 10_000_000_000


def num(value: Any) -> Optional[float]:
    """A finite float, or None. Booleans are not numbers; numeric strings are."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def usd(block: Any) -> Optional[float]:
    """The USD figure of a CoinStats money block (`{USD, BTC, ETH}`), or a bare number."""
    if isinstance(block, dict):
        return num(block.get("USD"))
    return num(block)


def usd_period(block: Any, period: str) -> Optional[float]:
    """`profit`, `averageBuy` and `profitPercent` are keyed by period (`allTime`,
    `hour24`, `unrealized`, …), each holding a money block."""
    if isinstance(block, dict):
        return usd(block.get(period))
    return None


def to_utc_datetime(stamp: Any) -> Optional[datetime]:
    """Epoch seconds, epoch milliseconds or ISO → naive UTC; `.date()` for the day.
    A bool, an out-of-range number or an unparseable string is None, never a raise."""
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        seconds = stamp / 1000 if stamp > _EPOCH_MS_THRESHOLD else stamp
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def shape(value: Any, depth: int = 0) -> str:
    """Keys and types, recursively for dicts and a list's first item; never a value."""
    if isinstance(value, dict):
        if depth >= 2:
            return "{...}"
        return "{" + ", ".join(
            f"{k}: {shape(v, depth + 1)}" for k, v in sorted(value.items())
        ) + "}"
    if isinstance(value, list):
        return f"list[{len(value)}]" + (
            f" of {shape(value[0], depth + 1)}" if value and depth < 2 else ""
        )
    return type(value).__name__
