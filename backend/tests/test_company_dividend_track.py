"""
A held company's own dividend record from its stored filings (review item FD4,
2026-11-08): the 5-year growth of its fiscal years' dividends per share, its raise streak
and its payout ratio, for the Dividends table's By position. Pure DB.
"""

from datetime import date

from app.clock import utcnow
from app.models.company_financials import CompanyFinancial
from app.services.company_financials_service import dividend_track, raise_streak


def _years(*pairs):
    return [(date(year, 12, 31), value) for year, value in pairs]


def test_the_streak_counts_years_that_paid_more_than_the_year_before():
    assert raise_streak(_years((2020, 1.0), (2021, 1.1), (2022, 1.2), (2023, 1.3))) == 3


def test_a_flat_year_a_cut_or_a_missing_year_ends_it():
    assert raise_streak(_years((2020, 1.0), (2021, 1.1), (2022, 1.1), (2023, 1.2))) == 1
    assert raise_streak(_years((2020, 1.0), (2021, 1.2), (2022, 0.9), (2023, 1.0))) == 1
    assert raise_streak(_years((2020, 1.0), (2021, None), (2022, 1.2), (2023, 1.3))) == 1
    # A gap year with no row at all: 2021 to 2023 is not a year apart.
    assert raise_streak(_years((2019, 0.9), (2021, 1.0), (2023, 1.3))) == 0


def test_a_company_that_paid_nothing_has_no_streak_rather_than_a_streak_of_zero():
    assert raise_streak(_years((2022, 1.0), (2023, None))) is None
    assert raise_streak(_years((2022, 1.0), (2023, 0.0))) is None
    assert raise_streak([]) is None
    # It paid, and no more than before: a measured zero.
    assert raise_streak(_years((2022, 1.0), (2023, 1.0))) == 0


def _row(year, fiscal_period, metric, value, end=None):
    return CompanyFinancial(
        cik=1, period_end=end or date(year, 12, 31), fiscal_year=year,
        fiscal_period=fiscal_period, metric=metric, value=value, unit="USD",
        source_concept="X", filed=date(year + 1, 2, 1), fetched_at=utcnow(),
    )


def test_the_track_reads_the_fiscal_years_and_the_ttm_payout():
    rows = [_row(2018 + i, "FY", "dividends_per_share", dps)
            for i, dps in enumerate((1.00, 1.10, 1.21, 1.33, 1.46, 1.61))]
    # The newest four quarters: 0.42 a quarter against 1.20 of EPS.
    for q, end in enumerate((date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30),
                             date(2023, 12, 31)), start=1):
        rows += [_row(2023, f"Q{q}", "dividends_per_share", 0.42, end),
                 _row(2023, f"Q{q}", "eps_diluted", 1.20, end)]
    track = dividend_track(rows)
    assert abs(track["dps_cagr_5y"] - ((1.61 / 1.00) ** (1 / 5) - 1)) < 1e-9
    assert track["raise_years"] == 5
    assert abs(track["payout_ratio"] - 0.35) < 1e-9


def test_a_company_without_dividends_has_no_track():
    rows = [_row(2022, "FY", "revenue", 100.0), _row(2023, "FY", "revenue", 110.0)]
    assert dividend_track(rows) == {
        "dps_cagr_5y": None, "raise_years": None, "payout_ratio": None,
    }
