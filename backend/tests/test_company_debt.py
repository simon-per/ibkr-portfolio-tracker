"""
A company's debt: the tagged total, else its halves, and the halves when the "total" is
smaller than they are (review item CO7, 2026-11-08: AMD's FY2021 year end read 1M against
313M of notes, so the next year's debt grew "+246,600%"). Pure.
"""
from app.services.company_financials_normaliser import normalise
from app.services.company_financials_service import _debt


def test_a_total_smaller_than_its_halves_is_one_of_them():
    assert _debt({"debt_total": 1.0, "debt_noncurrent": 312.0, "debt_current": 1.0}) == 313.0


def test_a_total_at_least_its_halves_stands():
    assert _debt({"debt_total": 400.0, "debt_noncurrent": 312.0, "debt_current": 1.0}) == 400.0
    assert _debt({"debt_total": 313.0, "debt_noncurrent": 312.0, "debt_current": 1.0}) == 313.0


def test_debt_counts_all_borrowings():
    # Walmart: noncurrent, its current maturities and short-term borrowings.
    assert _debt({"debt_noncurrent": 34_624.0, "debt_current": 3_542.0,
                  "current_maturities": 3_542.0, "short_term_borrowings": 6_596.0}) == 44_762.0
    # Exxon: a filer's total of everything current beside the noncurrent debt.
    assert _debt({"debt_noncurrent": 34_241.0, "debt_current": 6_200.0,
                  "current_maturities": 6_200.0, "debt_current_total": 9_296.0}) == 43_537.0
    # Linde: the long-term total, its current part included, plus short-term debt.
    assert _debt({"debt_total": 22_479.0, "short_term_borrowings": 4_510.0}) == 26_989.0
    # Short-term borrowings that hold the current maturities count once.
    assert _debt({"debt_total": 100.0, "debt_noncurrent": 90.0, "current_maturities": 10.0,
                  "short_term_borrowings": 25.0, "debt_current_total": 25.0}) == 115.0
    # One line tagged both as short-term borrowings and as current maturities counts once.
    assert _debt({"debt_total": 61_259.0, "debt_noncurrent": 54_836.0,
                  "current_maturities": 6_424.0, "short_term_borrowings": 6_424.0}) == 61_260.0
    # Rows from before these lines keep the term debt.
    assert _debt({"debt_noncurrent": 10.0, "debt_current": 2.0}) == 12.0


def _year(val):
    return [{"end": "2025-12-31", "val": val, "accn": "a-25", "filed": "2026-02-20",
             "form": "10-K", "fy": 2025, "fp": "FY"}]


def _doc(**instants):
    gaap = {"Revenues": {"units": {"USD": [{**_year(1e9)[0], "start": "2025-01-01"}]}}}
    for concept, (unit, val) in instants.items():
        gaap[concept] = {"units": {unit: _year(val)}}
    return {"entityName": "X", "facts": {"us-gaap": gaap}}


def test_an_inclusive_total_tagged_again_as_noncurrent_is_not_a_half():
    # Chevron tags one 39,781 as both, beside 2,345 of current maturities inside it.
    rows = normalise(_doc(
        LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities=("USD", 39_781.0),
        LongTermDebtAndCapitalLeaseObligations=("USD", 39_781.0),
        LongTermDebtCurrent=("USD", 2_345.0),
        ShortTermBorrowings=("USD", 977.0),
    )).rows
    base = {r["metric"]: r["value"] for r in rows if r["fiscal_period"] == "FY"}
    assert "debt_noncurrent" not in base
    assert _debt(base) == 40_758.0


def _with_cover(own, cover, diluted):
    doc = _doc(CommonStockSharesOutstanding=("shares", own))
    doc["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
        "units": {"shares": [{**_year(diluted)[0], "start": "2025-01-01"}]}}
    doc["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {**_year(cover)[0], "end": "2026-02-10"}]}}}
    return [r["value"] for r in normalise(doc).rows if r["metric"] == "shares_outstanding"]


def test_a_balance_sheet_count_the_cover_and_diluted_count_contradict():
    # Disney's 1,900m issued, tagged as outstanding, beside 1,708m on the cover.
    assert _with_cover(1_900e6, 1_708e6, 1_760e6) == [1_708e6]
    # Within a few percent the balance sheet's stands (Netflix's).
    assert _with_cover(4_222e6, 4_200e6, 4_300e6) == [4_222e6]
    # A split after the year end leaves the cover far off: the balance sheet's stands.
    assert _with_cover(100e6, 400e6, 101e6) == [100e6]


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
