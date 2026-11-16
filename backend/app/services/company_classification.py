"""
Every company's business model and sector, by a waterfall — the company's own regulator first
(owner, 2026-11-16; docs/company-financials.md, *Classification*).

Two answers per company, each with the source that decided it. Never a per-company table and never
a hand classification: every rule below reads a published standard or the company's own filing.

**The business model** decides which figures apply on the Company tab — an industrial's FCF, debt
and operating margin mean nothing for a bank, whose balance sheet is lending and borrowing:

1. **the statements** (`statement_model`): what the newest annual report tags — a bank's deposits
   and net interest income, a broker's customer payables, an insurer's premiums and claims. The
   company describing itself, audited and current. Read in the EDGAR pass, stored on
   `sec_companies.statement_model`.
2. **the SIC code** SEC assigned (`sec_companies.sic`): 6000-6299 a bank or broker, 6300-6411 an
   insurer, anything else an industrial.
3. **SEC's reviewing office** (`ownerOrg`, `sec_companies.owner_org`) where no SIC is stated.
4. the default, industrial (`DEFAULT`). The fund baskets' GICS sector is never used here: GICS
   puts Visa, Mastercard and PayPal in Financials, and they are not banks.

**The sector** (the overview, the screener, peers), in the app's eleven (`sector_taxonomy`):

1. **the SIC code**, translated by `SIC_SECTORS` — a translation of SEC's published code list,
   one range per industry group, not of any company. A catch-all code (`SIC_CATCH_ALL`: blank
   checks, "business services, not elsewhere classified" — Visa, Mastercard, PayPal) decides
   nothing and falls through.
2. **SEC's reviewing office**, where its name says one sector (`OFFICE_SECTORS`: Finance, Life
   Sciences, Technology); Manufacturing or Trade & Services say nothing a sector can use.
3. **the fund baskets' GICS sector** (`etf_holdings.sector` by the company's ISIN, the vote of the
   stored baskets, the Look-through's source).
4. **Yahoo's sector** of a held listing (`securities.sector`).
5. `Unknown`.

SEC first is the owner's choice and has a known cost: Alphabet and Meta are SIC 7370 (computer
programming), so Technology here where the Look-through, voting the baskets' GICS, says
Communications. A higher source disagreeing with a lower one is recorded (`conflicts`), never
resolved by hand. Pure: the inputs are what the stores hold.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.sector_taxonomy import UNKNOWN, normalise as normalise_sector

INDUSTRIAL = "industrial"
BANK = "bank"  # banks and brokers: their figures are interest, deposits, loans and fees
INSURER = "insurer"
MODELS = (INDUSTRIAL, BANK, INSURER)
DEFAULT = INDUSTRIAL

STATEMENTS = "statements"
SIC = "sic"
OFFICE = "sec_office"
GICS = "gics"
YAHOO = "yahoo"
DEFAULT_SOURCE = "default"

# ── The statements: what the newest annual report tags ──────────────────────

#: Per business model, the us-gaap concepts only that model's statements carry, and how many of
#: them the newest annual report must tag (measured over the 132 companies, 2026-11-16: see
#: docs/company-financials.md, *Classification*). A payments company holds customer funds
#: (PayPal) and tags none of a bank's: deposits and net interest income are a bank's
#: balance sheet and income statement, not a feature of one note.
STATEMENT_CONCEPTS: Dict[str, Tuple[Tuple[str, ...], int]] = {
    BANK: (
        (
            "Deposits",
            "InterestAndDividendIncomeOperating",
            "InterestIncomeExpenseNet",
            "NoninterestIncome",
            "NoninterestExpense",
            "InterestExpenseDeposits",
            "PayablesToCustomers",
            "ReceivablesFromBrokersDealersAndClearingOrganizations",
            "PayablesToBrokerDealersAndClearingOrganizations",
        ),
        3,
    ),
    INSURER: (
        (
            "PremiumsEarnedNet",
            "LiabilityForUnpaidClaimsAndClaimsAdjustmentExpense",
            "LiabilityForClaimsAndClaimsAdjustmentExpense",
            "LiabilityForFuturePolicyBenefits",
            "PolicyholderBenefitsAndClaimsIncurredNet",
            "IncurredClaimsPropertyCasualtyAndLiability",
            "DeferredPolicyAcquisitionCosts",
            "UnearnedPremiums",
            # BRK tags premiums and claims per segment (companyfacts leaves those out); the
            # changes in its reserves are undimensioned, and only an insurer has them.
            "IncreaseDecreaseInUnearnedPremiums",
            "IncreaseDecreaseInLiabilityForClaimsAndClaimsAdjustmentExpenseReserve",
        ),
        2,
    ),
}
ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "10-KT")


def statement_model(companyfacts: Dict[str, Any]) -> Optional[str]:
    """
    The business model the newest annual report's own concepts show, or None when the document
    holds no us-gaap annual report to read (an IFRS filer, a new filer): the next source decides.
    An insurer before a bank (an insurer holds investments and may tag interest income); an
    annual report tagging neither is an industrial's.
    """
    gaap = ((companyfacts or {}).get("facts") or {}).get("us-gaap") or {}
    newest: Optional[Tuple[str, str]] = None
    for body in gaap.values():
        for rows in ((body or {}).get("units") or {}).values():
            for row in rows or ():
                if row.get("form") in ANNUAL_FORMS and row.get("filed") and row.get("accn"):
                    key = (row["filed"], row["accn"])
                    if newest is None or key > newest:
                        newest = key
    if newest is None:
        return None
    accn = newest[1]

    def tagged(concept: str) -> bool:
        units = ((gaap.get(concept) or {}).get("units") or {}).values()
        return any(row.get("accn") == accn for rows in units for row in rows or ())

    for model in (INSURER, BANK):
        concepts, needed = STATEMENT_CONCEPTS[model]
        if sum(1 for c in concepts if tagged(c)) >= needed:
            return model
    return INDUSTRIAL


# ── SEC's codes ──────────────────────────────────────────────────────────────


def sic_model(sic: Optional[int]) -> Optional[str]:
    """The business model a SIC code says: banks, credit and brokers 6000-6299, insurers
    6300-6411, anything else an industrial; None without a code."""
    if sic is None:
        return None
    if 6000 <= sic <= 6299:
        return BANK
    if 6300 <= sic <= 6411:
        return INSURER
    return INDUSTRIAL


#: SIC codes that name no industry: blank checks, "services, not elsewhere classified" (7389:
#: Visa, Mastercard, PayPal), non-operating establishments. They decide no sector.
SIC_CATCH_ALL = frozenset({6770, 7389, 8900, 9995, 9999})

#: SEC's SIC code list, translated range by range into the app's sectors (`sector_taxonomy`).
#: First match wins, so a narrow range sits above the group it carves out of. A translation of
#: the published standard (www.sec.gov/search-filings/standard-industrial-classification-sic-code-list),
#: never of a company.
SIC_SECTORS: Tuple[Tuple[int, int, str], ...] = (
    # Technology carved out of manufacturing and services
    (3570, 3579, "Technology"),  # computer and office equipment
    (3661, 3669, "Technology"),  # telephone and communications equipment
    (3670, 3679, "Technology"),  # electronic components, semiconductors
    (3820, 3829, "Technology"),  # measuring and testing instruments
    (7370, 7379, "Technology"),  # computer programming, software, data processing
    # Healthcare
    (2830, 2836, "Healthcare"),  # drugs, biological products
    (3840, 3851, "Healthcare"),  # medical instruments and supplies, ophthalmic goods
    (5047, 5047, "Healthcare"),  # medical equipment wholesale
    (5122, 5122, "Healthcare"),  # drugs wholesale
    (8000, 8099, "Healthcare"),  # health services
    (8731, 8731, "Healthcare"),  # commercial physical and biological research
    # Consumer, carved out before their groups
    (2840, 2844, "Consumer Defensive"),  # soaps, cleaners, toiletries
    (3011, 3011, "Consumer Cyclical"),  # tires
    (3630, 3652, "Consumer Cyclical"),  # household appliances, audio and video
    (3711, 3716, "Consumer Cyclical"),  # motor vehicles and parts
    (3751, 3751, "Consumer Cyclical"),  # motorcycles, bicycles
    (3940, 3949, "Consumer Cyclical"),  # toys, sporting goods
    (5140, 5149, "Consumer Defensive"),  # groceries wholesale
    (5331, 5331, "Consumer Defensive"),  # variety stores
    (5400, 5499, "Consumer Defensive"),  # food stores
    (5912, 5912, "Consumer Defensive"),  # drug stores
    (4950, 4959, "Industrials"),  # sanitary services, waste
    (4610, 4619, "Energy"),  # pipelines
    (6798, 6798, "Real Estate"),  # real estate investment trusts
    (1520, 1531, "Consumer Cyclical"),  # residential building, operative builders
    # The divisions and major groups
    (100, 999, "Consumer Defensive"),  # agriculture, forestry, fishing
    (1000, 1099, "Basic Materials"),  # metal mining
    (1200, 1299, "Energy"),  # coal
    (1300, 1399, "Energy"),  # oil and gas extraction
    (1400, 1499, "Basic Materials"),  # nonmetallic minerals
    (1500, 1799, "Industrials"),  # construction
    (2000, 2199, "Consumer Defensive"),  # food, beverages, tobacco
    (2200, 2399, "Consumer Cyclical"),  # textiles, apparel
    (2400, 2499, "Basic Materials"),  # lumber, wood
    (2500, 2599, "Consumer Cyclical"),  # furniture
    (2600, 2699, "Basic Materials"),  # paper
    (2700, 2799, "Communications"),  # printing and publishing
    (2800, 2899, "Basic Materials"),  # chemicals
    (2900, 2999, "Energy"),  # petroleum refining
    (3000, 3099, "Basic Materials"),  # rubber and plastics
    (3100, 3199, "Consumer Cyclical"),  # leather, footwear
    (3200, 3399, "Basic Materials"),  # stone, glass, cement, primary metals
    (3400, 3599, "Industrials"),  # fabricated metal, industrial machinery
    (3600, 3699, "Industrials"),  # electrical equipment
    (3700, 3799, "Industrials"),  # transportation equipment
    (3800, 3899, "Industrials"),  # instruments
    (3900, 3999, "Consumer Cyclical"),  # miscellaneous manufacturing
    (4000, 4699, "Industrials"),  # transportation
    (4700, 4799, "Consumer Cyclical"),  # travel arrangement
    (4800, 4899, "Communications"),  # communications
    (4900, 4999, "Utilities"),  # electric, gas, water
    (5000, 5199, "Industrials"),  # wholesale
    (5200, 5999, "Consumer Cyclical"),  # retail
    (6000, 6499, "Financials"),  # banks, credit, brokers, insurance
    (6500, 6599, "Real Estate"),  # real estate
    (6700, 6799, "Financials"),  # holding and investment offices
    (7000, 7099, "Consumer Cyclical"),  # hotels
    (7200, 7299, "Consumer Cyclical"),  # personal services
    (7300, 7399, "Industrials"),  # business services
    (7500, 7599, "Consumer Cyclical"),  # auto repair and services
    (7800, 7899, "Communications"),  # motion pictures
    (7900, 7999, "Consumer Cyclical"),  # amusement and recreation
    (8200, 8299, "Consumer Cyclical"),  # education
    (8700, 8799, "Industrials"),  # engineering, accounting, management
)

#: SEC's reviewing offices whose name says one sector. The others (Manufacturing, Trade &
#: Services, Energy & Transportation, Real Estate & Construction, Industrial Applications) span
#: several and say nothing a sector can use.
OFFICE_SECTORS = {
    "finance": "Financials",
    "life sciences": "Healthcare",
    "technology": "Technology",
}


def sic_sector(sic: Optional[int]) -> Optional[str]:
    """The sector SEC's code list puts a SIC code in, or None (no code, a catch-all, or a code
    the list does not place)."""
    if sic is None or sic in SIC_CATCH_ALL:
        return None
    for low, high, sector in SIC_SECTORS:
        if low <= sic <= high:
            return sector
    return None


def _office(owner_org: Optional[str]) -> str:
    """`04 Manufacturing` -> `manufacturing`."""
    name = (owner_org or "").strip().lower()
    head, _, tail = name.partition(" ")
    return tail.strip() if head.isdigit() else name


#: SEC's reviewing offices that review operating companies, not financial ones. Finance reviews
#: banks, insurers and payments companies alike, and Crypto Assets brokers (IBKR) as well as
#: miners: neither can name a model.
INDUSTRIAL_OFFICES = frozenset({
    "manufacturing", "technology", "life sciences", "energy & transportation",
    "real estate & construction", "trade & services", "industrial applications and services",
})


def office_model(owner_org: Optional[str]) -> Optional[str]:
    """Industrial where SEC's reviewing office reviews operating companies; None otherwise."""
    return INDUSTRIAL if _office(owner_org) in INDUSTRIAL_OFFICES else None


def office_sector(owner_org: Optional[str]) -> Optional[str]:
    return OFFICE_SECTORS.get(_office(owner_org))


# ── The waterfall ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Classification:
    business_model: str
    business_model_source: str
    sector: str
    sector_source: str
    # A lower source naming another answer than the one that decided: (field, source, answer).
    conflicts: Tuple[Tuple[str, str, str], ...] = field(default_factory=tuple)


def gics_sector(basket_sectors: Iterable[Optional[str]]) -> Optional[str]:
    """The fund baskets' vote for one ISIN: the commonest canonical sector their rows give it
    (ties by name), or None."""
    votes = Counter(s for s in (normalise_sector(raw) for raw in basket_sectors) if s != UNKNOWN)
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: (kv[1], kv[0]))[0]


def classify(
    statements: Optional[str],
    sic: Optional[int],
    owner_org: Optional[str],
    gics: Optional[str] = None,
    yahoo: Optional[str] = None,
) -> Classification:
    """The two waterfalls over what the stores hold (see the module docstring)."""
    models: List[Tuple[str, Optional[str]]] = [
        (STATEMENTS, statements if statements in MODELS else None),
        (SIC, sic_model(sic)),
        (OFFICE, office_model(owner_org)),
    ]
    sectors: List[Tuple[str, Optional[str]]] = [
        (SIC, sic_sector(sic)),
        (OFFICE, office_sector(owner_org)),
        (GICS, normalise_sector(gics) if gics else None),
        (YAHOO, normalise_sector(yahoo) if yahoo else None),
    ]
    model, model_source = _first(models, DEFAULT)
    sector, sector_source = _first(sectors, UNKNOWN)
    conflicts = tuple(
        [("business_model", src, ans) for src, ans in models if ans and ans != model]
        + [("sector", src, ans) for src, ans in sectors if ans and ans != UNKNOWN and ans != sector]
    )
    return Classification(model, model_source, sector, sector_source, conflicts)


def _first(chain: Sequence[Tuple[str, Optional[str]]], default: str) -> Tuple[str, str]:
    for source, answer in chain:
        if answer and answer != UNKNOWN:
            return answer, source
    return default, DEFAULT_SOURCE
