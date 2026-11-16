"""
SEC companyfacts -> one row per (period, metric), written once (docs/company-financials.md).

The only place that knows which XBRL concept means what, how a 10-Q's year-to-date figures
become quarters, which of several filings of one period wins, and how a stock split is
undone. `company_financials_service` reads the rows this produces; nothing else parses
companyfacts. Pure: no database, no network, no clock.

The rules, each of which the NVDA sample (`tests/fixtures/edgar_nvda_companyfacts.json`)
exercises:

- **Concept chains, per period.** Each metric names a list of us-gaap concepts; for every
  period the first concept in the list that reports it wins, so a company that moved from
  `SalesRevenueNet` to `RevenueFromContractWithCustomer...` keeps one continuous series.
  Quarters are derived *within* one concept before the chains merge. The one difference
  taken across concepts is the last-resort Q4 = FY - (Q1 + Q2 + Q3) of the merged series,
  for a company that tags its years under one concept and its quarters under another (ORCL).
- **Durations, not `fp`/`fy`.** `fy` is the fiscal year of the *filing*, and every filing
  carries comparatives, so facts are keyed by (start, end) and classified by length: about
  a quarter, about a year, or a year-to-date rung in between.
- **Year-to-date ladders.** A 10-Q's cash-flow items run from the fiscal year's start. Two
  rungs of one ladder a quarter apart give the quarter between them, and the 10-K's year
  minus the nine-month rung gives Q4. A reported discrete quarter is preferred to a derived
  one. Flows and per-share figures are differenced; weighted-average share counts are
  differenced by day weight (`(Y*d_Y - N*d_N) / (d_Y - d_N)`), which is what a weighted
  average is. A derived Q4 EPS is the year's EPS less the nine months' — the usual
  approximation, exact only while the share count holds still.
- **The latest filing of a period wins**, which is how a restatement replaces the original.
- **Splits and scale slips are undone from EDGAR itself.** Every filing repeats earlier
  periods, so the same period appears in filings made before and after a split. Walking the
  filings from the newest back, each filing's values are compared with the already-adjusted
  values of newer filings for the same periods; the ratios vote for that filing's split
  factor (snapped to a plausible split ratio, `SPLIT_RATIOS`), and per concept for a
  factor-of-1000 slip (early XBRL filings tagged some counts in thousands). A filing that
  shares no period with a newer one inherits the next newer filing's factor, and a reported
  share count about 1000x off net income / EPS is rescaled (ONDS).
- **Tagging slips stay absent.** A quarter context carrying the year's figure is dropped;
  for a figure that cannot be negative, so is a negative reported fact, a negative derived
  quarter and a year smaller than its own nine months.
- **Fiscal periods are labelled from the period end.** A quarter belongs to the fiscal year
  whose span contains it, Q1-Q4 by its distance from that year's start, and the fiscal year
  is named after the calendar year it ends in (a year ending in the first week of January is
  named after the year before, as 52/53-week filers name it).
- **An unknown stays absent.** A concept a company never reports (a broker's capex) yields
  no row, never a zero.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.edgar_client import ANNUAL_FORM_PREFIXES, PERIODIC_FORMS

# ── What each metric is, and where it may come from ──────────────────────────

MONETARY = "monetary"
PER_SHARE = "per_share"
SHARES = "shares"

FLOW = "flow"  # a duration; quarters derived from year-to-date ladders
INSTANT = "instant"  # a balance at the period end


@dataclass(frozen=True)
class MetricSpec:
    kind: str  # MONETARY | PER_SHARE | SHARES
    timing: str  # FLOW | INSTANT
    concepts: Tuple[str, ...]
    # Never below zero. A negative derived quarter, or a year smaller than its own
    # nine months, is then a tagging slip (NVDA's FY2016 10-K tagged Q4's dividend as the
    # year's), and stays absent rather than charted.
    non_negative: bool = False


#: Continuing operations' EPS: net income, which includes the discontinued ones, over it is no
#: share count either.
CONTINUING_EPS = "IncomeLossFromContinuingOperationsPerDilutedShare"
#: The cover page's count (`dei`), for a company whose balance sheet tags none (NVDA, TSLA,
#: PEP). It is dated the cover date, weeks after the period; `_facts` keys it to the newest
#: period end its own filing reports, so it sits beside that filing's figures.
COVER_SHARES = "EntityCommonStockSharesOutstanding"
# The available-for-sale debt securities' total, and the metric read only to tell whether it
# is all current (`_total_only_when_current`, review item CO8).
AFS_TOTAL = "AvailableForSaleSecuritiesDebtSecurities"
NONCURRENT_INVESTMENTS = "noncurrent_investments"
# The basic count, read only to stand in for the diluted one in a loss period
# (`_basic_in_a_loss`); never stored as a figure.
BASIC_SHARES = "basic_shares"

# Verified across the book's SEC filers (2026-10-25). Order is preference.
#
# Revenue starts with `RevenuesNetOfInterestExpense`, which only a bank or a broker tags and
# which is then its top line: IBKR stopped tagging `Revenues` after 2019, and the next
# concept, `RevenueFromContractWithCustomer...`, is its commissions alone (a third of it).
METRICS: Dict[str, MetricSpec] = {
    "revenue": MetricSpec(
        MONETARY,
        FLOW,
        (
            "RevenuesNetOfInterestExpense",
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            # CRWD and ODFL report revenue with the taxes they collect included.
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueNet",
            # ODFL's revenue before 2018: freight services.
            "SalesRevenueServicesNet",
            # A utility's top line (XEL from 2019, when it stopped tagging `Revenues`: its
            # revenue had ended there). Last, so no company that tags another concept moves.
            "RegulatedAndUnregulatedOperatingRevenue",
        ),
        non_negative=True,
    ),
    # The parent's share before the consolidated one: IBKR stopped tagging
    # `NetIncomeLoss` after 2014, and its `ProfitLoss` includes the non-controlling
    # interests (about five times what its EPS is computed on).
    "net_income": MetricSpec(
        MONETARY,
        FLOW,
        (
            "NetIncomeLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "ProfitLoss",
        ),
    ),
    # A company without dilutive securities may tag one figure for both (ALNY to 2021); one
    # without discontinued operations may tag only the continuing figure (ABNB throughout,
    # MNST after 2011), which is then its EPS.
    "eps_diluted": MetricSpec(
        PER_SHARE,
        FLOW,
        (
            "EarningsPerShareDiluted",
            "EarningsPerShareBasicAndDiluted",
            CONTINUING_EPS,
        ),
    ),
    "operating_cash_flow": MetricSpec(
        MONETARY,
        FLOW,
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
    ),
    "capex": MetricSpec(
        MONETARY,
        FLOW,
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsForCapitalImprovements",
            # LLY's "purchases of property and equipment" (7.8B in 2025), and VZ's capex from
            # 2019 (17-23B a year, continuing `PaymentsToAcquireProductiveAssets` exactly) and
            # ROP's: without them their FCF ended in 2019-2022 (checked 2026-11-15).
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",
            "PaymentsToAcquireOtherProductiveAssets",
            # An oil producer's capex is its drilling: FANG tags nothing else.
            "PaymentsToAcquireOilAndGasPropertyAndEquipment",
            "PaymentsToAcquireOilAndGasProperty",
        ),
        non_negative=True,
    ),
    "sbc": MetricSpec(
        MONETARY,
        FLOW,
        (
            "ShareBasedCompensation",
            "AllocatedShareBasedCompensationExpense",
        ),
        non_negative=True,
    ),
    # A company with nothing dilutive, or in a loss quarter where dilution is anti-dilutive,
    # may tag one count for both (ZS, WDAY, PANW, DDOG): it is then the diluted count.
    "diluted_shares": MetricSpec(
        SHARES,
        FLOW,
        (
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        ),
        non_negative=True,
    ),
    "operating_income": MetricSpec(MONETARY, FLOW, ("OperatingIncomeLoss",)),
    "dividends_per_share": MetricSpec(
        PER_SHARE,
        FLOW,
        (
            "CommonStockDividendsPerShareDeclared",
            "CommonStockDividendsPerShareCashPaid",
        ),
        non_negative=True,
    ),
    "cash": MetricSpec(
        MONETARY,
        INSTANT,
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            # The balance sheet's cash while a business was held for sale (MDLZ 2016-2018).
            "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations",
        ),
    ),
    # Cash and short-term investments as one line (MSFT, GOOGL, ADBE), and the investments
    # alone for those that tag them apart: the line item first, then its older spellings.
    # Net cash needs them: cash alone showed NVDA, with $49B of securities, as a net debtor.
    "cash_and_short_term": MetricSpec(
        MONETARY, INSTANT, ("CashCashEquivalentsAndShortTermInvestments",)
    ),
    "short_term_investments": MetricSpec(
        MONETARY,
        INSTANT,
        (
            "ShortTermInvestments",
            "MarketableSecuritiesCurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
            "AvailableForSaleSecuritiesCurrent",
            # The total of the available-for-sale debt securities, current or not: KLAC's
            # balance-sheet "Marketable securities" (3.2B beside 1.5B of cash, read on the
            # server 2026-11-08, review item CO8). Counted only for a period with nothing
            # non-current tagged beside it (`NONCURRENT_INVESTMENTS`, `_total_only_when_current`).
            AFS_TOTAL,
        ),
    ),
    # Only read where the diluted count is missing in a loss period; never stored.
    BASIC_SHARES: MetricSpec(
        SHARES, FLOW, ("WeightedAverageNumberOfSharesOutstandingBasic",), non_negative=True
    ),
    # Only read to decide whether `AFS_TOTAL` is all current; never stored as a figure.
    NONCURRENT_INVESTMENTS: MetricSpec(
        MONETARY,
        INSTANT,
        (
            "MarketableSecuritiesNoncurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
            "AvailableForSaleSecuritiesNoncurrent",
            "HeldToMaturitySecuritiesNoncurrent",
            "LongTermInvestments",
        ),
    ),
    # Gross profit as tagged; the service falls back to revenue less cost of revenue, the
    # only form META, GOOGL and SPGI file. `CostOfServices` alone is a part of the cost
    # (SPGI, ORCL), so it is not in the chain.
    "gross_profit": MetricSpec(MONETARY, FLOW, ("GrossProfit",)),
    "cost_of_revenue": MetricSpec(
        MONETARY,
        FLOW,
        (
            "CostOfRevenue",
            "CostOfGoodsAndServicesSold",
            "CostOfGoodsSold",
        ),
        non_negative=True,
    ),
    "buybacks": MetricSpec(
        MONETARY,
        FLOW,
        ("PaymentsForRepurchaseOfCommonStock",),
        non_negative=True,
    ),
    "dividends_paid": MetricSpec(
        MONETARY,
        FLOW,
        (
            "PaymentsOfDividendsCommonStock",
            "PaymentsOfDividends",
            "PaymentsOfOrdinaryDividends",
        ),
        non_negative=True,
    ),
    # Debt, current maturities included: the total as tagged, and its two halves for the
    # companies that tag only those (MA, GOOGL's quarters); the service adds them. Never
    # `DebtInstrumentCarryingAmount`, which ORCL's 10-Qs tag per new bond issue, not as a
    # total. Commercial paper and short-term borrowings are not counted beside a current
    # maturity; they are the current half of a company that tags nothing else current (NFLX).
    # ORCL moved to notes-and-loans concepts in 2026, which left it with no debt at all. Not
    # `DebtLongtermAndShorttermCombinedAmount`: it is principal, ~1% off the carrying amounts
    # the halves add (STX).
    "debt_total": MetricSpec(
        MONETARY,
        INSTANT,
        (
            "LongTermDebt",
            "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        ),
    ),
    "debt_noncurrent": MetricSpec(
        MONETARY,
        INSTANT,
        (
            "LongTermDebtNoncurrent",
            "LongTermDebtAndCapitalLeaseObligations",
            "LongTermNotesAndLoans",
            # A company whose only debt is convertible notes (PANW, DDOG, ZS) tags nothing else.
            "ConvertibleDebtNoncurrent",
            "ConvertibleLongTermNotesPayable",
        ),
    ),
    "debt_current": MetricSpec(
        MONETARY,
        INSTANT,
        (
            "LongTermDebtCurrent",
            "DebtCurrent",
            "NotesPayableCurrent",
            "ShortTermBorrowings",
            # Current maturities with the finance leases (ODFL to 2022, CSX throughout). After
            # the others, so a company that tags `DebtCurrent` beside it keeps that figure.
            "LongTermDebtAndCapitalLeaseObligationsCurrent",
            "ConvertibleNotesPayableCurrent",
            "ConvertibleDebtCurrent",
        ),
    ),
    # The balance sheet's count at the period end, for the market cap. A company with several
    # classes tags it per class, which companyfacts leaves out: none is read for it.
    "shares_outstanding": MetricSpec(
        SHARES,
        INSTANT,
        ("CommonStockSharesOutstanding", COVER_SHARES),
        non_negative=True,
    ),
    # The parent's equity before the consolidated figure, as net income is the parent's.
    "equity": MetricSpec(
        MONETARY,
        INSTANT,
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
    ),
}

#: The source recorded for a share count derived as net income / diluted EPS — for the
#: periods a company reported EPS but not the count (GOOGL before 2022). Never from
#: `ProfitLoss`: it includes non-controlling interests, so dividing it by EPS is no share
#: count.
SHARES_FROM_EPS_EXCLUDED_INCOME = "ProfitLoss"
SHARES_FROM_EPS_SOURCE = "NetIncomeLoss/EarningsPerShareDiluted"
#: The other way round: a period's EPS the company did not tag, from net income over its
#: diluted share count. EXC's 10-Ks tag no year's EPS (its combined filing dimensions it), so
#: it had no fourth quarter and no TTM EPS. Not over `ProfitLoss`, nor over a count that was
#: itself net income over EPS. Where preferred dividends are paid, this reads them as common.
EPS_FROM_SHARES_SOURCE = "NetIncomeLoss/WeightedAverageNumberOfDilutedSharesOutstanding"
#: How imprecise the EPS may be (its rounding to the cent, relative) for that derivation.
SHARES_FROM_EPS_MAX_ERROR = 0.02
# The unit-slip check needs only to tell 1 from 1000, so it takes a far coarser EPS, and a
# diluted count may sit well off NI/EPS (anti-dilution in a loss year) without being one.
SHARES_SLIP_EPS_MAX_ERROR = 0.25
SHARES_SLIP_TOLERANCE = 0.5
SHARE_COUNT_SLIPS: Tuple[Fraction, ...] = (
    Fraction(1, 1000),
    Fraction(1000),
    Fraction(1, 1_000_000),
    Fraction(1_000_000),
)

ANNUAL = "FY"
QUARTERS = ("Q1", "Q2", "Q3", "Q4")

#: Forms whose facts count: `edgar_client.PERIODIC_FORMS`, the one set.
FORMS = PERIODIC_FORMS
# A concept named like a current investment (`_unchained_investments`): marketable or
# held-to-maturity securities, available-for-sale or debt securities, or investments,
# classified current. Restricted balances are not liquid.
INVESTMENT_LIKE = re.compile(
    r"^(?!Restricted)\w*(MarketableSecurities|Investments|AvailableForSale|HeldToMaturity"
    r"|DebtSecurities)\w*Current$"
)

# 12 to 14 weeks, or a retailer's 16- and 17-week fourth quarter: COST's 112 days had no Q4
# from its ladder, so its year-end balances never reached a quarter (no Q4 cash or equity).
QUARTER_DAYS = (75, 120)
YEAR_DAYS = (350, 380)
# Two figures this close are one figure reported twice.
SAME_VALUE = 1e-6
# A year-to-date figure carrying one quarter's figure is at most half the two or more
# reported quarters inside it; a restated quarter moves that sum by far less (NXPI's 2023
# 10-Q re-tagged Q3 2021's dividend at the 2023 rate: +17%), and must not unseat the rung.
NOT_CUMULATIVE_RATIO = 1.5

# Split ratios a filing's factor may snap to, as share-count multipliers (a 4:1 split is 4).
SPLIT_RATIOS: Tuple[Fraction, ...] = tuple(
    sorted(
        {Fraction(n) for n in range(1, 51)}
        | {Fraction(1, n) for n in range(2, 51)}
        | {Fraction(a, b) for a, b in ((3, 2), (4, 3), (5, 4), (5, 2), (5, 3))}
        | {Fraction(b, a) for a, b in ((3, 2), (4, 3), (5, 4), (5, 2), (5, 3))}
    )
)
# A concept's own unit slip: tagged in thousands or millions, or that many times too large
# (PCAR's 2019 10-K tags its share counts in millions; STX's 2024 10-K its dividends per
# share a million times over).
SCALE_SLIPS: Tuple[Fraction, ...] = (
    Fraction(1, 1_000_000),
    Fraction(1, 1000),
    Fraction(1),
    Fraction(1000),
    Fraction(1_000_000),
)
# A per-share fact one filing tags a thousand or a million times off the value this many
# other filings agree on, for the same concept and period, is that filing's slip in that
# context: DXCM's 2014 10-K re-tagged its 2012 quarters' EPS a million times over (-210000
# beside three filings' -0.21) while its years stayed right, so no one factor for the filing
# held both, and the older filings were voted a million times up to match it. Dropped before
# the filings are walked (`_drop_lone_slips`). Only these factors: a split's is far smaller.
LONE_SLIP_MIN_AGREEING = 2
# Loose on purpose: the factors are a thousand apart, and a small EPS is cent-rounded.
LONE_SLIP_TOLERANCE = 0.25

# The order a split vote tries them in: no slip first.
SLIPS_IN_ORDER: Tuple[Fraction, ...] = (
    Fraction(1),
    Fraction(1, 1000),
    Fraction(1000),
    Fraction(1, 1_000_000),
    Fraction(1_000_000),
)

# A filing that shares no period with a newer one (a 10-K, until the next 10-K) inherits
# the newer filing's split factor — wrong when a split fell between the two: BKNG's 25:1
# (2026) came after its last pre-split 10-K and Q3 10-Q, and every Q3, Q4 and year since
# 2012 stayed in the old basis while Q1 and Q2 were adjusted (CRWD's 4:1 and KLAC's 10:1
# the same). `_neighbour_split` reads such a filing's basis from the nearest periods.
NEIGHBOUR_DAYS = 100
# How far a derived quarter's share count may stand from a reported neighbour's: DXCM's 2020
# Q4 came to 518M between 398M and 418M (a restated year less unrestated nine months).
DERIVED_COUNT_MAX_STEP = 1.2
NEIGHBOUR_TOLERANCE = 0.05
NEIGHBOUR_MIN_COUNTS = 2
# The basic share count votes on splits and is never stored. A split moves it with the
# diluted count; a restatement of dilution moves only the diluted one (ASU 2020-06's
# if-converted method for convertibles), and shares and EPS then move by reciprocal factors
# exactly as in a split: REGN's counts before 2014 read 25% high (a "5:4"), DXCM's 2020
# quarters carried 3 for its 4:1. When a filing's basic counts vote, they decide.
SPLIT_WITNESS = "WeightedAverageNumberOfSharesOutstandingBasic"
#: The dividends declared in money, read only to witness a dividend ladder that carries one
#: rate on every rung (`_flat_rate_years`) and never stored: IBKR's 0.10 on its three-, six-,
#: nine- and twelve-month contexts beside 7M, 14M, 21M and 28M declared.
DIVIDEND_TOTALS = ("DividendsCommonStockCash", "DividendsCommonStock", "Dividends")
#: A year's dividend figures are one rate when the largest is within this of the smallest
#: (BKNG's 9.60 over its 25:1 split is 0.384 beside the restated 0.38); a raise is more.
FLAT_RATE_TOLERANCE = 0.02
#: ... and its quarters' declared totals each within this of their mean. Coarse on purpose:
#: IBKR tags 7M or 8M a quarter in millions, while an annual declarer's quarters (WMT) or a
#: semi-annual one's (INTC) are four times or twice the mean, or nothing.
FLAT_TOTALS_TOLERANCE = 0.25

# The ratios companies split by, and no change. Not every whole number: adjacent periods'
# weighted counts differ by a few percent, and BKNG's 24.3 snapped to 24, not 25. No 3:2,
# too close to an acquisition's issuance.
_COMMON_SPLITS = (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50)
NEIGHBOUR_RATIOS: Tuple[Fraction, ...] = tuple(
    sorted(
        {Fraction(1)}
        | {Fraction(n) for n in _COMMON_SPLITS}
        | {Fraction(1, n) for n in _COMMON_SPLITS}
    )
)

# Relative agreement two filings' values of one period need to vote. Per-share figures are
# rounded to the cent, so their tolerance grows as the figure shrinks, and a pair whose
# rounding alone could exceed `MAX_VOTE_TOLERANCE` does not vote.
VALUE_TOLERANCE = 0.02
CENT_ROUNDING = 0.005
MAX_VOTE_TOLERANCE = 0.04


@dataclass(frozen=True)
class Fact:
    concept: str
    kind: str
    unit: str
    start: Optional[date]
    end: date
    val: float
    accn: str
    filed: date
    form: str


@dataclass
class Obs:
    """One concept's value for one period, after adjustment and the latest-filing rule."""

    value: float
    filed: date
    start: Optional[date]
    end: date
    unit: str
    concept: str
    annual_form: bool = False
    # Absolute rounding bound, per-share figures only (for the shares-from-EPS check).
    error: float = 0.0
    # Differenced from a year-to-date ladder rather than reported.
    derived: bool = False
    # Every filing's (filed, adjusted value) for this span, oldest first (`_as_of`).
    vintages: Tuple[Tuple[date, float], ...] = ()


@dataclass
class Normalised:
    rows: List[Dict[str, Any]] = field(default_factory=list)
    currency: Optional[str] = None
    entity_name: Optional[str] = None
    # The splits the filings restated history for (`filed_splits`): each
    # {"after", "by", "ratio"} — the last filing on the old basis, the first on the new
    # (ISO dates), and new shares per old.
    splits: List[Dict[str, Any]] = field(default_factory=list)
    # Current-investment concepts outside every chain carrying a value at the newest
    # balance sheet where no chained one does (`_unchained_investments`).
    unchained_investments: List[str] = field(default_factory=list)
    # The concepts outside every chain that filled a figure's gaps (`_bridge`):
    # {metric: {concept: periods filled}}.
    bridged: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # The company's own concepts, named by their labels, that did (`_extend`).
    extended: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def has_data(self) -> bool:
        return bool(self.rows)


# ── Parsing ──────────────────────────────────────────────────────────────────


def _date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _is_currency(unit: str) -> bool:
    return len(unit) == 3 and unit.isalpha() and unit.isupper()


def _unit_fits(kind: str, unit: str) -> bool:
    if kind == MONETARY:
        return _is_currency(unit)
    if kind == PER_SHARE:
        head, _, tail = unit.partition("/")
        return tail == "shares" and _is_currency(head)
    return unit == "shares"


# A 20-F filer's IFRS concepts, read as their US-GAAP twins so every chain, ladder and vote
# below applies unchanged (review item R6, 2026-11-08: NU and TSMC file `ifrs-full`, annual
# only). Only clear equivalents: an IFRS concept with no exact twin (`Borrowings`, which is
# not long-term debt) stays unread, and its metric absent.
IFRS_TWINS: Dict[str, str] = {
    "Revenue": "Revenues",
    "RevenueFromContractsWithCustomers": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "ProfitLossAttributableToOwnersOfParent": "NetIncomeLoss",
    "ProfitLoss": "ProfitLoss",
    "DilutedEarningsLossPerShare": "EarningsPerShareDiluted",
    "ProfitLossFromOperatingActivities": "OperatingIncomeLoss",
    "GrossProfit": "GrossProfit",
    "CostOfSales": "CostOfRevenue",
    "CashFlowsFromUsedInOperatingActivities": "NetCashProvidedByUsedInOperatingActivities",
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": (
        "PaymentsToAcquirePropertyPlantAndEquipment"
    ),
    "AdjustedWeightedAverageShares": "WeightedAverageNumberOfDilutedSharesOutstanding",
    "DividendsRecognisedAsDistributionsToOwnersPerShare": "CommonStockDividendsPerShareDeclared",
    "DividendsPaidClassifiedAsFinancingActivities": "PaymentsOfDividends",
    "PaymentsToAcquireOrRedeemEntitysShares": "PaymentsForRepurchaseOfCommonStock",
    "CashAndCashEquivalents": "CashAndCashEquivalentsAtCarryingValue",
    "EquityAttributableToOwnersOfParent": "StockholdersEquity",
    "Equity": "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
}


def _taxonomy(companyfacts: Dict[str, Any], wanted: Dict[str, str]) -> Dict[str, Any]:
    """
    The us-gaap block, or — for a filer with no us-gaap concept a chain reads — its
    `ifrs-full` block keyed by the US-GAAP twins (`IFRS_TWINS`). Never both: a filer that
    switched taxonomies would otherwise count one year twice.
    """
    facts = (companyfacts or {}).get("facts") or {}
    gaap = facts.get("us-gaap") or {}
    if any(concept in gaap for concept in wanted):
        return gaap
    ifrs = facts.get("ifrs-full") or {}
    return {twin: ifrs[name] for name, twin in IFRS_TWINS.items() if name in ifrs}


def _reporting_currency(gaap: Dict[str, Any], wanted: Dict[str, str]) -> Optional[str]:
    """
    The currency the company reports in now: the one its newest periodic filing states its
    monetary figures in most. None when it states none.

    Per concept, "the currency it reports most" kept a company's past: NBIS (Yandex until
    2024) reported in RUB for fifteen years and in USD since, so every concept kept RUB and
    the series ended at FY2023 (2026-11-15). The newest filing is the company as it is;
    TSMC's 20-F adds a USD translation of its latest year beside TWD for every year, so it
    stays in TWD.
    """
    newest: Optional[Tuple[date, str]] = None
    by_filing: Dict[str, Counter] = defaultdict(Counter)
    for concept, kind in wanted.items():
        if kind != MONETARY:
            continue
        for unit, rows in ((gaap.get(concept) or {}).get("units") or {}).items():
            if not _is_currency(unit):
                continue
            for row in rows:
                filed, accn = _date(row.get("filed")), row.get("accn")
                if row.get("form") not in FORMS or filed is None or not accn:
                    continue
                by_filing[accn][unit] += 1
                if newest is None or (filed, accn) > newest:
                    newest = (filed, accn)
    if newest is None:
        return None
    return max(by_filing[newest[1]].items(), key=lambda item: (item[1], item[0]))[0]


def _unit_for(kind: str, fitting: Dict[str, Any], reporting: Optional[str]) -> Optional[str]:
    """
    The unit to read a concept in: the reporting currency's (or its per-share unit), and a
    concept stated only in another currency is not read at all — it is the company's past
    in an old currency, and beside the new one's figures it would mix the two in one series.
    A share count has no currency; without a reporting currency, the unit reported most.
    """
    if reporting is None or kind == SHARES:
        return max(fitting, key=lambda u: (len(fitting[u]), u))
    wanted = reporting if kind == MONETARY else f"{reporting}/shares"
    return wanted if wanted in fitting else None


def _wanted() -> Dict[str, str]:
    """Every concept read from companyfacts, with its kind."""
    wanted = {c: spec.kind for spec in METRICS.values() for c in spec.concepts}
    wanted[SPLIT_WITNESS] = SHARES
    wanted.update({c: MONETARY for c in DIVIDEND_TOTALS})
    return wanted


def reporting_currency(companyfacts: Dict[str, Any]) -> Optional[str]:
    """The currency the company reports in now (`_reporting_currency`)."""
    wanted = _wanted()
    return _reporting_currency(_taxonomy(companyfacts, wanted), wanted)


def _facts(companyfacts: Dict[str, Any]) -> List[Fact]:
    """Every usable fact of every concept a chain names, in the concept's main unit."""
    wanted = _wanted()
    never_negative = {
        c for spec in METRICS.values() if spec.non_negative for c in spec.concepts
    }
    gaap = _taxonomy(companyfacts, wanted)
    reporting = _reporting_currency(gaap, wanted)
    out: List[Fact] = []
    for concept, kind in wanted.items():
        units = (gaap.get(concept) or {}).get("units") or {}
        fitting = {u: rows for u, rows in units.items() if _unit_fits(kind, u) and rows}
        if not fitting:
            continue
        unit = _unit_for(kind, fitting, reporting)
        if unit is None:
            continue
        for row in fitting[unit]:
            if row.get("form") not in FORMS:
                continue
            end, filed = _date(row.get("end")), _date(row.get("filed"))
            val, accn = row.get("val"), row.get("accn")
            if end is None or filed is None or not accn or isinstance(val, bool):
                continue
            if not isinstance(val, (int, float)):
                continue
            # A sign slip in a reported figure that cannot be negative (HON's Q1 2009 10-Q
            # tags dividends paid as -224M): dropped before any ladder differences it, so
            # the period falls back to another filing's value or stays absent.
            if concept in never_negative and val < 0:
                continue
            start = _date(row.get("start"))
            out.append(
                Fact(
                    concept,
                    kind,
                    unit,
                    start,
                    end,
                    float(val),
                    accn,
                    filed,
                    row["form"],
                )
            )
    # The cover count, keyed to its filing's newest period end (COVER_SHARES).
    period_end: Dict[str, date] = {}
    for f in out:
        if f.start is not None and f.end <= f.filed:
            period_end[f.accn] = max(period_end.get(f.accn, f.end), f.end)
    dei = ((companyfacts or {}).get("facts") or {}).get("dei") or {}
    for row in ((dei.get(COVER_SHARES) or {}).get("units") or {}).get("shares") or []:
        accn, val, filed = row.get("accn"), row.get("val"), _date(row.get("filed"))
        if row.get("form") not in FORMS or accn not in period_end or filed is None:
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
            continue
        out.append(
            Fact(
                COVER_SHARES, SHARES, "shares", None, period_end[accn], float(val),
                accn, filed, row["form"],
            )
        )
    return out


def _filing_facts(
    base: Sequence[Fact],
    filing_facts: Iterable[Dict[str, Any]],
    reporting: Optional[str] = None,
) -> List[Fact]:
    """
    Facts read from the filings themselves (`edgar_filing_facts`: a dividend per share a
    company tagged only on a dimension member, which companyfacts leaves out), as `Fact`s
    under their own filing's accession number, so the split vote and the latest-filing rule
    treat them as that filing's. They only fill: a period companyfacts has any fact of that
    concept for keeps companyfacts' answer. A figure in another currency than the company
    reports in (`reporting`) is not read, as `_unit_for` reads companyfacts: TSMC's 20-F
    translates its latest year into USD beside the TWD every year is stated in.
    """
    wanted = {c: spec.kind for spec in METRICS.values() for c in spec.concepts}
    instant = {
        c for spec in METRICS.values() if spec.timing == INSTANT for c in spec.concepts
    }
    never_negative = {
        c for spec in METRICS.values() if spec.non_negative for c in spec.concepts
    }
    tagged = {(f.concept, f.start, f.end) for f in base}
    out: List[Fact] = []
    for row in filing_facts:
        concept, unit = row.get("concept"), row.get("unit")
        kind = wanted.get(concept)
        if kind is None or not isinstance(unit, str) or not _unit_fits(kind, unit):
            continue
        if reporting is not None and kind != SHARES and unit.split("/")[0] != reporting:
            continue
        start, end, filed = (_date(row.get(k)) for k in ("start", "end", "filed"))
        val, accn, form = row.get("val"), row.get("accn"), row.get("form")
        if None in (end, filed) or not accn or form not in FORMS:
            continue
        # A balance sheet's instant has no start (a filing companyfacts has not taken in
        # yet, review item CO5); a flow without one is not a period.
        if start is None and concept not in instant:
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if (concept in never_negative and val < 0) or (concept, start, end) in tagged:
            continue
        out.append(Fact(concept, kind, unit, start, end, float(val), accn, filed, form))
    return out


# ── Splits and scale slips ───────────────────────────────────────────────────


def _snap(
    x: float, candidates: Sequence[Fraction], tolerance: float
) -> Optional[Fraction]:
    if x <= 0:
        return None
    best, best_err = None, None
    for c in candidates:
        err = abs(x / float(c) - 1.0)
        if err <= tolerance and (best_err is None or err < best_err):
            best, best_err = c, err
    return best


def _tolerance(f: Fact, g: Fact) -> Optional[float]:
    if f.kind != PER_SHARE:
        return VALUE_TOLERANCE
    tol = VALUE_TOLERANCE + CENT_ROUNDING / abs(f.val) + CENT_ROUNDING / abs(g.val)
    return tol if tol <= MAX_VOTE_TOLERANCE else None


def _drop_lone_slips(facts: List[Fact]) -> List[Fact]:
    """
    The facts less each one a lone filing tagged a `SCALE_SLIPS` factor off the rest. Proven
    where `LONE_SLIP_MIN_AGREEING` other filings agree; then that filing's same concept off by
    the same factor from a single other filing is the same slip (DXCM's 2012 Q4, which only
    the 2013 10-K reported besides). Only where that filing's concept agrees with another
    filing somewhere: one off everywhere it overlaps is a filing in other units, which the
    vote in `adjustments` rescales whole. Per-share figures only: share counts in thousands
    were a convention several filings shared (MPWR's to 2013), so two of them agreeing is no
    majority, and dropping their overlap left MPWR's Q4 2012 at 36763 shares.
    """
    groups: Dict[Tuple[str, Optional[date], date], List[Fact]] = defaultdict(list)
    for f in facts:
        if f.val != 0 and f.kind == PER_SHARE:
            groups[(f.concept, f.start, f.end)].append(f)
    slips = [float(s) for s in SCALE_SLIPS if s != 1]

    def agreeing(f: Fact, group: List[Fact], factor: float) -> int:
        return sum(
            abs(f.val / (g.val * factor) - 1) <= LONE_SLIP_TOLERANCE
            for g in group
            if g.accn != f.accn
        )

    consistent = {
        (f.accn, f.concept)
        for group in groups.values()
        for f in group
        if agreeing(f, group, 1.0)
    }
    proven = set()
    for group in groups.values():
        for f in group:
            if (f.accn, f.concept) not in consistent or agreeing(f, group, 1.0):
                continue
            for factor in slips:
                if agreeing(f, group, factor) >= LONE_SLIP_MIN_AGREEING:
                    proven.add((f.accn, f.concept, factor))
    slipped = set()
    for group in groups.values():
        for f in group:
            if agreeing(f, group, 1.0):
                continue
            if any(
                (f.accn, f.concept, factor) in proven and agreeing(f, group, factor)
                for factor in slips
            ):
                slipped.add(id(f))
    return [f for f in facts if id(f) not in slipped]


def _split_part(kind: str, split: Fraction) -> Fraction:
    """A filing's split factor as the multiplier of a value of `kind`."""
    if kind == SHARES:
        return split
    if kind == PER_SHARE:
        return 1 / split
    return Fraction(1)


def _vote(votes: Counter, fallback: Fraction) -> Fraction:
    if not votes:
        return fallback
    top = max(votes.values())
    winners = [k for k, n in votes.items() if n == top]
    if fallback in winners:
        return fallback
    # A tie between two other factors: the one nearer the fallback.
    return min(winners, key=lambda k: abs(float(k) - float(fallback)))


def adjustments(
    facts: Iterable[Fact], events: Optional[List[Dict[str, Any]]] = None
) -> Dict[Tuple[str, str], float]:
    """
    (concept, accn) -> the multiplier that puts that filing's values of that concept into
    the newest filing's basis: its split factor (shares up, per-share down, money
    untouched) times the concept's own scale slip, if any. See the module docstring.
    `events`, when given, receives each change of split factor between two consecutive
    filings (`filed_splits`).
    """
    by_accn: Dict[str, List[Fact]] = defaultdict(list)
    by_period: Dict[Tuple[str, Optional[date], date], List[Fact]] = defaultdict(list)
    filed_of: Dict[str, date] = {}
    for f in facts:
        by_accn[f.accn].append(f)
        by_period[(f.concept, f.start, f.end)].append(f)
        filed_of[f.accn] = max(filed_of.get(f.accn, f.filed), f.filed)

    multiplier: Dict[Tuple[str, str], float] = {}
    # Each walked filing's split factor. A vote snaps the step from the newer filing it is
    # compared with, never the whole factor: two splits compound past `SPLIT_RATIOS`
    # (NFLX's 7:1 in 2015 and 10:1 in 2025 make 70, which snapped to 1/14 with a
    # thousands slip).
    split_of: Dict[str, Fraction] = {}
    # Share-count facts of the filings already walked, for `_neighbour_split`.
    walked_counts: Dict[str, List[Fact]] = defaultdict(list)
    previous_split = Fraction(1)
    previous_accn: Optional[str] = None
    for accn in sorted(by_accn, key=lambda a: (filed_of[a], a), reverse=True):
        pairs = []
        split_votes: Counter = Counter()
        witness_votes: Counter = Counter()
        for f in by_accn[accn]:
            if f.val == 0:
                continue
            for g in by_period[(f.concept, f.start, f.end)]:
                if g.accn not in split_of or g.val == 0 or (g.val > 0) != (f.val > 0):
                    continue
                tol = _tolerance(f, g)
                if tol is None:
                    continue
                ratio = g.val * multiplier[(g.concept, g.accn)] / f.val
                pairs.append((f, ratio, tol))
                if f.kind == MONETARY:
                    continue
                as_shares = ratio if f.kind == SHARES else 1.0 / ratio
                step = as_shares / float(split_of[g.accn])
                for slip in SLIPS_IN_ORDER:
                    snapped = _snap(step / float(slip), SPLIT_RATIOS, tol)
                    if snapped is not None:
                        split_votes[snapped * split_of[g.accn]] += 1
                        if f.concept == SPLIT_WITNESS:
                            witness_votes[snapped * split_of[g.accn]] += 1
                        break
        if witness_votes:
            split = _vote(witness_votes, previous_split)
        elif split_votes:
            split = _vote(split_votes, previous_split)
        else:
            split = (
                _neighbour_split(by_accn[accn], walked_counts, multiplier, split_of)
                or previous_split
            )

        scale_votes: Dict[str, Counter] = defaultdict(Counter)
        for f, ratio, tol in pairs:
            residual = ratio / float(_split_part(f.kind, split))
            slip = _snap(residual, SCALE_SLIPS, tol)
            if slip is not None:
                scale_votes[f.concept][slip] += 1
        for concept in {f.concept for f in by_accn[accn]}:
            kind = next(f.kind for f in by_accn[accn] if f.concept == concept)
            slip = _vote(scale_votes[concept], Fraction(1))
            multiplier[(concept, accn)] = float(slip * _split_part(kind, split))
        split_of[accn] = split
        for f in by_accn[accn]:
            if f.kind == SHARES and f.val > 0:
                walked_counts[f.concept].append(f)
        if events is not None and previous_accn is not None and split != previous_split:
            events.append({
                "after": filed_of[accn].isoformat(),
                "by": filed_of[previous_accn].isoformat(),
                "ratio": float(split / previous_split),
            })
        previous_split, previous_accn = split, accn
    return multiplier


def _unchained_investments(companyfacts: Dict[str, Any], end: date) -> List[str]:
    """
    The us-gaap concepts named like current investments that no chain reads, with a
    positive value on the balance sheet dated `end` (review item CO8, 2026-11-08). KLAC's
    marketable securities sit in one, so its net cash counted cash alone; the sync names
    each such concept so the chain can whitelist it (`short_term_investments`). Named, never
    read: a concept is a member of a chain only once someone saw what it holds.
    """
    gaap = ((companyfacts or {}).get("facts") or {}).get("us-gaap") or {}
    chained = {c for spec in METRICS.values() for c in spec.concepts}
    out = []
    for concept, body in gaap.items():
        if concept in chained or not INVESTMENT_LIKE.match(concept):
            continue
        rows = ((body or {}).get("units") or {}).get("USD") or []
        if any(
            row.get("form") in FORMS
            and not row.get("start")
            and _date(row.get("end")) == end
            and isinstance(row.get("val"), (int, float))
            and row["val"] > 0
            for row in rows
        ):
            out.append(concept)
    return sorted(out)


# ── Bridging: a gap filled from a concept that proves itself ─────────────────

#: Per metric, the us-gaap concepts that may stand in for it where its chain is silent:
#: (named like it, not named like something else). Flows and cash only — a debt total
#: agrees with its noncurrent half whenever nothing is current, so agreement proves less
#: there (debt has `_no_current_debt_line` instead).
BRIDGE_FAMILIES: Dict[str, Tuple[str, str]] = {
    "revenue": (
        r"Revenue|Sales",
        r"Cost|Deferred|Receivable|Unearned|Remaining|Liabilit|Tax|Gain|Loss|Proceeds"
        r"|Percent|Allowance|Discount|Reserve|Return|Adjustment|Per|Excise",
    ),
    "net_income": (
        r"^(NetIncomeLoss|ProfitLoss)",
        r"PerShare|Noncontrolling|Discontinued|Adjustment|Preferred|Participating|Diluted",
    ),
    "operating_income": (r"^OperatingIncomeLoss", r"Segment|Other"),
    "operating_cash_flow": (r"^NetCashProvidedByUsedInOperatingActivities", r"Discontinued"),
    "capex": (
        r"^PaymentsToAcquire.*(PropertyPlant|ProductiveAssets|CapitalAssets|FixedAssets)"
        r"|^PaymentsForCapitalImprovements|CapitalExpenditure",
        r"Incurred|Accrued|NotYetPaid|Business|Investment|Intangible|RealEstate|Held",
    ),
    "sbc": (r"ShareBasedCompensation$|StockBasedCompensation$", r"Tax|Capitalized|Unrecognized"),
    "cost_of_revenue": (
        r"^Cost(Of)?(Revenue|GoodsAndServicesSold|GoodsSold)",
        r"Amortization|Depreciation|Excluding|Member|Other|Related",
    ),
    "gross_profit": (r"^GrossProfit", r"Percent"),
    "buybacks": (r"^PaymentsForRepurchaseOf(Common|Equity)", r"Preferred|Redeemable"),
    "dividends_paid": (r"^PaymentsOf(Ordinary)?Dividends", r"Preferred|Noncontrolling|Minority"),
    "cash": (r"^Cash(AndCashEquivalents|CashEquivalents)", r"Increase|Decrease|Period|Restricted|Segregated|Disposal"),
}
#: How close a stand-in must be to the chain's own figure to count as agreeing, how many
#: periods must agree, and the share of the shared periods that must.
BRIDGE_TOLERANCE = 0.005
BRIDGE_MIN_AGREEMENTS = 2
BRIDGE_MIN_SHARE = 0.9


def _candidate_series(
    gaap: Dict[str, Any], concept: str, spec: MetricSpec, unit: str, mapping: Dict[date, date]
) -> Dict[Tuple[str, date], Obs]:
    """One concept outside the chains, read like a chain concept: its quarters and years
    (flows) or its balances (instants), by (grain, canonical end)."""
    rows = (((gaap.get(concept) or {}).get("units") or {}).get(unit)) or []
    facts = []
    for row in rows:
        end, filed = _date(row.get("end")), _date(row.get("filed"))
        val, accn = row.get("val"), row.get("accn")
        if row.get("form") not in FORMS or end is None or filed is None or not accn:
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if spec.non_negative and val < 0:
            continue
        facts.append(Fact(concept, spec.kind, unit, _date(row.get("start")), end,
                          float(val), accn, filed, row["form"]))
    return _series(facts, concept, spec, mapping)


def _series(
    facts: Sequence[Fact], concept: str, spec: MetricSpec, mapping: Dict[date, date]
) -> Dict[Tuple[str, date], Obs]:
    """One concept's facts read like a chain concept's, by (grain, canonical end)."""
    periods = _latest(facts, {}).get(concept, {})
    out: Dict[Tuple[str, date], Obs] = {}
    if spec.timing == INSTANT:
        for (start, end), obs in periods.items():
            if start is None:
                for grain in ("Q", ANNUAL):
                    out[(grain, mapping.get(end, end))] = obs
        return out
    quarters, years, _ = _quarters_and_years(spec.kind, periods, spec.non_negative)
    for grain, by_end in (("Q", quarters), (ANNUAL, years)):
        for end, obs in _rekey(by_end, mapping).items():
            out[(grain, end)] = obs
    return out


def _bridge(
    values: Dict[str, Dict[Tuple[str, date], Obs]],
    keys: Dict[Tuple[str, date], Tuple[int, str]],
    companyfacts: Dict[str, Any],
    mapping: Dict[date, date],
) -> Dict[str, Dict[str, int]]:
    """
    Fill a figure's missing periods from a concept outside every chain that has proved to
    be the same line: on the periods both report, it agrees with the chain's figure within
    `BRIDGE_TOLERANCE`, at least `BRIDGE_MIN_AGREEMENTS` times and on `BRIDGE_MIN_SHARE` of
    them. Only fills; never replaces a figure.

    The chains name the concepts seen so far, and a company that moves its line to another
    standard concept had its series stop there until someone noticed and added the concept
    by hand (owner, 2026-11-15: "this should be closed in the script, not one by one"). The
    agreement is the proof a name alone is not: `RegulatedOperatingRevenueGas` is named like
    revenue and is a part of it, so it never agrees and never fills. A concept that agrees
    but never overlaps — a company whose chain was empty from the start — proves nothing and
    fills nothing. Returns what filled what, which the pass records.
    """
    gaap = ((companyfacts or {}).get("facts") or {}).get("us-gaap") or {}
    if not gaap:
        return {}
    chained = {c for spec in METRICS.values() for c in spec.concepts}
    filled: Dict[str, Dict[str, int]] = {}
    for metric, (named, not_named) in BRIDGE_FAMILIES.items():
        spec, own = METRICS[metric], values[metric]
        missing = [k for k in keys if k not in own]
        if not missing or not own:
            continue
        unit = next(iter(own.values())).unit
        proven = []
        for concept in gaap:
            if concept in chained or not re.search(named, concept) or re.search(not_named, concept):
                continue
            series = _candidate_series(gaap, concept, spec, unit, mapping)
            if not any(k in series for k in missing):
                continue
            shared = [k for k in series if k in own and own[k].value]
            agree = [k for k in shared
                     if abs(series[k].value / own[k].value - 1) <= BRIDGE_TOLERANCE]
            if len(agree) >= BRIDGE_MIN_AGREEMENTS and len(agree) >= BRIDGE_MIN_SHARE * len(shared):
                proven.append((len(agree), concept, series))
        for _, concept, series in sorted(proven, key=lambda p: (-p[0], p[1])):
            for key in [k for k in keys if k not in own and k in series]:
                own[key] = series[key]
                filled.setdefault(metric, {}).setdefault(concept, 0)
                filled[metric][concept] += 1
    return filled


# ── A company's own concepts, named by their labels ──────────────────────────


def _extend(
    values: Dict[str, Dict[Tuple[str, date], Obs]],
    keys: Dict[Tuple[str, date], Tuple[int, str]],
    extension_facts: Iterable[Dict[str, Any]],
    mapping: Dict[date, date],
    reporting: Optional[str],
) -> Dict[str, Dict[str, int]]:
    """
    Fill a figure's missing periods from a company's own concept, which companyfacts never
    carries, read from its filings with the label the filing gives it
    (`edgar_filing_facts.EXTENSION_LABELS`: NVDA's "Purchases of property and equipment
    and intangible assets" FY2013-FY2021, CDNS's "Carrying value of debt outstanding").

    A label is a name; the figures are the proof, as in `_bridge`: on every period the
    concept and the chain both report it agrees with the chain's figure within
    `BRIDGE_TOLERANCE` (`BRIDGE_MIN_SHARE` of them) — a debt total with the two halves
    added where no total is tagged. Where they never overlap (the reason the concept is
    needed at all) the label alone names it. Only fills; never replaces a figure. Returns
    what filled what, which the pass records, and the source is the concept itself
    (`nvda:PurchasesOf...`).
    """
    by: Dict[Tuple[str, str], List[Fact]] = defaultdict(list)
    for row in extension_facts:
        metric, concept, unit = row.get("metric"), row.get("concept"), row.get("unit")
        spec = METRICS.get(metric) if isinstance(metric, str) else None
        if spec is None or spec.kind != MONETARY or not isinstance(concept, str):
            continue
        if not isinstance(unit, str) or not _is_currency(unit):
            continue
        if reporting is not None and unit != reporting:
            continue
        start, end, filed = (_date(row.get(k)) for k in ("start", "end", "filed"))
        val, accn, form = row.get("val"), row.get("accn"), row.get("form")
        if None in (end, filed) or not accn or form not in FORMS:
            continue
        if (start is None) != (spec.timing == INSTANT):
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if spec.non_negative and val < 0:
            continue
        by[(metric, concept)].append(
            Fact(concept, MONETARY, unit, start, end, float(val), accn, filed, form)
        )
    filled: Dict[str, Dict[str, int]] = {}
    for (metric, concept), facts in sorted(by.items()):
        spec, own = METRICS[metric], values[metric]
        series = _series(facts, concept, spec, mapping)
        missing = [k for k in keys if k not in own and k in series]
        if not missing:
            continue
        reference = dict(own)
        if metric == "debt_total":
            current, noncurrent = values["debt_current"], values["debt_noncurrent"]
            for key in current.keys() & noncurrent.keys():
                reference.setdefault(key, Obs(
                    value=current[key].value + noncurrent[key].value,
                    filed=current[key].filed, start=None, end=current[key].end,
                    unit=current[key].unit, concept="halves",
                ))
        shared = [k for k in series if k in reference and reference[k].value]
        agree = [k for k in shared
                 if abs(series[k].value / reference[k].value - 1) <= BRIDGE_TOLERANCE]
        if len(agree) < BRIDGE_MIN_SHARE * len(shared):
            continue
        for key in missing:
            if key in reference:  # a total the halves already give
                continue
            own[key] = series[key]
            filled.setdefault(metric, {}).setdefault(concept, 0)
            filled[metric][concept] += 1
    return filled


def _basic_in_a_loss(values: Dict[str, Dict[Tuple[str, date], Obs]]) -> None:
    """
    The basic count as the diluted one for a period with a net loss and no diluted count.

    In a loss every potential share is anti-dilutive, so diluted equals basic by definition
    (ASC 260), and some companies then tag only the basic count: TTWO's loss quarters from
    mid-2025 had no share count, so no per-share figure (2026-11-15). Never in a profitable
    period, where the two differ. Only a reported count: a quarter differenced out of two
    year-to-date averages is no count (TTWO's FY2014 Q4 came to 121M between 112M and 79M).
    The basic figures are dropped here, read for this alone.
    """
    basic = values.pop(BASIC_SHARES, {})
    diluted, income = values["diluted_shares"], values["net_income"]
    for key, obs in basic.items():
        loss = income.get(key)
        if obs.derived or key in diluted:
            continue
        if loss is not None and loss.value < 0:
            diluted[key] = obs


def _total_only_when_current(values: Dict[str, Dict[Tuple[str, date], Obs]]) -> None:
    """
    `AFS_TOTAL` stands for the short-term investments only where the company tagged no
    non-current investments for the same period: a total with nothing non-current beside
    it is all current (KLAC, review item CO8), while beside a non-current figure it would
    count long-dated securities as liquid. The non-current figures themselves are dropped
    here, read for this test alone.
    """
    noncurrent = values.pop(NONCURRENT_INVESTMENTS, {})
    investments = values.get("short_term_investments", {})
    for key, obs in list(investments.items()):
        if obs.concept == AFS_TOTAL and key in noncurrent:
            del investments[key]


#: The source recorded for a current-debt half read as zero (`_no_current_debt_line`).
NO_CURRENT_DEBT_LINE = "no current debt on the balance sheet"
# A balance-sheet line that is current debt under any concept, chained or not: the filing
# shows one, so a zero would be a guess. A lease alone is not debt here (no chain counts one
# beside the notes) — but debt and leases together are (CSX); securities, receivables and
# investments are assets, and a maturity schedule or a cash flow is no balance.
CURRENT_DEBT_LIKE = re.compile(r"(Debt|Notes|Borrowing|CommercialPaper).*Current|Current.*(Debt|Borrowing)")
# The maturity table's principal due within twelve months, and the share of the noncurrent
# half from which it contradicts a zero current half (`_no_current_debt_line`).
DUE_WITHIN_A_YEAR = (
    "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
    "LongtermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
)
DUE_SOON_SHARE = 0.02
NOT_CURRENT_DEBT = re.compile(
    r"AvailableForSale|Securities|Receivable|Investment|Asset|Maturities|Repayments|Proceeds"
    r"|Payments|FairValue|^(Finance|Operating|CapitalLease)Lease|LeaseLiabilit"
)


def _no_current_debt_line(
    values: Dict[str, Dict[Tuple[str, date], Obs]],
    facts: Sequence[Fact],
    companyfacts: Dict[str, Any],
    mapping: Dict[date, date],
) -> None:
    """
    A current-debt half of zero where the balance sheet has no current debt to show.

    Debt is the tagged total, else the two halves added (`company_financials_service._debt`),
    and a company with no current maturities tags no current line at all — so META, ABNB,
    TTWO, PYPL and WDAY had no debt, and no net cash, in most quarters (measured over the
    132 companies, 2026-11-15). The zero is read off the filing itself, not assumed: the
    newest filing that reports the noncurrent half at that date reports nothing current and
    debt-like there, under any concept (`CURRENT_DEBT_LIKE`), chained or not.

    companyfacts carries no company-specific concept, so a current portion tagged under one
    is invisible here (PYPL's current notes, about 1.4B). The maturity table is the witness:
    a company whose table states principal due within twelve months (`DUE_SOON_SHARE` of the
    noncurrent half or more) on any date this would zero gets no zero anywhere — debt stays
    unknown for it, as before. That also costs ORLY its zeros, which were right (it classes
    notes due within the year as noncurrent, refinanced through its revolver): a missed
    figure is the cheaper error.
    """
    noncurrent_chain = set(METRICS["debt_noncurrent"].concepts)
    newest: Dict[date, Tuple[date, str]] = {}
    for f in facts:
        if f.concept in noncurrent_chain and f.start is None and f.end in mapping:
            end = mapping[f.end]
            if end not in newest or (f.filed, f.accn) > newest[end]:
                newest[end] = (f.filed, f.accn)
    if not newest:
        return
    gaap = ((companyfacts or {}).get("facts") or {}).get("us-gaap") or {}
    shown: set = set()
    for concept, body in gaap.items():
        if not CURRENT_DEBT_LIKE.search(concept) or NOT_CURRENT_DEBT.search(concept):
            continue
        for rows in ((body or {}).get("units") or {}).values():
            for row in rows:
                end = _date(row.get("end"))
                if end is not None and "start" not in row:
                    shown.add((row.get("accn"), mapping.get(end, end)))
    due_soon: Dict[date, float] = {}
    for concept in DUE_WITHIN_A_YEAR:
        for rows in ((gaap.get(concept) or {}).get("units") or {}).values():
            for row in rows:
                end = _date(row.get("end"))
                if end is not None and isinstance(row.get("val"), (int, float)):
                    due_soon[mapping.get(end, end)] = float(row["val"])
    current, total = values["debt_current"], values["debt_total"]
    zeros = {}
    for key, half in values["debt_noncurrent"].items():
        filing = newest.get(key[1])
        if key in current or key in total or filing is None or (filing[1], key[1]) in shown:
            continue
        if due_soon.get(key[1], 0.0) >= DUE_SOON_SHARE * abs(half.value) > 0:
            return
        zeros[key] = Obs(
            value=0.0, filed=half.filed, start=None, end=half.end, unit=half.unit,
            concept=NO_CURRENT_DEBT_LINE, derived=True,
        )
    current.update(zeros)


BALANCE_SHEET_TOTAL = "LiabilitiesAndStockholdersEquity"
# The witness that a balance sheet carries debt companyfacts does not show (BRK and PCAR
# report theirs per segment, which companyfacts leaves out): interest paid on borrowings,
# annualised, above `INTEREST_SHARE` of the balance sheet. A debt-free company pays next to
# none (a lease's interest, a customer deposit's); BRK and PCAR pay about 1%.
INTEREST = re.compile(r"^Interest(Expense|Paid|AndDebtExpense)")
NOT_INTEREST = re.compile(r"Income|Capitalized|Receivable|Deposits|Operating")
INTEREST_SHARE = 0.002
# Interest for a period ending this close to a balance-sheet date speaks for it: a 10-Q's
# quarter, or the year a 10-K reports (PCAR states interest paid only annually).
INTEREST_WINDOW_DAYS = 200
DEBT_GAP_DAYS = 400
#: The source recorded for both halves of a debt-free balance sheet (`_no_debt_at_all`).
NO_DEBT_LINE = "no debt on the balance sheet"
# A borrowing of any kind, current or not, under any concept — a maturity table included,
# since one states principal owed (only balances are read, so a repayment or a proceeds
# flow never counts). Securities, receivables and investments are what the company holds,
# a facility's capacity or rate is not a drawn balance, and "liabilities other than
# long-term debt" (ISRG) is the opposite of a borrowing.
DEBT_LIKE = re.compile(r"Debt|Notes|Borrowing|CommercialPaper|LoansPayable|LineOfCredit")
NOT_DEBT = re.compile(
    r"AvailableForSale|Securities|Receivable|Investment|Asset|OtherThanLongtermDebt"
    r"|Capacity|Commitment|Percent|Rate|Covenant|Fee"
)


def _no_debt_at_all(
    values: Dict[str, Dict[Tuple[str, date], Obs]],
    facts: Sequence[Fact],
    companyfacts: Dict[str, Any],
    mapping: Dict[date, date],
) -> None:
    """
    Both debt halves of zero for a balance sheet that shows no borrowing at all.

    ANET, ISRG, PLTR, MPWR, TER and others carry no debt and tag none, so their debt — and
    with it their net cash, the figure that matters most for them — was unknown in every
    quarter (2026-11-15). The zero is read off the filing: the newest one reporting the
    company's cash at that date states nothing debt-like there (`DEBT_LIKE`), no maturity
    table either, under any concept, and it pays no material interest (`INTEREST_SHARE`):
    BRK and PCAR report their debt per segment, which companyfacts leaves out, and only
    their interest shows it. A balance sheet with a borrowing the chains cannot read keeps
    no debt, as before; this never fires beside any debt figure.
    """
    gaap = ((companyfacts or {}).get("facts") or {}).get("us-gaap") or {}
    # The filing whose balance sheet is at that date: its total. A cash figure alone can be a
    # cash flow statement's opening or closing balance for a date whose balance sheet that
    # filing does not carry (2009-2010 quarters read debt-free beside billions of notes).
    newest: Dict[date, Tuple[date, str]] = {}
    for rows in ((gaap.get(BALANCE_SHEET_TOTAL) or {}).get("units") or {}).values():
        for row in rows:
            end, filed, accn = _date(row.get("end")), _date(row.get("filed")), row.get("accn")
            if end is None or filed is None or not accn or "start" in row or end not in mapping:
                continue
            if row.get("form") not in FORMS:
                continue
            end = mapping[end]
            if end not in newest or (filed, accn) > newest[end]:
                newest[end] = (filed, accn)
    if not newest:
        return
    shown: set = set()
    for concept, body in gaap.items():
        if not DEBT_LIKE.search(concept) or NOT_DEBT.search(concept):
            continue
        for rows in ((body or {}).get("units") or {}).values():
            for row in rows:
                end = _date(row.get("end"))
                if end is not None and "start" not in row:
                    shown.add((row.get("accn"), mapping.get(end, end)))
    size: Dict[Tuple[str, date], float] = {}
    for rows in ((gaap.get(BALANCE_SHEET_TOTAL) or {}).get("units") or {}).values():
        for row in rows:
            end = _date(row.get("end"))
            if end is not None and isinstance(row.get("val"), (int, float)):
                size[(row.get("accn"), mapping.get(end, end))] = float(row["val"])
    # Interest paid in any filing, annualised, by period end.
    interest: List[Tuple[date, float]] = []
    for concept, body in gaap.items():
        if not INTEREST.search(concept) or NOT_INTEREST.search(concept):
            continue
        for rows in ((body or {}).get("units") or {}).values():
            for row in rows:
                start, end = _date(row.get("start")), _date(row.get("end"))
                val = row.get("val")
                if start is None or end is None or not isinstance(val, (int, float)):
                    continue
                # A few days' interest annualised is no rate (MPWR tags stub periods).
                if _days(start, end) < QUARTER_DAYS[0]:
                    continue
                interest.append((end, abs(float(val)) * 365 / _days(start, end)))
    by_date = {at: total for (_, at), total in size.items()}
    material = [
        end for end, paid in interest
        if any(paid > INTEREST_SHARE * total
               for at, total in by_date.items() if abs((at - end).days) <= 7)
    ]
    halves = ("debt_total", "debt_noncurrent", "debt_current")
    ever_debt = any(o.value for m in halves for o in values[m].values())
    # A borrower whose debt never shows here (segment-only: BRK, PCAR; a bank) gets no zero
    # up to its last material interest: BRK tags interest only from 2025, so before that
    # nothing nearer says it borrows. After it (VRTX repaid its notes by 2014) the
    # balance sheet speaks for itself again.
    blind_until = max(material) if material and not ever_debt else None
    zeros: Dict[Tuple[str, date], Obs] = {}
    for key, cash in values["cash"].items():
        filing = newest.get(key[1])
        if filing is None or (filing[1], key[1]) in shown:
            continue
        if blind_until is not None and key[1] <= blind_until + timedelta(days=INTEREST_WINDOW_DAYS):
            continue
        where = (filing[1], key[1])
        total = size.get(where)
        if total is None or any(
            paid > INTEREST_SHARE * total
            for end, paid in interest if abs((end - key[1]).days) <= INTEREST_WINDOW_DAYS
        ):
            continue
        if any(key in values[m] for m in halves):
            continue
        zeros[key] = Obs(
            value=0.0, filed=cash.filed, start=None, end=cash.end, unit=cash.unit,
            concept=NO_DEBT_LINE, derived=True,
        )
    # A balance sheet does not shed its debt for a quarter or two: a zero with debt on both
    # sides within `DEBT_GAP_DAYS` is a borrowing the filing did not tag (FAST's 2015 line of
    # credit, LIN's 2018). Years without debt between two borrowings are years without debt
    # (META from 2013 to its 2022 bonds).
    debt_ends = {k[1] for m in halves for k, o in values[m].items() if o.value}
    for key, obs in zeros.items():
        near = [(e - key[1]).days for e in debt_ends if abs((e - key[1]).days) <= DEBT_GAP_DAYS]
        if any(d < 0 for d in near) and any(d > 0 for d in near):
            continue
        for metric in ("debt_noncurrent", "debt_current"):
            values[metric][key] = obs


def filed_splits(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The splits the filings restated history for, oldest first (review item CO1,
    2026-11-08): the closes before one Yahoo never booked are on the old share basis while
    the figures are on the new (HON's 1:2 at its 2026 Aerospace separation doubled every
    earlier EPS and halved every earlier P/E), and `eps_basis` scales them. A single filing
    voted off its neighbours both ways is a mis-vote, not two splits, and is left out.
    """
    kept: List[Dict[str, Any]] = []
    for event in sorted(events, key=lambda e: e["after"]):
        last = kept[-1] if kept else None
        if (
            last is not None
            and last["by"] == event["after"]
            and abs(last["ratio"] * event["ratio"] - 1) <= NEIGHBOUR_TOLERANCE
        ):
            kept.pop()
            continue
        kept.append(event)
    return kept


def _neighbour_split(
    own: Sequence[Fact],
    walked_counts: Dict[str, List[Fact]],
    multiplier: Dict[Tuple[str, str], float],
    split_of: Dict[str, Fraction],
) -> Optional[Fraction]:
    """
    The split factor of a filing that shares no period with a newer one, from its share
    counts against the adjusted counts of the nearest newer periods (within
    `NEIGHBOUR_DAYS`), or None to inherit the newer filing's. Only when every compared
    count — at least `NEIGHBOUR_MIN_COUNTS` — steps by the same whole ratio
    (`NEIGHBOUR_RATIOS`, within `NEIGHBOUR_TOLERANCE`): BKNG's last pre-split 10-K steps
    24.3 and 24.3 to the next 10-Q, while ONDS's share issuance steps 1.50 and 2.08.
    """
    steps: List[Optional[Fraction]] = []
    for f in own:
        if f.kind != SHARES or f.val <= 0:
            continue
        near = [
            g
            for g in walked_counts.get(f.concept, ())
            if 0 < abs((g.end - f.end).days) <= NEIGHBOUR_DAYS
        ]
        if not near:
            continue
        g = min(near, key=lambda g: (abs((g.end - f.end).days), g.end))
        step = g.val * multiplier[(g.concept, g.accn)] / f.val / float(split_of[g.accn])
        snapped = _snap(step, NEIGHBOUR_RATIOS, NEIGHBOUR_TOLERANCE)
        steps.append(None if snapped is None else snapped * split_of[g.accn])
    if len(steps) < NEIGHBOUR_MIN_COUNTS or None in steps or len(set(steps)) != 1:
        return None
    return steps[0]


# ── Periods ──────────────────────────────────────────────────────────────────


def _days(start: date, end: date) -> int:
    return (end - start).days + 1


def _between(n: int, bounds: Tuple[int, int]) -> bool:
    return bounds[0] <= n <= bounds[1]


def _latest(
    facts: Iterable[Fact], multiplier: Dict[Tuple[str, str], float]
) -> Dict[str, Dict[Tuple[Optional[date], date], Obs]]:
    """Per concept, per (start, end): the value of the latest filing, adjusted."""
    chosen: Dict[Tuple[str, Optional[date], date], Fact] = {}
    annual_form: Dict[Tuple[str, Optional[date], date], bool] = defaultdict(bool)
    every: Dict[Tuple[str, Optional[date], date], Dict[date, Fact]] = defaultdict(dict)
    for f in facts:
        key = (f.concept, f.start, f.end)
        if f.form.startswith(ANNUAL_FORM_PREFIXES):
            annual_form[key] = True
        held = chosen.get(key)
        if held is None or (f.filed, f.accn) > (held.filed, held.accn):
            chosen[key] = f
        same_day = every[key].get(f.filed)
        if same_day is None or f.accn > same_day.accn:
            every[key][f.filed] = f
    out: Dict[str, Dict[Tuple[Optional[date], date], Obs]] = defaultdict(dict)
    for key, f in chosen.items():
        m = multiplier.get((f.concept, f.accn), 1.0)
        vintages = tuple(
            (day, g.val * multiplier.get((g.concept, g.accn), 1.0))
            for day, g in sorted(every[key].items())
        )
        error = CENT_ROUNDING * abs(m) if f.kind == PER_SHARE else 0.0
        out[f.concept][(f.start, f.end)] = Obs(
            value=f.val * m,
            filed=f.filed,
            start=f.start,
            end=f.end,
            unit=f.unit,
            concept=f.concept,
            annual_form=annual_form[key],
            error=error,
            vintages=vintages,
        )
    return out


def _as_of(obs: Obs, when: date) -> Obs:
    """
    `obs` as the filings up to `when` knew it: the newest value filed on or before it, or
    `obs` itself when none was (review item CO6, 2026-11-08).
    """
    if obs.filed <= when:
        return obs
    known = [(day, value) for day, value in obs.vintages if day <= when]
    if not known:
        return obs
    day, value = known[-1]
    return Obs(
        value=value,
        filed=day,
        start=obs.start,
        end=obs.end,
        unit=obs.unit,
        concept=obs.concept,
        annual_form=obs.annual_form,
        error=obs.error,
        derived=obs.derived,
    )


def _not_a_figure(
    kind: str, non_negative: bool, value: float, zero_shown: bool = False
) -> bool:
    """
    A derived quarter that stays absent: below zero for a figure that cannot be, or a
    dividend of exactly zero unless its ladder shows it is one (`zero_shown`,
    `_shows_zero`). Two equal dividend rungs are as often one quarterly rate tagged on
    every year-to-date context (IBKR before 2020: 3, 6, 9 and 12 months all 0.10; BKNG's
    2024) as a quarter without a dividend, and a 0.00 must not stand in for the unknown.
    """
    if not non_negative:
        return False
    return value < 0 or (kind == PER_SHARE and abs(value) < 1e-12 and not zero_shown)


def _shows_zero(rungs: Sequence[Obs], direct: Dict[date, Obs], start: date) -> bool:
    """
    Whether a dividend ladder's equal rungs can be a quarter without a dividend rather than
    one rate tagged on every rung: the ladder is cumulative — its largest rung at least
    `NOT_CUMULATIVE_RATIO` times its smallest, as INTC's semi-annual declarations double
    from six to nine months — or the company reported a quarter of it as zero (WMT
    declares the year's dividend in its first quarter and tags the second as 0). A flat
    ladder with no reported zero (IBKR before 2020, BKNG) shows neither.
    """
    positive = [r.value for r in rungs if r.value > 0]
    if positive and max(positive) >= min(positive) * NOT_CUMULATIVE_RATIO:
        return True
    last = max((r.end for r in rungs), default=start)
    return any(
        q.value == 0
        and not q.derived
        and q.start is not None
        and start <= q.start
        and q.end <= last
        for q in direct.values()
    )


def _accounted(rungs: Sequence[Obs], upto: int, direct: Dict[date, Obs]) -> bool:
    """
    Whether rung `upto` equals the sum of the quarters inside it, each as reported where it
    was and else the ladder's own step: no reported quarter contradicts the ladder below a
    zero. ADP's FY2016 10-K tagged FY2015's 1.95 on the nine-month context, against the
    reported 0.48 + 0.49 + 0.49; the zero fourth quarter it made was never declared.
    """
    total, error, previous = 0.0, rungs[upto].error, None
    for rung in rungs[: upto + 1]:
        reported = direct.get(rung.end)
        if reported is not None and not reported.derived:
            total += reported.value
            error += reported.error
        else:
            total += rung.value - (previous.value if previous is not None else 0.0)
            error += rung.error + (previous.error if previous is not None else 0.0)
        previous = rung
    return abs(total - rungs[upto].value) <= max(error, 1e-9)


def _difference(kind: str, longer: Obs, shorter: Obs) -> Obs:
    """The quarter between two rungs of one year-to-date ladder."""
    if kind == SHARES:
        d_long = _days(longer.start, longer.end)
        d_short = _days(shorter.start, shorter.end)
        value = (longer.value * d_long - shorter.value * d_short) / (d_long - d_short)
    else:
        value = longer.value - shorter.value
    return Obs(
        value=value,
        filed=max(longer.filed, shorter.filed),
        start=shorter.end + timedelta(days=1),
        end=longer.end,
        unit=longer.unit,
        concept=longer.concept,
        error=longer.error + shorter.error,
        derived=True,
    )


def _preferred(a: Obs, b: Obs) -> Obs:
    """Of two values for one period: a reported one over a derived one, then the later."""
    return max(a, b, key=lambda o: (not o.derived, o.filed))


def _quarters_and_years(
    kind: str,
    periods: Dict[Tuple[Optional[date], date], Obs],
    non_negative: bool = False,
) -> Tuple[Dict[date, Obs], Dict[date, Obs], List[date]]:
    """One concept's discrete quarters and fiscal years by end date, and its ladder starts."""
    direct: Dict[date, Obs] = {}
    years: Dict[date, Obs] = {}
    ladders: Dict[date, List[Obs]] = defaultdict(list)
    for (start, end), obs in periods.items():
        if start is None:
            continue
        n = _days(start, end)
        if _between(n, QUARTER_DAYS):
            direct[end] = obs
        if _between(n, YEAR_DAYS) and obs.annual_form:
            years[end] = obs
        if QUARTER_DAYS[0] <= n <= YEAR_DAYS[1]:
            ladders[start].append(obs)
    # A quarter-length context carrying the year's own figure is a tagging slip (ORCL's
    # 10-Ks tag the year's revenue on a March-May context): the ladder's Q4 replaces it.
    if kind != SHARES:
        for end in [e for e, q in direct.items() if e in years]:
            year = years[end]
            if year.value and abs(direct[end].value / year.value - 1) < SAME_VALUE:
                del direct[end]
    derived: Dict[date, Obs] = {}
    starts: List[date] = []
    for start, rungs in ladders.items():
        rungs.sort(key=lambda o: o.end)
        if any(_days(start, o.end) > QUARTER_DAYS[1] for o in rungs):
            starts.append(start)
        # A rung smaller than the reported quarters inside it is not year-to-date: IBKR
        # tags its nine-month and 10-K contexts with one quarter's dividend rate, and the
        # difference of two such rungs was a 0.00 Q4 dividend never declared. The rung
        # goes, and with it the year and every quarter it would have derived.
        if non_negative and kind != SHARES:
            kept = []
            for rung in rungs:
                inside = sum(
                    q.value
                    for q in direct.values()
                    if q.start is not None and q.start >= start and q.end <= rung.end
                )
                if (
                    _days(start, rung.end) > QUARTER_DAYS[1]
                    and inside > rung.value * NOT_CUMULATIVE_RATIO
                ):
                    if years.get(rung.end) is rung:
                        del years[rung.end]
                    continue
                kept.append(rung)
            rungs[:] = kept
        # An additive year cannot be smaller than its own nine months. An average (a
        # weighted share count) can, so the check is not asked of one.
        if non_negative and kind != SHARES:
            for rung in rungs:
                year = years.get(rung.end)
                if (
                    year is not None
                    and year.start == start
                    and any(r.end < rung.end and r.value > year.value for r in rungs)
                ):
                    del years[rung.end]
        cumulative = kind == PER_SHARE and _shows_zero(rungs, direct, start)
        for i, (shorter, longer) in enumerate(zip(rungs, rungs[1:])):
            if not _between((longer.end - shorter.end).days, QUARTER_DAYS):
                continue
            # Both rungs of one filing vintage: a shorter rung a newer filing restated (HON's
            # next 10-Q put the prior nine months on continuing operations) is taken as the
            # longer rung's filing knew it, or Q4 is the whole company's year less the
            # continuing nine months (review item CO6).
            candidate = _difference(kind, longer, _as_of(shorter, longer.filed))
            zero_shown = cumulative and _accounted(rungs, i, direct)
            if _not_a_figure(kind, non_negative, candidate.value, zero_shown):
                continue
            held = derived.get(longer.end)
            derived[longer.end] = (
                candidate if held is None else _preferred(held, candidate)
            )
    quarters = dict(derived)
    quarters.update(direct)  # a reported quarter beats a derived one
    return quarters, years, starts


def _canonical_ends(*by_end: Iterable[Dict[date, Obs]]) -> Dict[date, date]:
    """
    One end date per period. A filing that tagged a quarter as ending a day early (NVDA's
    6-month 2010 rung ends on the 31st, its quarter on 1 August) would otherwise make two
    quarters of one. Ends within a week are one period, named by the end most concepts
    report, a reported value counting twice a derived one.
    """
    votes: Counter = Counter()
    for per_concept in by_end:
        for end, obs in per_concept.items():
            votes[end] += 1 if obs.derived else 2
    mapping: Dict[date, date] = {}
    cluster: List[date] = []

    def close() -> None:
        if cluster:
            canonical = max(cluster, key=lambda e: (votes[e], e))
            mapping.update({e: canonical for e in cluster})

    for end in sorted(votes):
        if cluster and (end - cluster[-1]).days > 7:
            close()
            cluster = []
        cluster.append(end)
    close()
    return mapping


def _rekey(per_concept: Dict[date, Obs], mapping: Dict[date, date]) -> Dict[date, Obs]:
    out: Dict[date, Obs] = {}
    for end, obs in per_concept.items():
        canonical = mapping.get(end, end)
        held = out.get(canonical)
        out[canonical] = obs if held is None else _preferred(held, obs)
    return out


def fiscal_year_name(year_end: date) -> int:
    """The fiscal year a period ending on `year_end` is named after."""
    if year_end.month == 1 and year_end.day <= 7:
        return year_end.year - 1
    return year_end.year


@dataclass(frozen=True)
class FiscalYear:
    start: date
    end: date

    @property
    def name(self) -> int:
        return fiscal_year_name(self.end)


def _calendar(
    year_spans: Iterable[Tuple[date, date]],
    ladder_starts: Iterable[date],
    quarter_starts: Iterable[date] = (),
) -> List[FiscalYear]:
    """
    Fiscal years from the reported years, plus the open year a ladder has begun — or that
    a first quarter opens within a week of a reported year's end. Until the six-month rung
    is filed, the new year's Q1 10-Q is its only fact (SPGI and NXPI on 2026-10-25), and
    without the second rule that quarter had no year to be named in and was dropped.
    """
    years = {start: FiscalYear(start, end) for start, end in year_spans}
    ends = [y.end for y in years.values()]
    opening = [s for s in quarter_starts if any(0 < (s - e).days <= 7 for e in ends)]
    for start in [*ladder_starts, *opening]:
        if start in years or any(y.start <= start <= y.end for y in years.values()):
            continue
        years[start] = FiscalYear(start, start + timedelta(days=363))
    return sorted(years.values(), key=lambda y: y.start)


def _quarter_label(end: date, calendar: List[FiscalYear]) -> Optional[Tuple[int, str]]:
    for year in calendar:
        if year.start < end <= year.end + timedelta(days=7):
            index = round((end - year.start).days / 91.3)
            return year.name, QUARTERS[min(max(index, 1), 4) - 1]
    return None


def _implied_shares(
    values: Dict[str, Dict[Tuple[str, date], Obs]],
    key: Tuple[str, date],
    max_error: float,
) -> Optional[float]:
    """Net income over diluted EPS for one period, or None when either is missing, the
    income is the consolidated `ProfitLoss`, the EPS is continuing operations' only, or the
    EPS is too coarse for it."""
    income, eps = values["net_income"].get(key), values["eps_diluted"].get(key)
    if income is None or eps is None or eps.value == 0:
        return None
    if income.concept == SHARES_FROM_EPS_EXCLUDED_INCOME or eps.concept == CONTINUING_EPS:
        return None
    if eps.error / abs(eps.value) > max_error:
        return None
    shares = income.value / eps.value
    return shares if shares > 0 else None


def _rescale_share_slips(
    latest: Dict[str, Dict[Tuple[Optional[date], date], Obs]],
    spec_of: Dict[str, "MetricSpec"],
) -> None:
    """
    A reported share count a thousand times off net income over EPS for the same span,
    rescaled in place **before** any quarter is derived from it (review item CO4,
    2026-11-08). ONDS tags its 10-K counts in thousands and its 10-Qs in shares; the check
    ran on the finished values, after Q4 had been derived from the year still in
    thousands — negative, so dropped — and every TTM window since broke on the missing
    quarter. The same test as the later pass (`_implied_shares`), on the raw spans.
    """
    def chain(metric: str) -> Dict[Tuple[Optional[date], date], Obs]:
        out: Dict[Tuple[Optional[date], date], Obs] = {}
        for concept in METRICS[metric].concepts:
            for span, obs in latest.get(concept, {}).items():
                out.setdefault(span, obs)
        return out

    by_span = {"net_income": chain("net_income"), "eps_diluted": chain("eps_diluted")}
    for concept, periods in latest.items():
        spec = spec_of.get(concept)
        if spec is None or spec.kind != SHARES or spec.timing != FLOW:
            continue
        for span, obs in periods.items():
            implied = _implied_shares(by_span, span, SHARES_SLIP_EPS_MAX_ERROR)
            if implied is None:
                continue
            slip = _snap(obs.value / implied, SHARE_COUNT_SLIPS, SHARES_SLIP_TOLERANCE)
            if slip is not None:
                obs.value = obs.value / float(slip)


def _quarter_totals(
    periods: Dict[Tuple[Optional[date], date], Obs], mapping: Dict[date, date]
) -> Dict[date, float]:
    """A declared-dividends concept's quarters by canonical end, Q4 also as the year less
    three reported quarters (CEG tags its quarters and its year, no nine months)."""
    q, y, _ = _quarters_and_years(MONETARY, periods, non_negative=True)
    q, y = _rekey(q, mapping), _rekey(y, mapping)
    out = {end: o.value for end, o in q.items()}
    for end, year in y.items():
        inside = [v for e, v in out.items() if year.start is not None and year.start < e < end]
        if end not in out and len(inside) == 3:
            out[end] = year.value - sum(inside)
    return out


def _flat_rate_years(values, keys, calendar, latest, totals, mapping) -> None:
    """
    A fiscal year — or its tail, from a quarter to the year end — whose every
    dividend-per-share fact is one rate, and whose quarters each declared about the same
    money, paid that rate each quarter: the rungs and the year carry one quarter's rate, not
    a sum. IBKR before 2020 tagged 0.10 on every rung (declared 7M, 14M, 21M, 28M), BKNG its
    2024 8.75, CEG its year beside three reported quarters; IBKR's 2025 is 0.08 from April
    (its April-December context too) after 0.0625 in the first quarter.

    A quarter takes the rate only where it has no reported figure — one derived from two
    rungs is the difference of two rates (BKNG's FY2025 0.384 less nine months' 0.38 made a
    0.004 fourth quarter) — and only where one of the rate's facts ends inside it: MA's 2021
    is 0.44 from its second quarter, with 434M, 434M and 476M declared (within the totals'
    tolerance), and its fourth quarter's raise to 0.49 is only in the year's 1.81, which is
    no rate. A filed year that carries the rate becomes the four quarters, for a whole year
    only. An annual or semi-annual declarer's totals are not equal (WMT, INTC): both stay as
    the ladder rules leave them.
    """
    dps = values["dividends_per_share"]
    concepts = METRICS["dividends_per_share"].concepts
    by_concept = [_quarter_totals(totals[c], mapping) for c in DIVIDEND_TOTALS if c in totals]
    week = timedelta(days=7)
    for year in calendar:
        in_year = [
            o
            for c in concepts
            for (s, e), o in latest.get(c, {}).items()
            if s is not None and year.start <= s and e <= year.end + week and o.value > 0
        ]
        # The year's quarters of declared totals, two at least: a year in progress counts
        # (BKNG's 2026 rate is on its six- and nine-month rungs months before the 10-K).
        quarters = max(
            (
                [(e, t[e]) for e in sorted(t) if year.start < e <= year.end + week]
                for t in by_concept
            ),
            key=len,
            default=[],
        )
        for i in range(len(quarters) - 1):
            begins = year.start if i == 0 else quarters[i - 1][0] + timedelta(days=1)
            tail = quarters[i:]
            facts = [o for o in in_year if o.start >= begins - week]
            if len(facts) < 2:
                continue
            low, high = min(o.value for o in facts), max(o.value for o in facts)
            if high > low * (1 + FLAT_RATE_TOLERANCE):
                continue
            mean = sum(v for _, v in tail) / len(tail)
            if mean <= 0 or any(abs(v / mean - 1) > FLAT_TOTALS_TOLERANCE for _, v in tail):
                continue
            _fill_rate(dps, keys, calendar, year, facts, tail, whole_year=i == 0)
            break


def _fill_rate(dps, keys, calendar, year, facts, quarters, whole_year: bool) -> None:
    """`_flat_rate_years`' writes: the rate in each quarter it is shown in, and a filed year
    that carried it as the four quarters."""
    rate = min(facts, key=lambda o: (o.error, -o.filed.toordinal()))
    week = timedelta(days=7)

    def each_quarter(value: float, end: date) -> Obs:
        return Obs(
            value=value,
            filed=rate.filed,
            start=None,
            end=end,
            unit=rate.unit,
            concept=f"{rate.concept} each quarter",
            derived=True,
        )

    previous = None
    for end, _ in quarters:
        shown = any(
            (previous is None or o.end > previous + week) and o.end <= end + week
            for o in facts
        )
        previous = end
        label = keys.get(("Q", end)) or _quarter_label(end, calendar)
        held = dps.get(("Q", end))
        if shown and (held is None or held.derived) and label is not None:
            keys[("Q", end)] = label
            dps[("Q", end)] = each_quarter(rate.value, end)
    filed_year = [o for o in facts if _between(_days(o.start, o.end), YEAR_DAYS)]
    found = [dps.get(("Q", end)) for end, _ in quarters]
    if whole_year and len(found) == 4 and filed_year and all(found):
        keys.setdefault((ANNUAL, year.end), (year.name, ANNUAL))
        dps[(ANNUAL, year.end)] = each_quarter(sum(o.value for o in found), year.end)


# ── The whole thing ──────────────────────────────────────────────────────────


def _fill_eps_from_shares(values, keys) -> None:
    """Each period's missing diluted EPS, from net income over the diluted share count."""
    for key in keys:
        if key in values["eps_diluted"]:
            continue
        income, shares = values["net_income"].get(key), values["diluted_shares"].get(key)
        if income is None or shares is None or shares.value <= 0:
            continue
        if (
            income.concept == SHARES_FROM_EPS_EXCLUDED_INCOME
            or shares.concept == SHARES_FROM_EPS_SOURCE
        ):
            continue
        values["eps_diluted"][key] = Obs(
            value=income.value / shares.value,
            filed=max(income.filed, shares.filed),
            start=income.start,
            end=income.end,
            unit=f"{income.unit}/shares",
            concept=EPS_FROM_SHARES_SOURCE,
        )


def _fill_shares_from_eps(values, keys) -> None:
    """Each period's missing share count, from net income over diluted EPS when precise."""
    for key in keys:
        if key in values["diluted_shares"]:
            continue
        shares = _implied_shares(values, key, SHARES_FROM_EPS_MAX_ERROR)
        if shares is None:
            continue
        income, eps = values["net_income"][key], values["eps_diluted"][key]
        values["diluted_shares"][key] = Obs(
            value=shares,
            filed=max(income.filed, eps.filed),
            start=income.start,
            end=income.end,
            unit="shares",
            concept=SHARES_FROM_EPS_SOURCE,
        )


def normalise(
    companyfacts: Dict[str, Any], filing_facts: Iterable[Dict[str, Any]] = ()
) -> Normalised:
    """
    Rows for `company_financials`: dicts with `period_end`, `fiscal_year`,
    `fiscal_period` (Q1-Q4 or FY), `metric`, `value`, `unit`, `source_concept`, `filed`.
    An empty result means the document carries no 10-Q/10-K/20-F figure this module reads,
    in us-gaap or, for a 20-F filer, ifrs-full (`IFRS_TWINS`): a fund, a 40-F filer.
    `filing_facts` are facts read from the filings
    themselves (`{concept, unit, start, end, val, accn, form, filed}`, `_filing_facts`).
    """
    result = Normalised(entity_name=(companyfacts or {}).get("entityName"))
    base = _facts(companyfacts)
    reporting = reporting_currency(companyfacts)
    filing_facts = list(filing_facts)
    own_concepts = [f for f in filing_facts if "metric" in f]
    plain = [f for f in filing_facts if "metric" not in f]
    facts = _drop_lone_slips(base + _filing_facts(base, plain, reporting))
    if not facts:
        return result
    events: List[Dict[str, Any]] = []
    multiplier = adjustments(facts, events)
    result.splits = filed_splits(events)
    # The witness is read like any concept since it also stands in for a loss period's
    # diluted count (`BASIC_SHARES`, `_basic_in_a_loss`); it is never stored either way.
    latest = _latest(facts, multiplier)
    totals = {c: latest.pop(c) for c in DIVIDEND_TOTALS if c in latest}

    quarters: Dict[str, Dict[date, Obs]] = {}
    years: Dict[str, Dict[date, Obs]] = {}
    instants: Dict[str, Dict[date, Obs]] = {}
    year_spans, ladder_starts, quarter_starts = set(), set(), set()
    spec_of = {c: s for s in METRICS.values() for c in s.concepts}
    _rescale_share_slips(latest, spec_of)
    for concept, periods in latest.items():
        spec = spec_of[concept]
        if spec.timing == INSTANT:
            instants[concept] = {
                end: o for (start, end), o in periods.items() if start is None
            }
            continue
        q, y, starts = _quarters_and_years(spec.kind, periods, spec.non_negative)
        quarters[concept], years[concept] = q, y
        ladder_starts |= set(starts)
        quarter_starts |= {
            start
            for start, end in periods
            if start is not None and _between(_days(start, end), QUARTER_DAYS)
        }
    mapping = _canonical_ends(*quarters.values(), *years.values())
    for concept in quarters:
        quarters[concept] = _rekey(quarters[concept], mapping)
        years[concept] = _rekey(years[concept], mapping)
        year_spans |= {(o.start, end) for end, o in years[concept].items()}
    for concept in instants:
        instants[concept] = {
            mapping[end]: o for end, o in instants[concept].items() if end in mapping
        }
    calendar = _calendar(year_spans, ladder_starts, quarter_starts)

    # (fiscal_year, fiscal_period, period_end) for every quarter and year any flow reports.
    keys: Dict[Tuple[str, date], Tuple[int, str]] = {}
    for per_concept in quarters.values():
        for end in per_concept:
            label = _quarter_label(end, calendar)
            if label is not None:
                keys[("Q", end)] = label
    for year in calendar:
        if any(year.end in y for y in years.values()):
            keys[(ANNUAL, year.end)] = (year.name, ANNUAL)

    values: Dict[str, Dict[Tuple[str, date], Obs]] = defaultdict(dict)
    for metric, spec in METRICS.items():
        for key in keys:
            grain, end = key
            for concept in spec.concepts:
                if spec.timing == INSTANT:
                    source = instants.get(concept, {})
                else:
                    source = (quarters if grain == "Q" else years).get(concept, {})
                if end in source:
                    values[metric][key] = source[end]
                    break

    _flat_rate_years(values, keys, calendar, latest, totals, mapping)
    _total_only_when_current(values)
    result.bridged = _bridge(values, keys, companyfacts, mapping)
    result.extended = _extend(values, keys, own_concepts, mapping, reporting)
    _basic_in_a_loss(values)
    _no_current_debt_line(values, facts, companyfacts, mapping)
    _no_debt_at_all(values, facts, companyfacts, mapping)

    # Q4 = FY less the three quarters, across the chain, when no single concept could give
    # it: ORCL tags the year as `Revenues` and its quarters as `RevenueFromContract...`, so
    # neither concept has both ends of the subtraction. Last resort, after every concept's
    # own ladder; never for an average (the share count).
    for metric, spec in METRICS.items():
        if spec.timing != FLOW or spec.kind == SHARES:
            continue
        for year in calendar:
            annual = values[metric].get((ANNUAL, year.end))
            if annual is None:
                continue
            # The same slip across two concepts: a Q4 from one concept carrying the year's
            # figure from another (ORCL FY2020).
            q4 = values[metric].get(("Q", year.end))
            if q4 is not None and annual.value:
                if abs(q4.value / annual.value - 1) < SAME_VALUE:
                    del values[metric][("Q", year.end)]
            if ("Q", year.end) in values[metric]:
                continue
            quarters_of_year = {
                keys[k][1]: o
                for k, o in values[metric].items()
                if k[0] == "Q"
                and keys[k][0] == year.name
                and year.start < k[1] < year.end
            }
            if not all(q in quarters_of_year for q in QUARTERS[:3]):
                continue
            q4 = annual.value - sum(quarters_of_year[q].value for q in QUARTERS[:3])
            if _not_a_figure(spec.kind, spec.non_negative, q4):
                continue
            keys.setdefault(("Q", year.end), (year.name, QUARTERS[3]))
            values[metric][("Q", year.end)] = Obs(
                value=q4,
                filed=max(
                    [annual.filed] + [o.filed for o in quarters_of_year.values()]
                ),
                start=None,
                end=year.end,
                unit=annual.unit,
                concept=f"{annual.concept} less Q1-Q3",
                derived=True,
            )

    # A reported share count a thousand times off what net income over EPS implies is a
    # unit slip that no newer filing repeats a period of, so the vote above never saw it
    # (ONDS tags some 10-Ks and Q1 10-Qs in thousands, the other filings in shares).
    for key, shares in list(values["diluted_shares"].items()):
        implied = _implied_shares(values, key, SHARES_SLIP_EPS_MAX_ERROR)
        if implied is None:
            continue
        slip = _snap(shares.value / implied, SHARE_COUNT_SLIPS, SHARES_SLIP_TOLERANCE)
        if slip is not None:
            shares.value = shares.value / float(slip)

    # A share count the company did not report, from net income over diluted EPS, when the
    # EPS is precise enough for it (documented: GOOGL tags the count only from 2022).
    _fill_shares_from_eps(values, keys)

    # A derived quarter's share count far from every neighbour that is not derived is a
    # difference of two bases, not a count: the year's diluted average holds dilution the
    # loss quarters leave out (DXCM's 2020 Q4 came to 807M between 298M and 418M; REGN's
    # 2013 Q4 to 13M). It stays absent, unless net income over EPS gives it after all.
    # The neighbours include counts filled from EPS above: TTWO's 2013 Q4 (86.2M) is real
    # beside its next quarter's 87.2M, its only reported neighbour a convertible-diluted Q3.
    counts = values["diluted_shares"]
    anchors = [(k[1], o.value) for k, o in counts.items() if k[0] == "Q" and not o.derived]
    dropped = False
    for key, obs in list(counts.items()):
        if key[0] != "Q" or not obs.derived:
            continue
        near = [
            v
            for end, v in anchors
            if 0 < abs((end - key[1]).days) <= NEIGHBOUR_DAYS and v > 0
        ]
        # Between its two reported neighbours it is a count, however fast the company
        # dilutes (CO4: a quarter of heavy issuance is more than 1.2x from either side).
        between = len(near) >= 2 and min(near) <= obs.value <= max(near)
        if near and not between and not any(
            1 / DERIVED_COUNT_MAX_STEP <= obs.value / v <= DERIVED_COUNT_MAX_STEP
            for v in near
        ):
            del counts[key]
            dropped = True
    if dropped:
        _fill_shares_from_eps(values, keys)
    _fill_eps_from_shares(values, keys)

    cash = values["cash"]
    if cash:
        newest = max(cash, key=lambda k: k[1])
        if newest not in values["short_term_investments"] and newest not in values[
            "cash_and_short_term"
        ]:
            result.unchained_investments = _unchained_investments(
                companyfacts, cash[newest].end
            )

    currencies: Counter = Counter()
    for metric in ("revenue", "net_income"):
        currencies.update(o.unit for o in values[metric].values())
    result.currency = currencies.most_common(1)[0][0] if currencies else None

    for metric, by_key in values.items():
        for key, obs in by_key.items():
            fiscal_year, fiscal_period = keys[key]
            result.rows.append(
                {
                    "period_end": key[1],
                    "fiscal_year": fiscal_year,
                    "fiscal_period": fiscal_period,
                    "metric": metric,
                    "value": obs.value,
                    "unit": obs.unit,
                    "source_concept": obs.concept,
                    "filed": obs.filed,
                }
            )
    result.rows.sort(key=lambda r: (r["period_end"], r["fiscal_period"], r["metric"]))
    return result
