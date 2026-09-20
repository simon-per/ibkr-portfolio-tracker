"""
Settings Router
Read/update application-level settings, notably the portfolio's base (display) currency.
"""
import logging
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.taxlot import TaxLot
from app.repositories.app_settings_repository import (
    SUPPORTED_BASE_CURRENCIES,
    AppSettingsRepository,
)
from app.services.currency_service import CurrencyService

logger = logging.getLogger(__name__)

router = APIRouter()


class SettingsResponse(BaseModel):
    base_currency: str
    supported_currencies: list[str]
    dividend_forecast_withholding_pct: float


class UpdateBaseCurrencyRequest(BaseModel):
    base_currency: str


class UpdateDividendWithholdingRequest(BaseModel):
    dividend_forecast_withholding_pct: Decimal = Field(
        ge=0, le=100, decimal_places=3
    )


def _withholding_pct(net_factor: Decimal) -> float:
    return float((Decimal(1) - net_factor) * Decimal(100))


async def _settings_response(
    repo: AppSettingsRepository,
    *,
    base_currency: str | None = None,
) -> SettingsResponse:
    return SettingsResponse(
        base_currency=base_currency or await repo.get_base_currency(),
        supported_currencies=SUPPORTED_BASE_CURRENCIES,
        dividend_forecast_withholding_pct=_withholding_pct(
            await repo.get_dividend_net_factor()
        ),
    )


@router.get("", response_model=SettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    repo = AppSettingsRepository(db)
    return await _settings_response(repo)


@router.put("/base-currency", response_model=SettingsResponse)
async def update_base_currency(
    payload: UpdateBaseCurrencyRequest,
    db: AsyncSession = Depends(get_db),
):
    repo = AppSettingsRepository(db)
    try:
        base_currency = await repo.set_base_currency(payload.base_currency)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Backfill EUR -> base daily rates over the full portfolio history so read
    # endpoints can convert without hitting the network. One Frankfurter range call.
    if base_currency != "EUR":
        try:
            min_open = (await db.execute(select(func.min(TaxLot.open_date)))).scalar()
            today = date.today()
            start = min_open or (today - timedelta(days=365))
            currency_service = CurrencyService(db)
            await currency_service._batch_fetch_rates(
                from_currency="EUR",
                target_date=today,
                to_currency=base_currency,
                days_back=max((today - start).days, 30),
            )
        except Exception as e:  # non-fatal: the read path has its own safety net
            logger.warning(f"EUR->{base_currency} backfill failed: {e}")

    return await _settings_response(repo, base_currency=base_currency)


@router.put("/dividend-withholding", response_model=SettingsResponse)
async def update_dividend_withholding(
    payload: UpdateDividendWithholdingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Set the withholding assumption used only for gross-derived dividend forecasts."""
    repo = AppSettingsRepository(db)
    net_factor = Decimal(1) - (
        payload.dividend_forecast_withholding_pct / Decimal(100)
    )
    await repo.set_dividend_net_factor(net_factor)
    return await _settings_response(repo)
