"""
Tax Router
Per-year tax report (dividend income + withholding, realized gains, holdings)
with JSON and CSV output. Swiss-focused framing; see TaxService.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.database import get_db
from app.services.tax_service import TaxService

router = APIRouter()

_MIN_YEAR = 2000


def _resolve_year(year: Optional[int]) -> int:
    """
    The current calendar year is the ceiling and the default — read per request.

    It was a module constant, `date.today().year` evaluated at import, so on
    1 January `?year=<new year>` answered 422 and the default served the *prior*
    year until the container happened to restart. UTC, like every other clock here
    (`app.clock.utcnow`), so the flip is at midnight UTC rather than 01:00 Berlin.
    """
    max_year = utcnow().year
    if year is None:
        return max_year
    if year > max_year:
        raise HTTPException(
            status_code=422,
            detail=f"year must be between {_MIN_YEAR} and {max_year}",
        )
    return year


@router.get("/report")
async def get_tax_report(
    year: Optional[int] = Query(default=None, ge=_MIN_YEAR),
    db: AsyncSession = Depends(get_db),
):
    """Return the tax report for a calendar year in the configured base currency."""
    return await TaxService(db).get_tax_report(_resolve_year(year))


@router.get("/report.csv", response_class=PlainTextResponse)
async def get_tax_report_csv(
    year: Optional[int] = Query(default=None, ge=_MIN_YEAR),
    db: AsyncSession = Depends(get_db),
):
    """Return the tax report as a downloadable CSV."""
    year = _resolve_year(year)
    service = TaxService(db)
    report = await service.get_tax_report(year)
    csv_text = service.to_csv(report)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="tax_report_{year}.csv"'},
    )
