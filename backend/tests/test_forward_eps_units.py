"""
A forward P/E, and the forward-EPS growth fallback, only where forward EPS is in the
trailing EPS's units (review item Y5 = VI3, 2026-11-08).

KSPI's forward EPS is in tenge against a dollar trailing EPS, so Yahoo's forward P/E read
0.0135 and scored the watchlist's top points; SBI.TO's is a 0.01 placeholder, forward P/E
359 and forward EPS growth -99%. Their ratio outside 1/20-20 refuses both, in the one
reader the fundamentals passes and the watchlist share. Pure.
"""
import ast
from pathlib import Path

from app.services.fundamentals_service import extract_metrics
from app.services.yahoo_estimates import forward_eps_in_units, forward_growth, forward_pe

APP = Path(__file__).resolve().parent.parent / "app"

KSPI = {"forwardPE": 0.0135, "forwardEps": 7351.2, "trailingEps": 9.84,
        "quoteType": "EQUITY", "currency": "USD"}
SBI = {"forwardPE": 359.0, "forwardEps": 0.01, "trailingEps": 1.19,
       "quoteType": "EQUITY", "currency": "CAD"}
NVDA = {"forwardPE": 26.1, "forwardEps": 7.15, "trailingEps": 4.04,
        "quoteType": "EQUITY", "currency": "USD"}


def test_two_units_or_a_placeholder_have_no_forward_pe():
    assert forward_pe(KSPI) is None and forward_pe(SBI) is None
    assert forward_pe(NVDA) == 26.1
    assert extract_metrics(KSPI, symbol="KSPI")["forward_pe"] is None
    assert extract_metrics(SBI, symbol="SBI.TO")["forward_pe"] is None


def test_nor_a_forward_growth_off_them():
    assert forward_growth(None, None, SBI) == (None, None)
    assert forward_growth(None, None, KSPI) == (None, None)
    assert forward_growth(None, None, NVDA)[1] > 0
    # The consensus growth, when there is one, is not the fallback and stands.
    assert forward_growth(None, 0.2, SBI) == (None, 0.2)


def test_nothing_to_judge_is_not_a_refusal():
    assert forward_eps_in_units({"forwardPE": 20.0}) is True
    assert forward_eps_in_units({"forwardEps": 1.0, "trailingEps": 0}) is True
    # A swing from a loss is judged by size: -1 to 2 is a company, not two currencies.
    assert forward_eps_in_units({"forwardEps": 2.0, "trailingEps": -1.0}) is True


def test_only_the_shared_reader_reads_forward_pe_off_info():
    offenders = [
        f"{path.relative_to(APP)}:{node.lineno}"
        for path in APP.rglob("*.py") if path.name != "yahoo_estimates.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and node.value == "forwardPE"
    ]
    assert offenders == []
