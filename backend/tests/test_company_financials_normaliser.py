"""
The EDGAR normaliser (docs/company-financials.md), against NVIDIA's real companyfacts.

`tests/fixtures/edgar_nvda_companyfacts.json` is SEC public data, trimmed to the concepts
`company_financials_normaliser.METRICS` reads and to periods ending from 2015 on. It holds
every shape the rules exist for: 10-Q cash-flow items as year-to-date figures, Q4 only as a
10-K year, restated EPS, the 4:1 split of July 2021 and the 10:1 of June 2024, a fiscal year
ending late January, a concept switch (capex moved to `PaymentsToAcquireProductiveAssets`),
a 10-K that tagged one quarter's dividend as the year's, and capex absent for years.

The small synthetic documents below pin what NVDA does not show: a share count tagged in
thousands, a quarter context carrying the year's figure, a year and its quarters under two
concepts, the shares-from-EPS fallback, and an IFRS filer. Offline: no network.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.services.company_financials_normaliser import (
    SHARES_FROM_EPS_SOURCE,
    _facts,
    adjustments,
    fiscal_year_name,
    normalise,
)

FIXTURE = Path(__file__).parent / "fixtures" / "edgar_nvda_companyfacts.json"


@pytest.fixture(scope="module")
def raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(raw):
    return normalise(raw).rows


def _row(rows, metric, end, period):
    found = [
        r
        for r in rows
        if r["metric"] == metric
        and r["period_end"] == end
        and r["fiscal_period"] == period
    ]
    assert len(found) <= 1
    return found[0] if found else None


def _fact(raw, concept, start, end, filed=None):
    """The raw value of one (start, end) in the fixture — the latest filing, or `filed`'s."""
    facts = [
        f
        for unit in raw["facts"]["us-gaap"][concept]["units"].values()
        for f in unit
        if f.get("start") == start
        and f["end"] == end
        and (filed is None or f["filed"] == filed)
    ]
    assert facts, (concept, start, end)
    return max(facts, key=lambda f: f["filed"])["val"]


def test_the_fixture_is_trimmed_and_carries_no_one_but_the_company(raw):
    assert FIXTURE.stat().st_size < 300 * 1024
    assert set(raw) == {"cik", "entityName", "facts"}
    assert set(raw["facts"]) == {"us-gaap"}


def test_a_10q_cash_flow_quarter_is_the_difference_of_two_ytd_rungs(raw, rows):
    """NVDA files operating cash flow only year to date; Q2 FY2018 is 6M less 3M."""
    start = "2017-01-30"
    six = _fact(raw, "NetCashProvidedByUsedInOperatingActivities", start, "2017-07-30")
    three = _fact(
        raw, "NetCashProvidedByUsedInOperatingActivities", start, "2017-04-30"
    )
    q2 = _row(rows, "operating_cash_flow", date(2017, 7, 30), "Q2")
    assert q2["value"] == pytest.approx(six - three)
    assert q2["fiscal_year"] == 2018


def test_q4_is_the_year_less_nine_months(raw, rows):
    start, end = "2017-01-30", "2018-01-28"
    year = _fact(raw, "NetCashProvidedByUsedInOperatingActivities", start, end)
    nine = _fact(raw, "NetCashProvidedByUsedInOperatingActivities", start, "2017-10-29")
    assert _row(rows, "operating_cash_flow", date(2018, 1, 28), "Q4")[
        "value"
    ] == pytest.approx(year - nine)
    assert _row(rows, "operating_cash_flow", date(2018, 1, 28), "FY")[
        "value"
    ] == pytest.approx(year)


def test_an_income_item_takes_the_reported_three_months_not_a_ytd_rung(raw, rows):
    """Revenue has a discrete 3-month fact; picked by duration, not by `fp`."""
    q3 = _fact(raw, "Revenues", "2017-07-31", "2017-10-29")
    assert _row(rows, "revenue", date(2017, 10, 29), "Q3")["value"] == q3


def test_the_latest_filing_of_a_period_wins(raw, rows):
    """Q1 FY2017's diluted EPS was first filed as 0.33 and restated to 0.35 (then split 40:1)."""
    first = _fact(
        raw, "EarningsPerShareDiluted", "2016-02-01", "2016-05-01", filed="2016-05-25"
    )
    assert first == 0.33
    assert _row(rows, "eps_diluted", date(2016, 5, 1), "Q1")["value"] == pytest.approx(
        0.35 / 40
    )


def test_both_splits_are_undone_from_edgar_alone(raw, rows):
    """FY2020 was only ever filed before the 4:1 (2021) and the 10:1 (2024) splits."""
    assert (
        _fact(
            raw,
            "EarningsPerShareDiluted",
            "2019-01-28",
            "2020-01-26",
            filed="2020-02-20",
        )
        == 4.52
    )
    fy2020 = date(2020, 1, 26)
    assert _row(rows, "eps_diluted", fy2020, "FY")["value"] == pytest.approx(4.52 / 40)
    assert _row(rows, "diluted_shares", fy2020, "FY")["value"] == pytest.approx(
        618e6 * 40
    )
    assert _row(rows, "dividends_per_share", fy2020, "FY")["value"] == pytest.approx(
        0.64 / 40
    )
    # Between the splits: FY2022 was last filed after the 4:1, before the 10:1.
    assert _row(rows, "eps_diluted", date(2022, 1, 30), "FY")["value"] == pytest.approx(
        3.85 / 10
    )
    # After both: as filed.
    assert _row(rows, "eps_diluted", date(2025, 1, 26), "FY")["value"] == pytest.approx(
        2.94
    )
    # A net income figure is never split-adjusted.
    assert _row(rows, "net_income", fy2020, "FY")["value"] == pytest.approx(2796e6)


def test_the_share_count_is_continuous_across_both_splits(rows):
    counts = [
        r["value"]
        for r in rows
        if r["metric"] == "diluted_shares" and r["fiscal_period"] == "FY"
    ]
    assert counts and all(22e9 < c < 27e9 for c in counts)


def test_the_chain_falls_back_per_period(rows):
    assert _row(rows, "revenue", date(2019, 1, 27), "FY")["source_concept"] == (
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    assert (
        _row(rows, "revenue", date(2020, 1, 26), "FY")["source_concept"] == "Revenues"
    )
    assert _row(rows, "capex", date(2025, 1, 26), "FY")["source_concept"] == (
        "PaymentsToAcquireProductiveAssets"
    )
    assert _row(rows, "capex", date(2017, 7, 30), "Q2")["source_concept"] == (
        "PaymentsToAcquirePropertyPlantAndEquipment"
    )


def test_a_concept_never_reported_stays_absent(rows):
    """NVDA's FY2013-FY2021 capex is an extension concept companyfacts does not carry."""
    assert _row(rows, "capex", date(2018, 1, 28), "FY") is None
    assert _row(rows, "capex", date(2018, 1, 28), "Q4") is None
    assert all(r["value"] is not None for r in rows)


def test_a_year_smaller_than_its_own_nine_months_is_a_slip(raw, rows):
    """The FY2016 10-K tagged Q4's 0.115 dividend as the year's; the year comes from the
    next concept in the chain, and Q4 from the year less the three quarters."""
    assert (
        _fact(raw, "CommonStockDividendsPerShareDeclared", "2015-01-26", "2016-01-31")
        == 0.115
    )
    fy = _row(rows, "dividends_per_share", date(2016, 1, 31), "FY")
    assert fy["source_concept"] == "CommonStockDividendsPerShareCashPaid"
    assert fy["value"] == pytest.approx(0.395 / 40)
    q4 = _row(rows, "dividends_per_share", date(2016, 1, 31), "Q4")
    assert q4["value"] == pytest.approx(0.115 / 40)


def test_fiscal_periods_are_labelled_from_the_period_end(rows):
    """NVDA's fiscal 2025 ended 26 January 2025; its Q1 ended in April 2024."""
    assert _row(rows, "revenue", date(2025, 1, 26), "FY")["fiscal_year"] == 2025
    q1 = _row(rows, "revenue", date(2024, 4, 28), "Q1")
    assert q1["fiscal_year"] == 2025
    assert _row(rows, "revenue", date(2024, 10, 27), "Q3")["fiscal_year"] == 2025


def test_the_fiscal_year_is_named_after_the_year_it_ends_in():
    assert fiscal_year_name(date(2025, 1, 26)) == 2025
    assert fiscal_year_name(date(2025, 9, 27)) == 2025
    # A 52/53-week year ending in the first days of January is the year before's.
    assert fiscal_year_name(date(2023, 1, 1)) == 2022


def test_the_reporting_currency_comes_from_the_units(raw):
    result = normalise(raw)
    assert result.currency == "USD"
    assert {r["unit"] for r in result.rows} == {"USD", "USD/shares", "shares"}


# ── synthetic documents ──────────────────────────────────────────────────────


def _doc(concepts):
    return {"cik": 1, "entityName": "Example", "facts": {"us-gaap": concepts}}


def _f(start, end, val, filed, form="10-Q", accn=None):
    fact = {
        "end": end,
        "val": val,
        "filed": filed,
        "form": form,
        "accn": accn or f"accn-{filed}",
        "fy": 2099,
        "fp": "Q9",
    }
    if start:
        fact["start"] = start
    return fact


def test_an_ifrs_filer_yields_nothing():
    doc = {
        "cik": 2,
        "entityName": "A 20-F filer",
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "EUR": [
                            _f(
                                "2024-01-01",
                                "2024-12-31",
                                1e9,
                                "2025-02-01",
                                form="20-F",
                            )
                        ]
                    }
                }
            }
        },
    }
    assert not normalise(doc).has_data
    assert not normalise({}).has_data


def test_a_quarter_context_carrying_the_years_figure_is_dropped():
    """ORCL's 10-Ks tag the year's revenue on a three-month context; the ladder's Q4 wins."""
    doc = _doc(
        {
            "Revenues": {
                "units": {
                    "USD": [
                        _f("2017-06-01", "2018-02-28", 28.0e9, "2018-03-21"),
                        _f(
                            "2017-06-01",
                            "2018-05-31",
                            39.0e9,
                            "2018-06-22",
                            form="10-K",
                        ),
                        _f(
                            "2018-03-01",
                            "2018-05-31",
                            39.0e9,
                            "2019-06-21",
                            form="10-K",
                        ),
                    ]
                }
            }
        }
    )
    rows = normalise(doc).rows
    assert _row(rows, "revenue", date(2018, 5, 31), "Q4")["value"] == pytest.approx(
        11.0e9
    )


def test_q4_comes_from_the_year_less_three_quarters_across_two_concepts():
    """The year under one concept, the quarters under another (ORCL from 2019)."""
    doc = _doc(
        {
            "Revenues": {
                "units": {
                    "USD": [
                        _f(
                            "2019-06-01",
                            "2020-05-31",
                            40.0e9,
                            "2020-06-22",
                            form="10-K",
                        ),
                    ]
                }
            },
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {
                    "USD": [
                        _f("2019-06-01", "2019-08-31", 9.0e9, "2019-09-13"),
                        _f("2019-09-01", "2019-11-30", 10.0e9, "2019-12-13"),
                        _f("2019-12-01", "2020-02-29", 10.5e9, "2020-03-13"),
                    ]
                }
            },
        }
    )
    rows = normalise(doc).rows
    q4 = _row(rows, "revenue", date(2020, 5, 31), "Q4")
    assert q4["value"] == pytest.approx(10.5e9)
    assert q4["fiscal_year"] == 2020
    assert q4["source_concept"] == "Revenues less Q1-Q3"


def _eps_doc(shares_rows, income_concept="NetIncomeLoss"):
    # Two discrete quarters and their six-month rung, which places them in a fiscal year.
    concepts = {
        income_concept: {
            "units": {
                "USD": [
                    _f("2024-01-01", "2024-03-31", 100e6, "2024-05-01"),
                    _f("2024-04-01", "2024-06-30", 120e6, "2024-08-01"),
                    _f("2024-01-01", "2024-06-30", 220e6, "2024-08-01"),
                ]
            }
        },
        "EarningsPerShareDiluted": {
            "units": {
                "USD/shares": [
                    _f("2024-01-01", "2024-03-31", 2.00, "2024-05-01"),
                    _f("2024-04-01", "2024-06-30", 2.40, "2024-08-01"),
                ]
            }
        },
    }
    if shares_rows:
        concepts["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
            "units": {"shares": shares_rows}
        }
    return _doc(concepts)


def test_a_missing_share_count_is_net_income_over_eps():
    rows = normalise(_eps_doc(None)).rows
    shares = _row(rows, "diluted_shares", date(2024, 3, 31), "Q1")
    assert shares["value"] == pytest.approx(50e6)
    assert shares["source_concept"] == SHARES_FROM_EPS_SOURCE


def test_consolidated_profit_never_derives_a_share_count():
    """`ProfitLoss` includes non-controlling interests; over EPS it is no share count."""
    rows = normalise(_eps_doc(None, income_concept="ProfitLoss")).rows
    assert _row(rows, "net_income", date(2024, 3, 31), "Q1") is not None
    assert _row(rows, "diluted_shares", date(2024, 3, 31), "Q1") is None


def test_a_share_count_tagged_in_thousands_is_rescaled():
    """ONDS tags some filings' counts in thousands, in filings no newer one overlaps."""
    rows = normalise(
        _eps_doc(
            [
                _f("2024-01-01", "2024-03-31", 50_000, "2024-05-01"),
                _f("2024-04-01", "2024-06-30", 50_000_000, "2024-08-01"),
            ]
        )
    ).rows
    assert _row(rows, "diluted_shares", date(2024, 3, 31), "Q1")[
        "value"
    ] == pytest.approx(50e6)
    assert _row(rows, "diluted_shares", date(2024, 6, 30), "Q2")[
        "value"
    ] == pytest.approx(50e6)


def test_a_count_slip_in_an_older_filing_is_voted_out_by_a_newer_one():
    """The same period in thousands in one filing and in shares in a newer one."""
    doc = _doc(
        {
            "WeightedAverageNumberOfDilutedSharesOutstanding": {
                "units": {
                    "shares": [
                        _f("2023-01-01", "2023-03-31", 40_000, "2023-05-01"),
                        _f("2023-01-01", "2023-03-31", 40_000_000, "2024-05-01"),
                        _f("2024-01-01", "2024-03-31", 41_000_000, "2024-05-01"),
                        _f("2022-01-01", "2022-03-31", 39_000, "2023-05-01"),
                    ]
                }
            }
        }
    )
    multiplier = adjustments(_facts(doc))
    concept = "WeightedAverageNumberOfDilutedSharesOutstanding"
    # The 2023 filing's counts, Q1 2022 included (which no newer filing repeats), are
    # scaled by the slip its overlap with the 2024 filing shows; the 2024 filing is the basis.
    assert multiplier[(concept, "accn-2023-05-01")] == pytest.approx(1000)
    assert multiplier[(concept, "accn-2024-05-01")] == pytest.approx(1)


def test_a_split_is_voted_from_the_overlap_and_inherited_without_one():
    """A 4:1 split between two filings: the older filing's EPS is divided by four, and a
    still older filing that shares no period with a newer one inherits the factor."""
    doc = _doc(
        {
            "EarningsPerShareDiluted": {
                "units": {
                    "USD/shares": [
                        _f(
                            "2020-01-01", "2020-03-31", 4.00, "2020-05-01", accn="a2020"
                        ),
                        _f(
                            "2021-01-01", "2021-03-31", 6.00, "2021-05-01", accn="a2021"
                        ),
                        _f(
                            "2020-01-01", "2020-03-31", 4.00, "2021-05-01", accn="a2021"
                        ),
                        _f(
                            "2021-01-01", "2021-03-31", 1.50, "2022-05-01", accn="a2022"
                        ),
                        _f(
                            "2022-01-01", "2022-03-31", 2.00, "2022-05-01", accn="a2022"
                        ),
                        _f(
                            "2019-01-01", "2019-03-31", 3.00, "2019-05-01", accn="a2019"
                        ),
                    ]
                }
            }
        }
    )
    multiplier = adjustments(_facts(doc))
    assert multiplier[("EarningsPerShareDiluted", "a2022")] == pytest.approx(1)
    assert multiplier[("EarningsPerShareDiluted", "a2021")] == pytest.approx(0.25)
    assert multiplier[("EarningsPerShareDiluted", "a2020")] == pytest.approx(0.25)
    assert multiplier[("EarningsPerShareDiluted", "a2019")] == pytest.approx(0.25)
