"""
A company's Yahoo figures and analyst counts, from one source per company (review items
DR1 and DR2, 2026-11-08).

Three surfaces show them — the positions table, the watchlist, the Company tab's overview —
and each used to find its own row: the overview the lowest-symbol held listing of the
company, the positions table and the watchlist the exact ticker, and the watchlist its own
`.info` copy of figures the held pass already stores. So GOOGL's row read 10/43/5 analysts
and linked to an Alphabet page showing GOOG's 11/43/6, and a held company's P/E differed
between the Watchlist and Fundamentals the same morning. The rules, once:

- **Counts are the company's**: every listing of a company with an SEC CIK reads its
  *canonical* listing's (`canonical_listings`: the held listing with the lowest symbol,
  the one the overview already shows). A security with no CIK reads its own.
- **A watchlist ticker's figures** are a held listing's own `fundamental_metrics` row (the
  Fundamentals tab's), else a Company-tab company's `company_yahoo_profiles` row — only
  within the Company tab's scope (held, or in the newest Nasdaq-100 pass): a company that
  left the index keeps a profile nobody refreshes. Any other ticker's come from the
  watchlist's own `.info`.

`counts_of` is the one reading of the five counts off any row that stores them.
`tests/test_company_yahoo_family.py` holds the line.
"""

from typing import Dict, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analyst_rating import RATING_COUNT_FIELDS, AnalystRating
from app.models.company_financials import SecCompany, SecurityCik
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.repositories.company_financials_repository import CompanyFinancialsRepository
from app.repositories.security_repository import held
from app.services.yahoo_minor_units import minor_unit

def counts_of(row) -> Optional[Dict[str, int]]:
    """
    The five recommendation counts off any row that stores them, or None when there is no
    row or it was never asked (a NULL `strong_buy`: a profile or a watchlist item whose
    counts Yahoo has not answered yet). The one reading, which `rating_of` then labels.
    """
    if row is None or getattr(row, "strong_buy", None) is None:
        return None
    return {f: int(getattr(row, f) or 0) for f in RATING_COUNT_FIELDS}


async def canonical_listings(db: AsyncSession) -> Dict[int, int]:
    """
    Security id -> the security id whose counts it shows: for a held listing of a company
    with a CIK, the company's held listing with the lowest symbol (then id); every other
    security maps to itself by absence. Pure database.
    """
    rows = (
        await db.execute(
            select(Security.id, Security.symbol, SecurityCik.cik)
            .join(SecurityCik, SecurityCik.security_id == Security.id)
            .where(held(), SecurityCik.cik.isnot(None))
        )
    ).all()
    by_cik: Dict[int, list] = {}
    for sid, symbol, cik in rows:
        by_cik.setdefault(cik, []).append((symbol or "", sid))
    out: Dict[int, int] = {}
    for members in by_cik.values():
        canonical = min(members)[1]
        for _, sid in members:
            out[sid] = canonical
    return out


async def counts_for_securities(
    db: AsyncSession, security_ids: Iterable[int]
) -> Dict[int, Optional[Dict[str, int]]]:
    """Security id -> the counts its row shows (the company's, `canonical_listings`)."""
    wanted = list(security_ids)
    canonical = await canonical_listings(db)
    source = {sid: canonical.get(sid, sid) for sid in wanted}
    rows = {
        r.security_id: r
        for r in (
            await db.execute(
                select(AnalystRating).where(
                    AnalystRating.security_id.in_(set(source.values()))
                )
            )
        ).scalars()
    }
    return {sid: counts_of(rows.get(src)) for sid, src in source.items()}


async def company_tab_ciks(db: AsyncSession, ciks: Iterable[int]) -> set:
    """The CIKs among `ciks` the Company tab lists: held, or in the newest Nasdaq-100 pass
    (`CompanyFinancialsService.list_companies`'s scope)."""
    wanted = set(ciks)
    if not wanted:
        return set()
    held_ciks = set(
        (
            await db.execute(
                select(SecurityCik.cik)
                .join(Security, Security.id == SecurityCik.security_id)
                .where(held(), SecurityCik.cik.in_(wanted))
            )
        ).scalars()
    )
    latest = await CompanyFinancialsRepository(db).latest_universe_stamp()
    listed = set()
    if latest is not None:
        listed = set(
            (
                await db.execute(
                    select(SecCompany.cik).where(
                        SecCompany.cik.in_(wanted),
                        SecCompany.universe_seen_at == latest,
                    )
                )
            ).scalars()
        )
    return held_ciks | listed


class TickerSource:
    """Where a watchlist ticker's company figures, counts and filed growth come from."""

    def __init__(
        self,
        figures=None,
        counts: Optional[Dict[str, int]] = None,
        growth: Optional[Dict] = None,
    ):
        self.figures = figures
        self.counts = counts
        self.growth = growth


async def sources_for_tickers(
    db: AsyncSession, tickers: Iterable[str], held_ids: Dict[str, int]
) -> Dict[str, TickerSource]:
    """
    Upper-cased ticker -> its company's stored figures and counts: a held listing's own
    `fundamental_metrics` row with the company's counts, else a Company-tab company's
    profile by its SEC ticker. A ticker in neither is absent. `held_ids` is the upper-cased
    held ticker -> security id map the caller resolved (`security_ids_by_ticker`).
    """
    wanted = {t.upper() for t in tickers}
    out: Dict[str, TickerSource] = {}
    held_here = {t: held_ids[t] for t in wanted if t in held_ids}
    if held_here:
        metrics = {
            m.security_id: m
            for m in (
                await db.execute(
                    select(FundamentalMetrics).where(
                        FundamentalMetrics.security_id.in_(held_here.values())
                    )
                )
            ).scalars()
        }
        counts = await counts_for_securities(db, held_here.values())
        growth = await growth_by_security(db, held_here.values())
        for ticker, sid in held_here.items():
            out[ticker] = TickerSource(metrics.get(sid), counts.get(sid), growth.get(sid))
    rest = wanted - set(held_here)
    if rest:
        upper = func.upper(SecCompany.ticker)
        ciks = dict(
            (
                await db.execute(select(upper, SecCompany.cik).where(upper.in_(rest)))
            ).all()
        )
        in_scope = await company_tab_ciks(db, ciks.values())
        profiles = await CompanyFinancialsRepository(db).yahoo_profiles(in_scope)
        growth = await edgar_ttm_growth(db, in_scope)
        for ticker, cik in ciks.items():
            profile = profiles.get(cik)
            if (
                cik in in_scope
                and profile is not None
                and profile.fetched_at is not None
            ):
                out[ticker] = TickerSource(profile, counts_of(profile), growth.get(cik))
    return out


#: Watchlist column -> the stored figure it shows (`fundamental_metrics` and
#: `company_yahoo_profiles` share these names).
WATCHLIST_FIGURES = {
    "trailing_pe": "trailing_pe",
    "forward_pe": "forward_pe",
    "peg_ratio": "peg_ratio",
    "revenue_growth": "revenue_growth",
    "earnings_growth": "earnings_growth",
    "fwd_revenue_growth": "fwd_revenue_growth",
    "fwd_eps_growth": "fwd_eps_growth",
    "profit_margins": "profit_margins",
    "market_cap": "market_cap",
    "analyst_target": "target_mean_price",
    "analyst_count": "number_of_analysts",
    "data_currency": "data_currency",
}


def company_figures(figures) -> Dict[str, object]:
    """The watchlist columns a stored figures row fills, None included: the company's
    figure, absent where it has none, never the watchlist's own `.info` beside it."""
    return {column: getattr(figures, field, None) for column, field in WATCHLIST_FIGURES.items()}


#: `growth_basis` on a served row: what its revenue and EPS growth compare.
GROWTH_TTM = "ttm"          # the last four quarters against the four before (SEC filings)
GROWTH_QUARTER = "quarter"  # the latest quarter against the same one a year before (Yahoo)


async def edgar_ttm_growth(db: AsyncSession, ciks: Iterable[int]) -> Dict[int, Dict]:
    """
    CIK -> ``{"revenue_growth", "earnings_growth"}``, the TTM-over-TTM growth of revenue and
    diluted EPS from the company's stored SEC figures (`key_stats`' `revenue_yoy` and
    `eps_yoy`), for every CIK with stored figures. No request of any kind.

    **Why** (review item Y1, 2026-11-08): yfinance's quarterly timeseries returns at most
    five quarters, so the 8-quarter TTM tier of `ttm_growth` never runs and the "TTM"
    growth Yahoo's rows carry is the latest quarter against the same quarter a year
    before — NVDA's revenue +105.85% where its TTM is +83.4%. Where the filings are
    stored, they answer; elsewhere the Yahoo figure stays and says it is a quarter's.
    """
    from app.services.company_financials_service import key_stats

    repo = CompanyFinancialsRepository(db)
    out: Dict[int, Dict] = {}
    for cik in set(ciks):
        rows = await repo.rows_for(cik)
        if not rows:
            continue
        stats = key_stats(rows)
        out[cik] = {
            "revenue_growth": stats["revenue_yoy"],
            "earnings_growth": stats["eps_yoy"],
        }
    return out


async def growth_by_security(
    db: AsyncSession, security_ids: Iterable[int]
) -> Dict[int, Dict]:
    """Security id -> its company's `edgar_ttm_growth`, for securities with a CIK."""
    wanted = list(security_ids)
    if not wanted:
        return {}
    cik_of = dict((await db.execute(
        select(SecurityCik.security_id, SecurityCik.cik).where(
            SecurityCik.security_id.in_(wanted), SecurityCik.cik.isnot(None)
        )
    )).all())
    growth = await edgar_ttm_growth(db, cik_of.values())
    return {sid: growth[cik] for sid, cik in cik_of.items() if cik in growth}


def served_growth(
    revenue: Optional[float], earnings: Optional[float], edgar: Optional[Dict]
) -> Dict:
    """A row's ``revenue_growth``, ``earnings_growth`` and ``growth_basis``: the filings'
    TTM growth where the company's are stored, else Yahoo's quarter, None when neither."""
    if edgar is not None:
        return {**edgar, "growth_basis": GROWTH_TTM}
    basis = GROWTH_QUARTER if revenue is not None or earnings is not None else None
    return {"revenue_growth": revenue, "earnings_growth": earnings, "growth_basis": basis}


def source_columns(source: Optional[TickerSource]) -> Dict[str, object]:
    """
    The watchlist columns a ticker's company source fills (`company_figures`), its revenue
    and EPS growth the filings' TTM where stored (`served_growth`), and `growth_basis`.
    Empty for a ticker with no stored source: the row's own `.info` answers.
    """
    if source is None:
        return {}
    columns = company_figures(source.figures) if source.figures is not None else {}
    if source.growth is not None or columns:
        columns.update(served_growth(
            columns.get("revenue_growth"), columns.get("earnings_growth"), source.growth
        ))
    # The stored target is in the quote's unit, pence for a London listing; the watchlist
    # shows prices in the major unit (review item Y4).
    minor = minor_unit(columns.get("data_currency"))
    if minor and columns.get("analyst_target") is not None:
        columns["analyst_target"] = columns["analyst_target"] / float(minor[1])
    return columns


def target_upside(
    target: Optional[float],
    price: Optional[float],
    target_currency: Optional[str],
    price_currency: Optional[str],
) -> Optional[float]:
    """
    The analyst mean target over the price, less one (0.12 is +12%), or None: a figure
    missing, a price not positive, a currency unknown, or two currencies (a minor unit
    counts as its major, `minor_unit`). The one upside: the overview, the watchlist's
    column and its buy score all read it (review item DR7, 2026-11-08) — the watchlist
    computed its own twice, unguarded, beside the overview's guarded one.
    """
    if target is None or price is None or price <= 0:
        return None
    if not target_currency or not price_currency:
        return None

    def major(code: str) -> str:
        minor = minor_unit(code)
        return minor[0] if minor else code.upper()

    if major(target_currency) != major(price_currency):
        return None
    return target / price - 1
