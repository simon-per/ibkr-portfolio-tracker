"""
A Yahoo dividend and IBKR's payment of it pair by IBKR's own (ex-date, pay date) first,
proximity second (review item D4, 2026-11-08).

A Korean year-end pays 105-110 days after its ex-date, past `SLOW_PAYER_MAX_LAG_DAYS`, so
the proximity match left it unpaired — two entries for one dividend in the forecast's
history — or, worse, paired the payment with the next quarter's estimate. Every caller of
`match_estimates_to_ibkr` passes `dividend_date_pairs`, and an AST walk keeps it so. Pure
DB: no Yahoo, no Flex.
"""

import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.clock import utcnow
from app.models.dividend_date_pair import DividendDatePair
from app.models.dividend_payment import DividendPayment
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.dividend_service import DividendService, match_estimates_to_ibkr
from tests.memory_db import memory_engine

APP = Path(__file__).resolve().parent.parent / "app"


def _est(ex):
    return SimpleNamespace(security_id=1, ex_date=ex, pay_date=None, currency="KRW")


def _paid(pay):
    return SimpleNamespace(security_id=1, ex_date=pay, pay_date=pay, currency="KRW")


def test_a_year_end_paid_past_the_window_pairs_by_ibkrs_own_dates():
    year_end, march = _est(date(2025, 12, 29)), _est(date(2026, 3, 30))
    year_end_paid, march_paid = _paid(date(2026, 4, 15)), _paid(date(2026, 5, 20))

    # Proximity alone gives the year-end's cash to March's estimate, and March's cash
    # finds nothing left: the year-end is recorded twice, March once with the wrong pay.
    guessed = match_estimates_to_ibkr(
        [year_end, march], [year_end_paid, march_paid], exact_pairs=None
    )
    assert [(e.ex_date, r.pay_date) for e, r in guessed] == [
        (date(2026, 3, 30), date(2026, 4, 15))
    ]

    booked = {1: [(date(2025, 12, 29), date(2026, 4, 15))]}
    pairs = match_estimates_to_ibkr(
        [year_end, march], [year_end_paid, march_paid], exact_pairs=booked
    )
    assert [(e.ex_date, r.pay_date) for e, r in pairs] == [
        (date(2025, 12, 29), date(2026, 4, 15)),
        (date(2026, 3, 30), date(2026, 5, 20)),
    ]


def test_a_day_of_disagreement_between_the_sources_still_pairs():
    pairs = match_estimates_to_ibkr(
        [_est(date(2025, 12, 30))],
        [_paid(date(2026, 4, 15))],
        exact_pairs={1: [(date(2025, 12, 29), date(2026, 4, 15))]},
    )
    assert len(pairs) == 1


AS_OF = date(2026, 11, 7)
KRW_EUR = Decimal("0.00066")
SHARES = Decimal("7")
# (Yahoo ex-date, KRW per share, days to IBKR's payment): the year-end pays late.
SERIES = [
    (date(2025, 3, 28), "365", 50),
    (date(2025, 6, 27), "365", 50),
    (date(2025, 9, 29), "365", 50),
    (date(2025, 12, 29), "566", 107),
    (date(2026, 3, 30), "370", 50),
    (date(2026, 6, 29), "372", 50),
]


async def _growth(with_ibkr):
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
                open_date=date(2025, 1, 2),
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
        for ex, ps, lag in SERIES:
            gross = SHARES * Decimal(ps) * KRW_EUR
            session.add(
                DividendPayment(
                    security_id=1,
                    ex_date=ex,
                    pay_date=None,
                    currency="KRW",
                    source="yfinance_estimate",
                    amount_per_share=Decimal(ps),
                    shares_held=SHARES,
                    gross_amount_eur=gross,
                    net_amount_eur=gross * Decimal("0.835"),
                    withholding_tax_eur=gross * Decimal("0.165"),
                )
            )
            if with_ibkr:
                pay = ex + timedelta(days=lag)
                session.add(
                    DividendPayment(
                        security_id=1,
                        ex_date=pay,
                        pay_date=pay,
                        currency="KRW",
                        source="ibkr",
                        gross_amount_eur=gross,
                        withholding_tax_eur=gross * Decimal("0.165"),
                        net_amount_eur=gross * Decimal("0.835"),
                        per_share_native=Decimal(ps),
                        dividend_kind="ordinary",
                    )
                )
                session.add(
                    DividendDatePair(
                        security_id=1, ex_date=ex, pay_date=pay, first_seen_at=utcnow()
                    )
                )
        await session.commit()
        data = await DividendService(session).get_dividend_breakdown(
            include_forecast=True,
            as_of=AS_OF,
        )
        row = next(r for r in data["securities"] if r["security_id"] == 1)
        line = next(x for x in data["forecast_by_security"] if x["security_id"] == 1)
        return row["per_share_growth_pct"], line["level_eur"], row["forecast_samples"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ibkrs_payment_of_a_late_year_end_is_not_a_second_dividend():
    # IBKR paid exactly what Yahoo records, so the history reads as Yahoo's alone.
    assert await _growth(with_ibkr=True) == await _growth(with_ibkr=False)


def test_every_caller_passes_ibkrs_own_pairs():
    """The keyword is required, so a call without it fails at once; this names a caller
    that passes ``exact_pairs=None`` in app code, which would opt out silently."""
    offenders = []
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in ("match_estimates_to_ibkr", "_splice_by_era"):
                continue
            kw = next((k for k in node.keywords if k.arg == "exact_pairs"), None)
            if kw is None or (
                isinstance(kw.value, ast.Constant) and kw.value.value is None
            ):
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert offenders == []
