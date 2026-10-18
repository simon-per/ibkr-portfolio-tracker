"""
Today's uninvested cash has one read for a share-of-the-book denominator:
`CashService.uninvested_cash` (GitHub #28).

Allocation read it inline (`cash_source()` + `balance_as_of(balance_events(...))`, positive
only) while Look-through divided by holdings alone, so the same money read higher on one
tab than on the other under the same "% of portfolio" header. The scan pins the family
rather than the two call sites: both tabs go through the helper, neither re-derives the
balance itself, and no other service picks up `balance_as_of` without being named here
with its reason.
"""
import ast
from decimal import Decimal
from pathlib import Path

from app.services.cash_service import DERIVED, UNKNOWN, UninvestedCash

SERVICES_DIR = Path(__file__).resolve().parents[1] / "app" / "services"

#: The two "% of portfolio" denominators that include cash server-side.
SHARE_OF_BOOK_SERVICES = ("allocation_service.py", "lookthrough_service.py")

#: The low-level reads the helper wraps.
LOW_LEVEL = {"balance_as_of", "balance_events", "cash_source"}

#: Services allowed to call `balance_as_of` themselves, each with its reason.
BALANCE_AS_OF_ALLOWED = {
    "cash_service.py": "the helper's own home",
    # `_cash_summary` ships the summary card's balance beside its source and lets the
    # client's `cashIsTracked` decide; the timeline sweeps the events per day. Neither is
    # a share-of-the-book denominator.
    "portfolio_service.py": "summary card balance and timeline",
}


def _called_attributes(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_both_share_of_book_services_read_cash_through_the_one_helper():
    for name in SHARE_OF_BOOK_SERVICES:
        called = _called_attributes(SERVICES_DIR / name)
        assert "uninvested_cash" in called, f"{name} does not call CashService.uninvested_cash"
        assert not called & LOW_LEVEL, (
            f"{name} calls {sorted(called & LOW_LEVEL)} itself — a second derivation of "
            f"the balance beside uninvested_cash"
        )


def test_no_other_service_derives_todays_balance_on_its_own():
    offenders = [
        path.name
        for path in sorted(SERVICES_DIR.glob("*.py"))
        if path.name not in BALANCE_AS_OF_ALLOWED
        and "balance_as_of" in _called_attributes(path)
    ]
    assert offenders == [], (
        f"{offenders} call balance_as_of: use CashService.uninvested_cash, or name the "
        f"module in BALANCE_AS_OF_ALLOWED with the reason it is not a denominator"
    )


def test_only_a_known_positive_balance_joins_the_book():
    # The server half of a pair: `inBookCash` in frontend/src/lib/portfolioCash.test.ts pins
    # the same four cases for Positions' weight denominator (GitHub #52).
    assert UninvestedCash(DERIVED, Decimal("100")).in_book == Decimal("100")
    assert UninvestedCash(DERIVED, Decimal("-250")).in_book == Decimal("0")
    assert UninvestedCash(DERIVED, Decimal("0")).in_book == Decimal("0")
    assert UninvestedCash(UNKNOWN, None).in_book == Decimal("0")
