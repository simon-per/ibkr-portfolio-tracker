"""
An open accrual is kept in the payment's own currency and converted when read (review item
D2, 2026-11-08).

Its EUR columns froze the rate of the day the row was stored, while every other amount in
the forecast is converted at the newest cached rate: an unchanged dividend then read as a
raise or a cut whenever the rate had moved since — on the calendar's tag, in the payer's
level and per-share growth, and in the raises the forecast attributes from one day to the
next. And ``gross / quantity`` read the rate back off a gross rounded to the cent. Pure DB:
no Yahoo, no Flex.
"""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.clock import utc_today, utcnow
from app.models.dividend_accrual import DividendAccrual
from app.models.dividend_payment import DividendPayment
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.dividend_forecast_attribution import Holding, attribute_holding
from app.services.dividend_service import DividendService
from app.services.ibkr_service import IBKRService
from tests.memory_db import memory_engine

AS_OF = date(2026, 5, 15)
SHARES = Decimal("10")
RATE_NOW = Decimal("0.9")  # the newest cached USD->EUR on AS_OF
PAY_DATES = [date(2025, 6, 1), date(2025, 9, 1), date(2025, 12, 1), date(2026, 3, 1)]
ACCRUAL_EX, ACCRUAL_PAY = date(2026, 5, 20), date(2026, 6, 1)


async def _session():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(
        Security(
            id=1,
            isin="US0000000001",
            symbol="AAA",
            description="Alpha",
            currency="USD",
            conid=100,
            asset_category="STK",
            exchange="NYSE",
        )
    )
    session.add(
        TaxLot(
            security_id=1,
            open_date=date(2024, 1, 2),
            quantity=SHARES,
            cost_basis=Decimal("1000"),
            cost_basis_eur=Decimal("900"),
            price_per_unit=Decimal("100"),
            currency="USD",
            is_open=True,
        )
    )
    session.add(
        ExchangeRate(
            date=AS_OF - timedelta(days=1),
            from_currency="USD",
            to_currency="EUR",
            rate=RATE_NOW,
            source="test",
        )
    )
    for pay in PAY_DATES:
        gross = SHARES * Decimal("0.80") * RATE_NOW
        session.add(
            DividendPayment(
                security_id=1,
                ex_date=pay,
                pay_date=pay,
                currency="USD",
                source="ibkr",
                gross_amount_eur=gross,
                withholding_tax_eur=gross * Decimal("0.15"),
                net_amount_eur=gross * Decimal("0.85"),
                per_share_native=Decimal("0.80"),
                dividend_kind="ordinary",
            )
        )
    await session.flush()
    return engine, session


def _accrual(stored_at, rate="0.80", quantity=SHARES, gross=None, native=True):
    """A level dividend of ``rate`` a share, its EUR columns at the rate ``stored_at``."""
    gross = Decimal(gross) if gross is not None else quantity * Decimal(rate)
    tax = gross * Decimal("0.15")
    row = dict(
        security_id=1,
        ex_date=ACCRUAL_EX,
        pay_date=ACCRUAL_PAY,
        currency="USD",
        quantity=quantity,
        gross_amount_eur=gross * stored_at,
        withholding_tax_eur=tax * stored_at,
        net_amount_eur=(gross - tax) * stored_at,
        last_seen_at=utcnow(),
    )
    if native:
        row.update(
            gross_rate=Decimal(rate),
            gross_amount=gross,
            withholding_tax=tax,
            net_amount=gross - tax,
        )
    return DividendAccrual(**row)


async def _breakdown(accrual):
    engine, session = await _session()
    try:
        session.add(accrual)
        await session.commit()
        return await DividendService(session).get_dividend_breakdown(
            include_forecast=True,
            as_of=AS_OF,
        )
    finally:
        await session.close()
        await engine.dispose()


def _readings(data):
    announced = next(u for u in data["upcoming"] if u["amount_source"] == "announced")
    line = next(x for x in data["forecast_by_security"] if x["security_id"] == 1)
    row = next(r for r in data["securities"] if r["security_id"] == 1)
    level_native = Decimal(str(line["level_eur"])) / Decimal(str(line["fx_to_eur"]))
    return {
        "tag": announced["per_share_change_pct"],
        "calendar_net": announced["net_eur"],
        "level_native": round(level_native, 4),
        "growth": row["per_share_growth_pct"],
        "line": line,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_at", [Decimal("0.9108"), Decimal("0.8892")])
async def test_an_unchanged_dividend_reads_level_whatever_the_rate_on_the_day_it_was_stored(
    stored_at,
):
    today = _readings(await _breakdown(_accrual(RATE_NOW)))
    other_day = _readings(await _breakdown(_accrual(stored_at)))

    for r in (today, other_day):
        assert r["tag"] == 0.0
        assert r["level_native"] == Decimal("3.2000")
        assert r["growth"] == 0.0
        # The announced net at today's rate: 10 x 0.80 x 0.85 x 0.9.
        assert r["calendar_net"] == pytest.approx(6.12)

    # Nothing but the stored rate differs between the two days, so the forecast's
    # attribution finds no raise.
    a, b = today["line"], other_day["line"]
    change = attribute_holding(
        *(
            Holding(
                Decimal(str(x["shares"])),
                Decimal(str(x["next_12m_eur"])),
                Decimal(str(x["fx_to_eur"])),
                Decimal(str(x["level_eur"])),
            )
            for x in (a, b)
        ),
        Decimal("1"),
        Decimal("1"),
    )
    assert change.parts["raises"] == 0


@pytest.mark.asyncio
async def test_the_declared_rate_beats_gross_over_quantity():
    # 0.87 a share on 3 shares is 2.61; a gross rounded up to 2.62 reads 0.8733 back.
    declared = _readings(
        await _breakdown(
            _accrual(RATE_NOW, rate="0.87", quantity=Decimal("3"), gross="2.62")
        )
    )
    assert declared["tag"] == 8.8  # 0.87 / 0.80, one decimal

    unstated = _accrual(RATE_NOW, rate="0.87", quantity=Decimal("3"), gross="2.62")
    unstated.gross_rate = None
    assert _readings(await _breakdown(unstated))["tag"] == 9.2  # 0.8733 / 0.80


@pytest.mark.asyncio
async def test_a_row_stored_before_the_native_columns_reads_as_it_always_did():
    # Until the first sync after the deploy replaces it: its EUR at today's rate reads
    # level, as every row did until then.
    legacy = _readings(await _breakdown(_accrual(RATE_NOW, native=False)))
    assert (legacy["tag"], legacy["level_native"], legacy["growth"]) == (
        0.0,
        Decimal("3.2000"),
        0.0,
    )
    assert legacy["calendar_net"] == pytest.approx(6.12)


@pytest.mark.asyncio
async def test_the_sync_stores_the_amounts_in_the_payments_own_currency():
    engine, session = await _session()
    try:
        # The sync converts at the newest rate within the carry bound of today.
        session.add(ExchangeRate(date=utc_today() - timedelta(days=1), from_currency="USD",
                                 to_currency="EUR", rate=RATE_NOW, source="test"))
        await DividendService(session).sync_dividend_accruals(
            [
                {
                    "conid": "100",
                    "symbol": "AAA",
                    "ex_date": ACCRUAL_EX,
                    "pay_date": ACCRUAL_PAY,
                    "currency": "USD",
                    "quantity": SHARES,
                    "gross_rate": Decimal("0.87"),
                    "gross_amount": Decimal("8.70"),
                    "tax": Decimal("1.31"),
                    "net_amount": Decimal("7.39"),
                }
            ],
            {"100": 1},
        )
        await session.commit()
        row = (await session.execute(select(DividendAccrual))).scalar_one()
        assert (
            row.gross_rate,
            row.gross_amount,
            row.withholding_tax,
            row.net_amount,
        ) == (Decimal("0.87"), Decimal("8.70"), Decimal("1.31"), Decimal("7.39"))
        # The EUR columns still carry the day's conversion, for a reader of the old shape.
        assert row.net_amount_eur == Decimal("7.39") * RATE_NOW
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_extractor_passes_the_declared_rate_on():
    acc = SimpleNamespace(
        conid="100",
        symbol="AAA",
        exDate=ACCRUAL_EX,
        payDate=ACCRUAL_PAY,
        currency="USD",
        quantity=Decimal("10"),
        grossRate=Decimal("0.87"),
        grossAmount=Decimal("8.70"),
        tax=Decimal("1.31"),
        netAmount=Decimal("7.39"),
    )
    flex = {"statement": SimpleNamespace(OpenDividendAccruals=[acc])}
    (out,) = await IBKRService.extract_dividend_accruals(None, flex)
    assert out["gross_rate"] == Decimal("0.87")
