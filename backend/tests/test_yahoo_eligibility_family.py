"""
The family rule for `yahoo_eligible`.

Seven call sites across five service modules select `Security` and then hand every
row to Yahoo. None of them had a way to skip one until 2026-09-06, and the missing
opt-out is not a tidiness problem: `_get_yahoo_ticker` falls through to the variation
loop for anything `ticker_mappings` does not cover, the last variation tried is the
**bare symbol**, and a bare symbol that happens to match an unrelated US listing is
fetched cleanly and then *auto-saved as a mapping*. That is the SBI failure, and an
instrument Yahoo genuinely does not have -- a Swiss pillar 3a institutional tranche --
is exactly its shape.

So this is written as a family rule rather than five assertions, for the reason
`test_yahoo_rate_limit_family` is: the point is to catch the **sixth** service, which
nobody will remember to come back and add a case for.

Offline: no network, no DB. Pure AST.
"""
import ast
import pathlib


from app.models.security import PRICE_SOURCE_MANUAL, PRICE_SOURCE_YAHOO, Security
from app.services.yahoo_eligibility import is_yahoo_eligible, yahoo_eligible


SERVICES = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"

# Modules the walk matches but which are not in the family, each with the reason.
# A reason is mandatory and an *unused* entry fails the test below, which is what
# keeps this from becoming the stale exemption list that structural tests rot into:
# it can only shrink by someone fixing a module, never by someone forgetting one.
NOT_IN_THE_FAMILY = {
    "benchmark_service.py": (
        "Fetches by *benchmark ticker* from the hardcoded BENCHMARKS dict, never by "
        "portfolio security. Its only Security select is `select(TaxLot, Security)` "
        "for the money-in basis, which must blend across accounts — filtering it "
        "would make the comparison line stop investing a whole account's "
        "contributions."
    ),
}


def _imports_yfinance(tree: ast.AST) -> bool:
    """An AST import, not the word — several pure helpers only mention it in prose."""
    return any(
        (isinstance(n, ast.Import)
         and any(a.name.split(".")[0] == "yfinance" for a in n.names))
        or (isinstance(n, ast.ImportFrom)
            and (n.module or "").split(".")[0] == "yfinance")
        for n in ast.walk(tree)
    )


def _selects_security(tree: ast.AST) -> bool:
    """`select(Security)` anywhere — the shape that feeds a loop with rows to fetch."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "select"):
            continue
        for arg in node.args:
            if getattr(arg, "id", None) == "Security":
                return True
    return False


def test_every_yahoo_service_that_selects_securities_consults_the_predicate():
    """
    The check that survives a sixth service being added.

    Module-level rather than per-`select`, deliberately: three of these modules also
    hold a **read** path that selects `Security` and must *not* filter -- the
    allocation breakdown, the dividend breakdown and `get_fundamentals_for_portfolio`
    all have to name every holding, including ones Yahoo cannot price. Asserting on
    every call site would either fail on those or need an exemption list that goes
    stale. Asserting the module reaches the predicate at all catches the service that
    never heard of it, which is the failure that actually happens.
    """
    offenders = []
    for path in sorted(SERVICES.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        if not (_imports_yfinance(tree) and _selects_security(tree)):
            continue
        if "yahoo_eligible" not in source and path.name not in NOT_IN_THE_FAMILY:
            offenders.append(path.name)

    assert not offenders, (
        f"{offenders} select securities and hand them to Yahoo without consulting "
        "yahoo_eligibility.yahoo_eligible. A security Yahoo does not have will be "
        "resolved through the bare-symbol variation and the wrong listing auto-saved "
        "as its mapping — silently, and stickily, because tier 1 then shadows the "
        "suffix logic. See CLAUDE.md on SBI@TSE."
    )


def test_no_exemption_outlives_its_reason():
    """
    An exemption that no longer matches anything is a claim nobody is checking.

    Without this the dict only ever grows, and a module that later starts handing
    portfolio securities to Yahoo keeps its pass from a reason that stopped being
    true. Fails in *both* directions: a stale entry, and one that lost its reason.
    """
    matched = {
        path.name for path in sorted(SERVICES.glob("*.py"))
        if _imports_yfinance(tree := ast.parse(path.read_text(encoding="utf-8")))
        and _selects_security(tree)
    }
    stale = set(NOT_IN_THE_FAMILY) - matched
    assert not stale, (
        f"{sorted(stale)} are exempted from the yahoo_eligible family rule but no "
        "longer match the walk. Delete the entry — an exemption nothing exercises "
        "is a claim nobody is checking."
    )
    assert all(reason.strip() for reason in NOT_IN_THE_FAMILY.values()), (
        "Every exemption must say why, the same rule ISSUER_OVERRIDES and "
        "basket_proxy_reason are held to."
    )


def test_the_family_is_not_empty():
    """
    A structural test that matches nothing passes vacuously.

    `test_every_yahoo_looping_service_consults_the_shared_predicate` has a sibling
    version of this problem recorded in STATUS.md — a floor set far below the real
    count cannot fail. Pin that the walk actually finds the modules it is about.
    """
    matched = [
        path.name for path in sorted(SERVICES.glob("*.py"))
        if _imports_yfinance(tree := ast.parse(path.read_text(encoding="utf-8")))
        and _selects_security(tree)
    ]
    assert len(matched) >= 5, (
        f"Expected at least the five known Yahoo loops, found {matched}. If the walk "
        "stopped matching, this whole file is passing vacuously."
    )


def test_the_criterion_and_the_row_test_agree():
    """
    Two forms of one predicate, which is this codebase's dominant failure mode in
    miniature — so pin them equal rather than trusting they were written together.
    The query form gates the loops; the row form gates the inner functions those
    loops call, which are reachable directly.
    """
    criterion = str(yahoo_eligible().compile(compile_kwargs={"literal_binds": True}))
    assert PRICE_SOURCE_YAHOO in criterion and "price_source" in criterion

    for source, expected in ((PRICE_SOURCE_YAHOO, True), (PRICE_SOURCE_MANUAL, False)):
        assert is_yahoo_eligible(Security(price_source=source)) is expected


def test_a_sibling_priced_security_is_not_a_yahoo_instrument_to_the_family():
    """
    The market-data loop asks Yahoo for the *sibling's* bars and scales them; every
    other consumer — dividends, fundamentals, ratings, allocation — must leave the
    security alone, because the sibling's dividend history and `.info` are not its.
    So the family predicate says no, and only `is_sibling_priced` says yes.
    """
    from app.models.security import PRICE_SOURCE_SIBLING
    from app.services.yahoo_eligibility import is_sibling_priced

    sibling = Security(price_source=PRICE_SOURCE_SIBLING)
    assert is_yahoo_eligible(sibling) is False
    assert is_sibling_priced(sibling) is True
    assert is_sibling_priced(Security(price_source=PRICE_SOURCE_YAHOO)) is False
    assert is_sibling_priced(Security(price_source=PRICE_SOURCE_MANUAL)) is False


def test_a_security_from_before_the_column_existed_is_eligible():
    """
    The row form defaults to eligible when the attribute is absent.

    Several test modules build securities as `SimpleNamespace`, and production rows
    written before the migration take the server default. Both must read as ordinary
    Yahoo securities, or adding the column would silently stop pricing the portfolio.
    """
    class _Bare:
        pass

    assert is_yahoo_eligible(_Bare()) is True
