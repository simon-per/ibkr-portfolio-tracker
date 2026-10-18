"""
An unconvertible dividend is left uncomputed, never stored as if it were EUR.

`compute_dividend_income` is the **second** FX conversion path in
`dividend_service.py`, and it kept

    gross_eur = gross_amount  # fallback: store unconverted

long after the identical line was fixed in `TaxService._to_eur` (2026-07-30) and then in
this same file's `_to_eur`. Neither fix reached it, because it never called either
helper — fixing a helper is not fixing the file.

It is the worst of the three locations. The other two are read paths, where a wrong
figure lasts until the next request; this one is **ingest**, so the foreign amount is
persisted into `gross_amount_eur` and `net_amount_eur` and then read by the Dividends
tab, the forecast, the forward yield and the tax report's DA-1 income. A TWD payment
would sit there roughly 35x high with nothing marking it, and `realized_source` would
still say the data is authoritative.

Reachable rather than theoretical: `get_exchange_rate` raises once both providers are
exhausted, and TWD is outside the ECB set entirely while the er-api fallback refuses any
date older than `FALLBACK_MAX_AGE_DAYS`. The account holds TSMC.

Leaving the row uncomputed is what makes it self-healing — `shares_held IS NULL` is the
"awaiting computation" sentinel `prune_empty_dividends` already refuses to delete, so
the row is retried once a rate exists.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

import app.models  # noqa: F401  register all mappers
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.dividend_payment import DividendPayment
from app.services.dividend_service import DividendService

EX_DATE = date(2026, 3, 15)


async def _seed(db, currency="TWD"):
    """One TWD payer holding 100 shares, with an uncomputed dividend row."""
    security = Security(
        isin="TW0002330008", symbol="2330", description="TSMC", currency=currency,
        conid=4321, asset_category="STK", exchange="TWSE",
    )
    db.add(security)
    await db.flush()

    db.add(TaxLot(
        security_id=security.id, open_date=EX_DATE - timedelta(days=90),
        quantity=Decimal("100"), cost_basis=Decimal("1000"),
        price_per_unit=Decimal("10"), currency=currency,
        cost_basis_eur=Decimal("1000"), is_open=True,
    ))
    db.add(DividendPayment(
        security_id=security.id, ex_date=EX_DATE,
        amount_per_share=Decimal("4.0"), currency=currency,
        shares_held=None,  # the "awaiting computation" sentinel
    ))
    await db.commit()
    return security


class NoRate(Exception):
    pass


@pytest.mark.asyncio
async def test_an_unconvertible_dividend_is_not_stored_as_eur(db, monkeypatch):
    security = await _seed(db)
    service = DividendService(db)

    async def _no_rate(currency, on_date):
        raise NoRate(f"No {currency} rate for {on_date}")

    monkeypatch.setattr(service.currency_service, "get_exchange_rate", _no_rate)

    result = await service.compute_dividend_income()

    row = (await db.execute(
        select(DividendPayment).where(DividendPayment.security_id == security.id)
    )).scalars().one()

    # 4.0 TWD x 100 shares = 400 TWD. Storing that as EUR is the bug.
    assert row.gross_amount_eur != Decimal("400"), (
        "the foreign amount was persisted into a column named _eur"
    )
    assert row.gross_amount_eur is None
    assert row.net_amount_eur is None
    assert result["computed"] == 0
    assert result["fx_skipped"] == 1
    assert result["warnings"], "a skipped row must be reported, not silent"


@pytest.mark.asyncio
async def test_the_row_stays_retryable_rather_than_being_marked_done(db, monkeypatch):
    """
    `shares_held IS NULL` is what makes this self-healing: it is the sentinel the
    prune CLI refuses to delete and the next computation pass picks up. Stamping the
    row as processed would make the miss permanent.
    """
    security = await _seed(db)
    service = DividendService(db)

    async def _no_rate(currency, on_date):
        raise NoRate("nope")

    monkeypatch.setattr(service.currency_service, "get_exchange_rate", _no_rate)
    await service.compute_dividend_income()

    row = (await db.execute(
        select(DividendPayment).where(DividendPayment.security_id == security.id)
    )).scalars().one()
    assert row.shares_held is None
    assert row.last_computed is None


@pytest.mark.asyncio
async def test_it_computes_normally_once_a_rate_exists(db, monkeypatch):
    """The other direction: the refusal must not suppress a convertible row."""
    security = await _seed(db)
    service = DividendService(db)

    async def _rate(currency, on_date):
        return Decimal("0.03")  # 1 TWD = 0.03 EUR

    monkeypatch.setattr(service.currency_service, "get_exchange_rate", _rate)

    result = await service.compute_dividend_income()

    row = (await db.execute(
        select(DividendPayment).where(DividendPayment.security_id == security.id)
    )).scalars().one()
    assert row.gross_amount_eur == Decimal("12.00")  # 400 TWD x 0.03
    assert result["computed"] == 1
    assert result["fx_skipped"] == 0
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_a_eur_payer_needs_no_rate_at_all(db, monkeypatch):
    """EUR skips conversion entirely, so a dead FX provider cannot affect it."""
    security = await _seed(db, currency="EUR")
    service = DividendService(db)

    async def _no_rate(currency, on_date):
        raise AssertionError("EUR must not ask for a rate")

    monkeypatch.setattr(service.currency_service, "get_exchange_rate", _no_rate)

    result = await service.compute_dividend_income()

    row = (await db.execute(
        select(DividendPayment).where(DividendPayment.security_id == security.id)
    )).scalars().one()
    assert row.gross_amount_eur == Decimal("400")
    assert result["computed"] == 1
