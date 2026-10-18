from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.app_settings_repository import (
    DIVIDEND_FORECAST_NET_FACTOR_KEY,
    AppSettingsRepository,
)
from tests.memory_db import memory_engine


@pytest.mark.asyncio
async def test_dividend_net_factor_defaults_persists_and_recovers_from_bad_storage(
    caplog,
):
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        repo = AppSettingsRepository(session)
        assert await repo.get_dividend_net_factor() == Decimal("0.85")

        await repo.set_dividend_net_factor(Decimal("0.73625"))
        assert await repo.get_dividend_net_factor() == Decimal("0.73625")

        await repo.set(DIVIDEND_FORECAST_NET_FACTOR_KEY, "not-a-number")
        assert await repo.get_dividend_net_factor() == Decimal("0.85")
        assert "Invalid stored dividend forecast net factor" in caplog.text
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [Decimal("-0.001"), Decimal("1.001"), Decimal("NaN")])
async def test_dividend_net_factor_refuses_values_outside_zero_to_one(value):
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        with pytest.raises(ValueError):
            await AppSettingsRepository(session).set_dividend_net_factor(value)
    finally:
        await session.close()
        await engine.dispose()
