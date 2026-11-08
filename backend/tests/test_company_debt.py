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
