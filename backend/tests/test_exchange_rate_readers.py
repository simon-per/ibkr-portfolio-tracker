"""
Which modules may read `ExchangeRate` rows — and what the shared read helper promises.

Until 2026-09-28 the same preload-and-forward-fill existed twice
(`PortfolioService._preload_exchange_rates` and `BenchmarkService._preload_fx_rates`),
and the crypto view's history was about to be the third copy. They now share
`fx_preload`, and the EUR->base loader moved to `base_fx`. `currency_service` owns the
cache itself.

A fourth module selecting rows is either another copy of the preload, or a read path
about to reach for `CurrencyService.get_exchange_rate` — which asks Frankfurter on a miss
and writes a carried row, so inside a GET it makes network calls and takes SQLite's
write lock. Either way it should go through one of these three instead.
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models.exchange_rate import ExchangeRate
from app.services.base_fx import BaseFx
from app.services.fx_preload import PreloadedRates, eur_rate_on, preload_eur_rates
from app.services.native_amounts import NativeToBase

APP_DIR = Path(__file__).resolve().parents[1] / "app"

ALLOWED_READERS = {
    "services/currency_service.py",
    "services/base_fx.py",
    "services/fx_preload.py",
}

_ROW_SELECT = re.compile(r"select\(\s*ExchangeRate\s*\)")


def test_only_the_fx_modules_select_exchange_rate_rows():
    readers = {
        path.relative_to(APP_DIR).as_posix()
        for path in APP_DIR.rglob("*.py")
        if _ROW_SELECT.search(path.read_text(encoding="utf-8"))
    }
    assert readers == ALLOWED_READERS, (
        f"Unexpected ExchangeRate readers: {sorted(readers - ALLOWED_READERS)}; "
        f"missing: {sorted(ALLOWED_READERS - readers)}. Read rates through "
        f"fx_preload (dated amounts on a read path) or base_fx (EUR->base)."
    )


# A Friday rate and the following Monday's: the ECB publishes nothing in between.
FRIDAY = date(2026, 9, 18)
MONDAY = date(2026, 9, 21)


def test_a_weekend_forward_fills_from_friday_and_no_further_than_the_bound():
    cache = {("USD", FRIDAY): Decimal("0.90"), ("USD", MONDAY): Decimal("0.91")}
    assert eur_rate_on(cache, "USD", FRIDAY + timedelta(days=1)) == Decimal("0.90")
    assert eur_rate_on(cache, "USD", FRIDAY + timedelta(days=2)) == Decimal("0.90")
    assert eur_rate_on(cache, "USD", MONDAY) == Decimal("0.91")
    # Past the bound a rate is missing, not stale.
    assert eur_rate_on(cache, "USD", MONDAY + timedelta(days=20), max_lookback_days=14) is None
    assert eur_rate_on(cache, "CHF", MONDAY) is None


@pytest.mark.asyncio
async def test_preloaded_rates_convert_through_native_to_base_without_any_io():
    """`NativeToBase` over `PreloadedRates` is the read path's converter: the same
    two-step every reader uses, answering from the cache alone."""
    cache = {("USD", FRIDAY): Decimal("0.90")}
    chf = BaseFx("CHF", {FRIDAY: Decimal("0.94")})
    converter = NativeToBase(PreloadedRates(cache), chf)

    sunday = FRIDAY + timedelta(days=2)
    assert await converter.convert(Decimal("100"), "USD", sunday) == Decimal("100") * Decimal(
        "0.90") * Decimal("0.94")
    # Already the base currency: no conversion at all.
    usd = NativeToBase(PreloadedRates(cache), BaseFx("USD", {FRIDAY: Decimal("1.10")}))
    assert await usd.convert(Decimal("100"), "USD", sunday) == Decimal("100")
    # A miss is a missing amount, never zero.
    assert await converter.convert(Decimal("100"), "USD", FRIDAY - timedelta(days=30)) is None


@pytest.mark.asyncio
async def test_preload_reaches_back_for_the_fill_and_skips_eur():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        session.add_all([
            ExchangeRate(date=FRIDAY - timedelta(days=10), from_currency="USD",
                         to_currency="EUR", rate=Decimal("0.89"), source="test"),
            ExchangeRate(date=FRIDAY - timedelta(days=40), from_currency="USD",
                         to_currency="EUR", rate=Decimal("0.80"), source="test"),
        ])
        await session.commit()
        cache = await preload_eur_rates(session, {"USD", "EUR"}, FRIDAY, MONDAY)
        assert cache == {("USD", FRIDAY - timedelta(days=10)): Decimal("0.89")}
        assert await preload_eur_rates(session, {"EUR"}, FRIDAY, MONDAY) == {}
    finally:
        await session.close()
        await engine.dispose()
