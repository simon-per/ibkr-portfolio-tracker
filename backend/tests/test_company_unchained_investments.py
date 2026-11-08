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
