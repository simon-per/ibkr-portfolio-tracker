"""
The Company tab's series, from the stored `company_financials` rows (docs/company-financials.md).

**Every derived figure is computed here and only here** — the client draws what it is
given. TTM, free cash flow, margins, per-share figures and growth:

- **TTM** of a flow is the sum of four consecutive quarters; a missing quarter (or a gap in
  the quarters) makes it null, never a partial sum. TTM shares are the mean of the four.
- **FCF** = operating cash flow − capex; **FCF after SBC** = FCF − stock-based
  compensation; **per share** (revenue, FCF, FCF after SBC) = the figure / diluted shares
  of the same period — the Company tab's *Per share* view, whose net income is the reported
  diluted EPS. Any missing input makes the result null: a broker that reports no capex has
  no FCF, not its OCF.
- **Margins** = the figure / revenue of the same period, as fractions; null when revenue
  is missing or not positive. Gross profit is the tagged one, else revenue less cost of
  revenue.
- **Balances** (cash, debt) are the period end's; a TTM point carries its newest quarter's.
  Debt is the tagged total, else its noncurrent and current halves added (`_debt`). **No
  net cash**: cash is cash and equivalents only, and the short-term investments beside it are
  tagged too unevenly to add (NVDA's stop in 2025), so cash − debt showed the book's most
  cash-rich company as a net debtor.
- **CAGR** over the range's first and last known values: null when the start is not
  positive, the end is negative, or they are less than a year apart. **YoY** compares the
  latest point with the point a year before it (four quarters back, or the previous fiscal
  year): null when that point is missing or not positive. Every point carries its own YoY by
  the same rule, for the tab's hover. **3/5/10-year CAGRs** (`cagrs`) compare the latest
  point with the one that many years before it, over the whole history. None of these is
  computed for a margin, whose growth rate would read as a figure nobody means.
- **Key stats** are the newest TTM point's figures, whatever period the charts show.
- **Annual** is the fiscal years as filed (the FY facts), not a sum of quarters.

Figures stay in the reporting currency. Converting a history into the base currency would
fold a decade of exchange-rate moves into every growth rate.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_financials import CompanyFinancial, SecCompany
from app.models.security import Security
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.repositories.security_repository import held
from app.repositories.sync_run_repository import utc_iso
from app.services.company_financials_normaliser import ANNUAL

PERIODS = ("quarterly", "ttm", "annual")
RANGES = {"5y": 5, "10y": 10, "max": None}
DEFAULT_PERIOD = "ttm"
DEFAULT_RANGE = "10y"

SERIES = (
    "revenue",
    "revenue_per_share",
    "operating_income",
    "net_income",
    "eps_diluted",
    "fcf",
    "fcf_after_sbc",
    "fcf_per_share",
    "fcf_after_sbc_per_share",
    "diluted_shares",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "fcf_margin",
    "dividends_per_share",
    "buybacks",
    "dividends_paid",
    "cash",
    "debt",
)
MARGINS = frozenset({"gross_margin", "operating_margin", "net_margin", "fcf_margin"})
# The stored metrics a TTM sums; the share count is averaged, a balance is the newest.
SUMMED = (
    "revenue",
    "gross_profit",
    "cost_of_revenue",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "capex",
    "sbc",
    "operating_income",
    "dividends_per_share",
    "buybacks",
    "dividends_paid",
)
AVERAGED = ("diluted_shares",)
BALANCES = ("cash", "debt_total", "debt_noncurrent", "debt_current")
NO_GROWTH = MARGINS
# The compound growth the cards and the key figures quote, in years back from the latest.
CAGR_YEARS = (3, 5, 10)
# Two quarter ends this far apart are consecutive (13 or 14 weeks, a little slack).
CONSECUTIVE_DAYS = (80, 105)
# How far from exactly a year back the YoY comparison point may sit (52/53-week years).
YOY_SLACK_DAYS = 20
DAYS_PER_YEAR = 365.25


@dataclass
class Point:
    period_end: date
    label: str
    base: Dict[str, Optional[float]] = field(default_factory=dict)
    series: Dict[str, Optional[float]] = field(default_factory=dict)


def _label(fiscal_year: int, fiscal_period: str) -> str:
    if fiscal_period == ANNUAL:
        return f"FY{fiscal_year}"
    return f"{fiscal_period} FY{fiscal_year}"


def _points(rows: Sequence[CompanyFinancial], annual: bool) -> List[Point]:
    by_end: Dict[date, Point] = {}
    for row in rows:
        if (row.fiscal_period == ANNUAL) != annual:
            continue
        point = by_end.get(row.period_end)
        if point is None:
            point = by_end[row.period_end] = Point(
                row.period_end, _label(row.fiscal_year, row.fiscal_period)
            )
        point.base[row.metric] = row.value
    return [by_end[end] for end in sorted(by_end)]


def _consecutive(window: Sequence[Point]) -> bool:
    return all(
        CONSECUTIVE_DAYS[0] <= (b.period_end - a.period_end).days <= CONSECUTIVE_DAYS[1]
        for a, b in zip(window, window[1:])
    )


def _ttm(quarters: List[Point]) -> List[Point]:
    out: List[Point] = []
    for i in range(3, len(quarters)):
        window = quarters[i - 3 : i + 1]
        point = Point(quarters[i].period_end, quarters[i].label)
        whole = _consecutive(window)
        for metric in BALANCES:
            point.base[metric] = quarters[i].base.get(metric)
        for metric in SUMMED + AVERAGED:
            values = [q.base.get(metric) for q in window]
            if not whole or any(v is None for v in values):
                point.base[metric] = None
            elif metric in AVERAGED:
                point.base[metric] = sum(values) / len(values)
            else:
                point.base[metric] = sum(values)
        out.append(point)
    return out


def _ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _debt(base: Dict[str, Optional[float]]) -> Optional[float]:
    """
    The tagged total, else the noncurrent and current halves added when both are tagged.
    A total of exactly zero is a tagging slip, not a debt-free company (ORCL's FY2022 and
    AMD's FY2021 10-Ks tag a 0 beside billions of notes); a company without debt tags none.
    """
    total = base.get("debt_total")
    if total:
        return total
    noncurrent, current = base.get("debt_noncurrent"), base.get("debt_current")
    if noncurrent is not None and current is not None:
        return noncurrent + current
    return None


def _derive(point: Point) -> None:
    b = point.base
    ocf, capex, sbc = b.get("operating_cash_flow"), b.get("capex"), b.get("sbc")
    fcf = ocf - capex if ocf is not None and capex is not None else None
    fcf_after_sbc = fcf - sbc if fcf is not None and sbc is not None else None
    revenue, shares = b.get("revenue"), b.get("diluted_shares")
    gross, cost = b.get("gross_profit"), b.get("cost_of_revenue")
    if gross is None and revenue is not None and cost is not None:
        gross = revenue - cost
    cash, debt = b.get("cash"), _debt(b)
    point.series = {
        "revenue": revenue,
        "revenue_per_share": _ratio(revenue, shares),
        "operating_income": b.get("operating_income"),
        "net_income": b.get("net_income"),
        "eps_diluted": b.get("eps_diluted"),
        "fcf": fcf,
        "fcf_after_sbc": fcf_after_sbc,
        "fcf_per_share": _ratio(fcf, shares),
        "fcf_after_sbc_per_share": _ratio(fcf_after_sbc, shares),
        "diluted_shares": shares,
        "gross_margin": _ratio(gross, revenue),
        "operating_margin": _ratio(b.get("operating_income"), revenue),
        "net_margin": _ratio(b.get("net_income"), revenue),
        "fcf_margin": _ratio(fcf, revenue),
        "dividends_per_share": b.get("dividends_per_share"),
        "buybacks": b.get("buybacks"),
        "dividends_paid": b.get("dividends_paid"),
        "cash": cash,
        "debt": debt,
    }


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


def cagr(points: Sequence[tuple]) -> Optional[float]:
    """(period_end, value) pairs in order -> the compound annual growth, or None."""
    known = [(d, v) for d, v in points if v is not None]
    if len(known) < 2:
        return None
    (start_date, start), (end_date, end) = known[0], known[-1]
    years = (end_date - start_date).days / DAYS_PER_YEAR
    if start <= 0 or end < 0 or years < 1:
        return None
    return (end / start) ** (1 / years) - 1


def yoy(points: Sequence[tuple]) -> Optional[float]:
    """The last (period_end, value) against the one a year before it, or None."""
    return yoy_at(points, len(points) - 1) if points else None


def yoy_at(points: Sequence[tuple], index: int) -> Optional[float]:
    """Point `index` of (period_end, value) pairs against the one a year before it."""
    return growth_at(points, index, 1)


def growth_at(points: Sequence[tuple], index: int, years: int) -> Optional[float]:
    """
    The compound annual growth from the point `years` before point `index` (within
    `YOY_SLACK_DAYS`) to it; one year back is the YoY. None when either value is missing
    or the earlier is not positive, and over several years when the later is negative
    (a root of a negative ratio is no growth rate).
    """
    last_date, last = points[index]
    if last is None:
        return None
    target = _years_before(last_date, years)
    earlier = [
        (abs((d - target).days), v)
        for d, v in points[:index]
        if abs((d - target).days) <= YOY_SLACK_DAYS
    ]
    if not earlier:
        return None
    _, previous = min(earlier, key=lambda pair: pair[0])
    if previous is None or previous <= 0 or (years > 1 and last < 0):
        return None
    return (last / previous) ** (1 / years) - 1


def cagrs(points: Sequence[tuple]) -> Dict[str, Optional[float]]:
    """`CAGR_YEARS` growth to the latest point, keyed `y3`, `y5`, `y10`."""
    return {
        f"y{n}": growth_at(points, len(points) - 1, n) if points else None
        for n in CAGR_YEARS
    }


def _series_points(rows: Sequence[CompanyFinancial], period: str) -> List[Point]:
    """Every period of `period`, oldest first, with its derived series."""
    if period == "annual":
        points = _points(rows, annual=True)
    else:
        points = _points(rows, annual=False)
        if period == "ttm":
            points = _ttm(points)
    for point in points:
        _derive(point)
    return points


def key_stats(rows: Sequence[CompanyFinancial]) -> Dict[str, Any]:
    """
    The company's key figures at its newest TTM point, whatever period the charts show:
    the sums of the last four quarters, the balances at the newest quarter end, the YoY
    and 5-year growth by the series' rules. The payout ratio is TTM dividends per share
    over TTM diluted EPS, null when the EPS is not positive.
    """
    points = _series_points(rows, "ttm")
    last = points[-1] if points else None

    def value(name: str) -> Optional[float]:
        return last.series.get(name) if last is not None else None

    def growth(name: str, years: int) -> Optional[float]:
        if last is None:
            return None
        return growth_at(
            [(p.period_end, p.series.get(name)) for p in points], len(points) - 1, years
        )

    eps, dps = value("eps_diluted"), value("dividends_per_share")
    return {
        "label": last.label if last is not None else None,
        "period_end": last.period_end.isoformat() if last is not None else None,
        "revenue": value("revenue"),
        "revenue_yoy": growth("revenue", 1),
        "revenue_cagr_5y": growth("revenue", 5),
        "net_income": value("net_income"),
        "eps_diluted": eps,
        "eps_yoy": growth("eps_diluted", 1),
        "eps_cagr_5y": growth("eps_diluted", 5),
        "fcf": value("fcf"),
        "fcf_per_share": value("fcf_per_share"),
        "fcf_yoy": growth("fcf", 1),
        "gross_margin": value("gross_margin"),
        "operating_margin": value("operating_margin"),
        "net_margin": value("net_margin"),
        "fcf_margin": value("fcf_margin"),
        "diluted_shares": value("diluted_shares"),
        "shares_yoy": growth("diluted_shares", 1),
        "dividends_per_share": dps,
        "payout_ratio": dps / eps
        if dps is not None and eps is not None and eps > 0
        else None,
        "buybacks": value("buybacks"),
        "dividends_paid": value("dividends_paid"),
    }


def build_series(
    rows: Sequence[CompanyFinancial], period: str, range_key: str
) -> Dict[str, Any]:
    """The `series` object of the response, from one security's stored rows."""
    points = _series_points(rows, period)
    years = RANGES[range_key]
    shown = points
    if years is not None and points:
        cutoff = _years_before(points[-1].period_end, years)
        shown = [p for p in points if p.period_end > cutoff]
    # `shown` is the newest stretch of `points`, so a shown point's YoY may reach back
    # before the range: the first year of a 5Y chart still has its growth.
    offset = len(points) - len(shown)
    out: Dict[str, Any] = {}
    for name in SERIES:
        full = [(p.period_end, p.series.get(name)) for p in points]
        visible = [(p.period_end, p.series.get(name)) for p in shown]
        out[name] = {
            "points": [
                {
                    "period_end": p.period_end.isoformat(),
                    "label": p.label,
                    "value": p.series.get(name),
                    "yoy": None if name in NO_GROWTH else yoy_at(full, offset + i),
                }
                for i, p in enumerate(shown)
            ],
            "cagr": None if name in NO_GROWTH else cagr(visible),
            "yoy": None if name in NO_GROWTH else yoy(full),
            # Measured on the whole history, not the range: the 10Y figure of a 5Y chart.
            "cagrs": dict.fromkeys(cagrs(full)) if name in NO_GROWTH else cagrs(full),
        }
    return out


class CompanyFinancialsService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = CompanyFinancialsRepository(db)

    async def _held_listings(self, ciks) -> Dict[int, Security]:
        """CIK -> its held listing with the lowest symbol (GOOG before GOOGL)."""
        listings = await self.repo.listings(ciks)
        ids = [sid for sids in listings.values() for sid in sids]
        if not ids:
            return {}
        result = await self.db.execute(
            select(Security).where(held(), Security.id.in_(ids))
        )
        securities = {s.id: s for s in result.scalars()}
        out: Dict[int, Security] = {}
        for cik, sids in listings.items():
            members = [securities[i] for i in sids if i in securities]
            if members:
                out[cik] = min(members, key=lambda s: (s.symbol, s.id))
        return out

    @staticmethod
    def _identity(company: SecCompany, listing: Optional[Security]) -> Dict[str, Any]:
        """Who the company is on the tab: its held listing when there is one."""
        if listing is not None:
            return {
                "cik": company.cik,
                "security_id": listing.id,
                "symbol": listing.symbol,
                "name": listing.description,
                "isin": listing.isin,
                "held": True,
            }
        symbol = company.ticker or str(company.cik)
        return {
            "cik": company.cik,
            "security_id": None,
            "symbol": symbol,
            "name": company.name or symbol,
            "isin": company.isin,
            "held": False,
        }

    async def list_companies(self) -> List[Dict[str, Any]]:
        """
        Companies with stored figures: those held, and the Nasdaq-100 constituents of the
        newest full pass (`universe_seen_at`), one entry per company (CIK).
        """
        companies = await self.repo.companies()
        listings = await self._held_listings(companies)
        latest = await self.repo.latest_universe_stamp()
        listed = [
            c
            for c in companies.values()
            if c.cik in listings
            or (latest is not None and c.universe_seen_at == latest)
        ]
        coverage = await self.repo.coverage(c.cik for c in listed)
        out = []
        for company in listed:
            cov = coverage.get(company.cik)
            if cov is None:
                continue
            out.append(
                {
                    **self._identity(company, listings.get(company.cik)),
                    "currency": cov["currency"],
                    "first_period_end": cov["first"].isoformat()
                    if cov["first"]
                    else None,
                    "last_period_end": cov["last"].isoformat() if cov["last"] else None,
                    "last_fetched_at": utc_iso(cov["fetched_at"]),
                }
            )
        out.sort(key=lambda c: (c["symbol"], c["cik"]))
        return out

    async def get_financials(
        self,
        company: SecCompany,
        period: str = DEFAULT_PERIOD,
        range_key: str = DEFAULT_RANGE,
    ) -> Dict[str, Any]:
        rows = await self.repo.rows_for(company.cik)
        coverage = (await self.repo.coverage([company.cik])).get(company.cik)
        identity = self._identity(
            company, (await self._held_listings([company.cik])).get(company.cik)
        )
        warnings: List[str] = []
        if not rows:
            warnings.append(
                f"No SEC financial statements are stored for {identity['symbol']}."
            )
        return {
            **identity,
            "currency": coverage["currency"] if coverage else None,
            "period": period,
            "range": range_key,
            "as_of": utc_iso(coverage["fetched_at"]) if coverage else None,
            "warnings": warnings,
            "key_stats": key_stats(rows),
            "series": build_series(rows, period, range_key),
        }
