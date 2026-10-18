"""
The family rule for `security_repository.held()` (GitHub issue #42).

`held()` is the one criterion for "a security with at least one open lot". Until
2026-10-18 six readers wrote it themselves — a join on `TaxLot.is_open == True`, and the
backtest CLI on `TaxLot.close_date IS NULL`, a different predicate that agreed only
because every writer keeps the two columns in step. A copy that agrees is still a copy:
the day the invariant breaks, the copies stop agreeing and nothing notices.

So every open-lot predicate in `app/` is caught here: `TaxLot.is_open == True`,
`TaxLot.is_open.is_(True)`, `TaxLot.close_date.is_(None)` / `== None`, and the same in a
raw `text()` string. The two repositories own the predicate; a reader that selects the
LOTS themselves (not a set of securities) is listed below with its reason, and its count
is pinned, so a second predicate added beside an exempted one still fails.

Offline: no network, no DB. Pure AST.
"""
import ast
import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

# The modules that own an open-lot predicate.
OWNERS = {
    "repositories/security_repository.py",  # held(), held_at(): the criteria themselves
    "repositories/taxlot_repository.py",    # the lot reader's own is_open filter
}

# (module, enclosing function) -> (matches, reason). Lot-level readers only: each
# selects TaxLot rows and values them, which `held()` (a set of securities) cannot do.
NOT_IN_THE_FAMILY = {
    ("services/portfolio_service.py", "get_current_portfolio_summary"): (
        1, "selects the open (TaxLot, Security) pairs and values each lot",
    ),
    ("services/portfolio_service.py", "get_positions_breakdown"): (
        1, "selects the open (TaxLot, Security) pairs and aggregates them per security",
    ),
}

_TEXT_PREDICATE = re.compile(
    r"is_open\s*(?:=|==|\bIS\b)\s*(?:1|TRUE)\b|close_date\s+IS\s+NULL", re.IGNORECASE,
)


def _taxlot_column(node: ast.AST, *names: str) -> bool:
    return (
        isinstance(node, ast.Attribute) and node.attr in names
        and isinstance(node.value, ast.Name) and node.value.id == "TaxLot"
    )


def _const(node: ast.AST, value) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def _is_open_lot_predicate(node: ast.AST) -> bool:
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, op, right = node.left, node.ops[0], node.comparators[0]
        if not isinstance(op, (ast.Eq, ast.Is)):
            return False
        return (
            (_taxlot_column(left, "is_open") and _const(right, True))
            or (_taxlot_column(left, "close_date") and _const(right, None))
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        method, arg = node.func, node.args[0]
        if method.attr != "is_":
            return False
        return (
            (_taxlot_column(method.value, "is_open") and _const(arg, True))
            or (_taxlot_column(method.value, "close_date") and _const(arg, None))
        )
    return False


def _is_text_predicate(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call) and getattr(node.func, "id", None) == "text"
        and any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                and _TEXT_PREDICATE.search(a.value) for a in node.args)
    )


def _sites():
    """{(module, enclosing function): [line, ...]} for every predicate outside OWNERS."""
    found = {}
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel in OWNERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node, func, rel=rel):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = node.name
            if _is_open_lot_predicate(node) or _is_text_predicate(node):
                found.setdefault((rel, func), []).append(node.lineno)
            for child in ast.iter_child_nodes(node):
                visit(child, func)

        visit(tree, "<module>")
    return found


def test_no_hand_written_open_lot_predicate_outside_the_repositories():
    offenders = {
        site: lines for site, lines in _sites().items()
        if site not in NOT_IN_THE_FAMILY
        or len(lines) != NOT_IN_THE_FAMILY[site][0]
    }
    assert not offenders, (
        f"Open-lot predicates written by hand: {offenders}. A set of held securities is "
        "`select(Security...).where(security_repository.held())`; ISINs are "
        "`identity_service.held_isins`. A reader that genuinely selects the lots goes in "
        "NOT_IN_THE_FAMILY with its reason and count."
    )


def test_no_exemption_outlives_its_reason():
    sites = _sites()
    stale = [site for site in NOT_IN_THE_FAMILY if site not in sites]
    assert not stale, (
        f"{stale} are exempted but no longer write an open-lot predicate. Delete the "
        "entry — an exemption nothing exercises is a claim nobody is checking."
    )
    assert all(reason.strip() for _, reason in NOT_IN_THE_FAMILY.values())


def test_the_walk_sees_the_owners():
    """A walk that matches nothing passes vacuously: it must see the predicate in held()."""
    tree = ast.parse((APP / "repositories/security_repository.py").read_text(encoding="utf-8"))
    assert any(_is_open_lot_predicate(n) for n in ast.walk(tree))


def test_the_walk_catches_each_shape():
    for source in (
        "select(Security.id).join(TaxLot).where(TaxLot.is_open == True)",
        "select(TaxLot.security_id).where(TaxLot.is_open.is_(True))",
        "select(TaxLot).where(TaxLot.close_date.is_(None))",
        "select(TaxLot).where(TaxLot.close_date == None)",
        "db.execute(text('SELECT security_id FROM taxlots WHERE is_open = 1'))",
        "db.execute(text('SELECT security_id FROM taxlots WHERE close_date IS NULL'))",
    ):
        nodes = ast.walk(ast.parse(source))
        assert any(_is_open_lot_predicate(n) or _is_text_predicate(n) for n in nodes), source
