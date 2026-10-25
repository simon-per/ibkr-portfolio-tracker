"""
`/api/company-financials`: the Company tab's reads (docs/company-financials.md).

Public reads, rate-limited like every `/api/` read by `app/rate_limit.py`. **No write
route**: the figures arrive through the weekly EDGAR pass and
`app/cli/fetch_company_financials.py` only. Pure database reads; nothing here reaches
EDGAR, Yahoo or IBKR.
"""

from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company_financials import SecCompany
from app.routers._params import DbId
from app.schemas.company_financials import (
    CompanyFinancialsCompany,
    CompanyFinancialsResponse,
)
from app.services.company_financials_service import (
    DEFAULT_PERIOD,
    DEFAULT_RANGE,
    CompanyFinancialsService,
)

router = APIRouter()


@router.get("/companies", response_model=List[CompanyFinancialsCompany])
async def list_companies(db: AsyncSession = Depends(get_db)):
    """Held companies and the Nasdaq-100's with stored SEC figures, one per company."""
    return await CompanyFinancialsService(db).list_companies()


@router.get("/{cik}", response_model=CompanyFinancialsResponse)
async def get_company_financials(
    cik: DbId,
    period: Literal["quarterly", "ttm", "annual"] = Query(DEFAULT_PERIOD),
    range_key: Literal["5y", "10y", "max"] = Query(DEFAULT_RANGE, alias="range"),
    db: AsyncSession = Depends(get_db),
):
    """One company's series, by CIK. 404 for a company the pass never looked at; 200
    with a warning when it has no stored figures."""
    company = await db.get(SecCompany, cik)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return await CompanyFinancialsService(db).get_financials(
        company, period, range_key
    )
