"""
The back-off for an upstream that keeps answering "nothing" about one security, written once.

The price loop's rule (GitHub issue #10), extracted on 2026-10-18 when the fundamentals pass
became its second user (issue #66): after a miss — an answer with nothing usable in it, or a
failure that is not a rate limit — the security is not asked again for one day, doubling per
consecutive miss, capped at `MISS_BACKOFF_CAP_DAYS`. Less `MISS_BACKOFF_SLACK`, so a security
stamped during a pass is due again at the same slot a day later, not the one after (the
fundamentals' 20-hour `STALE_AFTER`, for the same reason).

Each caller keeps its own pair of columns (`<x>_checked_at`, `<x>_miss_count`), stamps them
only on an answer — never on a rate limit, which says nothing about the security — and clears
them on any row written and on a mapping change. What a caller exempts is its own: the price
loop never backs off a held security. `tests/test_miss_backoff_family.py` fails a second
doubling anywhere under `app/`, and a `*_miss_count` column no caller hands to this function.
"""
from datetime import datetime, timedelta
from typing import Optional

MISS_BACKOFF_CAP_DAYS = 30
MISS_BACKOFF_SLACK = timedelta(hours=4)


def miss_backoff_until(
    checked_at: Optional[datetime], misses: Optional[int]
) -> Optional[datetime]:
    """When the security may next be asked, or None when it is not backing off."""
    misses = misses or 0
    if checked_at is None or misses < 1:
        return None
    days = min(2 ** min(misses - 1, 10), MISS_BACKOFF_CAP_DAYS)
    return checked_at + timedelta(days=days) - MISS_BACKOFF_SLACK
