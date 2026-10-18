"""
One rule turns a foreign amount into EUR where a row can be skipped (`eur_conversion`).

Four copies lived in three services and had drifted twice — on the unconverted-amount
fallback, and on what a missing currency meant (EUR in two, USD in the inline one, and
`NativeToBase` read it as EUR too). The family tests are the source scans: no module may
define its own `_to_eur`, none may guess a missing currency, and none may feed a to-EUR
result into a second `.convert(...)` — the native->EUR->base two-step `NativeToBase` owns.
The rest pins the rule.
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


#: Calls that turn a native amount into EUR — a to-EUR result fed onward into another
#: `.convert(...)` is the native->EUR->base two-step, which `NativeToBase` owns.
EUR_SOURCES = {"to_eur_or_none", "convert_to_eur", "get_exchange_rate"}
#: The one module allowed to chain them: it *is* the two-step.
TWO_STEP_HOME = "services/native_amounts.py"


def _call_name(node: ast.AST):
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _hand_written_two_steps(source: str):
    """Line numbers where a to-EUR result reaches a `.convert(...)` in the same function.

    Taint is per function: a name bound to a to-EUR call is tainted, and so is any name
    assigned from an expression that mentions a tainted one (`cost_e = proceeds_e -
    gain_e`, `eur = amount * rate`). A `.convert(...)` call whose arguments mention a
    tainted name, or contain a to-EUR call outright, is the copy.
    """
    def mentions(expr, tainted):
        for n in ast.walk(expr):
            if isinstance(n, ast.Name) and n.id in tainted:
                return True
            if _call_name(n) in EUR_SOURCES:
                return True
        return False

    hits = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigns = [n for n in ast.walk(fn) if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))]
        tainted: set = set()
        changed = True
        while changed:
            changed = False
            for a in assigns:
                if a.value is None or not mentions(a.value, tainted):
                    continue
                targets = a.targets if isinstance(a, ast.Assign) else [a.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name) and n.id not in tainted:
                            tainted.add(n.id)
                            changed = True
        for call in ast.walk(fn):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "convert"
                    and any(mentions(arg, tainted)
                            for arg in [*call.args, *(k.value for k in call.keywords)])):
                hits.append(call.lineno)
    return sorted(set(hits))


#: The shape `tax_service`'s realized loop had until GitHub issue #46.
_THE_OLD_TAX_LOOP = '''
async def realized(self, t, base_fx):
    proceeds_e = await to_eur_or_none(self.cs, t.proceeds, t.currency, t.trade_date)
    gain_e = await to_eur_or_none(self.cs, t.realized_pnl, t.currency, t.trade_date)
    cost_e = proceeds_e - gain_e
    proceeds = base_fx.convert(proceeds_e, t.trade_date)
    cost = base_fx.convert(cost_e, t.trade_date)
    inline = base_fx.convert(await to_eur_or_none(self.cs, t.x, t.currency, d), d)
'''


def test_the_two_step_scan_sees_the_old_tax_loop():
    assert _hand_written_two_steps(_THE_OLD_TAX_LOOP) == [6, 7, 8]
    # A stored `*_eur` column projected to base is the one-step it should be.
    assert _hand_written_two_steps(
        "def f(base_fx, r):\n    return base_fx.convert(r.gross_amount_eur, r.d)\n"
    ) == []


def test_no_module_hand_writes_the_native_to_base_two_step():
    """native->EUR then EUR->base outside `NativeToBase` is a copy (GitHub issue #46).

    The copies skip its memo (one rate lookup per row instead of per (currency, date))
    and its base-currency short-circuit (a franc sale under a franc base round-tripped
    through EUR instead of being its own amount).
    """
    offenders = [
        f"{path.relative_to(APP_DIR).as_posix()}:{line}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if path.relative_to(APP_DIR).as_posix() != TWO_STEP_HOME
        for line in _hand_written_two_steps(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"convert native money through native_amounts.NativeToBase: {offenders}"


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
