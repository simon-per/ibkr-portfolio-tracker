"""
An announced dividend that raises the payout — `upcoming[].per_share_change_pct` on
`/api/dividends/breakdown` (GitHub issue #74), the calendar's "+x%" tag.

The announced amount is IBKR's open accrual, gross over quantity; the comparison is the
last regular IBKR payment's stated rate (`per_share_native`), both per share in the
payment's own currency. The ways it could be wrong: a special or a bonus taken as the
level (or an announced special read as a raise), a figure invented where the last rate
was never stated, and two currencies compared as one. Pure DB: no Yahoo, no Flex.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.clock import utcnow
from app.database import Base
import app.models  # noqa: F401
from app.models.dividend_accrual import DividendAccrual
from app.models.dividend_payment import DividendPayment
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.dividend_service import DividendService

AS_OF = date(2026, 5, 15)
USD_EUR = Decimal("0.9")
SHARES = Decimal("10")
PAY_DATES = [date(2025, 6, 1), date(2025, 9, 1), date(2025, 12, 1), date(2026, 3, 1)]


async def _session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="AAA", description="Alpha",
                         currency="USD", conid=100, asset_category="STK", exchange="NYSE"))
    session.add(TaxLot(
        security_id=1, open_date=date(2024, 1, 2), quantity=SHARES,
        cost_basis=Decimal("1000"), cost_basis_eur=Decimal("900"),
        price_per_unit=Decimal("100"), currency="USD", is_open=True,
    ))
    session.add(ExchangeRate(date=AS_OF - timedelta(days=1), from_currency="USD",
                             to_currency="EUR", rate=USD_EUR, source="test"))
    for pay in PAY_DATES:
        session.add(_paid(pay, "0.80"))
    await session.flush()
    return engine, session


def _paid(pay, rate, kind="ordinary", stated=True):
    """An IBKR dividend of ``rate`` USD a share on the ten shares held."""
    gross = SHARES * Decimal(rate) * USD_EUR
    return DividendPayment(
        security_id=1, ex_date=pay, pay_date=pay, currency="USD", source="ibkr",
        gross_amount_eur=gross, withholding_tax_eur=gross * Decimal("0.15"),
        net_amount_eur=gross * Decimal("0.85"),
        per_share_native=Decimal(rate) if stated else None, dividend_kind=kind,
    )


def _accrual(ex, pay, rate, currency="USD"):
    gross = SHARES * Decimal(rate) * USD_EUR
    return DividendAccrual(
        security_id=1, ex_date=ex, pay_date=pay, currency=currency, quantity=SHARES,
        gross_amount_eur=gross, withholding_tax_eur=gross * Decimal("0.15"),
        net_amount_eur=gross * Decimal("0.85"), last_seen_at=utcnow(),
    )


async def _announced(session):
    await session.commit()
    data = await DividendService(session).get_dividend_breakdown(
        include_forecast=True, as_of=AS_OF,
    )
    return data["upcoming"]


def _by_ex(upcoming):
    return {u["ex_date"]: u["per_share_change_pct"]
            for u in upcoming if u["amount_source"] == "announced"}


@pytest.mark.asyncio
async def test_an_announced_raise_is_measured_against_the_last_regular_rate():
    engine, session = await _session()
    try:
        session.add(_accrual(date(2026, 5, 20), date(2026, 6, 1), "0.88"))

        upcoming = await _announced(session)

        assert _by_ex(upcoming) == {"2026-05-20": 10.0}     # 0.80 -> 0.88 USD
        # Only an announced amount is compared: a projection carries nothing.
        assert all(u["per_share_change_pct"] is None
                   for u in upcoming if u["amount_source"] != "announced")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_special_payment_is_not_the_level_a_raise_is_measured_from():
    engine, session = await _session()
    try:
        session.add(_paid(date(2026, 3, 20), "3.00", kind="special"))
        session.add(_paid(date(2026, 3, 25), "2.00", kind="bonus"))
        session.add(_accrual(date(2026, 5, 20), date(2026, 6, 1), "0.88"))

        assert _by_ex(await _announced(session)) == {"2026-05-20": 10.0}
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_announced_special_is_not_a_raise():
    """
    An accrual carries no kind, so the forecast's own rule decides: a payment over twice
    its neighbours, off the schedule beside the regular one, is a special. The regular
    accrual next to it is still compared.
    """
    engine, session = await _session()
    try:
        session.add(_accrual(date(2026, 5, 20), date(2026, 6, 1), "0.88"))
        session.add(_accrual(date(2026, 5, 28), date(2026, 6, 5), "5.00"))

        assert _by_ex(await _announced(session)) == {
            "2026-05-20": 10.0, "2026-05-28": None,
        }
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_cut_is_served_signed():
    engine, session = await _session()
    try:
        session.add(_accrual(date(2026, 5, 20), date(2026, 6, 1), "0.72"))

        assert _by_ex(await _announced(session)) == {"2026-05-20": -10.0}
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_without_a_stated_last_rate_the_change_is_absent():
    """The last regular payment's rate never parsed: unknown, not an older rate."""
    engine, session = await _session()
    try:
        session.add(_paid(date(2026, 4, 1), "0.80", stated=False))
        session.add(_accrual(date(2026, 5, 20), date(2026, 6, 1), "0.88"))

        assert _by_ex(await _announced(session)) == {"2026-05-20": None}
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_currencies_are_never_compared():
    engine, session = await _session()
    try:
        session.add(_accrual(date(2026, 5, 20), date(2026, 6, 1), "0.88", currency="EUR"))

        assert _by_ex(await _announced(session)) == {"2026-05-20": None}
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_first_dividend_has_nothing_to_beat():
    engine, session = await _session()
    try:
        await session.execute(DividendPayment.__table__.delete())
        session.add(_accrual(date(2026, 5, 20), date(2026, 6, 1), "0.88"))

        assert _by_ex(await _announced(session)) == {"2026-05-20": None}
    finally:
        await session.close()
        await engine.dispose()
