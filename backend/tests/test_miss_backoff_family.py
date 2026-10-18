"""
One back-off for "this upstream keeps answering nothing about this security" (issue #66).

The price loop's rule (issue #10) was the only one; the fundamentals pass re-asked an
unservable security every pass, and the fix would have been its second copy. It is
`app/services/miss_backoff.miss_backoff_until`, and each caller keeps only its own
columns and its own exemptions. The family, by AST: no other module under `app/` doubles
anything (`2 ** n`, `1 << n`), and every `*_miss_count` column on `Security` is handed to
`miss_backoff_until` somewhere — a new pair of stamps cannot bring its own schedule.
"""
import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.models.security import Security
from app.repositories.fundamentals_repository import fundamentals_backoff_until
from app.services.market_data_service import price_backoff_until
from app.services.miss_backoff import MISS_BACKOFF_CAP_DAYS, MISS_BACKOFF_SLACK

APP_DIR = Path(__file__).resolve().parents[1] / "app"
HOME = APP_DIR / "services" / "miss_backoff.py"


def _modules():
    for path in sorted(APP_DIR.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def test_no_module_doubles_a_backoff_of_its_own():
    offenders = []
    for path, tree in _modules():
        if path == HOME:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.left, ast.Constant):
                continue
            # A doubling backoff raises 2 to a COUNT; a constant exponent is a fixed
            # number (routers/_params.DB_INT_MAX = 2**63 - 1), not a backoff.
            if isinstance(node.right, ast.Constant):
                continue
            if ((isinstance(node.op, ast.Pow) and node.left.value == 2)
                    or (isinstance(node.op, ast.LShift) and node.left.value == 1)):
                offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
    assert offenders == [], f"use miss_backoff.miss_backoff_until: {offenders}"


def test_every_miss_count_column_goes_through_the_shared_rule():
    columns = {c.name for c in Security.__table__.columns if c.name.endswith("_miss_count")}
    assert {"price_miss_count", "fundamentals_miss_count"} <= columns
    handed = set()
    for _, tree in _modules():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", None))
                    == "miss_backoff_until"):
                handed |= {a.attr for a in node.args if isinstance(a, ast.Attribute)}
    assert columns <= handed, f"read without miss_backoff_until: {sorted(columns - handed)}"


def test_both_callers_keep_one_schedule():
    checked = datetime(2026, 10, 1, 5, 0)
    for misses in range(1, 12):
        stamps = SimpleNamespace(
            price_checked_at=checked, price_miss_count=misses,
            fundamentals_checked_at=checked, fundamentals_miss_count=misses,
        )
        days = min(2 ** (misses - 1), MISS_BACKOFF_CAP_DAYS)
        expected = checked + timedelta(days=days) - MISS_BACKOFF_SLACK
        assert price_backoff_until(stamps, is_held=False) == expected
        assert fundamentals_backoff_until(stamps) == expected
    unstamped = SimpleNamespace(fundamentals_checked_at=None, fundamentals_miss_count=None)
    assert fundamentals_backoff_until(unstamped) is None
