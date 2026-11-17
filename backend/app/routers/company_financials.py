"""
`/api/company-financials`: the Company tab's reads (docs/company-financials.md).

Public reads, rate-limited like every `/api/` read by `app/rate_limit.py`. **No write
route**: the figures arrive through the weekly EDGAR pass and
`app/cli/fetch_company_financials.py` only. Pure database reads; nothing here reaches
EDGAR, OpenDART, ESEF, Yahoo or IBKR.

A company is named in the path by its key (`sec:<cik>`, `dart:<corp_code>`, `lei:<LEI>`) or,
as every link did before other regulators' companies existed, by its bare CIK.
"""

from typing import Dict, List, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.company_financials import (
    CompanyDividendTrack,
    CompanyFinancialsCompany,
    CompanyFinancialsResponse,
    CompanyPeers,
    CompanyScreenerRow,
)
from app.services.company_financials_service import (
    DEFAULT_PERIOD,
    DEFAULT_RANGE,
    CompanyFinancialsService,
)

router = APIRouter()

#: A key or a bare CIK; anything longer is no company.
CompanyRef = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9:._-]+$")


async def _company(db: AsyncSession, ref: str):
    company = await CompanyFinancialsService(db).company_by_key(ref)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/companies", response_model=List[CompanyFinancialsCompany])
async def list_companies(db: AsyncSession = Depends(get_db)):
    """Held companies and the Nasdaq-100's with stored SEC figures, one per company."""
    return await CompanyFinancialsService(db).list_companies()


@router.get("/dividend-track", response_model=Dict[int, CompanyDividendTrack])
async def dividend_track(db: AsyncSession = Depends(get_db)):
    """Each held listing's company dividend record from its filings, by security id.
    Declared before `/{cik}`, which would otherwise take the path and refuse it."""
    return await CompanyFinancialsService(db).dividend_track()


@router.get("/screener", response_model=List[CompanyScreenerRow])
async def screener(db: AsyncSession = Depends(get_db)):
    """Every company the tab lists, one row each: the overview's key figures, its dividend
    record and today's multiples against their own 5-year medians. Declared before `/{cik}`."""
    return await CompanyFinancialsService(db).screener()


@router.get("/{ref}/peers", response_model=CompanyPeers)
async def company_peers(ref: str = CompanyRef, db: AsyncSession = Depends(get_db)):
    """The companies the tab lists in this one's SEC industry, with the overview's key
    figures (review item R1). 404 for a company the pass never looked at."""
    company = await _company(db, ref)
    return await CompanyFinancialsService(db).peers(company)


@router.get("/{ref}", response_model=CompanyFinancialsResponse)
async def get_company_financials(
    ref: str = CompanyRef,
    period: Literal["quarterly", "ttm", "annual"] = Query(DEFAULT_PERIOD),
    range_key: Literal["3y", "5y", "10y", "max"] = Query(DEFAULT_RANGE, alias="range"),
    db: AsyncSession = Depends(get_db),
):
    """One company's series, by key or CIK. 404 for a company the pass never looked at;
    200 with a warning when it has no stored figures."""
    company = await _company(db, ref)
    return await CompanyFinancialsService(db).get_financials(
        company, period, range_key
    )
