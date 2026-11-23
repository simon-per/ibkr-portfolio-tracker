"""
ESEF's dividend-per-share tag has no one meaning across filers (docs/company-financials.md,
*Europe*): a year's figure stands only where the dividends paid over the diluted shares bear
it out (`esef_sync.witnessed_dividends`).
"""

from datetime import date

from app.services.esef_sync import witnessed_dividends

END = date(2025, 12, 31)


def _rows(dps, paid=None, shares=1_000.0):
    rows = [{"period_end": END, "metric": "dividends_per_share", "value": dps}]
    if paid is not None:
        rows.append({"period_end": END, "metric": "dividends_paid", "value": paid})
    if shares is not None:
        rows.append({"period_end": END, "metric": "diluted_shares", "value": shares})
    return rows


def _kept(rows):
    return [r["value"] for r in witnessed_dividends(rows) if r["metric"] == "dividends_per_share"]


def test_a_whole_years_dividend_stands():
    assert _kept(_rows(0.70, paid=698.3)) == [0.70]  # Prysmian's


def test_profit_over_eps_stands_in_for_a_filer_that_tags_no_count():
    rows = _rows(1.432, paid=8_472.0, shares=None) + [
        {"period_end": END, "metric": "net_income", "value": 17_840.0},
        {"period_end": END, "metric": "eps_diluted", "value": 2.95},
    ]
    assert _kept(rows) == [1.432]  # Shell's


def test_an_interim_alone_or_a_figure_in_cents_is_absent():
    assert _kept(_rows(0.115, paid=213.2)) == []  # Santander's interim of a 0.24 year
    assert _kept(_rows(11.92, paid=340.1)) == []  # Terna's euro cents


def test_without_cash_or_shares_to_weigh_it_is_absent_and_other_rows_stay():
    rows = _rows(0.253)
    assert _kept(rows) == []
    assert [r["metric"] for r in witnessed_dividends(rows)] == ["diluted_shares"]
    assert _kept(_rows(0.5, paid=500.0, shares=None)) == []
