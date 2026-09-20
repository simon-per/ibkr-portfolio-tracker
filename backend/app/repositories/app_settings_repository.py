"""
App Settings Repository
Key/value persistence for application-level settings.
"""
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_settings import AppSetting

logger = logging.getLogger(__name__)

# The base (display) currency the whole portfolio is reported in.
BASE_CURRENCY_KEY = "base_currency"
DEFAULT_BASE_CURRENCY = "EUR"
SUPPORTED_BASE_CURRENCIES = ["EUR", "CHF", "USD"]

# Read-time forecast policy. Yahoo publishes gross dividends per share; this factor
# turns them into an estimated net amount without rewriting the stored gross history.
DIVIDEND_FORECAST_NET_FACTOR_KEY = "dividend_forecast_net_factor"
DEFAULT_DIVIDEND_NET_FACTOR = Decimal("0.85")

# The to_date of the last successful IBKR sync. Used as the window start when
# attributing a share-count drop to trades / corporate actions in the period
# since the previous sync (precise windowing; falls back to heuristic when unset).
LAST_IBKR_SYNC_TO_DATE_KEY = "last_ibkr_sync_to_date"

# The earliest date from which the cash_flows deposit ledger is COMPLETE, taken from
# the from_date of a statement that actually carried deposit rows. This is the splice
# point in the contributions report: before it, contributions are measured from lot
# cost basis; from it onward, from real deposits (which a rotation cannot inflate).
#
# It has to be the statement period start rather than the first deposit's date — a
# week with no deposits is still *covered*, and using the first row would hand that
# week's purchases to the lot side and double-count them against its deposits.
CASH_FLOWS_COVERED_FROM_KEY = "cash_flows_covered_from"


class AppSettingsRepository:
    """Repository for the app_settings key/value table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        result = await self.session.execute(
            select(AppSetting).where(AppSetting.key == key)
        )
        row = result.scalar_one_or_none()
        return row.value if row else default

    async def set(self, key: str, value: str) -> AppSetting:
        result = await self.session.execute(
            select(AppSetting).where(AppSetting.key == key)
        )
        row = result.scalar_one_or_none()
        if row:
            row.value = value
        else:
            row = AppSetting(key=key, value=value)
            self.session.add(row)
        await self.session.flush()
        return row

    async def get_base_currency(self) -> str:
        return await self.get(BASE_CURRENCY_KEY, DEFAULT_BASE_CURRENCY)

    async def set_base_currency(self, currency: str) -> str:
        currency = (currency or "").upper()
        if currency not in SUPPORTED_BASE_CURRENCIES:
            raise ValueError(
                f"Unsupported base currency '{currency}'. "
                f"Choose from: {', '.join(SUPPORTED_BASE_CURRENCIES)}"
            )
        await self.set(BASE_CURRENCY_KEY, currency)
        return currency

    async def get_dividend_net_factor(self) -> Decimal:
        """Return the shared gross-to-net forecast factor, safely defaulted."""
        raw = await self.get(DIVIDEND_FORECAST_NET_FACTOR_KEY)
        if raw is None:
            return DEFAULT_DIVIDEND_NET_FACTOR
        try:
            factor = Decimal(raw)
        except (InvalidOperation, TypeError):
            factor = Decimal("NaN")
        if not factor.is_finite() or not Decimal(0) <= factor <= Decimal(1):
            logger.warning(
                "Invalid stored dividend forecast net factor %r; using %s",
                raw,
                DEFAULT_DIVIDEND_NET_FACTOR,
            )
            return DEFAULT_DIVIDEND_NET_FACTOR
        return factor

    async def set_dividend_net_factor(self, factor: Decimal) -> Decimal:
        """Persist a validated gross-to-net forecast factor in the inclusive 0..1 range."""
        try:
            factor = Decimal(str(factor))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Dividend forecast net factor must be a number") from exc
        if not factor.is_finite() or not Decimal(0) <= factor <= Decimal(1):
            raise ValueError("Dividend forecast net factor must be between 0 and 1")
        await self.set(DIVIDEND_FORECAST_NET_FACTOR_KEY, format(factor, "f"))
        return factor

    async def get_last_sync_to_date(self) -> Optional[date]:
        val = await self.get(LAST_IBKR_SYNC_TO_DATE_KEY)
        if not val:
            return None
        try:
            return date.fromisoformat(val)
        except ValueError:
            return None

    async def set_last_sync_to_date(self, to_date: date) -> None:
        if to_date is not None:
            await self.set(LAST_IBKR_SYNC_TO_DATE_KEY, to_date.isoformat())

    async def get_cash_flows_covered_from(self) -> Optional[date]:
        val = await self.get(CASH_FLOWS_COVERED_FROM_KEY)
        if not val:
            return None
        try:
            return date.fromisoformat(val)
        except ValueError:
            return None

    async def widen_cash_flows_covered_from(self, from_date: date) -> Optional[date]:
        """
        Move the deposit-coverage start earlier, never later.

        Coverage can only grow backwards — a later YTD statement must not shrink it,
        and a one-off prior-year import should extend it. Callers must only invoke
        this for a statement that actually contained deposit rows; otherwise a export
        taken without the Deposits & Withdrawals option would claim coverage it has
        no data for. Returns the resulting boundary.
        """
        if from_date is None:
            return await self.get_cash_flows_covered_from()

        existing = await self.get_cash_flows_covered_from()
        if existing is None or from_date < existing:
            await self.set(CASH_FLOWS_COVERED_FROM_KEY, from_date.isoformat())
            return from_date
        return existing

    async def ensure_default_base_currency(self) -> bool:
        """Seed the default base currency if not present. Returns True if seeded."""
        existing = await self.get(BASE_CURRENCY_KEY)
        if existing is None:
            await self.set(BASE_CURRENCY_KEY, DEFAULT_BASE_CURRENCY)
            return True
        return False
