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


# ── bridging: a concept outside the chains that proves itself ────────────────


def _quarters(*values, start_year=2023):
    """Discrete quarters of one year, Q1..Qn, each filed a month after it ends."""
    ends = ["03-31", "06-30", "09-30", "12-31"]
    starts = ["01-01", "04-01", "07-01", "10-01"]
    out = []
    for i, v in enumerate(values):
        y = start_year + i // 4
        q = i % 4
        filed_month = {0: "05", 1: "08", 2: "11", 3: "02"}[q]
        filed_year = y + 1 if q == 3 else y
        out.append(_f(f"{y}-{starts[q]}", f"{y}-{ends[q]}", v, f"{filed_year}-{filed_month}-01",
                      accn=f"a-{y}-{q}"))
    return out


def _bridge_doc(chain, other, other_name="SalesRevenueGoodsNet"):
    # The 10-K years place the quarters in fiscal years, as every filer's do.
    income = _quarters(*([10e6] * 8)) + [
        _f("2023-01-01", "2023-12-31", 40e6, "2024-02-15", "10-K", "k-2023"),
        _f("2024-01-01", "2024-12-31", 40e6, "2025-02-15", "10-K", "k-2024"),
    ]
    return {"cik": 1, "entityName": "Example", "facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": income}},
        "Revenues": {"units": {"USD": _quarters(*chain)}},
        other_name: {"units": {"USD": _quarters(*other)}},
    }}}


def _revenue(result):
    return {r["period_end"]: r for r in result.rows
            if r["metric"] == "revenue" and r["fiscal_period"] != "FY"}


def test_a_concept_that_agrees_where_both_report_fills_where_the_chain_is_silent():
    """JNJ, MNST and WDC tagged revenue as `SalesRevenueGoodsNet` before 2018: on the
    quarters both report they agree, so it fills the ones the chain lacks."""
    result = normalise(_bridge_doc(chain=(100e6, 110e6, 120e6), other=(100e6, 110e6, 120e6, 130e6, 140e6)))
    revenue = _revenue(result)
    assert revenue[date(2024, 3, 31)]["value"] == 140e6
    assert revenue[date(2024, 3, 31)]["source_concept"] == "SalesRevenueGoodsNet"
    assert revenue[date(2023, 3, 31)]["source_concept"] == "Revenues"
    assert result.bridged == {"revenue": {"SalesRevenueGoodsNet": 2}}


def test_a_part_named_like_the_figure_never_agrees_and_never_fills():
    """A utility's gas revenue is named like revenue and is a part of it."""
    result = normalise(_bridge_doc(chain=(100e6, 110e6, 120e6), other=(40e6, 45e6, 50e6, 55e6, 60e6),
                                   other_name="RegulatedOperatingRevenueGas"))
    assert date(2024, 3, 31) not in _revenue(result)
    assert result.bridged == {}


def test_one_agreement_is_no_proof():
    result = normalise(_bridge_doc(chain=(100e6,), other=(100e6, 105e6, 110e6)))
    assert date(2023, 6, 30) not in _revenue(result)


def test_a_concept_that_disagrees_often_is_not_proven():
    result = normalise(_bridge_doc(chain=(100e6, 110e6, 120e6, 130e6),
                                   other=(100e6, 110e6, 99e6, 77e6, 140e6)))
    assert date(2024, 3, 31) not in _revenue(result)


def test_bridging_never_replaces_a_figure():
    result = normalise(_bridge_doc(chain=(100e6, 110e6, 120e6), other=(100e6, 110e6, 121e6, 130e6)))
    assert _revenue(result)[date(2023, 9, 30)]["value"] == 120e6


# ── a balance sheet with no debt at all ──────────────────────────────────────


def _balance(cash=5e9, total=20e9, end="2024-06-30", filed="2024-08-01", accn="10q", **more):
    concepts = {
        "CashAndCashEquivalentsAtCarryingValue": {"USD": [_instant(end, cash, filed, accn)]},
        "LiabilitiesAndStockholdersEquity": {"USD": [_instant(end, total, filed, accn)]},
    }
    concepts.update(more)
    return _doc(**concepts)


def test_a_balance_sheet_without_any_borrowing_has_no_debt():
    """ANET, ISRG, PLTR: no debt and nothing tagged, so no net cash before."""
    result = normalise(_balance())
    for half in ("debt_noncurrent", "debt_current"):
        row = _value(result, half, Q2)
        assert row["value"] == 0
        assert row["source_concept"] == "no debt on the balance sheet"


def test_debt_securities_held_are_no_borrowing():
    result = normalise(_balance(AvailableForSaleSecuritiesDebtSecuritiesCurrent={
        "USD": [_instant("2024-06-30", 3e9, accn="10q")]}))
    assert _value(result, "debt_current", Q2)["value"] == 0


def test_a_borrowing_under_any_concept_leaves_debt_unknown():
    result = normalise(_balance(OtherLongTermDebtNoncurrent={
        "USD": [_instant("2024-06-30", 1e9, accn="10q")]}))
    assert _value(result, "debt_current", Q2) is None


def test_a_maturity_table_is_a_borrowing():
    result = normalise(_balance(LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths={
        "USD": [_instant("2024-06-30", 1e9, accn="10q")]}))
    assert _value(result, "debt_current", Q2) is None


def test_material_interest_says_there_is_debt_companyfacts_does_not_show():
    """BRK and PCAR report their debt per segment, which companyfacts leaves out."""
    result = normalise(_balance(InterestExpense={"USD": [
        _f("2024-04-01", "2024-06-30", 0.05e9, "2024-08-01", accn="10q")]}))
    assert _value(result, "debt_current", Q2) is None


def test_a_few_days_interest_is_no_rate():
    """MPWR tags stub periods: a few days' interest annualised looks like a borrower's."""
    result = normalise(_balance(InterestExpenseOther={"USD": [
        _f("2024-06-20", "2024-06-30", 0.003e9, "2024-08-01", accn="10q")]}))
    assert _value(result, "debt_current", Q2)["value"] == 0


def test_a_cash_flow_statements_closing_cash_is_no_balance_sheet():
    """A later filing's cash flow statement reports the date's cash without its balance sheet;
    only a filing whose balance sheet is at that date may say there is no debt."""
    doc = _doc(CashAndCashEquivalentsAtCarryingValue={"USD": [_instant("2024-06-30", 5e9)]})
    assert _value(normalise(doc), "debt_current", Q2) is None


def test_a_years_interest_speaks_until_the_next_annual_report():
    """PCAR states interest only for whole years: its FY interest still says it borrows nine
    months on, where a quarter's would have lapsed (its Q3 2026 read debt-free, 2026-11-19)."""
    yearly = _balance(InterestExpense={"USD": [
        _f("2022-10-01", "2023-09-30", 0.2e9, "2023-11-15", form="10-K", accn="10k")]})
    assert _value(normalise(yearly), "debt_current", Q2) is None
    quarterly = _balance(InterestExpense={"USD": [
        _f("2023-07-01", "2023-09-30", 0.05e9, "2023-11-15", accn="10q3")]})
    assert _value(normalise(quarterly), "debt_current", Q2)["value"] == 0
