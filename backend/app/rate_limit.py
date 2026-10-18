"""
Per-client request throttle for the public API.

`single_flight` fences the *sync pipelines* — the routes that reach IBKR and Yahoo —
but says nothing about reads, and some reads are expensive: `/api/portfolio/
value-over-time` sweeps up to five years of days across every security, `/api/tax/
report` rebuilds a year-end holdings snapshot, and `/api/portfolio/benchmark` walks a
timeline per benchmark. All of them are anonymous and unmetered behind a public nginx.

This is a fixed-window counter per client, kept deliberately simple:

- **In-process**, like `single_flight`, on the same single-uvicorn-worker assumption.
  A second worker would give each its own window — which loosens the limit rather than
  breaking anything, and the deployment has one worker.
- **Fixed window, not sliding.** A sliding window needs a timestamp deque per client;
  a fixed window needs two integers. The cost is that a client can burst across a
  window boundary, which for a limit whose job is to stop runaway loops is irrelevant.
- **Generous by default.** This must never make the dashboard feel broken: a single
  page load issues a dozen or so requests and a tab switch several more.

Set `RATE_LIMIT_PER_MINUTE=0` to disable it entirely.
"""
import ipaddress
import logging
import time
from typing import Dict, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

# Never throttled: the deploy script polls it, and it exposes nothing.
EXEMPT_PATHS = frozenset({"/health", "/"})

# GETs under these prefixes skip the counter. Logo images are one request per avatar
# (a look-through page draws up to a hundred), each a primary-key/ISIN lookup served
# `immutable` once versioned; counting them let a cold image cache 429 the page's own
# data requests. The trailing slash keeps the manifest, `GET /api/logos`, counted.
EXEMPT_GET_PREFIXES = ("/api/logos/",)

WINDOW_SECONDS = 60

_KEY_CHARS = 64

# client key -> (window index, count in that window)
_counters: Dict[str, Tuple[int, int]] = {}

# Bounds the dict against a spray of distinct keys, which would otherwise make the
# limiter itself the memory leak it exists to prevent. Well above any real client count
# for a single-tenant deployment. A new key arriving while the table is full is counted
# in `_overflow`, one bucket shared by every such key — never waved through.
_MAX_TRACKED_CLIENTS = 10_000
_overflow: Tuple[int, int] = (-1, 0)
# The window `_prune` last ran in: it only drops closed windows, so once per window
# is enough, and a full table must not cost a scan per request.
_pruned_window: Optional[int] = None


def reset() -> None:
    """Drop all counters. For tests — the window state is process-lifetime."""
    global _overflow, _pruned_window
    _counters.clear()
    _overflow = (-1, 0)
    _pruned_window = None


def _is_trusted_peer(host: str) -> bool:
    """A loopback or private address: the proxy in front of us, never the internet."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback or ip.is_private


def client_key(request: Request) -> str:
    """
    Who to count against.

    The backend is reached through one proxy, the host nginx, which connects to the
    container's loopback-published port, so the direct peer is a loopback or private
    (Docker bridge) address and would make one bucket for the whole internet. That proxy
    *appends* the address it saw to `X-Forwarded-For`; everything to the left of it was
    supplied by the client and proves nothing. So the key is the entry
    `TRUSTED_PROXY_HOPS` places from the right (the rightmost with the default of one
    proxy) — the only entry a client cannot choose, which stops a client from either
    splitting its own bucket or charging its requests to someone else's.

    The header is honoured only when the peer itself is loopback/private. A peer with a
    public address reached the backend directly, so its own address is the key and any
    header it sent is ignored.
    """
    peer = request.client.host if request.client else "unknown"
    hops = settings.trusted_proxy_hops
    forwarded = request.headers.get("X-Forwarded-For", "")
    if hops > 0 and forwarded and _is_trusted_peer(peer):
        entries = [e.strip() for e in forwarded.split(",")]
        # Fewer entries than hops: every entry present was appended by a trusted proxy,
        # so the leftmost is the outermost one's view of the client.
        chosen = entries[-min(hops, len(entries))]
        if chosen:
            return chosen[:_KEY_CHARS]
    return peer[:_KEY_CHARS]


def is_exempt(method: str, path: str) -> bool:
    """Paths the limiter never counts."""
    return path in EXEMPT_PATHS or (method == "GET" and path.startswith(EXEMPT_GET_PREFIXES))


def check(key: str, limit: int, now: Optional[float] = None) -> int:
    """
    Count one request against ``key``.

    Returns 0 when it is allowed, or the seconds remaining in the window when the limit
    is already spent. Counting happens even on rejection so a client that keeps hammering
    does not reset itself.
    """
    global _overflow
    if limit <= 0:
        return 0

    now = time.monotonic() if now is None else now
    window = int(now // WINDOW_SECONDS)

    if key not in _counters and len(_counters) >= _MAX_TRACKED_CLIENTS:
        _prune(window)
        if len(_counters) >= _MAX_TRACKED_CLIENTS:
            # Full table and a new client: count it in the shared overflow bucket, so
            # an internal bound can tighten the limit but never lift it.
            _overflow, retry_after = _count(_overflow, window, limit, now)
            return retry_after

    _counters[key], retry_after = _count(_counters.get(key), window, limit, now)
    return retry_after


def _count(
    state: Optional[Tuple[int, int]], window: int, limit: int, now: float
) -> Tuple[Tuple[int, int], int]:
    """One request against a (window, count) slot: the new slot and the retry-after."""
    recorded_window, count = state if state is not None else (window, 0)
    if recorded_window != window:
        recorded_window, count = window, 0
    if count >= limit:
        return (recorded_window, count + 1), int(WINDOW_SECONDS - (now % WINDOW_SECONDS)) + 1
    return (recorded_window, count + 1), 0


def _prune(current_window: int) -> None:
    """Drop counters from a window that has already closed (at most once per window)."""
    global _pruned_window
    if _pruned_window == current_window:
        return
    _pruned_window = current_window
    stale = [k for k, (w, _) in _counters.items() if w != current_window]
    for k in stale:
        del _counters[k]


async def rate_limit_middleware(request: Request, call_next):
    limit = settings.rate_limit_per_minute
    if limit <= 0 or is_exempt(request.method, request.url.path):
        return await call_next(request)

    retry_after = check(client_key(request), limit)
    if retry_after:
        logger.warning(
            "Rate limited %s %s (>%d req/min)", request.method, request.url.path, limit
        )
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many requests. Retry in {retry_after}s."},
            headers={"Retry-After": str(retry_after)},
        )

    return await call_next(request)
