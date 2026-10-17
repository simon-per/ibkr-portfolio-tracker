"""
Settings Router
Read/update application-level settings, notably the portfolio's base (display) currency.
"""
import logging
from datetime import date
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
    normalize_base_currency,
)
from app.services.base_fx import default_start, load_base_fx
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
        base_currency = normalize_base_currency(payload.base_currency)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Backfill EUR -> base daily rates over the full portfolio history so read
    # endpoints can convert without hitting the network. One Frankfurter range call.
    if base_currency != "EUR":
        min_open = (await db.execute(select(func.min(TaxLot.open_date)))).scalar()
        start = default_start(min_open)
        currency_service = CurrencyService(db)
        try:
            today = date.today()
            await currency_service._batch_fetch_rates(
                from_currency="EUR",
                target_date=today,
                to_currency=base_currency,
                days_back=max((today - start).days, 30),
            )
        except Exception as e:  # a gap is fine; having no rate at all is not (below)
            logger.warning(f"EUR->{base_currency} backfill failed: {e}")

        # Refuse whole, before the setting is written. With no EUR->base rate at all,
        # `BaseFx.convert` hands back the EUR amount, so every figure in the app would be
        # EUR printed under a CHF or USD label — right numbers, wrong currency, nothing on
        # screen inviting doubt. One rate is enough (`BaseFx` carries the nearest one
        # forward and back), and the check is the read path's own loader, so it cannot
        # disagree with what the next GET would find. The backfill may have committed
        # rates already; that is harmless, the base currency is what must not move.
        fx = await load_base_fx(db, currency_service, base_currency, start, backfill=False)
        if not fx.rate_cache:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"No EUR->{base_currency} exchange rate could be loaded, so the "
                    "display currency was not changed. Try again later."
                ),
            )

    base_currency = await repo.set_base_currency(base_currency)
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
