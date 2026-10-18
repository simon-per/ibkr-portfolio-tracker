"""
One definition of "Yahoo is rate-limiting us", shared by every service that loops.

Rule 1 in CLAUDE.md is that a rate limit means **stop immediately**, because
continuing is what turns a short block into a long one. `MarketDataService` learned
that on 2026-08-04: its detection aborted the ticker *variations* for the security in
hand while the caller logged the failure and moved on to the next of forty, asking the
same IP again seconds later.

Three other services still had the older shape — `FundamentalsService`,
`AnalystRatingService` and `WatchlistService` all caught, logged and continued — and
fundamentals is the most expensive of the four at roughly five Yahoo calls per
security. So the predicate lives here rather than being copied a fourth time, which is
what this codebase's dominant failure mode would predict happening to it.

**The marker list is deliberately narrower than CLAUDE.md's symptom list.** That
document also names "HTTP 404 with `Expecting value: line 1 column 1 (char 0)`" and
empty JSON as things a rate limit can look like, and those are **not** matched here on
purpose: they are equally what a delisted or mistyped ticker looks like, and
`_try_fetch_yahoo` uses this same verdict to decide whether to go on trying ticker
*variations*. Treating a JSON-decode failure as a rate limit would abort auto-discovery
for every security Yahoo simply does not know, which is a real cost paid on good data.
The markers below are unambiguous: nothing but a rate limit says "429".

The asymmetry that decides the default: a false positive costs one pass of freshness
and the next slot recovers it, because the dates never reached are simply still
missing. A false negative spends the rest of the pass hammering a limited IP.

**The hold (GitHub issue #57).** Abandoning the pass was per *pass*: each latch lived on
one service instance and died with it. After a 429 the next public
`GET /api/portfolio/benchmark` built a new `BenchmarkService` and asked again at once,
the next chart load for another benchmark did the same, and a `POST .../sync` started a
fresh pass — eight paths, each of which had been told and forgot. So a refusal is now
remembered process-wide: `is_rate_limit` stamps "Yahoo refused at T" whenever it says
yes, and every module that imports yfinance calls `check_yahoo_hold()` before it asks,
which raises `YahooHold` for `YAHOO_HOLD_SECONDS` after the stamp. `YahooHold`'s text
satisfies `is_rate_limit`, so each loop's existing breaker treats it exactly like the
429 it stands for — the pass stops and reports `rate_limited` with its usual warning —
and it does not re-stamp, so a hold ends on time rather than renewing itself on every
refusal. `tests/test_yahoo_rate_limit_family.py` fails a yfinance module that never
consults it, and drives every service's pass under a hold.

The verdict and the record are one call on purpose: a caller cannot learn that Yahoo
refused without the next caller learning it too.

The stamp is cached here and written through to `app_settings` (`yahoo_refused_at`), so
a restart — a deploy — does not forget it; it is read from there once per process, on
the first consult. The write runs as its own task on its own session rather than inside
the caller's: the caller may hold uncommitted writes (SQLite's single write lock), and
awaiting a second writer from inside that transaction waits out `busy_timeout` and then
fails. A process that does not share this memory (the `manage_mappings` CLI) reads the
stamp at its first consult; the server does not see a stamp the CLI wrote until it
restarts, which is the CLI's one request at most.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Set

from app.clock import utcnow

logger = logging.getLogger(__name__)

RATE_LIMIT_MARKERS = ("429", "too many requests", "rate limit", "blocked")

# How long nothing in this process asks Yahoo after it refused us. Rule 1's recovery is
# "wait 30-60 minutes"; 45 sits inside it on both counts that matter. Well past the
# minimum, because Yahoo's limit is IP-based and counted over roughly an hour, and the
# first request after a too-short hold is the one that extends the block. Short of the
# hour, because the Yahoo slots are two or more hours apart except the 04:00 watchlist
# and 05:00 fundamentals refreshes (`scheduler_service`): a limit in the watchlist's
# first quarter-hour still lets the fundamentals pass run that morning rather than
# costing it a day. Everything else it delays is a chart load or a manual sync, which
# serves the cache meanwhile.
YAHOO_HOLD_SECONDS = 45 * 60

YAHOO_REFUSED_AT_KEY = "yahoo_refused_at"

# In `YahooHold`'s message: what tells our own refusal apart from Yahoo's, so it never
# re-stamps. It contains "rate limit", which is what makes every breaker treat it as one.
HOLD_MARKER = "yahoo rate limit hold"


class YahooHold(Exception):
    """Raised by `check_yahoo_hold` while a Yahoo refusal is still being honoured."""

    def __init__(self, refused_at: datetime, until: datetime):
        self.refused_at = refused_at
        self.until = until
        super().__init__(
            f"Yahoo rate limit hold: Yahoo refused us at {refused_at:%Y-%m-%d %H:%M} UTC; "
            f"not asking again before {until:%H:%M} UTC"
        )


_refused_at: Optional[datetime] = None
# The persisted stamp has been read into `_refused_at`.
_loaded = False
# Stamped where no event loop ran (a worker thread); the next consult writes it.
_dirty = False
_pending_writes: Set[asyncio.Task] = set()


def _session_factory():
    """Where the stamp persists. A function so the tests can point it elsewhere or off."""
    from app.database import AsyncSessionLocal

    return AsyncSessionLocal


def is_rate_limit(error: object) -> bool:
    """
    True when an exception (or message) is Yahoo refusing us for volume — and, when it
    is, the refusal is recorded for the whole process (see the module docstring).

    Takes ``object`` rather than ``BaseException`` because two callers hold only the
    stringified error: ``WatchlistService.sync_all`` reads the ``{"error": str(e)}``
    that ``sync_item`` returns, and the market-data path lowercases the message before
    testing. Both must reach the same verdict as a caller holding the exception.
    """
    if error is None:
        return False
    text = str(error).lower()
    if not any(marker in text for marker in RATE_LIMIT_MARKERS):
        return False
    if HOLD_MARKER not in text:
        _record_refusal()
    return True


def _record_refusal() -> None:
    global _refused_at
    _refused_at = utcnow()
    logger.warning(
        f"Yahoo refused a request; no Yahoo request until "
        f"{_refused_at + timedelta(seconds=YAHOO_HOLD_SECONDS):%H:%M} UTC"
    )
    _schedule_write()


def _schedule_write() -> None:
    global _dirty
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # A worker thread (`yahoo_estimates.read_optional` runs inside one): no loop to
        # write from here. The memory stamp already holds; the next consult writes it.
        _dirty = True
        return
    _dirty = False
    task = loop.create_task(_write(_refused_at))
    _pending_writes.add(task)
    task.add_done_callback(_pending_writes.discard)


async def _write(stamp: Optional[datetime]) -> None:
    factory = _session_factory()
    if factory is None or stamp is None:
        return
    from app.repositories.app_settings_repository import AppSettingsRepository

    try:
        async with factory() as session:
            await AppSettingsRepository(session).set(YAHOO_REFUSED_AT_KEY, stamp.isoformat())
            await session.commit()
    except Exception as e:
        # The memory stamp still holds this process; only a restart would forget it.
        logger.warning(f"Could not persist the Yahoo rate-limit hold: {e}")


async def _load() -> None:
    """Read the persisted stamp once per process; a later or failed read changes nothing."""
    global _loaded, _refused_at
    if _loaded:
        return
    _loaded = True
    factory = _session_factory()
    if factory is None:
        return
    from app.repositories.app_settings_repository import AppSettingsRepository

    try:
        async with factory() as session:
            raw = await AppSettingsRepository(session).get(YAHOO_REFUSED_AT_KEY)
    except Exception as e:
        logger.warning(f"Could not read the Yahoo rate-limit hold: {e}")
        return
    if not raw:
        return
    try:
        # Never later than now: a stamp from a skewed clock or a restored copy must not
        # hold for longer than one hold.
        stored = min(datetime.fromisoformat(raw), utcnow())
    except ValueError:
        logger.warning(f"Ignoring an unreadable {YAHOO_REFUSED_AT_KEY} value {raw!r}")
        return
    if _refused_at is None or stored > _refused_at:
        _refused_at = stored


def yahoo_hold_until(now: Optional[datetime] = None) -> Optional[datetime]:
    """When the current hold ends (naive UTC), or None. Memory only — see `check_yahoo_hold`."""
    if _refused_at is None:
        return None
    until = _refused_at + timedelta(seconds=YAHOO_HOLD_SECONDS)
    return until if (now or utcnow()) < until else None


async def check_yahoo_hold() -> None:
    """
    Raise `YahooHold` while a Yahoo refusal is being honoured; otherwise return.

    Call it immediately before a Yahoo request, inside the `try` whose handler already
    asks `is_rate_limit`: that handler then abandons the pass and reports it as rate
    limited, with no second code path for "held".
    """
    await _load()
    if _dirty:
        _schedule_write()
    until = yahoo_hold_until()
    if until is not None:
        raise YahooHold(_refused_at, until)


async def flush_hold_writes() -> None:
    """Wait for pending stamp writes. For a process about to exit (the CLIs) and tests."""
    if _pending_writes:
        await asyncio.gather(*list(_pending_writes), return_exceptions=True)


def reset_yahoo_hold(*, reload: bool = False) -> None:
    """
    Forget the in-memory stamp. For tests: process-lifetime state, like
    `benchmark_service.reset_upstream_throttle`. ``reload=True`` simulates a restart —
    the next consult reads the persisted stamp again.
    """
    global _refused_at, _loaded, _dirty
    _refused_at = None
    _loaded = not reload
    _dirty = False
