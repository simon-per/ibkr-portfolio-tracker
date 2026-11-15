"""
The Company tab's gaps that were the normaliser's, not the filings' (2026-11-15).

Measured over the 132 companies on production: of the quarters missing in the last eight,
most were figures the company did file under a concept no chain read, or a convention the
normaliser did not know. Each rule below names the company it was found at; each test pins
the rule and the case where it must not fire. Synthetic documents, offline.
"""
from datetime import date

import pytest

from app.services.company_financials_normaliser import NO_CURRENT_DEBT_LINE, normalise

Q1, Q2 = date(2024, 3, 31), date(2024, 6, 30)


def _f(start, end, val, filed, form="10-Q", accn=None):
    fact = {"end": end, "val": val, "filed": filed, "form": form,
            "accn": accn or f"accn-{filed}", "fy": 2099, "fp": "Q9"}
    if start:
        fact["start"] = start
    return fact


def _flows(income=(100e6, 120e6)):
    """Two quarters and their six-month rung: what places a balance date in a fiscal year."""
    q1, q2 = income
    return {
        "NetIncomeLoss": {"units": {"USD": [
            _f("2024-01-01", "2024-03-31", q1, "2024-05-01"),
            _f("2024-04-01", "2024-06-30", q2, "2024-08-01"),
            _f("2024-01-01", "2024-06-30", q1 + q2, "2024-08-01"),
        ]}},
    }


def _doc(**concepts):
    facts = _flows()
    facts.update({name: {"units": units} for name, units in concepts.items()})
    return {"cik": 1, "entityName": "Example", "facts": {"us-gaap": facts}}


def _value(result, metric, end, period="Q2"):
    rows = [r for r in result.rows
            if r["metric"] == metric and r["period_end"] == end and r["fiscal_period"] == period]
    assert len(rows) <= 1
    return rows[0] if rows else None


def _instant(end, val, filed="2024-08-01", accn=None):
    return _f(None, end, val, filed, accn=accn)


# ── concepts the chains did not read ─────────────────────────────────────────


def test_a_utilitys_revenue_is_its_regulated_and_unregulated_operating_revenue():
    """XEL stopped tagging `Revenues` in 2019; its revenue ended there."""
    result = normalise(_doc(RegulatedAndUnregulatedOperatingRevenue={"USD": [
        _f("2024-04-01", "2024-06-30", 3.1e9, "2024-08-01"),
    ]}))
    assert _value(result, "revenue", Q2)["value"] == 3.1e9


def test_the_utility_concept_never_displaces_revenue_tagged_otherwise():
    result = normalise(_doc(
        Revenues={"USD": [_f("2024-04-01", "2024-06-30", 5e9, "2024-08-01")]},
        RegulatedAndUnregulatedOperatingRevenue={"USD": [
            _f("2024-04-01", "2024-06-30", 4e9, "2024-08-01")]},
    ))
    assert _value(result, "revenue", Q2)["value"] == 5e9


@pytest.mark.parametrize("concept", [
    "PaymentsToAcquireOtherPropertyPlantAndEquipment",  # LLY
    "PaymentsToAcquireOtherProductiveAssets",  # VZ and ROP from 2019
])
def test_capex_under_the_other_property_concepts(concept):
    result = normalise(_doc(**{concept: {"USD": [
        _f("2024-04-01", "2024-06-30", 2e9, "2024-08-01")]}}))
    assert _value(result, "capex", Q2)["value"] == 2e9


def test_a_count_tagged_for_basic_and_diluted_alike_is_the_diluted_count():
    """ZS, WDAY, PANW and DDOG tag one count for both in their loss years."""
    result = normalise(_doc(WeightedAverageNumberOfShareOutstandingBasicAndDiluted={"shares": [
        _f("2024-04-01", "2024-06-30", 150e6, "2024-08-01")]}))
    row = _value(result, "diluted_shares", Q2)
    assert row["value"] == 150e6
    assert row["source_concept"] == "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"


def test_cash_while_a_business_is_held_for_sale():
    """MDLZ 2016-2018 tagged its balance-sheet cash including the discontinued operations."""
    result = normalise(_doc(CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations={
        "USD": [_instant("2024-06-30", 1.5e9)]}))
    assert _value(result, "cash", Q2)["value"] == 1.5e9


# ── a loss period's basic count ──────────────────────────────────────────────


def _basic(*facts):
    return {"WeightedAverageNumberOfSharesOutstandingBasic": {"units": {"shares": list(facts)}}}


def test_a_loss_quarters_basic_count_is_its_diluted_count():
    """TTWO tags only the basic count in a loss quarter: anti-dilution makes them equal."""
    doc = {"cik": 1, "entityName": "Example", "facts": {"us-gaap": {
        **_flows(income=(-30e6, -40e6)),
        **_basic(_f("2024-04-01", "2024-06-30", 185e6, "2024-08-01")),
    }}}
    row = _value(normalise(doc), "diluted_shares", Q2)
    assert row["value"] == 185e6
    assert row["source_concept"] == "WeightedAverageNumberOfSharesOutstandingBasic"


def test_a_profitable_quarters_basic_count_is_never_its_diluted_count():
    doc = {"cik": 1, "entityName": "Example", "facts": {"us-gaap": {
        **_flows(income=(30e6, 40e6)),
        **_basic(_f("2024-04-01", "2024-06-30", 185e6, "2024-08-01")),
    }}}
    assert _value(normalise(doc), "diluted_shares", Q2) is None


def test_the_basic_count_is_never_stored():
    doc = {"cik": 1, "entityName": "Example", "facts": {"us-gaap": {
        **_flows(income=(-30e6, -40e6)),
        **_basic(_f("2024-04-01", "2024-06-30", 185e6, "2024-08-01")),
    }}}
    assert not any(r["metric"] == "basic_shares" for r in normalise(doc).rows)


def test_a_differenced_basic_quarter_is_no_count():
    """TTWO's FY2014 Q4 came to 121M as the year less nine months, between 112M and 79M:
    averages do not subtract. Only a reported basic count stands in."""
    doc = {"cik": 1, "entityName": "Example", "facts": {"us-gaap": {
        **_flows(income=(-30e6, -40e6)),
        # Only the three- and six-month rungs: Q2 would have to be differenced.
        **_basic(_f("2024-01-01", "2024-03-31", 80e6, "2024-05-01"),
                 _f("2024-01-01", "2024-06-30", 90e6, "2024-08-01")),
    }}}
    result = normalise(doc)
    assert _value(result, "diluted_shares", Q1, "Q1")["value"] == 80e6
    assert _value(result, "diluted_shares", Q2) is None


# ── debt ─────────────────────────────────────────────────────────────────────


def test_convertible_notes_are_both_halves_of_a_convertible_issuers_debt():
    """PANW, DDOG and ZS tag nothing but their convertible notes."""
    result = normalise(_doc(
        ConvertibleDebtNoncurrent={"USD": [_instant("2024-06-30", 1.8e9)]},
        ConvertibleDebtCurrent={"USD": [_instant("2024-06-30", 0.2e9)]},
    ))
    assert _value(result, "debt_noncurrent", Q2)["value"] == 1.8e9
    assert _value(result, "debt_current", Q2)["value"] == 0.2e9


def test_current_maturities_with_leases_fill_only_what_nothing_else_does():
    """ODFL and CSX tag `LongTermDebtAndCapitalLeaseObligationsCurrent`; a company that
    tags `DebtCurrent` beside it keeps that figure."""
    alone = normalise(_doc(LongTermDebtAndCapitalLeaseObligationsCurrent={
        "USD": [_instant("2024-06-30", 0.3e9)]}))
    assert _value(alone, "debt_current", Q2)["value"] == 0.3e9
    both = normalise(_doc(
        DebtCurrent={"USD": [_instant("2024-06-30", 0.5e9)]},
        LongTermDebtAndCapitalLeaseObligationsCurrent={"USD": [_instant("2024-06-30", 0.3e9)]},
    ))
    assert _value(both, "debt_current", Q2)["value"] == 0.5e9


def test_no_current_debt_on_the_balance_sheet_is_a_zero_current_half():
    """META, ABNB, TTWO: no current maturities, no current line, so no debt at all before."""
    result = normalise(_doc(LongTermDebtNoncurrent={"USD": [_instant("2024-06-30", 18e9)]}))
    row = _value(result, "debt_current", Q2)
    assert row["value"] == 0
    assert row["source_concept"] == NO_CURRENT_DEBT_LINE


def test_a_current_debt_line_under_an_unread_concept_leaves_the_half_unknown():
    """The filing shows current debt, so a zero would be a guess."""
    result = normalise(_doc(
        LongTermDebtNoncurrent={"USD": [_instant("2024-06-30", 18e9)]},
        SeniorNotesCurrent={"USD": [_instant("2024-06-30", 1e9)]},
    ))
    assert _value(result, "debt_current", Q2) is None


def test_a_lease_alone_is_no_current_debt_line():
    result = normalise(_doc(
        LongTermDebtNoncurrent={"USD": [_instant("2024-06-30", 18e9)]},
        FinanceLeaseLiabilityCurrent={"USD": [_instant("2024-06-30", 0.1e9)]},
    ))
    assert _value(result, "debt_current", Q2)["value"] == 0


def test_another_filings_current_line_does_not_count():
    """Only the filing that reports the noncurrent half at that date is read."""
    result = normalise(_doc(
        LongTermDebtNoncurrent={"USD": [_instant("2024-06-30", 18e9, accn="10q")]},
        SeniorNotesCurrent={"USD": [_instant("2024-06-30", 1e9, filed="2024-05-01", accn="old")]},
    ))
    assert _value(result, "debt_current", Q2)["value"] == 0


def test_a_maturity_table_contradicting_a_zero_withholds_every_zero():
    """PYPL's current notes sit in a company concept companyfacts does not carry; its maturity
    table states 1.4B due within twelve months. Such a company gets no zero anywhere."""
    result = normalise(_doc(
        LongTermDebtNoncurrent={"USD": [
            _instant("2024-03-31", 11e9, filed="2024-05-01"),
            _instant("2024-06-30", 11e9),
        ]},
        LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths={"USD": [
            _instant("2024-06-30", 1.4e9)]},
    ))
    assert _value(result, "debt_current", Q1, "Q1") is None
    assert _value(result, "debt_current", Q2) is None


def test_a_small_amount_due_is_no_contradiction():
    result = normalise(_doc(
        LongTermDebtNoncurrent={"USD": [_instant("2024-06-30", 11e9)]},
        LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths={"USD": [
            _instant("2024-06-30", 0.01e9)]},
    ))
    assert _value(result, "debt_current", Q2)["value"] == 0


def test_no_zero_beside_a_tagged_total():
    result = normalise(_doc(
        LongTermDebt={"USD": [_instant("2024-06-30", 20e9)]},
        LongTermDebtNoncurrent={"USD": [_instant("2024-06-30", 18e9)]},
    ))
    assert _value(result, "debt_current", Q2) is None


# ── the reporting currency ───────────────────────────────────────────────────


def _two_currency_doc():
    """Net income in RUB in five older filings, and in USD in the two newest (`_flows`)."""
    rub = [_f("2019-01-01", "2019-03-31", 9e9, f"2019-0{i + 4}-01", accn=f"rub-{i}")
           for i in range(5)]
    usd = _flows()["NetIncomeLoss"]["units"]["USD"]
    return {"cik": 1, "entityName": "Example", "facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"RUB": rub, "USD": usd}},
    }}}


def test_a_company_that_changed_currency_reports_in_its_newest_filings():
    """NBIS (Yandex until 2024) reported in RUB for fifteen years and in USD since; keeping
    the currency reported most ended its series at FY2023."""
    result = normalise(_two_currency_doc())
    assert result.currency == "USD"
    assert _value(result, "net_income", Q1, "Q1")["value"] == 100e6
    assert {r["unit"] for r in result.rows} == {"USD"}


def test_a_convenience_translation_beside_the_reporting_currency_is_not_read():
    """TSMC's 20-F gives its latest year in USD beside TWD for every year: TWD stays."""
    doc = {"cik": 1, "entityName": "Example", "facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {
            "TWD": [_f("2023-01-01", "2023-12-31", 900e9, "2024-04-01", "20-F", "twenty-f"),
                    _f("2022-01-01", "2022-12-31", 1000e9, "2024-04-01", "20-F", "twenty-f")],
            "USD": [_f("2023-01-01", "2023-12-31", 29e9, "2024-04-01", "20-F", "twenty-f")],
        }},
    }}}
    result = normalise(doc)
    assert result.currency == "TWD"
    assert {r["unit"] for r in result.rows} == {"TWD"}


def test_a_concept_only_in_the_old_currency_is_not_mixed_in():
    doc = _two_currency_doc()
    doc["facts"]["us-gaap"]["Revenues"] = {"units": {"RUB": [
        _f("2024-01-01", "2024-03-31", 50e9, "2024-05-01", accn="rub-x")]}}
    result = normalise(doc)
    assert _value(result, "revenue", Q1, "Q1") is None
    assert {r["unit"] for r in result.rows} == {"USD"}
