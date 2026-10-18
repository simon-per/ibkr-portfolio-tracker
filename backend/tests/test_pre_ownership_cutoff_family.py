"""
`pre_ownership_cutoff` is the one definition of the oldest dividend ex-date worth keeping.

Both Yahoo dividend readers in `dividend_service` call it, and the forecast infers cadence
from the rows it keeps. `app/cli/prune_empty_dividends.py` wrote the same arithmetic by
hand (`first_lot - timedelta(days=365 * PRE_OWNERSHIP_HISTORY_YEARS)`). The two agreed,
but a prune that deletes what the ingest would keep gets re-ingested on the next sync, and
one that keeps what the ingest skips is noise (issue #45). A correct copy is still a copy:
this fails on any module under `app/` other than `dividend_service` that multiplies
`PRE_OWNERSHIP_HISTORY_YEARS`, so the next copy is caught too.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
CONSTANT = "PRE_OWNERSHIP_HISTORY_YEARS"


def cutoff_derivations(source: str, label: str) -> list[str]:
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            names = {
                n.id if isinstance(n, ast.Name) else n.attr
                for n in ast.walk(node) if isinstance(n, (ast.Name, ast.Attribute))
            }
            if CONSTANT in names:
                offenders.append(f"{label}:{node.lineno}")
    return offenders


def test_only_pre_ownership_cutoff_derives_the_cutoff():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if path.name == "dividend_service.py" or "__pycache__" in path.parts:
            continue
        offenders += cutoff_derivations(
            path.read_text(encoding="utf-8"), str(path.relative_to(APP.parent))
        )
    assert offenders == [], f"re-derives pre_ownership_cutoff, call it instead: {offenders}"


def test_the_scan_catches_a_copy():
    snippet = (
        "from datetime import timedelta\n"
        "import app.services.dividend_service as ds\n"
        "a = d - timedelta(days=365 * PRE_OWNERSHIP_HISTORY_YEARS)\n"
        "b = d - timedelta(days=ds.PRE_OWNERSHIP_HISTORY_YEARS * 365)\n"
    )
    assert cutoff_derivations(snippet, "s.py") == ["s.py:3", "s.py:4"]


def test_the_definition_itself_is_where_the_scan_says():
    source = (APP / "services" / "dividend_service.py").read_text(encoding="utf-8")
    assert cutoff_derivations(source, "dividend_service.py"), (
        "the scan exempts dividend_service because the one derivation lives there"
    )
