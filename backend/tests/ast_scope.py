"""
Scope predicates shared by the AST family tests (GitHub issue #72).

A family test chooses the modules it holds to a rule by what the rule is about, never by
directory or name (docs/failure-modes.md, issue #44). The Yahoo family tests
(`test_yahoo_rate_limit_family`, `test_yahoo_eligibility_family`,
`test_yahoo_minor_unit_family`) each wrote "imports yfinance" themselves; three copies of
a scope are three chances for one family to stop seeing a module the others still see.
`tests/test_fixture_copies.py` refuses a test module that redefines one of these.
"""
import ast


def imports_yfinance(tree: ast.AST) -> bool:
    """An AST import of `yfinance`, not the word: several pure helper modules (peg_ratio,
    safe_numbers, ttm_growth) only mention it in prose, and matching those would make a
    family assertion noise that gets silenced rather than read."""
    return any(
        (isinstance(n, ast.Import) and any(a.name.split(".")[0] == "yfinance" for a in n.names))
        or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "yfinance")
        for n in ast.walk(tree)
    )
