"""
One rule turns a foreign amount into EUR where a row can be skipped (`eur_conversion`).

Four copies lived in three services and had drifted twice — on the unconverted-amount
fallback, and on what a missing currency meant (EUR in two, USD in the inline one, and
`NativeToBase` read it as EUR too). The family tests are the source scans: no module may
define its own `_to_eur`, and none may guess a missing currency. The rest pins the rule.
"""
import ast
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.base_fx import BaseFx
from app.services.eur_conversion import to_eur_or_none
from app.services.native_amounts import NativeToBase

APP_DIR = Path(__file__).resolve().parents[1] / "app"
ON = date(2026, 3, 2)

#: `currency or "EUR"`, `t.currency or "USD"`, `a.get("currency") or "EUR"`, ...
CURRENCY_GUESS = re.compile(r"""currency["')\]]*\s+or\s+["'](?:EUR|USD|CHF)["']""")


def _code(path: Path) -> str:
    return "\n".join(line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())


def test_no_module_defines_its_own_to_eur():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_to_eur":
                offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
    assert offenders == [], f"use eur_conversion.to_eur_or_none instead of a copy: {offenders}"


def test_no_module_guesses_a_missing_currency():
    offenders = [
        f"{path.relative_to(APP_DIR)}: {m.group(0)}"
        for path in sorted(APP_DIR.rglob("*.py"))
        for m in CURRENCY_GUESS.finditer(_code(path))
    ]
    assert offenders == [], (
        f"a missing currency is unconvertible, never a guess (eur_conversion): {offenders}"
    )


class _Rates:
    def __init__(self, rate=None):
        self.rate = rate
        self.calls = 0

    async def convert_to_eur(self, amount, from_currency, target_date):
        self.calls += 1
        if self.rate is None:
            raise LookupError(f"no {from_currency} rate")
        return amount * self.rate

    async def get_exchange_rate(self, currency, on_date):
        self.calls += 1
        if self.rate is None:
            raise LookupError(f"no {currency} rate")
        return self.rate


@pytest.mark.asyncio
async def test_the_rule():
    rates = _Rates(Decimal("0.5"))
    assert await to_eur_or_none(rates, Decimal("0"), None, ON) == Decimal("0")
    assert await to_eur_or_none(rates, Decimal("10"), "EUR", ON) == Decimal("10")
    assert await to_eur_or_none(rates, Decimal("10"), "USD", ON) == Decimal("5.0")
    # No currency is not EUR and not USD: it is unconvertible, and no rate is asked for.
    calls = rates.calls
    assert await to_eur_or_none(rates, Decimal("10"), None, ON) is None
    assert await to_eur_or_none(rates, Decimal("10"), "", ON) is None
    assert rates.calls == calls
    # A failed rate is None, never the unconverted amount.
    assert await to_eur_or_none(_Rates(None), Decimal("400"), "TWD", ON) is None


@pytest.mark.asyncio
async def test_native_to_base_agrees_about_a_missing_currency():
    to_base = NativeToBase(_Rates(Decimal("0.5")), BaseFx("EUR", {}))
    assert await to_base.convert(Decimal("10"), None, ON) is None
    # Zero stays zero whatever the currency, as before.
    assert await to_base.convert(Decimal("0"), None, ON) == Decimal("0")
    assert await to_base.convert(Decimal("10"), "USD", ON) == Decimal("5.0")
