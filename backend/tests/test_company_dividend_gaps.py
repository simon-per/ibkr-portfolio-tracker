"""
The dividend-per-share gaps closed 2026-11-19 (docs/company-financials.md, *Durations*): a first
quarter nothing tags (CAT), a declared-basis ladder in its year in progress, and a fiscal year
tagged only on its payment dates (ACN's FY2026 10-K, DIS). Offline, synthetic companyfacts.
"""

from datetime import date

import pytest

from app.services.company_financials_normaliser import normalise
from tests.test_company_financials_normaliser import _doc, _f, _row

DPS = "dividends_per_share"


def _q(rows, end, period, metric=DPS):
    found = _row(rows, metric, end, period)
    return found["value"] if found else None


def test_a_first_quarter_without_a_declaration_is_the_half_year_less_the_second():
    """CAT declares two dividends in June and two in December, so its Q1 10-Q tags none: 2.92
    at six months beside the second quarter's 2.92 is a first quarter of 0. Its 2026, a year
    in progress with no 10-K to show the nine months is no rate, reads as 2025 did."""
    rows = normalise(
        _doc(
            {
                "CommonStockDividendsPerShareDeclared": {
                    "units": {
                        "USD/shares": [
                            _f("2025-04-01", "2025-06-30", 2.92, "2025-08-06"),
                            _f("2025-01-01", "2025-06-30", 2.92, "2025-08-06"),
                            _f("2025-01-01", "2025-09-30", 2.92, "2025-11-03"),
                            _f(
                                "2025-01-01",
                                "2025-12-31",
                                5.94,
                                "2026-02-13",
                                form="10-K",
                            ),
                            _f("2026-04-01", "2026-06-30", 3.14, "2026-08-05"),
                            _f("2026-01-01", "2026-06-30", 3.14, "2026-08-05"),
                            _f("2026-01-01", "2026-09-30", 3.14, "2026-11-04"),
                        ]
                    }
                }
            }
        )
    ).rows
    year_2025 = [
        _q(rows, date(2025, 3, 31), "Q1"),
        _q(rows, date(2025, 6, 30), "Q2"),
        _q(rows, date(2025, 9, 30), "Q3"),
        _q(rows, date(2025, 12, 31), "Q4"),
    ]
    assert year_2025 == [0.0, 2.92, 0.0, pytest.approx(3.02)]
    assert [
        _q(rows, date(2026, 3, 31), "Q1"),
        _q(rows, date(2026, 6, 30), "Q2"),
        _q(rows, date(2026, 9, 30), "Q3"),
    ] == [0.0, 3.14, 0.0]


def test_a_flat_rate_after_a_flat_rate_still_derives_no_zero():
    """IBKR tagged 0.10 on every rung for years: no year adds up, so none lends the next its
    reading, and no first quarter of 0 appears beside a half-year of the rate."""
    facts = []
    for year in (2017, 2018):
        facts += [
            _f(f"{year}-04-01", f"{year}-06-30", 0.10, f"{year}-08-01"),
            _f(f"{year}-01-01", f"{year}-06-30", 0.10, f"{year}-08-01"),
            _f(f"{year}-01-01", f"{year}-09-30", 0.10, f"{year}-11-01"),
        ]
    rows = normalise(
        _doc({"CommonStockDividendsPerShareDeclared": {"units": {"USD/shares": facts}}})
    ).rows
    for year in (2017, 2018):
        assert _q(rows, date(year, 3, 31), "Q1") is None
        assert _q(rows, date(year, 9, 30), "Q3") is None


def test_a_first_quarter_of_a_flow_is_kept_only_where_its_year_adds_up():
    """Revenue's first quarter, untagged, is the half-year less the second; never an EPS,
    whose half-year is no sum of its quarters."""

    def doc(year_total):
        return _doc(
            {
                "Revenues": {
                    "units": {
                        "USD": [
                            _f("2025-04-01", "2025-06-30", 120, "2025-08-01"),
                            _f("2025-01-01", "2025-06-30", 220, "2025-08-01"),
                            _f("2025-07-01", "2025-09-30", 130, "2025-11-01"),
                            _f("2025-01-01", "2025-09-30", 350, "2025-11-01"),
                            _f(
                                "2025-10-01",
                                "2025-12-31",
                                140,
                                "2026-02-01",
                                form="10-K",
                            ),
                            _f(
                                "2025-01-01",
                                "2025-12-31",
                                year_total,
                                "2026-02-01",
                                form="10-K",
                            ),
                        ]
                    }
                },
                "EarningsPerShareDiluted": {
                    "units": {
                        "USD/shares": [
                            _f("2025-04-01", "2025-06-30", 1.2, "2025-08-01"),
                            _f("2025-01-01", "2025-06-30", 2.2, "2025-08-01"),
                        ]
                    }
                },
            }
        )

    rows = normalise(doc(490)).rows
    assert _q(rows, date(2025, 3, 31), "Q1", "revenue") == 100
    assert _q(rows, date(2025, 3, 31), "Q1", "eps_diluted") is None
    # A year the quarters do not add up to (a half-year restated beside a second quarter that
    # was not): the derived first quarter goes.
    rows = normalise(doc(520)).rows
    assert _q(rows, date(2025, 3, 31), "Q1", "revenue") is None


def _payments_doc(paid_total):
    """ACN's FY2026: three quarters reported, a 10-K tagging 1.63 on each payment date and no
    year, beside the year's dividends paid and diluted count."""
    fy = ("2025-09-01", "2026-08-31")
    cash_paid = [
        _f("2025-09-01", "2025-11-30", 1.63, "2025-12-18"),
        _f("2025-12-01", "2026-02-28", 1.63, "2026-03-19"),
        _f("2026-03-01", "2026-05-31", 1.63, "2026-06-18"),
    ] + [
        _f(None, day, 1.63, "2026-10-16", form="10-K")
        for day in ("2025-11-14", "2026-02-13", "2026-05-15", "2026-08-14")
    ]
    return _doc(
        {
            "CommonStockDividendsPerShareCashPaid": {
                "units": {"USD/shares": cash_paid}
            },
            "PaymentsOfDividendsCommonStock": {
                "units": {"USD": [_f(*fy, paid_total, "2026-10-16", form="10-K")]}
            },
            "WeightedAverageNumberOfDilutedSharesOutstanding": {
                "units": {"shares": [_f(*fy, 625e6, "2026-10-16", form="10-K")]}
            },
        }
    )


def test_a_year_tagged_on_its_payment_dates_is_their_sum():
    rows = normalise(_payments_doc(4.0e9)).rows  # 6.40 a share: within 10% of 6.52
    assert _q(rows, date(2026, 8, 31), "FY") == pytest.approx(6.52)
    assert _q(rows, date(2026, 8, 31), "Q4") == pytest.approx(1.63)
    assert _q(rows, date(2025, 11, 30), "Q1") == 1.63  # reported, untouched


def test_payment_dates_the_money_paid_does_not_vouch_for_fill_nothing():
    rows = normalise(_payments_doc(2.0e9)).rows  # 3.20 a share: a payment is missing
    assert _q(rows, date(2026, 8, 31), "FY") is None
    assert _q(rows, date(2026, 8, 31), "Q4") is None
