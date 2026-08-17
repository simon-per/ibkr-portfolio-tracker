"""
Portfolio Router
API endpoints for portfolio value and positions.
"""
import logging
from typing import List, Optional
from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.activity_service import (
    ActivityService,
    DEFAULT_LIMIT,
    KINDS,
    MAX_LIMIT,
)
from app.services.portfolio_service import PortfolioService
from app.services.benchmark_service import BenchmarkService, BENCHMARKS
from app.services.lookthrough_service import COMPANY_LIMIT_MAX, LookthroughService
from app.schemas.lookthrough import LookthroughResponse
from app.schemas.portfolio import (
    PortfolioValuePoint,
    PortfolioSummary,
    PositionResponse,
    AnnualizedReturnResponse,
    BenchmarkResponse,
    BenchmarkInfo,
    PerformanceAttributionResponse,
    ContributionsResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Same reasoning as the 5-year cap on value-over-time: the route is anonymous, so the
# window a caller can ask for has to be bounded somewhere.
MAX_ACTIVITY_DAYS = 365 * 5


@router.get("/value-over-time", response_model=List[PortfolioValuePoint])
async def get_portfolio_value_over_time(
    start_date: date = Query(
        default=None,
        description="Start date for the chart (defaults to 1 year ago)"
    ),
    end_date: date = Query(
        default=None,
        description="End date for the chart (defaults to today)"
    ),
    db: AsyncSession = Depends(get_db)
):
    """
    Get portfolio value (cost basis and market value) over time.

    This endpoint provides the data for the dual-line chart showing:
    - Cost basis: Total amount invested over time
    - Market value: Current worth of holdings over time

    Returns data points for each business day in the range.
    """
    if not end_date:
        end_date = date.today()

    if not start_date:
        # Default to 1 year ago
        start_date = end_date - timedelta(days=365)

    # Validate date range
    if start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="start_date must be before or equal to end_date"
        )

    # Limit to reasonable range (max 5 years to prevent huge queries)
    max_days = 365 * 5
    if (end_date - start_date).days > max_days:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large. Maximum allowed is {max_days} days (5 years)"
        )

    portfolio_service = PortfolioService(db)

    logger.debug(f"API CALL: start_date={start_date}, end_date={end_date}")
    timeline = await portfolio_service.get_portfolio_value_over_time(start_date, end_date)
    logger.debug(f"API RESULT: {len(timeline)} data points")
    if timeline:
        logger.debug(f"First point: {timeline[0]}")
        logger.debug(f"Last point: {timeline[-1]}")

    return timeline


@router.get("/summary", response_model=PortfolioSummary)
async def get_portfolio_summary(db: AsyncSession = Depends(get_db)):
    """
    Get current portfolio summary with total values.

    Returns:
    - total_cost_basis_eur: Total amount invested
    - total_market_value_eur: Current worth
    - total_gain_loss_eur: Unrealized gain/loss
    - total_gain_loss_percent: Percentage gain/loss
    - num_positions: Number of unique securities held
    """
    portfolio_service = PortfolioService(db)
    summary = await portfolio_service.get_current_portfolio_summary()

    return summary


@router.get("/contributions", response_model=ContributionsResponse)
async def get_contributions(db: AsyncSession = Depends(get_db)):
    """
    Average money added to the account per month, over all time / 12M / 6M / 3M.

    Derived from tax lots (capital deployed minus capital released), because the
    Flex Query carries no deposit rows — see PortfolioService.get_contributions.
    """
    portfolio_service = PortfolioService(db)
    return await portfolio_service.get_contributions()


@router.get("/annualized-return", response_model=AnnualizedReturnResponse)
async def get_annualized_return(
    start_date: date = Query(..., description="Start date for the calculation"),
    end_date: date = Query(..., description="End date for the calculation"),
    db: AsyncSession = Depends(get_db)
):
    """
    Calculate XIRR (money-weighted annualized return) for the portfolio.

    Treats each tax lot purchase as a dated cash flow and finds the annualized
    rate that makes the NPV of all cash flows equal zero.
    """
    if start_date >= end_date:
        raise HTTPException(
            status_code=400,
            detail="start_date must be before end_date"
        )

    portfolio_service = PortfolioService(db)
    xirr_pct, num_cash_flows, eff_start, eff_end, method = await portfolio_service.calculate_xirr(start_date, end_date)

    return AnnualizedReturnResponse(
        # "simple_period" for <30-day windows — a raw period return the UI must
        # not present as annualized.
        method=method,
        annualized_return_pct=round(xirr_pct, 2) if xirr_pct is not None else None,
        start_date=eff_start.isoformat(),
        end_date=eff_end.isoformat(),
        num_cash_flows=num_cash_flows,
        # A latch rather than a sixth tuple element — see calculate_xirr. Above 0 means
        # this return was measured against a partial valuation, which the client says so
        # about rather than presenting a plausible understatement as a measurement.
        unpriced_holdings=portfolio_service.last_xirr_unpriced,
    )


@router.get("/attribution", response_model=PerformanceAttributionResponse)
async def get_performance_attribution(
    start_date: date = Query(..., description="Start date for the attribution period"),
    end_date: date = Query(..., description="End date for the attribution period"),
    db: AsyncSession = Depends(get_db)
):
    """
    Get per-security P&L attribution for a time period.

    Shows which securities drove portfolio gains/losses by comparing
    market values at start and end dates, accounting for new investments.
    """
    if start_date >= end_date:
        raise HTTPException(
            status_code=400,
            detail="start_date must be before end_date"
        )

    portfolio_service = PortfolioService(db)
    result = await portfolio_service.get_performance_attribution(start_date, end_date)

    return PerformanceAttributionResponse(**result)


def _activity_window(start_date: Optional[date], end_date: Optional[date]) -> tuple:
    """
    Default and validate the activity window, mirroring the guards on value-over-time.

    The default is one year rather than everything: the ledger is the landing view of a
    tab, and an unbounded default would make the first paint the most expensive query
    on the site.
    """
    end_date = end_date or date.today()
    start_date = start_date or (end_date - timedelta(days=365))

    if start_date > end_date:
        raise HTTPException(
            status_code=400, detail="start_date must be before or equal to end_date"
        )
    if (end_date - start_date).days > MAX_ACTIVITY_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large. Maximum allowed is {MAX_ACTIVITY_DAYS} days",
        )
    return start_date, end_date


def _activity_kinds(kind: Optional[str]) -> Optional[List[str]]:
    """
    Parse the `kind` filter. Comma-separated so several can be combined in one
    querystring, and an unknown value is refused rather than silently ignored — a typo
    that quietly returns everything is the kind of thing nobody notices.
    """
    if not kind:
        return None
    wanted = [k.strip() for k in kind.split(",") if k.strip()]
    unknown = [k for k in wanted if k not in KINDS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown kind(s): {', '.join(unknown)}. Valid: {', '.join(KINDS)}",
        )
    return wanted or None


@router.get("/activity")
async def get_activity(
    start_date: date = Query(default=None, description="Window start (defaults to 1 year ago)"),
    end_date: date = Query(default=None, description="Window end (defaults to today)"),
    kind: str = Query(default=None, description=f"Comma-separated subset of: {', '.join(KINDS)}"),
    symbol: str = Query(default=None, description="Exact ticker match"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """
    The account's transaction ledger: trades, dividends, cash flows and corporate
    actions in one chronological list, newest first.

    All four tables were ingested and depended on but had no read surface at all — so
    "what happened on this date" had no answer, and the transfer audit that guards the
    money-added figure was an ssh command. Cash-flow rows carry `counts_as_money_in`
    for exactly that.
    """
    start_date, end_date = _activity_window(start_date, end_date)
    return await ActivityService(db).get_activity(
        start_date=start_date, end_date=end_date,
        kinds=_activity_kinds(kind), symbol=symbol, limit=limit, offset=offset,
    )


@router.get("/activity.csv", response_class=PlainTextResponse)
async def get_activity_csv(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    kind: str = Query(default=None),
    symbol: str = Query(default=None),
    limit: int = Query(default=MAX_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """
    The same ledger as a download. Defaults to the maximum page rather than the UI's,
    since someone exporting wants the window they asked for, not one screen of it.
    """
    start_date, end_date = _activity_window(start_date, end_date)
    service = ActivityService(db)
    result = await service.get_activity(
        start_date=start_date, end_date=end_date,
        kinds=_activity_kinds(kind), symbol=symbol, limit=limit, offset=offset,
    )
    return PlainTextResponse(
        content=service.to_csv(result),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="activity_{start_date}_{end_date}.csv"'
        },
    )


@router.get("/benchmarks", response_model=List[BenchmarkInfo])
async def get_available_benchmarks():
    """Return list of available benchmark indices for comparison."""
    return [
        BenchmarkInfo(key=key, name=info["name"], ticker=info["ticker"], currency=info["currency"])
        for key, info in BENCHMARKS.items()
    ]


@router.get("/benchmark", response_model=BenchmarkResponse)
async def get_benchmark_comparison(
    start_date: date = Query(default=None, description="Start date (defaults to 1 year ago)"),
    end_date: date = Query(default=None, description="End date (defaults to today)"),
    benchmark: str = Query(default="sp500", description="Benchmark: sp500 or nasdaq"),
    db: AsyncSession = Depends(get_db),
):
    """
    Compare portfolio against a benchmark index.

    Simulates investing every tax lot into S&P 500 or NASDAQ instead,
    using the same dates and EUR amounts.
    """
    if benchmark not in BENCHMARKS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown benchmark '{benchmark}'. Choose from: {', '.join(BENCHMARKS.keys())}",
        )

    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=365)

    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be before or equal to end_date")

    max_days = 365 * 5
    if (end_date - start_date).days > max_days:
        raise HTTPException(status_code=400, detail=f"Date range too large. Maximum {max_days} days.")

    bench_info = BENCHMARKS[benchmark]
    service = BenchmarkService(db)
    data = await service.calculate_benchmark_value_over_time(start_date, end_date, benchmark)

    return BenchmarkResponse(
        benchmark_name=bench_info["name"],
        benchmark_ticker=bench_info["ticker"],
        data=data,
    )


@router.get("/positions", response_model=List[PositionResponse])
async def get_positions_breakdown(db: AsyncSession = Depends(get_db)):
    """
    Get breakdown of all current positions by security.

    Returns list of positions with:
    - Security information (symbol, description, ISIN)
    - Quantity held
    - Cost basis (total invested)
    - Current market value
    - Unrealized gain/loss
    - Individual taxlots that make up the position

    Positions are sorted by market value (largest first).
    """
    portfolio_service = PortfolioService(db)
    positions = await portfolio_service.get_positions_breakdown()

    return positions


@router.get("/lookthrough", response_model=LookthroughResponse)
async def get_lookthrough(
    limit: int = Query(
        50,
        ge=1,
        le=COMPANY_LIMIT_MAX,
        description="How many company rows to return, largest first",
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Company-level exposure: direct holdings plus every held fund decomposed into the
    companies inside it, folded across listings and share classes.

    Pure DB read — touches no provider, so it is safe at any hour. Baskets arrive through
    `app/cli/import_etf_basket.py`; a fund with no stored basket is reported in `funds`
    with the reason why, never dropped.

    **Read `coverage_pct` before any row.** Every percentage is a share of the whole
    portfolio and nothing is renormalised onto the decomposed subset, so while some funds
    have no basket each company figure is an understatement by whatever those funds hold.
    """
    service = LookthroughService(db)
    return await service.get_lookthrough(limit=limit)
