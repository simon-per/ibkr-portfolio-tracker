"""
One-at-a-time gate with a start cooldown for the sync pipelines.

`/api/` is proxied publicly with no auth, and several POST routes start real work
against rate-limited upstreams: the IBKR Flex token (10 requests/minute, and
repeated failed generations trip the undocumented Code=1025 lockout that blocks
all syncing for hours) and Yahoo Finance (IP-based bans). Nothing prevented
concurrent or rapid-fire triggers — APScheduler's max_instances=1 only fences
jobs it dispatches itself, and POST /api/scheduler/trigger runs the job as a
bare coroutine outside the job store entirely.

Everything that can reach IBKR or Yahoo shares ONE gate name (SYNC_PIPELINE):
two pipelines racing each other is exactly the failure mode, regardless of
which endpoint started them. Scheduled jobs enter with no cooldown (their slots
are hours apart; a colliding job waits a bounded time for the gate, then records
itself as skipped and the next slot recovers); the public endpoints add per-route cooldowns so rapid-fire POSTs
get a 429 instead of a queue of pipelines.

In-process by design: the deployment is a single uvicorn worker (the dividends
router's `_sync_in_progress` flag set the precedent). The check-and-set below
has no await between test and set, so it is atomic on the event loop.
"""
import time
from contextlib import contextmanager
from typing import Dict, Set

SYNC_PIPELINE = "sync-pipeline"


class SyncBusy(Exception):
    """Another run of this pipeline is in flight, or its cooldown hasn't elapsed."""

    def __init__(self, message: str, retry_after_seconds: int = 120):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


_running: Set[str] = set()
_last_start: Dict[str, float] = {}


def is_running(name: str) -> bool:
    """For BackgroundTasks-based routes: the handler returns before the work
    starts, so it checks here and the background function holds the gate."""
    return name in _running


def cooldown_remaining(name: str, cooldown_seconds: int) -> int:
    """
    Seconds until ``name`` may be entered again, or 0 if it may be entered now.

    Tests the cooldown without acquiring. A BackgroundTasks route answers before
    its work starts, so the gate it holds is in the background function — the
    handler needs to know it would be refused in order to say so, rather than
    replying "started" to a run the background half then drops on the floor.
    """
    if not cooldown_seconds:
        return 0
    last = _last_start.get(name)
    if last is None:
        return 0
    elapsed = time.monotonic() - last
    if elapsed >= cooldown_seconds:
        return 0
    return int(cooldown_seconds - elapsed) + 1


@contextmanager
def single_flight(name: str, cooldown_seconds: int = 0):
    """
    Hold the named gate for the duration of the block.

    Raises SyncBusy when the gate is already held, or when it was last entered
    less than ``cooldown_seconds`` ago — the cooldown counts from the START and
    applies to failed runs too, since a failed Flex generation is precisely what
    Code=1025 counts.
    """
    if name in _running:
        raise SyncBusy(f"'{name}' is already running")
    now = time.monotonic()
    last = _last_start.get(name)
    if cooldown_seconds and last is not None and (now - last) < cooldown_seconds:
        remaining = int(cooldown_seconds - (now - last)) + 1
        raise SyncBusy(
            f"'{name}' started moments ago; retry in ~{remaining}s",
            retry_after_seconds=remaining,
        )
    _running.add(name)
    _last_start[name] = now
    try:
        yield
    finally:
        _running.discard(name)
