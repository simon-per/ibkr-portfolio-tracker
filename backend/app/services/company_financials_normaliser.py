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

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
    "eps_diluted": MetricSpec(PER_SHARE, FLOW, ("EarningsPerShareDiluted",)),
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
    "diluted_shares": MetricSpec(
        SHARES,
        FLOW,
        ("WeightedAverageNumberOfDilutedSharesOutstanding",),
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
    # total. Commercial paper and short-term borrowings are not counted.
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
        ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"),
    ),
    "debt_current": MetricSpec(
        MONETARY, INSTANT, ("LongTermDebtCurrent", "DebtCurrent")
    ),
}

#: The source recorded for a share count derived as net income / diluted EPS — for the
#: periods a company reported EPS but not the count (GOOGL before 2022). Never from
#: `ProfitLoss`: it includes non-controlling interests, so dividing it by EPS is no share
#: count.
SHARES_FROM_EPS_EXCLUDED_INCOME = "ProfitLoss"
SHARES_FROM_EPS_SOURCE = "NetIncomeLoss/EarningsPerShareDiluted"
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

#: Forms whose facts count. 10-K/10-Q and their amendments and transition reports; an 8-K's
#: or a proxy's stray fact does not, and a 20-F's are IFRS and out of scope.
FORMS = frozenset(
    {"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-KT/A", "10-QT", "10-QT/A"}
)

QUARTER_DAYS = (75, 105)
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


@dataclass
class Normalised:
    rows: List[Dict[str, Any]] = field(default_factory=list)
    currency: Optional[str] = None
    entity_name: Optional[str] = None

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


def _facts(companyfacts: Dict[str, Any]) -> List[Fact]:
    """Every usable fact of every concept a chain names, in the concept's main unit."""
    gaap = ((companyfacts or {}).get("facts") or {}).get("us-gaap") or {}
    wanted = {c: spec.kind for spec in METRICS.values() for c in spec.concepts}
    wanted[SPLIT_WITNESS] = SHARES
    never_negative = {
        c for spec in METRICS.values() if spec.non_negative for c in spec.concepts
    }
    out: List[Fact] = []
    for concept, kind in wanted.items():
        units = (gaap.get(concept) or {}).get("units") or {}
        fitting = {u: rows for u, rows in units.items() if _unit_fits(kind, u) and rows}
        if not fitting:
            continue
        # A concept reported in two currencies keeps the one it reports most.
        unit = max(fitting, key=lambda u: (len(fitting[u]), u))
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


def adjustments(facts: Iterable[Fact]) -> Dict[Tuple[str, str], float]:
    """
    (concept, accn) -> the multiplier that puts that filing's values of that concept into
    the newest filing's basis: its split factor (shares up, per-share down, money
    untouched) times the concept's own scale slip, if any. See the module docstring.
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
        previous_split = split
    return multiplier


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
    for f in facts:
        key = (f.concept, f.start, f.end)
        if f.form.startswith("10-K"):
            annual_form[key] = True
        held = chosen.get(key)
        if held is None or (f.filed, f.accn) > (held.filed, held.accn):
            chosen[key] = f
    out: Dict[str, Dict[Tuple[Optional[date], date], Obs]] = defaultdict(dict)
    for key, f in chosen.items():
        m = multiplier.get((f.concept, f.accn), 1.0)
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
        )
    return out


def _not_a_figure(kind: str, non_negative: bool, value: float) -> bool:
    """
    A derived quarter that stays absent: below zero for a figure that cannot be, or a
    dividend of exactly zero. Two equal dividend rungs are as often one quarterly rate
    tagged on every year-to-date context (IBKR before 2020: 3, 6, 9 and 12 months all
    0.10) as a quarter without a dividend, and a 0.00 must not stand in for the unknown.
    """
    if not non_negative:
        return False
    return value < 0 or (kind == PER_SHARE and abs(value) < 1e-12)


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
        for shorter, longer in zip(rungs, rungs[1:]):
            if not _between((longer.end - shorter.end).days, QUARTER_DAYS):
                continue
            candidate = _difference(kind, longer, shorter)
            if _not_a_figure(kind, non_negative, candidate.value):
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
    income is the consolidated `ProfitLoss`, or the EPS is too coarse for it."""
    income, eps = values["net_income"].get(key), values["eps_diluted"].get(key)
    if income is None or eps is None or eps.value == 0:
        return None
    if income.concept == SHARES_FROM_EPS_EXCLUDED_INCOME:
        return None
    if eps.error / abs(eps.value) > max_error:
        return None
    shares = income.value / eps.value
    return shares if shares > 0 else None


# ── The whole thing ──────────────────────────────────────────────────────────


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


def normalise(companyfacts: Dict[str, Any]) -> Normalised:
    """
    Rows for `company_financials`: dicts with `period_end`, `fiscal_year`,
    `fiscal_period` (Q1-Q4 or FY), `metric`, `value`, `unit`, `source_concept`, `filed`.
    An empty result means the document carries no us-gaap 10-Q/10-K figures this module
    reads (an IFRS 20-F filer, a fund).
    """
    result = Normalised(entity_name=(companyfacts or {}).get("entityName"))
    facts = _facts(companyfacts)
    if not facts:
        return result
    multiplier = adjustments(facts)
    latest = _latest([f for f in facts if f.concept != SPLIT_WITNESS], multiplier)

    quarters: Dict[str, Dict[date, Obs]] = {}
    years: Dict[str, Dict[date, Obs]] = {}
    instants: Dict[str, Dict[date, Obs]] = {}
    year_spans, ladder_starts, quarter_starts = set(), set(), set()
    spec_of = {c: s for s in METRICS.values() for c in s.concepts}
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
        if near and not any(
            1 / DERIVED_COUNT_MAX_STEP <= obs.value / v <= DERIVED_COUNT_MAX_STEP
            for v in near
        ):
            del counts[key]
            dropped = True
    if dropped:
        _fill_shares_from_eps(values, keys)

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
