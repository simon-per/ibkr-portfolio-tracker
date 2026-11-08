"""
A company's debt: the tagged total, else its halves, and the halves when the "total" is
smaller than they are (review item CO7, 2026-11-08: AMD's FY2021 year end read 1M against
313M of notes, so the next year's debt grew "+246,600%"). Pure.
"""
from app.services.company_financials_service import _debt


def test_a_total_smaller_than_its_halves_is_one_of_them():
    assert _debt({"debt_total": 1.0, "debt_noncurrent": 312.0, "debt_current": 1.0}) == 313.0


def test_a_total_at_least_its_halves_stands():
    assert _debt({"debt_total": 400.0, "debt_noncurrent": 312.0, "debt_current": 1.0}) == 400.0
    assert _debt({"debt_total": 313.0, "debt_noncurrent": 312.0, "debt_current": 1.0}) == 313.0


def test_the_old_rules_hold():
    assert _debt({"debt_total": 0.0, "debt_noncurrent": 5.0, "debt_current": 1.0}) == 6.0
    assert _debt({"debt_total": 0.0}) is None
    assert _debt({"debt_noncurrent": 5.0}) is None
    assert _debt({"debt_total": 7.0}) == 7.0


def test_the_cash_and_debt_card_adds_up():
    """Review item VI1: net cash counts short-term investments the card never showed, so
    AMD read cash 5.1B, debt 3.2B, net cash 9.9B. `investments` is served beside them."""
    from datetime import date

    from app.clock import utcnow
    from app.models.company_financials import CompanyFinancial
    from app.services.company_financials_service import build_series

    def row(metric, value):
        return CompanyFinancial(
            cik=1, period_end=date(2025, 12, 27), fiscal_year=2025, fiscal_period="FY",
            metric=metric, value=value, unit="USD", source_concept="X",
            filed=date(2026, 2, 1), fetched_at=utcnow(),
        )

    rows = [row("cash", 5.1), row("short_term_investments", 8.0), row("debt_total", 3.2)]
    series = build_series(rows, "annual", "max")
    point = {k: series[k]["points"][-1]["value"] for k in ("cash", "investments", "debt", "net_cash")}
    assert point["cash"] + point["investments"] - point["debt"] == point["net_cash"]
    assert point["investments"] == 8.0
