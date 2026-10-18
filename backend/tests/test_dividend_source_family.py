"""
`dividend_payments.source` has two values, and `dividend_repository` names them once:
`IBKR_SOURCE` and `YFINANCE_SOURCE`.

About thirty readers spelled them out instead — `p.source == "ibkr"` across the era splice,
the summary's provenance flag, the tax report, the scheduler's estimate refresh, two CLIs
(GitHub #71). They agreed; a correct copy is still a copy. This fails, anywhere under
`app/` but the repository module:

- the string `"yfinance_estimate"` at all;
- the string `"ibkr"` where it stands for a source: compared with, assigned to, or keyed
  under anything whose name mentions `source`. The same word also names the IBKR
  *account* (`app.accounts.IBKR`), the cash figure's provenance (`cash_service.MEASURED`)
  and a sync type, none of which is this column — those pass. (A price row's
  `market_prices.source` would be another vocabulary; it has never been written "ibkr" in
  code, and if it is, it needs its own constant, not this one.)

The one reasoned exemption is the wire contract: a pydantic field annotated
`Literal["ibkr", "mixed", "yfinance_estimate"]` must spell its values, so a statement
whose annotation is a `Literal[...]` passes — and the last test pins those Literals to
the constants, so the two cannot drift either.
"""
import ast
from pathlib import Path
from typing import get_args

from app.repositories.dividend_repository import IBKR_SOURCE, YFINANCE_SOURCE
from app.schemas.portfolio import DividendSummaryResponse, DividendTtmPoint

APP_DIR = Path(__file__).resolve().parents[1] / "app"
HOME = "repositories/dividend_repository.py"


def _mentions_source(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and "source" in sub.id.lower():
            return True
        if isinstance(sub, ast.Attribute) and "source" in sub.attr.lower():
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "source" in sub.value.lower():
            return True
    return False


def _is_literal_annotation(statement: ast.AST) -> bool:
    return isinstance(statement, ast.AnnAssign) and any(
        isinstance(sub, ast.Subscript)
        and (
            (isinstance(sub.value, ast.Name) and sub.value.id == "Literal")
            or (isinstance(sub.value, ast.Attribute) and sub.value.attr == "Literal")
        )
        for sub in ast.walk(statement.annotation)
    )


def _context(const: ast.Constant, parents: dict) -> tuple[bool, bool]:
    """
    `(stands for a source, inside a Literal-annotated field)` for one string constant.

    Climbs to the largest expression holding it, stopping at a dict value (judged by its
    key) and a keyword argument (judged by its name), then reads that expression and,
    for an assignment, its targets.
    """
    node = const
    while True:
        parent = parents.get(node)
        if isinstance(parent, ast.Dict):
            if node in parent.values:
                key = parent.keys[parent.values.index(node)]
                return key is not None and _mentions_source(key), False
            return False, False
        if isinstance(parent, ast.keyword):
            return parent.arg is not None and "source" in parent.arg.lower(), False
        if isinstance(parent, (ast.expr, ast.comprehension)):
            node = parent
            continue
        break
    if _is_literal_annotation(parent):
        return True, True
    context = [node]
    if isinstance(parent, ast.Assign):
        context += parent.targets
    elif isinstance(parent, (ast.AnnAssign, ast.AugAssign)):
        context.append(parent.target)
    return any(_mentions_source(c) for c in context), False


def _bare_sources(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and node.value in (IBKR_SOURCE, YFINANCE_SOURCE)):
            continue
        is_source, in_literal = _context(node, parents)
        if in_literal:
            continue
        if node.value == YFINANCE_SOURCE or is_source:
            found.append((node.lineno, node.value))
    return found


def test_the_scan_recognises_the_forms():
    """A detector that quietly stopped matching would pass everything below."""
    for form in (
        'rows = [p for p in ps if p.source == "ibkr"]',
        'q = select(D).where(D.source != "ibkr")',
        'label = "ibkr" if sources <= {"ibkr"} else "mixed"',
        'row = {"source": "ibkr", "amount": 1}',
        'dividend_source = "ibkr"',
        'row["sources"].add("ibkr" if p.source == "ibkr" else "estimate")',
        'x = "yfinance_estimate"',
        'upsert(source="ibkr")',
    ):
        assert _bare_sources(form), form
    for other in (
        'IBKR = "ibkr"',
        'MEASURED = "ibkr"',
        'run = SyncRun(sync_type="ibkr", status="ok")',
        'account: str = "ibkr"',
        'TYPES = ("ibkr", "ibkr_sync")',
        'row = {"account": "ibkr", "source": src}',
        'source: Literal["ibkr", "mixed", "yfinance_estimate"] = "yfinance_estimate"',
        "rows = [p for p in ps if p.source == IBKR_SOURCE]",
    ):
        assert _bare_sources(other) == [], other


def test_no_module_spells_a_dividend_source():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        name = path.relative_to(APP_DIR).as_posix()
        if name == HOME:
            continue
        for line, value in _bare_sources(path.read_text(encoding="utf-8")):
            offenders.append(f"{name}:{line} {value!r}")
    assert offenders == [], (
        f"use dividend_repository.IBKR_SOURCE / YFINANCE_SOURCE: {offenders}"
    )


def test_the_wire_contract_spells_the_same_two_values():
    for model in (DividendSummaryResponse, DividendTtmPoint):
        annotation = model.model_fields["source"].annotation
        values = set(get_args(annotation))
        for arg in get_args(annotation):
            values |= set(get_args(arg))
        assert {IBKR_SOURCE, YFINANCE_SOURCE} <= values, (model.__name__, values)
