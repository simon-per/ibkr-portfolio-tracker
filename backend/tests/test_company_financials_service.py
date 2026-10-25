"""
The Company tab's derived figures (docs/company-financials.md): TTM, FCF, margins,
per-share, CAGR and YoY are computed in `company_financials_service` and nowhere else.

Pure: rows are stand-ins with the stored columns, built quarter by quarter. Offline.
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services.company_financials_service import (
    MARGINS,
    SERIES,
    build_series,
    cagr,
    yoy,
)


def _quarter_ends(first: date, n: int):
    """n calendar-quarter ends from `first` (a 31 Mar / 30 Jun / 30 Sep / 31 Dec)."""
    ends, d = [], first
    for _ in range(n):
        ends.append(d)
        month = d.month + 3
        year = d.year + (month > 12)
        month = month - 12 if month > 12 else month
        nxt = date(year, month, 28)
        while (nxt + timedelta(days=1)).month == month:
            nxt += timedelta(days=1)
        d = nxt
    return ends


def _rows(ends, metrics, skip=()):
    """Quarter rows with `metrics[name](i)` as the i-th quarter's value; FY rows at each Q4."""
    out = []
    for i, end in enumerate(ends):
        quarter = (end.month - 1) // 3 + 1
        for name, value in metrics.items():
            if (name, i) in skip:
                continue
            out.append(
                SimpleNamespace(
                    period_end=end,
                    fiscal_year=end.year,
                    fiscal_period=f"Q{quarter}",
                    metric=name,
                    value=value(i),
                )
            )
    return out


BASE = {
    "revenue": lambda i: 100.0 + 10 * i,
    "net_income": lambda i: 20.0 + i,
    "eps_diluted": lambda i: 0.20 + 0.01 * i,
    "operating_cash_flow": lambda i: 30.0,
    "capex": lambda i: 5.0,
    "sbc": lambda i: 2.0,
    "diluted_shares": lambda i: 100.0 + i,
    "operating_income": lambda i: 25.0,
    "dividends_per_share": lambda i: 0.05,
}
ENDS = _quarter_ends(date(2015, 3, 31), 44)  # eleven years of quarters


def _values(series, name):
    return [p["value"] for p in series[name]["points"]]


def test_every_series_is_served():
    series = build_series(_rows(ENDS, BASE), "ttm", "max")
    assert set(series) == set(SERIES)
    for name in SERIES:
        assert set(series[name]) == {"points", "cagr", "yoy"}


def test_ttm_sums_four_quarters_and_averages_the_share_count():
    series = build_series(_rows(ENDS[:5], BASE), "ttm", "max")
    revenue = series["revenue"]["points"]
    assert [p["period_end"] for p in revenue] == [
        ENDS[3].isoformat(),
        ENDS[4].isoformat(),
    ]
    assert revenue[0]["value"] == pytest.approx(100 + 110 + 120 + 130)
    assert revenue[0]["label"] == "Q4 FY2015"
    assert _values(series, "diluted_shares")[0] == pytest.approx(
        (100 + 101 + 102 + 103) / 4
    )
    assert _values(series, "eps_diluted")[0] == pytest.approx(0.20 + 0.21 + 0.22 + 0.23)
    fcf = 4 * (30.0 - 5.0)
    assert _values(series, "fcf")[0] == pytest.approx(fcf)
    assert _values(series, "fcf_after_sbc")[0] == pytest.approx(fcf - 8.0)
    assert _values(series, "fcf_per_share")[0] == pytest.approx(
        fcf / ((100 + 101 + 102 + 103) / 4)
    )
    assert _values(series, "fcf_margin")[0] == pytest.approx(fcf / 460)
    assert _values(series, "operating_margin")[0] == pytest.approx(100 / 460)
    assert _values(series, "net_margin")[0] == pytest.approx((20 + 21 + 22 + 23) / 460)


def test_a_missing_quarter_nulls_every_ttm_it_touches_never_a_partial_sum():
    series = build_series(_rows(ENDS[:8], BASE, skip={("revenue", 4)}), "ttm", "max")
    revenue = _values(series, "revenue")
    # TTMs ending at quarters 3..7; quarter 4 is in the windows ending at 4, 5, 6, 7.
    assert revenue[0] is not None
    assert revenue[1:] == [None, None, None, None]
    # The net income of the same windows is unaffected.
    assert None not in _values(series, "net_income")
    assert _values(series, "net_margin")[1] is None


def test_a_gap_in_the_quarters_breaks_the_window():
    """A quarter with no row at all: its neighbours are not consecutive."""
    ends = ENDS[:4] + ENDS[5:9]
    series = build_series(_rows(ends, BASE), "ttm", "max")
    assert _values(series, "revenue")[0] is not None
    assert _values(series, "revenue")[1:4] == [None, None, None]


def test_no_capex_means_no_fcf_never_ocf():
    """A broker reports no capex: FCF and everything built on it stay absent."""
    rows = [r for r in _rows(ENDS[:8], BASE) if r.metric != "capex"]
    series = build_series(rows, "quarterly", "max")
    for name in ("fcf", "fcf_after_sbc", "fcf_per_share", "fcf_margin"):
        assert set(_values(series, name)) == {None}
        assert series[name]["cagr"] is None and series[name]["yoy"] is None
    assert None not in _values(series, "revenue")


def test_a_margin_over_no_revenue_is_absent():
    rows = _rows(ENDS[:4], {**BASE, "revenue": lambda i: 0.0 if i == 1 else 100.0})
    series = build_series(rows, "quarterly", "max")
    assert _values(series, "operating_margin")[1] is None
    assert _values(series, "operating_margin")[0] == pytest.approx(0.25)


def test_annual_is_the_fiscal_years_as_filed():
    rows = _rows(ENDS[:8], BASE) + [
        SimpleNamespace(
            period_end=date(2015, 12, 31),
            fiscal_year=2015,
            fiscal_period="FY",
            metric="revenue",
            value=999.0,
        ),
    ]
    series = build_series(rows, "annual", "max")
    assert series["revenue"]["points"] == [
        {"period_end": "2015-12-31", "label": "FY2015", "value": 999.0}
    ]
    assert _values(series, "net_income") == [None]


def test_the_range_counts_back_from_the_latest_point():
    rows = _rows(ENDS, BASE)
    assert len(build_series(rows, "quarterly", "5y")["revenue"]["points"]) == 20
    assert len(build_series(rows, "quarterly", "10y")["revenue"]["points"]) == 40
    assert len(build_series(rows, "quarterly", "max")["revenue"]["points"]) == 44
    # The first TTM of a 5-year range is computed from quarters before the range.
    ttm = build_series(rows, "ttm", "5y")["revenue"]["points"]
    assert len(ttm) == 20 and ttm[0]["value"] is not None


def test_growth_is_computed_for_every_series_but_the_margins():
    series = build_series(_rows(ENDS, BASE), "ttm", "10y")
    for name in SERIES:
        if name in MARGINS:
            assert series[name]["cagr"] is None and series[name]["yoy"] is None
    first, last = series["revenue"]["points"][0], series["revenue"]["points"][-1]
    years = (
        date.fromisoformat(last["period_end"]) - date.fromisoformat(first["period_end"])
    ).days / 365.25
    assert series["revenue"]["cagr"] == pytest.approx(
        (last["value"] / first["value"]) ** (1 / years) - 1
    )
    four_back = series["revenue"]["points"][-5]["value"]
    assert series["revenue"]["yoy"] == pytest.approx(last["value"] / four_back - 1)


D = date(2020, 3, 31)


def _p(*pairs):
    return [(D.replace(year=D.year + y), v) for y, v in pairs]


@pytest.mark.parametrize(
    "points, expected",
    [
        (_p((0, 100.0), (2, 121.0)), pytest.approx(0.1, rel=1e-3)),
        (_p((0, 0.0), (2, 121.0)), None),  # a start of nothing grows by no rate
        (_p((0, -50.0), (2, 121.0)), None),  # a loss to a profit has no CAGR
        (_p((0, 100.0), (2, -10.0)), None),  # a profit to a loss neither
        (_p((0, 100.0), (2, 0.0)), -1.0),  # all of it lost: -100%
        (_p((0, 100.0)), None),
        ([(D, 100.0), (D + timedelta(days=300), 150.0)], None),  # under a year apart
        (_p((0, None), (1, 100.0), (3, 121.0)), pytest.approx(0.1, rel=1e-3)),
    ],
)
def test_cagr_edges(points, expected):
    assert cagr(points) == expected


@pytest.mark.parametrize(
    "points, expected",
    [
        (_p((0, 100.0), (1, 150.0)), pytest.approx(0.5)),
        (_p((0, -100.0), (1, 150.0)), None),  # from a loss: no growth rate
        (_p((0, 0.0), (1, 150.0)), None),
        (_p((0, None), (1, 150.0)), None),  # the comparison point is unknown
        (_p((1, 150.0)), None),  # there is none
        (_p((0, 100.0), (1, None)), None),  # the latest is unknown
        (_p((0, 100.0), (1, -50.0)), pytest.approx(-1.5)),  # into a loss: a figure
        (
            [(D, 100.0), (D + timedelta(days=371), 110.0)],
            pytest.approx(0.1),
        ),  # 53-week year
        ([(D, 100.0), (D + timedelta(days=200), 110.0)], None),
    ],
)
def test_yoy_edges(points, expected):
    assert yoy(points) == expected
