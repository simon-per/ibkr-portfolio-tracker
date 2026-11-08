"""
A fourth quarter is derived from rungs of one filing vintage (review item CO6, 2026-11-08).

HON's newest 10-Q (after its 2026 Aerospace separation) restated the prior year's nine
months to continuing operations, while the year itself stood as the whole-company 10-K
reported it until the next 10-K restates it. Q4 = year less nine months mixed the two
bases: operating income 5.56B, an operating margin of 57%. The nine months now come as
the year's own filing knew them: the newest value filed on or before the year's.
Pure: HON-shaped companyfacts.
"""
from app.services.company_financials_normaliser import normalise

Q1, Q2, Q3, K, NEXT_Q3 = (
    "0000773840-25-000010", "0000773840-25-000020", "0000773840-25-000030",
    "0000773840-26-000005", "0000773840-26-000030",
)


def f(start, end, val, accn, filed, form="10-Q"):
    return {"start": start, "end": end, "val": val, "accn": accn, "filed": filed, "form": form}


def ladder(q, nine, year, restated_q3, restated_nine):
    """One concept: three quarters and the nine months as first filed, the year from the
    10-K, and the next year's Q3 10-Q restating the prior year's Q3 and nine months."""
    return [
        f("2025-01-01", "2025-03-31", q, Q1, "2025-04-25"),
        f("2025-04-01", "2025-06-30", q, Q2, "2025-07-25"),
        f("2025-01-01", "2025-06-30", 2 * q, Q2, "2025-07-25"),
        f("2025-07-01", "2025-09-30", q, Q3, "2025-10-24"),
        f("2025-01-01", "2025-09-30", nine, Q3, "2025-10-24"),
        f("2025-01-01", "2025-12-31", year, K, "2026-02-13", "10-K"),
        f("2025-07-01", "2025-09-30", restated_q3, NEXT_Q3, "2026-10-23"),
        f("2025-01-01", "2025-09-30", restated_nine, NEXT_Q3, "2026-10-23"),
    ]


facts = {"cik": 773840, "entityName": "Honeywell", "facts": {"us-gaap": {
    "Revenues": {"units": {"USD": ladder(9e9, 27e9, 37e9, 5e9, 19e9)}},
    "OperatingIncomeLoss": {"units": {"USD": ladder(2e9, 6e9, 8e9, 0.9e9, 4.5e9)}},
}}}


def _q4(metric):
    (row,) = [
        r for r in normalise(facts).rows
        if r["metric"] == metric and r["fiscal_period"] == "Q4"
    ]
    return row


def test_the_fourth_quarter_is_the_year_less_its_own_nine_months():
    assert _q4("revenue")["value"] == 10e9  # 37 - 27, not 37 - 19
    assert _q4("operating_income")["value"] == 2e9  # 8 - 6, not 8 - 4.5
    # Filed when the later of its two rungs was: the 10-K.
    assert str(_q4("revenue")["filed"]) == "2026-02-13"


def test_a_restated_quarter_still_wins_where_it_is_reported():
    rows = {
        (r["fiscal_period"], r["metric"]): r["value"] for r in normalise(facts).rows
    }
    assert rows[("Q3", "revenue")] == 5e9
