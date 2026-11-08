"""
The fundamentals of what the account owns, through its funds as well as directly (review
item R2, 2026-11-08; docs/lookthrough.md, *Fundamentals of what you own*).

Each look-through company with SEC figures on the Company tab contributes its
**share-equivalent**: the account's value in it times the company's own TTM figure over its
market cap — owned earnings, owned revenue — a ratio, so the base currency and the
company's reporting currency never meet. Then:

- **P/E** = the covered value over the owned earnings (an earnings-yield weighting: a loss
  lowers the earnings, never vanishes as an absent P/E would);
- **revenue and earnings growth** = the owned figures now against the same share-equivalents
  a year before (each company's TTM YoY, `revenue_yoy` and `eps_yoy` — per share, which is
  what a share-equivalent holds);
- **operating and FCF margin** = owned profit over owned revenue;
- **coverage** = the share of the companies' value whose earnings yield is known, as a
  figure: the rest (non-US companies, funds without a basket, the look-through's tail past
  `COMPANY_LIMIT_MAX`) is reported as missing, never renormalised onto.

Each company's figures are the overview's (`CompanyFinancialsService.key_figures`). Pure DB.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_financials import SecCompany, SecurityCik
from app.models.security import Security
from app.services.company_financials_service import CompanyFinancialsService
from app.services.lookthrough_service import COMPANY_LIMIT_MAX, LookthroughService
from app.services.percentages import share_pct


@dataclass
class Owned:
    """One company: what the account owns of it, and its key figures."""

    value: float
    stats: Dict[str, Any]


def _per_value(stats: Dict[str, Any], name: str) -> Optional[float]:
    """A TTM figure per unit of market value (earnings yield, revenue yield), or None."""
    cap, figure = stats.get("market_cap"), stats.get(name)
    if cap is None or cap <= 0 or figure is None:
        return None
    return figure / cap


def _growth(pairs: Sequence[tuple]) -> Optional[float]:
    """(owned now, its YoY) pairs: the owned total against the same holdings a year before."""
    parts = [(now, g) for now, g in pairs if g is not None and g != -1]
    before = sum(now / (1 + g) for now, g in parts)
    if not parts or before <= 0:
        return None
    return sum(now for now, _ in parts) / before - 1


def aggregate(owned: Sequence[Owned], companies_value: float) -> Dict[str, Any]:
    """The card's figures over `owned`; `companies_value` is every look-through company's."""
    rows = [
        (
            o.value,
            _per_value(o.stats, "net_income"),
            _per_value(o.stats, "revenue"),
            o.stats,
        )
        for o in owned
        if o.value > 0
    ]
    earning = [(v, ey) for v, ey, _, _ in rows if ey is not None]
    covered = sum(v for v, _ in earning)
    earnings = sum(v * ey for v, ey in earning)
    selling = [(v * ry, s) for v, _, ry, s in rows if ry is not None and ry > 0]
    revenue = sum(x for x, _ in selling)

    def margin(name: str) -> Optional[float]:
        parts = [(x, s[name]) for x, s in selling if s.get(name) is not None]
        total = sum(x for x, _ in parts)
        return sum(x * m for x, m in parts) / total if parts and total > 0 else None

    return {
        "pe_ratio": covered / earnings if earning and earnings > 0 else None,
        "revenue_growth": _growth([(x, s.get("revenue_yoy")) for x, s in selling])
        if revenue > 0
        else None,
        "earnings_growth": _growth(
            [(v * ey, s.get("eps_yoy")) for v, ey, _, s in rows if ey is not None]
        ),
        "operating_margin": margin("operating_margin"),
        "fcf_margin": margin("fcf_margin"),
        "coverage_pct": share_pct(covered, companies_value)
        if companies_value > 0
        else None,
        "companies_covered": len(earning),
    }


async def _ciks_by_isin(db: AsyncSession) -> Dict[str, int]:
    """ISIN -> CIK: each company's own, then each held listing's."""
    out: Dict[str, int] = {}
    for isin, cik in await db.execute(
        select(SecCompany.isin, SecCompany.cik).where(SecCompany.isin.is_not(None))
    ):
        out.setdefault(isin.strip().upper(), cik)
    for isin, cik in await db.execute(
        select(Security.isin, SecurityCik.cik)
        .join(SecurityCik, SecurityCik.security_id == Security.id)
        .where(SecurityCik.cik.is_not(None), Security.isin.is_not(None))
    ):
        out.setdefault(isin.strip().upper(), cik)
    return out


async def lookthrough_fundamentals(db: AsyncSession) -> Dict[str, Any]:
    """The Look-through tab's fundamentals card (see the module docstring)."""
    view = await LookthroughService(db).get_lookthrough(limit=COMPANY_LIMIT_MAX)
    rows: List[Dict[str, Any]] = view.get("companies") or []
    companies_value = float(view.get("shown_value_eur") or 0) + float(
        view.get("other_companies_eur") or 0
    )
    by_isin = await _ciks_by_isin(db)
    value_of: Dict[int, float] = {}
    for row in rows:
        cik = next(
            (
                by_isin[i.upper()]
                for i in row.get("isins") or []
                if i.upper() in by_isin
            ),
            None,
        )
        if cik is not None:
            value_of[cik] = value_of.get(cik, 0.0) + float(row["value_eur"])
    service = CompanyFinancialsService(db)
    owned = []
    for cik, value in value_of.items():
        company = await db.get(SecCompany, cik)
        if company is not None:
            _, stats = await service.key_figures(company)
            owned.append(Owned(value, stats))
    return {
        "as_of": view.get("as_of"),
        **aggregate(owned, companies_value),
        "companies": int(view.get("company_count_total") or 0),
    }
