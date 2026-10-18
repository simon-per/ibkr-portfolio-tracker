"""
The family rule for "a lot held at D" (GitHub issue #43).

docs/data-model-and-valuation.md's close-date convention — a lot sold on D is not held at
D's close, `open_date <= D < close_date` — was written inline seven times: twice in
`dividend_service` (line for line), twice in SQL and three times in Python in
`portfolio_service`. The doc said "change these together" and nothing made that happen.
Since 2026-10-18 there is one definition, `security_repository.lot_held_at` (Python) and
`security_repository.held_at` (SQL), pinned equal in `test_close_date_boundary.py`.

The signature of a copy is its close-date half, so every ordering comparison against a
`close_date` attribute and every `close_date.is_(None)` in `app/` is caught here, outside
the helpers' module. What remains are not "held at D" questions — a disposal window, a
trade-to-lot match — each listed with its reason and its count, so a copy written beside
an exempted comparison still fails.

Offline: no network, no DB. Pure AST.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

OWNER = "repositories/security_repository.py"  # held_at, lot_held_at, held(closed_within_days)

# (module, enclosing function) -> (matches, reason).
NOT_IN_THE_FAMILY = {
    ("services/portfolio_service.py", "realized_rows_from_closed_lots"): (
        4, "the disposal window `start <= close_date <= end` — sold inside it, not held at a date; "
        "once in SQL, once on `AttributionInputs`' preloaded lots (pinned equal in "
        "test_read_path_query_cost.py)",
    ),
    ("services/sync_helper.py", "restamp_unsourced_closed_lots"): (
        1, "matches a SELL trade to the lot it closed (`trade_date <= close_date`)",
    ),
}

_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _close_date(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "close_date"


def _is_copy_shape(node: ast.AST) -> bool:
    if isinstance(node, ast.Compare):
        operands = [node.left, *node.comparators]
        return any(
            isinstance(op, _ORDERING) and (_close_date(a) or _close_date(b))
            for op, a, b in zip(node.ops, operands, operands[1:])
        )
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "is_" and _close_date(node.func.value)
    )


def _sites():
    found = {}
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel == OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node, func, rel=rel):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = node.name
            if _is_copy_shape(node):
                found.setdefault((rel, func), []).append(node.lineno)
            for child in ast.iter_child_nodes(node):
                visit(child, func)

        visit(tree, "<module>")
    return found


def test_no_inline_copy_of_held_at():
    offenders = {
        site: lines for site, lines in _sites().items()
        if site not in NOT_IN_THE_FAMILY
        or len(lines) != NOT_IN_THE_FAMILY[site][0]
    }
    assert not offenders, (
        f"close_date compared by hand: {offenders}. 'Is this lot held at D' is "
        "security_repository.lot_held_at(lot, d) in Python and held_at(d) in SQL — "
        "one definition of the close-date convention. Anything else goes in "
        "NOT_IN_THE_FAMILY with its reason and count."
    )


def test_no_exemption_outlives_its_reason():
    sites = _sites()
    stale = [site for site in NOT_IN_THE_FAMILY if site not in sites]
    assert not stale, (
        f"{stale} are exempted but no longer compare a close_date. Delete the entry."
    )
    assert all(reason.strip() for _, reason in NOT_IN_THE_FAMILY.values())


def test_the_walk_catches_each_shape():
    """Every form a copy has taken here, so the walk cannot pass vacuously."""
    for source in (
        "if lot.close_date and lot.close_date <= d: pass",
        "x = lot.open_date <= d and (lot.close_date is None or lot.close_date > d)",
        "or_(TaxLot.close_date.is_(None), TaxLot.close_date > on_date)",
        "if not (taxlot.close_date and d >= taxlot.close_date): pass",
    ):
        assert any(_is_copy_shape(n) for n in ast.walk(ast.parse(source))), source
    owner = ast.parse((APP / OWNER).read_text(encoding="utf-8"))
    assert any(_is_copy_shape(n) for n in ast.walk(owner))
