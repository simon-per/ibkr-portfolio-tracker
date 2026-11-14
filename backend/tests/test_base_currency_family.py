"""
The selectable base currencies are spelled in one place, and every FX warmer reads it.

`SUPPORTED_BASE_CURRENCIES` is the list the switch accepts; `eur_to_base_pairs()` is the
EUR->base pairs both warmers keep in the cache (the daily exchange-rate job and the crypto
book's `_warm_fx`). Until GBP joined (2026-11-14) the daily job hard-coded `("CHF", "USD")`
— a copy of the list. Adding a base to the list alone would have made switching to it
work (the switch backfills that rate's history itself) and then let the rate stop
moving: every figure converted at a rate a day older each morning, and the staleness
warning the only clue. The test that catches that is "is there a copy at all", so this
walks the tree for one.
"""
import ast
from pathlib import Path

from app.repositories.app_settings_repository import (
    SUPPORTED_BASE_CURRENCIES,
    eur_to_base_pairs,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"
HOME = APP_DIR / "repositories" / "app_settings_repository.py"


def test_gbp_is_a_base_currency():
    assert SUPPORTED_BASE_CURRENCIES == ["EUR", "CHF", "USD", "GBP"]


def test_the_pairs_cover_every_non_eur_base_and_nothing_else():
    pairs = eur_to_base_pairs()
    assert all(src == "EUR" for src, _ in pairs)
    assert [dst for _, dst in pairs] == [b for b in SUPPORTED_BASE_CURRENCIES if b != "EUR"]


def _string_elements(node):
    return [
        e.value for e in node.elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]


def test_no_module_spells_its_own_list_of_base_currencies():
    """A literal list/tuple/set made only of base currencies, with two or more non-EUR
    ones, outside the module that owns them. A wider currency list (the ECB set, the warm
    set) is not a copy and is not flagged."""
    bases = set(SUPPORTED_BASE_CURRENCIES)
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in path.parts or path == HOME:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                continue
            names = _string_elements(node)
            if len(names) != len(node.elts) or not names:
                continue
            if set(names) <= bases and len(set(names) - {"EUR"}) >= 2:
                offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno}: {names}")
    assert not offenders, (
        "Read SUPPORTED_BASE_CURRENCIES / eur_to_base_pairs() instead of a copy:\n"
        + "\n".join(offenders)
    )


def test_both_warmers_read_the_shared_pairs():
    for rel in ("services/scheduler_service.py", "services/crypto_service.py"):
        source = (APP_DIR / rel).read_text(encoding="utf-8")
        assert "eur_to_base_pairs()" in source, f"{rel} no longer warms eur_to_base_pairs()"
