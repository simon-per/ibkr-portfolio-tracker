"""
An announced dividend Yahoo has no row for yet reads the same as once Yahoo's row lands
(review item D1, 2026-11-08).

Korean payers declare a month after the ex-date, so IBKR's accrual arrives before Yahoo's
row. It joined the history as amount-only evidence, the varying payer's slot was sized
from a year earlier, and the day Yahoo's row landed the level jumped and the forecast
attributed the jump to raises. An accrual that fills the schedule's next open slot now
joins as a scheduled payment; one between two slots still sizes without bending the
schedule. Pure DB: no Yahoo, no Flex.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.clock import utcnow
from app.models.dividend_accrual import DividendAccrual
from app.models.dividend_payment import DividendPayment
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.dividend_forecast import HistPayment, takes_open_slot
from app.services.dividend_forecast_attribution import Holding, attribute_holding
from app.services.dividend_service import DividendService
from tests.memory_db import memory_engine

AS_OF = date(2026, 11, 7)
KRW_EUR = Decimal("0.00066")
SHARES = Decimal("7")
# Yahoo's ex-dated series of a quarterly payer with a larger year-end: varying.
YAHOO = [
    (date(2024, 9, 27), "361"),
    (date(2024, 12, 27), "363"),
    (date(2025, 3, 28), "365"),
    (date(2025, 6, 27), "365"),
    (date(2025, 9, 29), "365"),
    (date(2025, 12, 29), "566"),
    (date(2026, 3, 30), "370"),
    (date(2026, 6, 29), "372"),
]


async def _breakdown(accrual_ex, per_share, with_yahoo_row):
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        session.add(
            Security(
                id=1,
                isin="KR7005930003",
                symbol="005930.KS",
                description="SAMSUNG",
                currency="KRW",
                conid=100,
                asset_category="STK",
                exchange="KRX",
            )
        )
        session.add(
            TaxLot(
                security_id=1,
                open_date=date(2026, 7, 8),
                quantity=SHARES,
                cost_basis=Decimal("1000000"),
                cost_basis_eur=Decimal("660"),
                price_per_unit=Decimal("142857"),
                currency="KRW",
                is_open=True,
            )
        )
        session.add(
            ExchangeRate(
                date=AS_OF - timedelta(days=1),
                from_currency="KRW",
                to_currency="EUR",
                rate=KRW_EUR,
                source="test",
            )
        )
        rows = list(YAHOO) + ([(accrual_ex, per_share)] if with_yahoo_row else [])
        for ex, ps in rows:
            session.add(
                DividendPayment(
                    security_id=1,
                    ex_date=ex,
                    pay_date=None,
                    currency="KRW",
                    source="yfinance_estimate",
                    amount_per_share=Decimal(ps),
                    shares_held=Decimal("0"),
                    gross_amount_eur=Decimal("0"),
                    net_amount_eur=Decimal("0"),
                    withholding_tax_eur=Decimal("0"),
                )
            )
        gross = SHARES * Decimal(per_share)
        session.add(
            DividendAccrual(
                security_id=1,
                ex_date=accrual_ex,
                pay_date=accrual_ex + timedelta(days=59),
                currency="KRW",
                quantity=SHARES,
                gross_rate=Decimal(per_share),
                gross_amount=gross,
                withholding_tax=gross * Decimal("0.165"),
                net_amount=gross * Decimal("0.835"),
                gross_amount_eur=gross * KRW_EUR,
                withholding_tax_eur=gross * Decimal("0.165") * KRW_EUR,
                net_amount_eur=gross * Decimal("0.835") * KRW_EUR,
                last_seen_at=utcnow(),
            )
        )
        await session.commit()
        return await DividendService(session).get_dividend_breakdown(
            include_forecast=True,
            as_of=AS_OF,
        )
    finally:
        await session.close()
        await engine.dispose()


def _line(data):
    x = next(x for x in data["forecast_by_security"] if x["security_id"] == 1)
    return Holding(
        Decimal(str(x["shares"])),
        Decimal(str(x["next_12m_eur"])),
        Decimal(str(x["fx_to_eur"])),
        Decimal(str(x["level_eur"])),
    )


def _readings(data):
    row = next(r for r in data["securities"] if r["security_id"] == 1)
    return {
        "line": _line(data),
        "growth": row["per_share_growth_pct"],
        "upcoming": [
            (u["date"], u["net_eur"], u["amount_source"]) for u in data["upcoming"]
        ],
    }


@pytest.mark.asyncio
# Level, a raise, a doubling and the 12x year-end Samsung declared: the last two read a
# different level and growth until Yahoo's row landed.
@pytest.mark.parametrize("per_share", ["372", "400", "740", "4601"])
async def test_an_accrual_on_the_next_slot_reads_as_once_yahoos_row_lands(per_share):
    ex = date(2026, 9, 29)
    accrual_only = _readings(await _breakdown(ex, per_share, with_yahoo_row=False))
    both = _readings(await _breakdown(ex, per_share, with_yahoo_row=True))

    assert accrual_only == both
    change = attribute_holding(
        accrual_only["line"], both["line"], Decimal("1"), Decimal("1")
    )
    assert all(v == 0 for v in change.parts.values())


@pytest.mark.asyncio
async def test_an_accrual_between_two_slots_does_not_bend_the_schedule():
    # A month after the June slot: an extra payment, not the September one.
    ex = date(2026, 7, 30)
    data = await _breakdown(ex, "372", with_yahoo_row=False)
    projected = sorted(
        u["ex_date"] for u in data["upcoming"] if u["amount_source"] != "announced"
    )
    assert projected[0] == "2026-09-29"
    assert all(d.endswith("-29") or d.endswith("-30") for d in projected)


def test_takes_open_slot():
    quarterly = [HistPayment(on_date=d, per_share_eur=Decimal("1")) for d, _ in YAHOO]
    assert takes_open_slot(quarterly, date(2026, 9, 29))
    assert not takes_open_slot(quarterly, date(2026, 7, 30))  # 31 days after June's
    # Amount-only evidence fills no slot.
    extra = quarterly + [HistPayment(on_date=date(2026, 9, 28), cadence=False)]
    assert takes_open_slot(extra, date(2026, 9, 29))
    # No cadence, no judgement.
    assert not takes_open_slot(quarterly[:1], date(2026, 9, 29))
