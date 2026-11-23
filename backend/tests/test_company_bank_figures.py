"""
A bank's own figures (2026-11-16): the KPI set the classification's `bank` model reads.

Measured over the 132 companies: BAC, JPM, SOFI and IBKR tag net interest income, non-interest
income and expense in all of their last eight quarters, the three banks deposits and loans; a
credit-loss provision only JPM lately; IBKR, a broker, no deposits or loans. Adding the chains
added rows only — no figure that existed moved. Synthetic documents, offline.
"""

from datetime import date

import pytest

from app.services.company_financials_normaliser import normalise
from app.services.company_financials_service import _whole_company_count, build_series
from tests.test_company_financials_service import _quarter_ends, _rows


def _f(start, end, val, filed="2025-02-20", form="10-K"):
    fact = {"end": end, "val": val, "filed": filed, "form": form, "accn": f"a-{filed}"}
    if start:
        fact["start"] = start
    return fact


def test_the_chains_read_a_banks_lines():
    gaap = {
        "RevenuesNetOfInterestExpense": {"units": {"USD": [_f("2024-01-01", "2024-12-31", 100e9)]}},
        "InterestIncomeExpenseNet": {"units": {"USD": [_f("2024-01-01", "2024-12-31", 60e9)]}},
        "NoninterestIncome": {"units": {"USD": [_f("2024-01-01", "2024-12-31", 40e9)]}},
        "NoninterestExpense": {"units": {"USD": [_f("2024-01-01", "2024-12-31", 55e9)]}},
        "ProvisionForLoanLeaseAndOtherLosses": {"units": {"USD": [_f("2024-01-01", "2024-12-31", 9e9)]}},
        "Deposits": {"units": {"USD": [_f(None, "2024-12-31", 2.4e12)]}},
        "FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss": {
            "units": {"USD": [_f(None, "2024-12-31", 1.3e12)]}},
    }
    rows = {r["metric"]: r["value"] for r in normalise({"facts": {"us-gaap": gaap}}).rows
            if r["fiscal_period"] == "FY"}
    assert rows["net_interest_income"] == 60e9 and rows["noninterest_income"] == 40e9
    assert rows["noninterest_expense"] == 55e9 and rows["credit_loss_provision"] == 9e9
    assert rows["deposits"] == 2.4e12 and rows["loans"] == 1.3e12


BANK = {
    "revenue": lambda i: 25.0,
    "net_income": lambda i: 5.0,
    "net_interest_income": lambda i: 15.0,
    "noninterest_income": lambda i: 10.0,
    "noninterest_expense": lambda i: 15.0,
    "deposits": lambda i: 2000.0 + i,
    "loans": lambda i: 1300.0,
    "equity": lambda i: 300.0,
    "shares_outstanding": lambda i: 10.0,
}


def test_book_value_needs_a_count_of_the_whole_company():
    # BHP's cover page counts the Limited line alone, beside the dual-listed group's equity.
    assert _whole_company_count(2_945e6, 5_069e6) is None
    assert _whole_company_count(5_062e6, 5_069e6) == 5_062e6
    assert _whole_company_count(5_062e6, None) == 5_062e6


def test_the_service_derives_the_efficiency_ratio_and_book_value_per_share():
    series = build_series(_rows(_quarter_ends(date(2024, 3, 31), 8), BANK), "ttm", "max")
    last = {name: series[name]["points"][-1]["value"]
            for name in ("net_interest_income", "efficiency_ratio", "deposits",
                         "book_value_per_share")}
    # TTM: four quarters' flows summed, the newest balance.
    assert last["net_interest_income"] == pytest.approx(60.0)
    assert last["efficiency_ratio"] == pytest.approx(60.0 / 100.0)
    assert last["deposits"] == pytest.approx(2007.0)
    assert last["book_value_per_share"] == pytest.approx(30.0)
    # A ratio has no growth rate.
    assert series["efficiency_ratio"]["cagr"] is None


def test_without_either_part_there_is_no_efficiency_ratio():
    rows = _rows(_quarter_ends(date(2024, 3, 31), 4),
                 {k: v for k, v in BANK.items() if k != "noninterest_income"})
    series = build_series(rows, "quarterly", "max")
    assert {p["value"] for p in series["efficiency_ratio"]["points"]} == {None}
