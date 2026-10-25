"""
The Company tab's series, from the stored `company_financials` rows (docs/company-financials.md).

**Every derived figure is computed here and only here** — the client draws what it is
given. TTM, free cash flow, margins, per-share figures and growth:

- **TTM** of a flow is the sum of four consecutive quarters; a missing quarter (or a gap in
  the quarters) makes it null, never a partial sum. TTM shares are the mean of the four.
- **FCF** = operating cash flow − capex; **FCF after SBC** = FCF − stock-based
  compensation; **FCF per share** = FCF / diluted shares of the same period. Any missing
  input makes the result null: a broker that reports no capex has no FCF, not its OCF.
- **Margins** = the figure / revenue of the same period, as fractions; null when revenue
  is missing or not positive.
- **CAGR** over the range's first and last known values: null when the start is not
  positive, the end is negative, or they are less than a year apart. **YoY** compares the
  latest point with the point a year before it (four quarters back, or the previous fiscal
  year): null when that point is missing or not positive. Neither is computed for a margin,
  whose growth rate would read as a figure nobody means.
- **Annual** is the fiscal years as filed (the FY facts), not a sum of quarters.

Figures stay in the reporting currency. Converting a history into the base currency would
fold a decade of exchange-rate moves into every growth rate.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_financials import CompanyFinancial, SecurityCik
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
    "net_income",
    "eps_diluted",
    "fcf",
    "fcf_after_sbc",
    "fcf_per_share",
    "diluted_shares",
    "operating_margin",
    "net_margin",
    "fcf_margin",
    "dividends_per_share",
)
MARGINS = frozenset({"operating_margin", "net_margin", "fcf_margin"})
# The stored metrics a TTM sums; the share count is averaged instead.
SUMMED = (
    "revenue",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "capex",
    "sbc",
    "operating_income",
    "dividends_per_share",
)
AVERAGED = ("diluted_shares",)
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


def _derive(point: Point) -> None:
    b = point.base
    ocf, capex, sbc = b.get("operating_cash_flow"), b.get("capex"), b.get("sbc")
    fcf = ocf - capex if ocf is not None and capex is not None else None
    revenue, shares = b.get("revenue"), b.get("diluted_shares")
    point.series = {
        "revenue": revenue,
        "net_income": b.get("net_income"),
        "eps_diluted": b.get("eps_diluted"),
        "fcf": fcf,
        "fcf_after_sbc": fcf - sbc if fcf is not None and sbc is not None else None,
        "fcf_per_share": _ratio(fcf, shares),
        "diluted_shares": shares,
        "operating_margin": _ratio(b.get("operating_income"), revenue),
        "net_margin": _ratio(b.get("net_income"), revenue),
        "fcf_margin": _ratio(fcf, revenue),
        "dividends_per_share": b.get("dividends_per_share"),
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
    if not points:
        return None
    last_date, last = points[-1]
    if last is None:
        return None
    target = _years_before(last_date, 1)
    earlier = [
        (abs((d - target).days), v)
        for d, v in points[:-1]
        if abs((d - target).days) <= YOY_SLACK_DAYS
    ]
    if not earlier:
        return None
    _, previous = min(earlier, key=lambda pair: pair[0])
    if previous is None or previous <= 0:
        return None
    return last / previous - 1


def build_series(
    rows: Sequence[CompanyFinancial], period: str, range_key: str
) -> Dict[str, Any]:
    """The `series` object of the response, from one security's stored rows."""
    if period == "annual":
        points = _points(rows, annual=True)
    else:
        points = _points(rows, annual=False)
        if period == "ttm":
            points = _ttm(points)
    for point in points:
        _derive(point)
    years = RANGES[range_key]
    shown = points
    if years is not None and points:
        cutoff = _years_before(points[-1].period_end, years)
        shown = [p for p in points if p.period_end > cutoff]
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
                }
                for p in shown
            ],
            "cagr": None if name in MARGINS else cagr(visible),
            "yoy": None if name in MARGINS else yoy(full),
        }
    return out


class CompanyFinancialsService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = CompanyFinancialsRepository(db)

    async def list_companies(self) -> List[Dict[str, Any]]:
        """Held securities with stored figures, one per company (CIK)."""
        result = await self.db.execute(select(Security).where(held()))
        securities = {s.id: s for s in result.scalars()}
        coverage = await self.repo.coverage(securities)
        ciks = await self.repo.cik_rows(coverage)
        # One listing per company: the one with the most rows, then the lowest symbol.
        by_company: Dict[Any, List[int]] = {}
        for sid in coverage:
            cik = ciks[sid].cik if sid in ciks else None
            by_company.setdefault(cik if cik is not None else ("id", sid), []).append(
                sid
            )
        chosen = [
            min(ids, key=lambda i: (-coverage[i]["rows"], securities[i].symbol, i))
            for ids in by_company.values()
        ]
        out = []
        for sid in chosen:
            security, cov = securities[sid], coverage[sid]
            cik_row: Optional[SecurityCik] = ciks.get(sid)
            out.append(
                {
                    "security_id": sid,
                    "symbol": security.symbol,
                    "name": security.description,
                    "cik": cik_row.cik if cik_row is not None else None,
                    "currency": cov["currency"],
                    "first_period_end": cov["first"].isoformat()
                    if cov["first"]
                    else None,
                    "last_period_end": cov["last"].isoformat() if cov["last"] else None,
                    "last_fetched_at": utc_iso(cov["fetched_at"]),
                }
            )
        out.sort(key=lambda c: (c["symbol"], c["security_id"]))
        return out

    async def get_financials(
        self,
        security: Security,
        period: str = DEFAULT_PERIOD,
        range_key: str = DEFAULT_RANGE,
    ) -> Dict[str, Any]:
        rows = await self.repo.rows_for(security.id)
        coverage = (await self.repo.coverage([security.id])).get(security.id)
        warnings: List[str] = []
        if not rows:
            warnings.append(
                f"No SEC financial statements are stored for {security.symbol}."
            )
        return {
            "security_id": security.id,
            "symbol": security.symbol,
            "name": security.description,
            "currency": coverage["currency"] if coverage else None,
            "period": period,
            "range": range_key,
            "as_of": utc_iso(coverage["fetched_at"]) if coverage else None,
            "warnings": warnings,
            "series": build_series(rows, period, range_key),
        }
