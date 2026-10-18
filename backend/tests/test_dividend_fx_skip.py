"""
An unconvertible dividend must not be stored as if it were EUR.

`DividendService._to_eur` ended with `return amount  # fallback: store
unconverted`, so a payment whose currency had no rate for its pay date was written
into `gross_amount_eur` / `withholding_tax_eur` / `net_amount_eur` as the **raw
foreign figure**. A TWD dividend would sit there roughly 35x high.

This is the identical defect fixed in `TaxService._to_eur` on 2026-07-30 — whose
docstring lists the consumers already skipping correctly (sync_helper,
portfolio_service, benchmark_service) and misses this sibling. It is also the worse
instance of the two: the tax report computed its number at read time, while this is
the **ingest** path, so the wrong figure is persisted and then read by the Dividends
tab, the forecast, and the tax report's own DA-1 income.

Skipping matches every other ingest here — `reconcile_taxlots` skips the lot into
`taxlots_skipped`, cash-flow ingest skips one row rather than failing the sync — and
is recoverable, since re-ingesting a statement is idempotent.

Offline: the currency service is faked, so no FX provider is reached.
"""

from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401  register all mappers
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.services.dividend_service import DividendService
from tests.memory_db import memory_engine

PAY_DATE = date(2026, 7, 24)


@pytest_asyncio.fixture
async def db():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(
        id=1, isin="TW0002330008", symbol="2330", description="TSMC",
        currency="TWD", conid=4567, asset_category="STK", exchange="TWSE",
    ))
    await session.commit()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


class FakeCurrencyService:
    """Converts EUR-adjacent currencies; refuses the rest, as the ECB set does."""

    def __init__(self, rates):
        self.rates = rates

    async def convert_to_eur(self, amount, from_currency, target_date):
        if from_currency not in self.rates:
            raise ValueError(f"No {from_currency}->EUR rate for {target_date}")
        return amount * self.rates[from_currency]


def cash_txn(txn_type, amount, currency="TWD", conid="4567"):
    return {
        "type": txn_type,
        "conid": conid,
        "pay_date": PAY_DATE,
        "amount": Decimal(str(amount)),
        "currency": currency,
    }


async def run_ingest(db, rates):
    service = DividendService(db)
    service.currency_service = FakeCurrencyService(rates)
    return await service.sync_dividends_from_cash_transactions(
        [cash_txn("DIVIDEND", 3500), cash_txn("WHTAX", -700)],
        {"4567": 1},
    )


async def stored(db):
    return list((await db.execute(select(DividendPayment))).scalars().all())


@pytest.mark.asyncio
async def test_an_unconvertible_dividend_is_not_stored(db):
    result = await run_ingest(db, rates={})  # no TWD rate

    assert result["ibkr_dividends"] == 0
    assert result["dividends_skipped"] == 1
    assert await stored(db) == []


@pytest.mark.asyncio
async def test_it_does_not_store_the_foreign_figure_as_eur(db):
    """The regression itself: 3500 TWD is not 3500 EUR — it is about 100."""
    await run_ingest(db, rates={})

    rows = await stored(db)
    assert not any(r.gross_amount_eur == Decimal("3500") for r in rows)


@pytest.mark.asyncio
async def test_the_skip_is_reported_rather_than_silent(db):
    result = await run_ingest(db, rates={})

    assert result["warnings"], "a skipped dividend rides on a successful sync"
    message = result["warnings"][0]
    assert "TWD" in message
    assert "understated" in message


@pytest.mark.asyncio
async def test_a_convertible_dividend_is_still_stored_and_converted(db):
    result = await run_ingest(db, rates={"TWD": Decimal("0.028")})

    assert result["ibkr_dividends"] == 1
    assert result["dividends_skipped"] == 0
    assert result["warnings"] == []

    row = (await stored(db))[0]
    assert row.gross_amount_eur == Decimal("3500") * Decimal("0.028")
    assert row.withholding_tax_eur == Decimal("700") * Decimal("0.028")
    assert row.net_amount_eur == row.gross_amount_eur - row.withholding_tax_eur
    assert row.source == "ibkr"


@pytest.mark.asyncio
async def test_a_eur_dividend_needs_no_rate_at_all(db):
    service = DividendService(db)
    service.currency_service = FakeCurrencyService({})
    result = await service.sync_dividends_from_cash_transactions(
        [cash_txn("DIVIDEND", 42, currency="EUR"), cash_txn("WHTAX", -2, currency="EUR")],
        {"4567": 1},
    )

    assert result["ibkr_dividends"] == 1
    assert (await stored(db))[0].gross_amount_eur == Decimal("42")


@pytest.mark.asyncio
async def test_several_skips_are_counted_per_currency(db):
    service = DividendService(db)
    service.currency_service = FakeCurrencyService({})
    result = await service.sync_dividends_from_cash_transactions(
        [
            cash_txn("DIVIDEND", 100, currency="TWD"),
            cash_txn("WHTAX", -10, currency="TWD"),
        ],
        {"4567": 1},
    )
    assert result["dividends_skipped"] == 1
    assert "1 in TWD" in result["warnings"][0]
