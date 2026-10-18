"""
One notion of "the same company" (issue #69).

`company_identity.company_groups` folds listings and share classes on ISIN /
shareClassFIGI / LEI / `ISSUER_OVERRIDES`, but it is pure: it folds only on the
identifiers it is handed. A caller that skips the `isin_identities` cache folds GOOGL with
ABEA (one ISIN) and leaves GOOG apart (only the LEI joins it); one that skips the overrides
splits TSMC from its ADR. The look-through table attached both in a private method, and the
earnings lists, needing the same fold, would have been its second copy.

So `identity_service.group_companies` is the one caller under `app/`, and every reader
asks it (`companies_of_securities` for a set of securities). Any other call fails here.

Offline: no network, no DB. Pure AST.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

OWNER = "services/identity_service.py"
DEFINED_IN = "services/company_identity.py"


def _calls(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called == name:
                yield node.lineno


def test_company_groups_is_called_only_by_group_companies():
    offenders = []
    owner_calls = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel == DEFINED_IN:
            continue
        lines = list(_calls(ast.parse(path.read_text(encoding="utf-8")), "company_groups"))
        if rel == OWNER:
            owner_calls = lines
        else:
            offenders += [f"{rel}:{line}" for line in lines]
    assert not offenders, (
        "fold companies through identity_service.group_companies, which attaches the "
        f"identity cache and ISSUER_OVERRIDES: {offenders}"
    )
    assert len(owner_calls) == 1, owner_calls


def test_both_readers_fold_through_identity_service():
    """The look-through table and the earnings lists: if either stops asking, the scan
    above passes vacuously for it."""
    for rel, name in (
        ("services/lookthrough_service.py", "group_companies"),
        ("services/fundamentals_service.py", "companies_of_securities"),
    ):
        tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
        assert list(_calls(tree, name)), f"{rel} no longer calls {name}"
