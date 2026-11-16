"""
Every company's business model and sector by a waterfall, the company's own regulator first
(owner, 2026-11-16; docs/company-financials.md, *Classification*).

Measured over the 132 companies on SEC's sources alone: 5 banks and brokers (BAC, JPM, IBKR,
SOFI by their statements, NU by its SIC), 1 insurer (BRK), 126 industrials; 124 sectors from the
SIC code, the 8 others the catch-all 7389 (Visa, Mastercard, PayPal, ...) left to the baskets'
GICS. Each rule pinned with the company it was measured at. Synthetic documents, offline.
"""

import pytest

from app.models.company_financials import SecCompany
from app.models.etf_basket import EtfHolding
from app.services.company_classification import (
    BANK,
    INDUSTRIAL,
    INSURER,
    classify,
    gics_sector,
    office_model,
    office_sector,
    sic_model,
    sic_sector,
    statement_model,
)
from app.services.company_financials_service import CompanyFinancialsService
from app.services.edgar_client import submissions_profile


def _doc(*concepts, form="10-K", accn="a-new", filed="2026-02-20"):
    gaap = {c: {"units": {"USD": [{"end": "2025-12-31", "val": 1, "form": form,
                                   "accn": accn, "filed": filed}]}} for c in concepts}
    gaap["Revenues"] = {"units": {"USD": [{"start": "2025-01-01", "end": "2025-12-31",
                                           "val": 9, "form": form, "accn": accn,
                                           "filed": filed}]}}
    return {"facts": {"us-gaap": gaap}}


# ── the statements ───────────────────────────────────────────────────────────


def test_a_banks_statements_say_bank():
    """BAC and JPM tag six of the bank concepts; IBKR, a broker, three."""
    assert statement_model(_doc("Deposits", "InterestIncomeExpenseNet",
                                "NoninterestIncome")) == BANK
    assert statement_model(_doc("InterestIncomeExpenseNet", "NoninterestIncome",
                                "PayablesToCustomers")) == BANK


def test_one_bank_like_line_is_an_industrials():
    """AMD, ADSK and ROST tag `InterestIncomeExpenseNet`; PCAR's finance arm a loan-loss
    provision: one line is no bank."""
    assert statement_model(_doc("InterestIncomeExpenseNet")) == INDUSTRIAL
    assert statement_model(_doc("ProvisionForLoanLeaseAndOtherLosses")) == INDUSTRIAL


def test_an_insurers_statements_say_insurer_even_tagged_per_segment():
    """BRK tags premiums and claims per segment; its reserves' changes are undimensioned."""
    assert statement_model(_doc("IncreaseDecreaseInUnearnedPremiums",
                                "IncreaseDecreaseInLiabilityForClaimsAndClaimsAdjustmentExpenseReserve",
                                "LiabilityForFuturePolicyBenefits")) == INSURER


def test_only_the_newest_annual_report_counts():
    old_bank = _doc("Deposits", "InterestIncomeExpenseNet", "NoninterestIncome",
                    accn="a-old", filed="2019-02-20")
    newer = _doc(accn="a-new", filed="2026-02-20")
    merged = {"facts": {"us-gaap": {**old_bank["facts"]["us-gaap"],
                                    "Revenues": newer["facts"]["us-gaap"]["Revenues"]}}}
    assert statement_model(merged) == INDUSTRIAL


def test_no_us_gaap_annual_report_decides_nothing():
    """NU and TSMC file IFRS; HONA and SPCX had no 10-K yet."""
    assert statement_model({"facts": {"ifrs-full": {}}}) is None
    assert statement_model(_doc("Deposits", form="10-Q")) is None


# ── SEC's codes ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sic, model", [
    (6021, BANK), (6199, BANK), (6211, BANK), (6331, INSURER), (6411, INSURER),
    (3674, INDUSTRIAL), (6798, INDUSTRIAL), (None, None),
])
def test_the_sic_code_names_a_model(sic, model):
    assert sic_model(sic) == model


@pytest.mark.parametrize("sic, sector", [
    (3674, "Technology"),  # NVDA
    (7370, "Technology"),  # GOOGL, META: SEC's code, not GICS's Communications
    (2834, "Healthcare"),  # LLY
    (5961, "Consumer Cyclical"),  # AMZN
    (5331, "Consumer Defensive"),  # WMT, COST
    (4813, "Communications"),  # VZ
    (4911, "Utilities"),  # AEP
    (2911, "Energy"),  # XOM
    (6021, "Financials"),  # JPM
    (6798, "Real Estate"),
    (3559, "Industrials"),  # ASML, LRCX: special industry machinery
    (7389, None),  # Visa, Mastercard, PayPal: a catch-all decides nothing
    (6770, None),  # blank checks
    (None, None),
])
def test_the_sic_code_names_a_sector(sic, sector):
    assert sic_sector(sic) == sector


#: The SIC codes of the 132 companies measured on 2026-11-16, Visa's catch-all aside.
MEASURED_SICS = (
    1311, 1600, 1731, 2000, 2080, 2086, 2320, 2810, 2834, 2835, 2836, 2911, 3480, 3533, 3559,
    3570, 3571, 3572, 3576, 3577, 3600, 3663, 3669, 3674, 3711, 3724, 3760, 3823, 3825, 3827,
    3841, 3842, 3844, 4011, 4213, 4700, 4812, 4813, 4841, 4911, 4931, 5200, 5331, 5500, 5531,
    5651, 5810, 5961, 6021, 6199, 6211, 6331, 7011, 7320, 7340, 7370, 7371, 7372, 7374, 7841,
    8700,
)


def test_every_measured_code_has_a_sector():
    assert [sic for sic in MEASURED_SICS if sic_sector(sic) is None] == []


def test_the_reviewing_office():
    assert office_sector("06 Technology") == "Technology"
    assert office_sector("04 Manufacturing") is None  # spans many sectors
    assert office_model("07 Trade & Services") == INDUSTRIAL
    assert office_model("02 Finance") is None  # banks and payments companies alike
    assert office_model("09 Crypto Assets") is None  # IBKR
    assert submissions_profile({"ownerOrg": "04 Manufacturing"})["owner_org"] == "04 Manufacturing"


# ── the waterfall ────────────────────────────────────────────────────────────


def test_the_statements_outrank_the_sic_code_and_the_conflict_is_recorded():
    """MSTR is SIC 6199, finance; its statements are an industrial's."""
    kind = classify(INDUSTRIAL, 6199, "09 Crypto Assets")
    assert (kind.business_model, kind.business_model_source) == (INDUSTRIAL, "statements")
    assert ("business_model", "sic", BANK) in kind.conflicts


def test_an_ifrs_filer_falls_to_its_sic_code():
    """NU: no us-gaap annual report, SIC 6199."""
    kind = classify(None, 6199, "02 Finance")
    assert (kind.business_model, kind.business_model_source) == (BANK, "sic")


def test_a_catch_all_code_falls_to_the_baskets_gics_then_yahoo():
    """Visa: SIC 7389, Trade & Services; the baskets say Financials."""
    kind = classify(INDUSTRIAL, 7389, "07 Trade & Services", gics="Financials")
    assert (kind.sector, kind.sector_source) == ("Financials", "gics")
    assert classify(INDUSTRIAL, 7389, None, yahoo="Financial Services").sector_source == "yahoo"
    assert classify(None, None, None).sector == "Unknown"
    # GICS never makes a payments company a bank.
    assert classify(INDUSTRIAL, 7389, None, gics="Financials").business_model == INDUSTRIAL


def test_sec_first_beats_gics_and_says_so():
    """Alphabet: SIC 7370 Technology where the baskets' GICS says Communications."""
    kind = classify(INDUSTRIAL, 7370, "06 Technology", gics="Communication Services")
    assert (kind.sector, kind.sector_source) == ("Technology", "sic")
    assert ("sector", "gics", "Communications") in kind.conflicts


def test_the_baskets_vote_by_the_commonest_canonical_sector():
    assert gics_sector(["Information Technology", "Information Technology", "Other",
                        "Communication"]) == "Technology"
    assert gics_sector([None, "Other"]) is None


@pytest.mark.asyncio
async def test_the_overview_classifies_from_the_stores(db):
    db.add(SecCompany(cik=1, ticker="V", isin="US92826C8394", sic=7389,
                      owner_org="07 Trade & Services", statement_model=INDUSTRIAL))
    db.add(EtfHolding(fund_isin="IE0000000001", line_no=1, constituent_isin="US92826C8394",
                      constituent_name="VISA INC", weight_pct=1, sector="Financials"))
    await db.commit()
    company = await db.get(SecCompany, 1)
    kind = await CompanyFinancialsService(db)._classification(company, None)
    assert (kind.business_model, kind.sector, kind.sector_source) == (
        INDUSTRIAL, "Financials", "gics")


@pytest.mark.asyncio
async def test_each_fetch_stores_what_the_statements_show(db, monkeypatch):
    from app.config import settings
    from app.services.company_financials_sync import CompanyFinancialsSync
    from tests.test_company_financials_sync import NVDA_CIK, UA, Edgar, _hold, _sync

    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)

    async def no(self, cik, facts):
        return False

    monkeypatch.setattr(CompanyFinancialsSync, "_reads_extensions", no)
    monkeypatch.setattr(CompanyFinancialsSync, "_reads_segments", lambda *a: False)
    await _hold(db, 1, "NVDA")
    await db.commit()
    await _sync(db, Edgar()).refresh()
    company = await db.get(SecCompany, NVDA_CIK)
    assert company.statement_model == INDUSTRIAL
