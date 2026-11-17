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

**Beyond the SEC** (2026-11-17) the company's own regulator's code comes first in both chains:
a Korean company's KSIC code from OpenDART (`induty_code`), translated division by division by
`KSIC_SECTORS` and `ksic_model` (banking and brokerage a bank, insurance an insurer). An ESEF
report states no industry, so a European company falls to the baskets' GICS.

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
KSIC = "ksic"
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


# ── Korea's standard industrial classification (KSIC, 10th revision) ─────────

#: KSIC's divisions and the groups carved out of them, translated into the app's sectors as
#: `SIC_SECTORS` translates SEC's list: (code prefix, sector, the standard's name). The longest
#: prefix wins. A translation of the published standard (kssc.kostat.go.kr), never of a company.
KSIC_SECTORS: Tuple[Tuple[str, str, str], ...] = (
    ("01", "Consumer Defensive", "Agriculture"),
    ("02", "Basic Materials", "Forestry"),
    ("03", "Consumer Defensive", "Fishing"),
    ("05", "Energy", "Mining of coal"),
    ("06", "Energy", "Extraction of crude petroleum and natural gas"),
    ("07", "Basic Materials", "Mining of metal ores"),
    ("08", "Basic Materials", "Mining of non-metallic minerals"),
    ("10", "Consumer Defensive", "Manufacture of food products"),
    ("11", "Consumer Defensive", "Manufacture of beverages"),
    ("12", "Consumer Defensive", "Manufacture of tobacco products"),
    ("13", "Consumer Cyclical", "Manufacture of textiles"),
    ("14", "Consumer Cyclical", "Manufacture of wearing apparel"),
    ("15", "Consumer Cyclical", "Manufacture of leather and footwear"),
    ("16", "Basic Materials", "Manufacture of wood products"),
    ("17", "Basic Materials", "Manufacture of pulp and paper"),
    ("18", "Industrials", "Printing and reproduction of recorded media"),
    ("19", "Energy", "Manufacture of coke and refined petroleum products"),
    ("20", "Basic Materials", "Manufacture of chemicals"),
    ("21", "Healthcare", "Manufacture of pharmaceuticals"),
    ("22", "Basic Materials", "Manufacture of rubber and plastic products"),
    ("23", "Basic Materials", "Manufacture of non-metallic mineral products"),
    ("24", "Basic Materials", "Manufacture of basic metals"),
    ("25", "Industrials", "Manufacture of fabricated metal products"),
    ("26", "Technology", "Manufacture of electronic components, computers and communication equipment"),
    ("261", "Technology", "Manufacture of semiconductors"),
    ("262", "Technology", "Manufacture of electronic components"),
    ("263", "Technology", "Manufacture of computers and peripheral equipment"),
    ("264", "Technology", "Manufacture of telecommunication and broadcasting apparatuses"),
    ("265", "Consumer Cyclical", "Manufacture of video and audio equipment"),
    ("27", "Technology", "Manufacture of medical, precision and optical instruments"),
    ("271", "Healthcare", "Manufacture of medical and dental equipment"),
    ("274", "Consumer Cyclical", "Manufacture of watches and clocks"),
    ("28", "Industrials", "Manufacture of electrical equipment"),
    ("29", "Industrials", "Manufacture of other machinery and equipment"),
    ("30", "Consumer Cyclical", "Manufacture of motor vehicles and trailers"),
    ("31", "Industrials", "Manufacture of other transport equipment"),
    ("32", "Consumer Cyclical", "Manufacture of furniture"),
    ("33", "Consumer Cyclical", "Other manufacturing"),
    ("34", "Industrials", "Repair of industrial machinery"),
    ("35", "Utilities", "Electricity, gas, steam and air conditioning supply"),
    ("36", "Utilities", "Water supply"),
    ("37", "Industrials", "Sewage and waste treatment"),
    ("38", "Industrials", "Waste collection and materials recovery"),
    ("39", "Industrials", "Remediation activities"),
    ("41", "Industrials", "General construction"),
    ("42", "Industrials", "Specialized construction"),
    ("45", "Consumer Cyclical", "Sale of motor vehicles and parts"),
    ("46", "Industrials", "Wholesale trade"),
    ("47", "Consumer Cyclical", "Retail trade"),
    ("49", "Industrials", "Land transport"),
    ("50", "Industrials", "Water transport"),
    ("51", "Industrials", "Air transport"),
    ("52", "Industrials", "Warehousing and transport support"),
    ("55", "Consumer Cyclical", "Accommodation"),
    ("56", "Consumer Cyclical", "Food and beverage service"),
    ("58", "Communications", "Publishing"),
    ("582", "Technology", "Software publishing"),
    ("59", "Communications", "Motion picture, video and audio production"),
    ("60", "Communications", "Broadcasting"),
    ("61", "Communications", "Telecommunications"),
    ("62", "Technology", "Computer programming, consultancy and related activities"),
    ("63", "Technology", "Information service activities"),
    ("64", "Financials", "Financial service activities"),
    ("65", "Financials", "Insurance and pension funding"),
    ("66", "Financials", "Activities auxiliary to financial services"),
    ("68", "Real Estate", "Real estate activities"),
    ("70", "Industrials", "Research and development"),
    ("71", "Industrials", "Professional services"),
    ("72", "Industrials", "Architectural and engineering services"),
    ("73", "Industrials", "Other scientific and technical services"),
    ("74", "Industrials", "Business facilities management and landscape services"),
    ("75", "Industrials", "Business support services"),
    ("76", "Industrials", "Rental and leasing"),
    ("85", "Consumer Cyclical", "Education"),
    ("86", "Healthcare", "Human health activities"),
    ("87", "Healthcare", "Social work activities"),
    ("90", "Consumer Cyclical", "Creative, arts and recreation related services"),
    ("91", "Consumer Cyclical", "Sports and amusement activities"),
    ("94", "Industrials", "Membership organizations"),
    ("95", "Consumer Cyclical", "Maintenance and repair services"),
    ("96", "Consumer Cyclical", "Other personal services"),
)


def _ksic(code: Optional[str]) -> Optional[Tuple[str, str]]:
    """The longest `KSIC_SECTORS` prefix of a code: (sector, the standard's name), or None."""
    code = (code or "").strip()
    if not code.isdigit():
        return None
    best = None
    for prefix, sector, name in KSIC_SECTORS:
        if code.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, sector, name)
    return (best[1], best[2]) if best else None


def ksic_sector(code: Optional[str]) -> Optional[str]:
    found = _ksic(code)
    return found[0] if found else None


def ksic_name(code: Optional[str]) -> Optional[str]:
    """The standard's English name for the code's division or group (`KSIC_SECTORS`)."""
    found = _ksic(code)
    return found[1] if found else None


def ksic_model(code: Optional[str]) -> Optional[str]:
    """
    The business model a KSIC code says: banking (641) and securities brokerage (6612) a bank
    or broker, insurance and pension funding (65) an insurer, any other code an industrial;
    None without a code — and for the other financial groups (642 trusts and collective
    investment, 649 other financial services, which holds every holding company, financial
    or not), which no model can be read off.
    """
    code = (code or "").strip()
    if not code.isdigit():
        return None
    if code.startswith("641") or code.startswith("6612"):
        return BANK
    if code.startswith("65"):
        return INSURER
    if code.startswith(("642", "649")) or code == "64":
        return None
    return INDUSTRIAL


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
    ksic: Optional[str] = None,
) -> Classification:
    """The two waterfalls over what the stores hold (see the module docstring)."""
    models: List[Tuple[str, Optional[str]]] = [
        (KSIC, ksic_model(ksic)),
        (STATEMENTS, statements if statements in MODELS else None),
        (SIC, sic_model(sic)),
        (OFFICE, office_model(owner_org)),
    ]
    sectors: List[Tuple[str, Optional[str]]] = [
        (KSIC, ksic_sector(ksic)),
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
