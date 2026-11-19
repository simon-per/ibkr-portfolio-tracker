"""
A current-investments concept no chain reads is named, never read (review item CO8,
2026-11-08).

KLAC's marketable securities sit in a concept outside the `short_term_investments` chain,
so its net cash counted cash alone (-4.2B for a company with billions in securities). The
normaliser now names every us-gaap concept shaped like a current investment that carries a
value on the newest balance sheet where no chained concept does, and the sync warns with
it, so the chain can whitelist it. Offline: hand-made facts and the trimmed NVDA fixture.
"""
import copy

import pytest

from app.services.company_financials_normaliser import METRICS, normalise
from tests.test_company_financials_sync import (  # noqa: F401 - the fixture
    FIXTURE,
    NVDA_CIK,
    Edgar,
    _book,
    _sync,
    configured,
)


def f(end, val, accn, filed, form="10-K"):
    return {"end": end, "val": val, "accn": accn, "filed": filed, "form": form}


def facts(extra):
    gaap = {
        "Revenues": {"units": {"USD": [
            {**f("2026-06-30", 12e9, "k26", "2026-08-01"), "start": "2025-07-01"},
        ]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            f("2026-06-30", 1.65e9, "k26", "2026-08-01"),
        ]}},
    }
    gaap.update(extra)
    return {"cik": 319201, "entityName": "KLA", "facts": {"us-gaap": gaap}}


def test_an_investments_concept_outside_the_chain_is_named():
    result = normalise(facts({
        "OtherMarketableSecuritiesCurrent": {"units": {"USD": [
            f("2026-06-30", 2.8e9, "k26", "2026-08-01"),
        ]}},
        # Not liquid, and not on this balance sheet: neither is named.
        "RestrictedCashAndInvestmentsCurrent": {"units": {"USD": [
            f("2026-06-30", 0.1e9, "k26", "2026-08-01"),
        ]}},
        "OtherShortTermInvestmentsCurrent": {"units": {"USD": [
            f("2025-06-30", 0.2e9, "k25", "2025-08-01"),
        ]}},
    }))
    assert result.unchained_investments == ["OtherMarketableSecuritiesCurrent"]


def test_nothing_is_named_where_a_chained_concept_holds_the_investments():
    chained = METRICS["short_term_investments"].concepts[0]
    result = normalise(facts({
        chained: {"units": {"USD": [f("2026-06-30", 2.8e9, "k26", "2026-08-01")]}},
        "OtherMarketableSecuritiesCurrent": {"units": {"USD": [
            f("2026-06-30", 2.8e9, "k26", "2026-08-01"),
        ]}},
    }))
    assert result.unchained_investments == []


@pytest.mark.asyncio
async def test_the_pass_warns_with_the_company_and_the_concept(db, configured):  # noqa: F811
    nvda = copy.deepcopy(FIXTURE)
    gaap = nvda["facts"]["us-gaap"]
    for concept in (
        *METRICS["short_term_investments"].concepts,
        *METRICS["cash_and_short_term"].concepts,
    ):
        gaap.pop(concept, None)
    cash = gaap["CashAndCashEquivalentsAtCarryingValue"]["units"]["USD"]
    newest = max(cash, key=lambda r: r["end"])
    gaap["OtherMarketableSecuritiesCurrent"] = {"units": {"USD": [dict(newest)]}}

    await _book(db)
    summary = await _sync(db, Edgar(facts={NVDA_CIK: nvda})).refresh()

    assert summary["unchained_investments"] == ["NVDA (OtherMarketableSecuritiesCurrent)"]
    assert any(
        "NVDA (OtherMarketableSecuritiesCurrent)" in w for w in summary["warnings"]
    )


def _investments(result):
    return {r["period_end"].isoformat(): r["value"] for r in result.rows
            if r["metric"] == "short_term_investments"}


def test_klacs_available_for_sale_total_is_its_marketable_securities():
    """
    Read on the server (2026-11-08): KLAC tags its balance-sheet "Marketable securities" as
    `AvailableForSaleSecuritiesDebtSecurities` — a total, with no "Current" suffix, so the
    check above would never have named it. With nothing non-current tagged beside it, it is
    all current, and net cash counts it.
    """
    result = normalise(facts({
        "AvailableForSaleSecuritiesDebtSecurities": {"units": {"USD": [
            f("2026-06-30", 3.206e9, "k26", "2026-08-01"),
        ]}},
    }))
    assert _investments(result) == {"2026-06-30": 3.206e9}
    assert result.unchained_investments == []


def test_the_total_beside_a_noncurrent_figure_is_not_liquid():
    result = normalise(facts({
        "AvailableForSaleSecuritiesDebtSecurities": {"units": {"USD": [
            f("2026-06-30", 3.206e9, "k26", "2026-08-01"),
        ]}},
        "MarketableSecuritiesNoncurrent": {"units": {"USD": [
            f("2026-06-30", 1.0e9, "k26", "2026-08-01"),
        ]}},
    }))
    assert _investments(result) == {}
    # The non-current figure is read for the test alone, never stored.
    assert not any(r["metric"] == "noncurrent_investments" for r in result.rows)


def test_a_current_concept_wins_over_the_total():
    result = normalise(facts({
        "MarketableSecuritiesCurrent": {"units": {"USD": [
            f("2026-06-30", 2.0e9, "k26", "2026-08-01"),
        ]}},
        "AvailableForSaleSecuritiesDebtSecurities": {"units": {"USD": [
            f("2026-06-30", 3.0e9, "k26", "2026-08-01"),
        ]}},
    }))
    assert _investments(result) == {"2026-06-30": 2.0e9}



@pytest.mark.asyncio
async def test_the_pass_records_what_a_proven_concept_filled(db, configured):  # noqa: F811
    """`_bridge` (2026-11-15): a concept outside the chains that agrees with the chain's
    figure fills its gaps, and the pass records which — information, never a warning."""
    nvda = copy.deepcopy(FIXTURE)
    gaap = nvda["facts"]["us-gaap"]
    revenue = next(c for c in METRICS["revenue"].concepts if c in gaap)
    rows = gaap[revenue]["units"]["USD"]
    newest_year = max(r["end"] for r in rows)[:4]
    gaap["SalesRevenueGoodsNet"] = {"units": {"USD": [dict(r) for r in rows]}}
    gaap[revenue]["units"]["USD"] = [r for r in rows if not r["end"].startswith(newest_year)]
    await _book(db)
    summary = await _sync(db, Edgar(facts={NVDA_CIK: nvda})).refresh()
    bridged = summary["bridged"]
    assert len(bridged) == 1 and bridged[0].startswith("NVDA revenue <- SalesRevenueGoodsNet (")
    assert not any("SalesRevenueGoodsNet" in w for w in summary.get("warnings", []))


@pytest.mark.parametrize(
    "concept",
    [
        # DXCM, ZS, IBM, INTU: the 2020 successor of the available-for-sale current line.
        "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
        # UNP's balance-sheet "Short-term investments".
        "HeldToMaturitySecuritiesCurrent",
    ],
)
def test_the_chain_reads_the_investments_these_filers_tag(concept):
    """Named by the warning on production (2026-11-19) and read in the filings: each holds
    the balance sheet's current investments, so net cash counts them."""
    result = normalise(facts({
        concept: {"units": {"USD": [f("2026-06-30", 2.5e9, "k26", "2026-08-01")]}},
    }))
    assert _investments(result) == {"2026-06-30": 2.5e9}
    assert result.unchained_investments == []


def test_interest_accrued_on_the_securities_is_no_investment():
    """GE tags the interest accrued on its insurance portfolio's securities as a current
    asset: a receivable, so neither read nor named."""
    result = normalise(facts({
        "DebtSecuritiesAvailableForSaleAccruedInterestAfterAllowanceForCreditLossCurrent": {
            "units": {"USD": [f("2026-06-30", 0.5e9, "k26", "2026-08-01")]}
        },
    }))
    assert _investments(result) == {}
    assert result.unchained_investments == []
