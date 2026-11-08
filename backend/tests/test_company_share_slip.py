"""
A share count tagged in thousands in one filing is rescaled before any quarter is derived
from it (review item CO4, 2026-11-08).

ONDS tags its 10-K counts in thousands and its 10-Qs in shares. The NI/EPS slip check ran
on the finished values, after Q4 had been derived from the year still in thousands —
negative, so dropped — and every TTM window since broke on the missing quarter (P/S,
revenue and FCF per share blank since Q3 FY2023). Pure: the reviewers' ONDS-shaped facts.
"""
from app.services.company_financials_normaliser import normalise

Q1, Q2, Q3, K = "0001-24-000001", "0001-24-000002", "0001-24-000003", "0001-25-000004"


def f(start, end, val, accn, filed, form="10-Q"):
    return {"start": start, "end": end, "val": val, "accn": accn, "filed": filed, "form": form,
            "fy": 2024, "fp": "Q"}


facts = {"cik": 1, "entityName": "X", "facts": {"us-gaap": {
    "Revenues": {"units": {"USD": [
        f("2024-01-01", "2024-03-31", 10e6, Q1, "2024-05-10"),
        f("2024-04-01", "2024-06-30", 11e6, Q2, "2024-08-10"),
        f("2024-01-01", "2024-06-30", 21e6, Q2, "2024-08-10"),
        f("2024-07-01", "2024-09-30", 12e6, Q3, "2024-11-10"),
        f("2024-01-01", "2024-09-30", 33e6, Q3, "2024-11-10"),
        f("2024-01-01", "2024-12-31", 46e6, K, "2025-03-20", "10-K"),
    ]}},
    "NetIncomeLoss": {"units": {"USD": [
        f("2024-01-01", "2024-03-31", -10e6, Q1, "2024-05-10"),
        f("2024-04-01", "2024-06-30", -10e6, Q2, "2024-08-10"),
        f("2024-01-01", "2024-06-30", -20e6, Q2, "2024-08-10"),
        f("2024-07-01", "2024-09-30", -10e6, Q3, "2024-11-10"),
        f("2024-01-01", "2024-09-30", -30e6, Q3, "2024-11-10"),
        f("2024-01-01", "2024-12-31", -40e6, K, "2025-03-20", "10-K"),
    ]}},
    "EarningsPerShareDiluted": {"units": {"USD/shares": [
        f("2024-01-01", "2024-03-31", -0.17, Q1, "2024-05-10"),
        f("2024-04-01", "2024-06-30", -0.15, Q2, "2024-08-10"),
        f("2024-01-01", "2024-06-30", -0.32, Q2, "2024-08-10"),
        f("2024-07-01", "2024-09-30", -0.14, Q3, "2024-11-10"),
        f("2024-01-01", "2024-09-30", -0.46, Q3, "2024-11-10"),
        f("2024-01-01", "2024-12-31", -0.57, K, "2025-03-20", "10-K"),
    ]}},
    "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [
        f("2024-01-01", "2024-03-31", 60e6, Q1, "2024-05-10"),
        f("2024-04-01", "2024-06-30", 66e6, Q2, "2024-08-10"),
        f("2024-01-01", "2024-06-30", 63e6, Q2, "2024-08-10"),
        f("2024-07-01", "2024-09-30", 71e6, Q3, "2024-11-10"),
        f("2024-01-01", "2024-09-30", 65.7e6, Q3, "2024-11-10"),
        # the 10-K's year in thousands (70,000 = 70.0M shares)
        f("2024-01-01", "2024-12-31", 70_000, K, "2025-03-20", "10-K"),
    ]}},
}}}



def test_the_fourth_quarter_count_survives_a_year_tagged_in_thousands():
    rows = {(r["fiscal_period"], r["metric"]): r["value"] for r in normalise(facts).rows}
    assert rows[("FY", "diluted_shares")] == 70e6
    # 4 x 70.0M less 3 x 65.7M is not this exactly (the ladder weighs by days), but it is
    # a count between the third quarter's and well above it, not absent.
    q4 = rows[("Q4", "diluted_shares")]
    assert 71e6 < q4 < 90e6
