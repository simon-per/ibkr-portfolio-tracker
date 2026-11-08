"""
Annual figures for the 20-F filers (review item R6, 2026-11-08).

Read on the server that day, one companyfacts request each: ASML files its 20-F in US GAAP
(623 us-gaap concepts, no ifrs-full), NU and TSMC in IFRS (`ifrs-full`, annual, USD and TWD).
The normaliser kept 10-K/10-Q facts only and read us-gaap only, so all three were misses.
Now the 20-F is an annual report, an IFRS filer's concepts are read under their US-GAAP
twins, and an annual-only filer's TTM view and key figures are its fiscal years. Pure.
"""

from datetime import date

from app.clock import utcnow
from app.models.company_financials import CompanyFinancial
from app.services.company_financials_normaliser import IFRS_TWINS, METRICS, normalise
from app.services.company_financials_service import build_series, key_stats


def _f(end, val, accn, filed, start=None):
    row = {
        "end": end,
        "val": val,
        "accn": accn,
        "filed": filed,
        "form": "20-F",
        "fy": int(end[:4]),
        "fp": "FY",
    }
    if start:
        row["start"] = start
    return row


def _year(y, val):
    accn, filed = f"nu-{y}", f"{y + 1}-04-15"
    return _f(f"{y}-12-31", val, accn, filed, start=f"{y}-01-01")


NU = {
    "cik": 1691493,
    "entityName": "Nu Holdings Ltd.",
    "facts": {
        "ifrs-full": {
            "Revenue": {"units": {"USD": [_year(2023, 8.0e9), _year(2024, 11.5e9)]}},
            "ProfitLossAttributableToOwnersOfParent": {
                "units": {"USD": [_year(2023, 1.0e9), _year(2024, 2.0e9)]}
            },
            "DilutedEarningsLossPerShare": {
                "units": {"USD/shares": [_year(2023, 0.21), _year(2024, 0.41)]}
            },
            "AdjustedWeightedAverageShares": {
                "units": {"shares": [_year(2023, 4.9e9), _year(2024, 4.9e9)]}
            },
            "CashAndCashEquivalents": {
                "units": {"USD": [_f("2024-12-31", 7.0e9, "nu-2024", "2025-04-15")]}
            },
            # Not a twin of any chain concept: never read.
            "Borrowings": {
                "units": {"USD": [_f("2024-12-31", 1.0e9, "nu-2024", "2025-04-15")]}
            },
        }
    },
}


def _rows(doc):
    return {
        (r["fiscal_year"], r["fiscal_period"], r["metric"]): r
        for r in normalise(doc).rows
    }


def test_an_ifrs_20f_filer_yields_its_years():
    rows = _rows(NU)
    assert rows[(2024, "FY", "revenue")]["value"] == 11.5e9
    assert rows[(2024, "FY", "net_income")]["value"] == 2.0e9
    assert rows[(2024, "FY", "eps_diluted")]["value"] == 0.41
    assert rows[(2024, "FY", "diluted_shares")]["value"] == 4.9e9
    assert rows[(2024, "FY", "cash")]["value"] == 7.0e9
    assert all(r["fiscal_period"] == "FY" for r in normalise(NU).rows)
    assert normalise(NU).currency == "USD"


def test_every_twin_is_a_concept_a_chain_reads():
    chained = {c for spec in METRICS.values() for c in spec.concepts}
    assert set(IFRS_TWINS.values()) <= chained


def test_a_us_gaap_20f_filer_yields_its_years():
    asml = {
        "cik": 937966,
        "entityName": "ASML HOLDING NV",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "EUR": [
                            _f(
                                "2024-12-31",
                                28.3e9,
                                "asml-2024",
                                "2025-02-12",
                                start="2024-01-01",
                            ),
                        ]
                    }
                },
            }
        },
    }
    assert _rows(asml)[(2024, "FY", "revenue")]["value"] == 28.3e9


def test_a_filer_with_both_taxonomies_is_read_in_us_gaap_alone():
    both = {
        "cik": 1,
        "entityName": "X",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _f(
                                "2024-12-31",
                                5.0e9,
                                "x-2024",
                                "2025-02-01",
                                start="2024-01-01",
                            ),
                        ]
                    }
                }
            },
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "USD": [
                            _f(
                                "2023-12-31",
                                4.0e9,
                                "x-2023",
                                "2024-02-01",
                                start="2023-01-01",
                            ),
                        ]
                    }
                }
            },
        },
    }
    assert {(y, m) for y, _, m in _rows(both)} == {(2024, "revenue")}


def _stored(doc):
    return [
        CompanyFinancial(cik=1, fetched_at=utcnow(), **row)
        for row in normalise(doc).rows
    ]


def test_an_annual_filers_ttm_and_key_figures_are_its_years():
    rows = _stored(NU)
    ttm = build_series(rows, "ttm", "max")["revenue"]["points"]
    assert [(p["label"], p["value"]) for p in ttm] == [
        ("FY2023", 8.0e9),
        ("FY2024", 11.5e9),
    ]
    stats = key_stats(rows)
    assert (stats["label"], stats["revenue"], stats["eps_diluted"]) == (
        "FY2024",
        11.5e9,
        0.41,
    )
    assert stats["period_end"] == date(2024, 12, 31).isoformat()
    # A quarterly view has no quarters to show, and says nothing rather than repeating years.
    assert build_series(rows, "quarterly", "max")["revenue"]["points"] == []
