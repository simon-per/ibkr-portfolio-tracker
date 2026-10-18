"""
Bounds every integer a route takes from the URL (GitHub #64).

SQLite's INTEGER is a signed 64-bit value, and the driver refuses to bind a larger Python
int: `OverflowError`, which escaped as a 500 with its traceback logged twice (the app's
handler and uvicorn's). `GET /api/logos/99999999999999999999` did that on every request,
and the logo images are exempt from the rate limiter, so an anonymous loop could fill the
container log's rotation cap and push out the sync evidence `docs/deployment.md` says to
grep. Bounded here, the same request is a 422 from validation, before a handler runs and
without a log line of its own.

`tests/test_int_param_bounds_family.py` walks the live route table: every int path or
query parameter must carry a lower bound and an upper bound inside `DB_INT_MAX`, and every
int path parameter is a row id at least 1.
"""
from typing import Annotated

from fastapi import Path

# The largest value SQLite's INTEGER (signed 64-bit) can hold.
DB_INT_MAX = 2**63 - 1

# A row id taken from the path: positive and bindable, or 422.
DbId = Annotated[int, Path(ge=1, le=DB_INT_MAX)]
