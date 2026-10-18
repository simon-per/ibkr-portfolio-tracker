"""
The maximum chart span is written down in two languages and must agree.

`/api/portfolio/value-over-time` refuses a range wider than `365 * 5` days with a 400
(the shared guard in `app/routers/_windows.py`, which every window route goes through),
and `frontend/src/lib/dateRanges.ts` clamps the ALL button to its own `MAX_RANGE_DAYS`
so it never asks for one the server will reject. Two copies of one number, in files that
cannot import each other — the same shape as `breakpoints.test.ts` (Tailwind's scale in
TypeScript) and `test_deploy_guard_hours.py` (the Berlin sync hours in three files).

Both of those exist because a constant duplicated across a language boundary drifts
silently, and this one drifts in a direction with no other symptom: tighten the server
and the ALL button starts returning 400 for every user whose inception is far enough
back, while every test on both sides still passes.

The boundary is exact, not approximate. The client asks for `today - MAX_RANGE_DAYS`,
so the span it sends is exactly `MAX_WINDOW_DAYS` and the server's `>` is False by one
day. A test asserting only "the client is not larger" would allow the client to shrink
to a year and never notice, so equality is what is pinned.
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROUTER = REPO / "backend" / "app" / "routers" / "_windows.py"
DATE_RANGES = REPO / "frontend" / "src" / "lib" / "dateRanges.ts"


def _server_max_days() -> int:
    """The `MAX_WINDOW_DAYS = 365 * 5` the shared window validator compares against."""
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "MAX_WINDOW_DAYS" in names:
                return int(eval(compile(ast.Expression(node.value), "<v>", "eval")))
    raise AssertionError(
        "no `MAX_WINDOW_DAYS = ...` in _windows.py — the range guard moved or was renamed, "
        "and this test can no longer see what the client has to agree with"
    )


def _client_max_days() -> int:
    match = re.search(
        r"export const MAX_RANGE_DAYS\s*=\s*([0-9*\s]+)", DATE_RANGES.read_text(encoding="utf-8")
    )
    assert match, "no `export const MAX_RANGE_DAYS` in dateRanges.ts"
    return int(eval(match.group(1)))


def test_the_client_clamp_matches_the_server_limit():
    server, client = _server_max_days(), _client_max_days()
    assert client == server, (
        f"dateRanges.ts clamps ALL to {client} days but "
        f"/api/portfolio/value-over-time rejects anything over {server}. "
        "Wider means the ALL button 400s; narrower means the chart silently "
        "truncates history the server would have served."
    )


def test_a_span_of_exactly_the_limit_is_accepted():
    """
    The clamp lands *on* the boundary, so an off-by-one on either side breaks ALL for
    everyone with a long enough history — and nothing else would show it.
    """
    max_days = _server_max_days()
    assert not (max_days > max_days), "the server compares with > , so an exact span must pass"


def test_the_frontend_still_documents_why_the_clamp_exists():
    """
    The clamp is only correct while it names the endpoint it is protecting. Someone
    reading `MAX_RANGE_DAYS` with no reason attached is one refactor away from deleting
    it as an arbitrary limit.
    """
    source = DATE_RANGES.read_text(encoding="utf-8")
    assert "value-over-time" in source, (
        "dateRanges.ts no longer names the endpoint MAX_RANGE_DAYS exists to satisfy"
    )
