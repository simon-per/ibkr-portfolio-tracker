"""
Write authentication for the public API.

`/api/` is proxied publicly by nginx with no authentication at all. Reads are only
this account's data, but the **mutating** routes are another matter: a stranger can
change the base currency (`PUT /api/settings/base-currency`), delete watchlist rows,
and start real syncs against the IBKR Flex token and Yahoo — the two upstreams whose
rate limits this project has already been locked out of three times. `single_flight`
throttles those routes, but throttling is not authorization: it bounds how fast an
anonymous caller can do it, not whether they may.

**Enforced as middleware, not a per-route dependency.** Every router here takes only
`Depends(get_db)`, so a dependency would have to be added to ~14 mutating routes and
remembered on every route added later. Keying on the HTTP method instead means a new
`POST` is protected the moment it exists, which is the property worth having.

**Off unless configured.** With `API_ADMIN_TOKEN` empty the middleware is a pass-through
and behaviour is exactly what it is today, so enabling it is a deliberate act rather
than something a deploy does to a running site. Startup logs a warning when it is off,
for the same reason `SCHEDULER_ENABLED=false` does: a security control that is silently
inactive looks identical to one that is working.
"""
import logging
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

# Anything that can change state. GET/HEAD/OPTIONS stay open: the frontend is served
# from the same origin and has no login, so gating reads would black out the whole UI.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

API_KEY_HEADER = "X-API-Key"


def write_auth_enabled() -> bool:
    """True when a token is configured. Read through `settings` at call time so tests
    can monkeypatch it, rather than being frozen at import."""
    return bool((settings.api_admin_token or "").strip())


def _presented_key(request: Request) -> str:
    """
    The key the caller offered, from `X-API-Key` or an `Authorization: Bearer` header.

    Both are accepted because the browser uses the former and `curl`/CI conventionally
    reaches for the latter; requiring a single spelling buys nothing.
    """
    header = request.headers.get(API_KEY_HEADER)
    if header:
        return header.strip()
    authorization = request.headers.get("Authorization", "")
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() == "bearer":
        return credentials.strip()
    return ""


def request_requires_write_auth(request: Request) -> bool:
    """
    Whether this request must present a key.

    Scoped to `/api/` so `/health` stays reachable by the deploy script and any uptime
    check — those are unauthenticated by necessity and expose nothing.
    """
    if not write_auth_enabled():
        return False
    if request.method.upper() not in MUTATING_METHODS:
        return False
    return request.url.path.startswith("/api/")


async def write_auth_middleware(request: Request, call_next):
    if not request_requires_write_auth(request):
        return await call_next(request)

    presented = _presented_key(request)
    expected = (settings.api_admin_token or "").strip()

    # compare_digest, not ==: a plain comparison short-circuits on the first differing
    # byte and leaks the shared prefix through response timing. Compared as bytes:
    # Starlette decodes headers as latin-1, so any byte >= 0x80 in the header arrives
    # as a non-ASCII str, and compare_digest raises TypeError on those — which turned a
    # malformed credential into a 500 with a traceback per attempt instead of a 401.
    if not presented or not secrets.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    ):
        # The path, never the key or any part of it. Logging a near-miss would put
        # guessed keys in a file that outlives the guess.
        logger.warning(
            "Rejected unauthenticated %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=401,
            content={
                "detail": (
                    f"This endpoint changes data and requires the {API_KEY_HEADER} header."
                )
            },
            # Tells a generic client how to authenticate rather than making it guess.
            headers={"WWW-Authenticate": 'Bearer realm="portfolio-api"'},
        )

    return await call_next(request)


def log_write_auth_state() -> None:
    """Called at startup. Loud when off, because an unprotected write API that nobody
    notices is the failure this module exists to prevent."""
    if write_auth_enabled():
        logger.info(
            "Write authentication ENABLED - %s or 'Authorization: Bearer' required "
            "for POST/PUT/PATCH/DELETE under /api/",
            API_KEY_HEADER,
        )
    else:
        logger.warning(
            "Write authentication DISABLED (API_ADMIN_TOKEN unset) - every mutating "
            "/api/ route is open to anyone who can reach it, including the sync "
            "triggers that spend the IBKR Flex and Yahoo rate limits"
        )
