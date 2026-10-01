"""
A cash flow already in the base currency is its own amount, never `amount_eur` projected back.

Found 2026-10-01: a CHF 4,500.00 withdrawal was stored as EUR 4,766.30 (a stale fallback
CHF->EUR rate) and projected back at that day's EUR->CHF rate, so the activity ledger,
Money In and the cash line all read CHF 4,517.50 — on the figure the owner checks against
the bank. Four readers each converted `amount_eur` inline; `cash_flow_in_base` is the one
place now, and the family test below fails the day a fifth copy appears.
"""
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.services.native_amounts import cash_flow_in_base
from app.services.portfolio_service import BaseFx

DAY = date(2026, 9, 28)
CHF_BASE = BaseFx("CHF", {DAY: Decimal("0.9464")})


def _flow(amount, currency, amount_eur):
    return SimpleNamespace(amount=Decimal(amount), currency=currency,
                           amount_eur=Decimal(amount_eur), flow_date=DAY)


def test_a_franc_flow_under_a_franc_base_is_exact_whatever_the_stored_rate():
    withdrawal = _flow("-4500", "CHF", "-4766.30")   # the stale-rate EUR figure
    assert cash_flow_in_base(withdrawal, CHF_BASE) == Decimal("-4500")


def test_a_foreign_flow_is_still_projected_from_its_eur_amount():
    deposit = _flow("7000", "EUR", "7000")
    assert cash_flow_in_base(deposit, CHF_BASE) == Decimal("7000") * Decimal("0.9464")


def test_under_a_eur_base_the_stored_eur_amount_is_the_answer():
    withdrawal = _flow("-4500", "CHF", "-4766.30")
    assert cash_flow_in_base(withdrawal, BaseFx("EUR", {})) == Decimal("-4766.30")


def test_no_reader_projects_a_cash_flow_amount_eur_inline():
    app = Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(r"convert\([^)]*\b\w+\.amount_eur\b")
    offenders = [
        f"{path.relative_to(app)}:{n}"
        for path in app.rglob("*.py") if path.name != "native_amounts.py"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], f"use cash_flow_in_base instead: {offenders}"
