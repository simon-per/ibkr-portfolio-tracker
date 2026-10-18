"""
A withholding line posted on a day with no dividend of its security (GitHub issue #60).

IBKR posts refunds and corrections of withheld tax — a 30% withholding corrected to the
15% treaty rate, a US fund's year-end adjustment — as standalone WHTAX lines dated
after the dividend. `sync_dividends_from_cash_transactions` groups lines by (security,
settle date), so such a line formed a group with no gross, and that group was skipped
without a count: the dividend kept the over-withheld amount, and the tax report's DA-1
figure with it. Result of the repro before the fix:
``{'ibkr_dividends': 1, 'dividends_skipped': 0, 'warnings': []}`` with 30% stored.

The fold is into the security's nearest preceding IBKR dividend in the same tax year,
and it must be idempotent: the Flex window is ~30 days and overlaps the last statement,
so the same line arrives several times, with or without its dividend beside it.

Offline: every amount is in EUR, which `to_eur_or_none` returns as is.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.dividend_payment import DividendPayment
from app.models.dividend_withholding_adjustment import DividendWithholdingAdjustment
from app.models.security import Security
from app.services.dividend_service import (
    DividendService,
    WHT_ADJUSTMENT_MAX_LOOKBACK_DAYS,
)

DIV_DATE = date(2026, 3, 10)
CONIDS = {"1": 1}


def _div(on, amount="100", conid="1"):
    return {"conid": conid, "pay_date": on, "type": "DIVIDEND",
            "amount": Decimal(amount), "currency": "EUR", "symbol": "AAA",
            "ib_key": f"div-{on}",
            "description": "AAA(US0000000001) Cash Dividend EUR 1.00 per Share (Ordinary Dividend)"}


def _wht(on, amount, key=None, conid="1"):
    return {"conid": conid, "pay_date": on, "type": "WHTAX",
            "amount": Decimal(amount), "currency": "EUR", "symbol": "AAA",
            "ib_key": key or f"wht-{on}-{amount}", "description": "AAA - US Tax"}


async def _security(db):
    db.add(Security(id=1, isin="US0000000001", symbol="AAA", description="A",
                    currency="EUR", conid=1, exchange="NYSE"))
    await db.flush()


async def _ibkr_rows(db):
    return list((await db.execute(
        select(DividendPayment).where(DividendPayment.source == "ibkr")
        .order_by(DividendPayment.ex_date)
    )).scalars().all())


async def _ingest(db, txns):
    return await DividendService(db).sync_dividends_from_cash_transactions(txns, CONIDS)


DIVIDEND = [_div(DIV_DATE), _wht(DIV_DATE, "-30")]
REFUND = _wht(DIV_DATE + timedelta(days=20), "15", key="refund-1")


@pytest.mark.asyncio
async def test_a_later_withholding_refund_reaches_the_dividend_it_corrects(db):
    """The issue's repro: 30% withheld on D, +15% refunded on D+20 → 15% stored."""
    await _security(db)
    result = await _ingest(db, DIVIDEND + [REFUND])

    [row] = await _ibkr_rows(db)
    assert row.withholding_tax_eur == Decimal("15")
    assert row.net_amount_eur == Decimal("85")
    assert row.gross_amount_eur == Decimal("100")
    assert result["withholding_adjustments_applied"] == 1
    assert result["withholding_adjustments_unmatched"] == 0
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_overlapping_statements_apply_the_refund_once(db):
    """
    Every order the ~30-day window can deliver the two in: the dividend alone, then
    both, then the refund alone (the dividend has left the window), then both again.
    The figure is the corrected one after each and never double-corrected.
    """
    await _security(db)
    await _ingest(db, DIVIDEND)
    assert (await _ibkr_rows(db))[0].withholding_tax_eur == Decimal("30")

    for statement in (DIVIDEND + [REFUND], [REFUND], [REFUND], DIVIDEND + [REFUND], DIVIDEND):
        result = await _ingest(db, statement)
        [row] = await _ibkr_rows(db)
        assert row.withholding_tax_eur == Decimal("15"), statement
        assert row.net_amount_eur == Decimal("85"), statement
        assert result["warnings"] == []

    adjustments = (await db.execute(select(DividendWithholdingAdjustment))).scalars().all()
    assert len(adjustments) == 1
    assert adjustments[0].target_pay_date == DIV_DATE


@pytest.mark.asyncio
async def test_a_refund_arriving_before_its_dividend_is_folded_in_once_it_arrives(db):
    """An older statement ingested late (the CLI path): the refund waits, then attaches."""
    await _security(db)
    first = await _ingest(db, [REFUND])
    assert first["withholding_adjustments_unmatched"] == 1
    assert first["warnings"]

    await _ingest(db, DIVIDEND)
    [row] = await _ibkr_rows(db)
    assert row.withholding_tax_eur == Decimal("15")
    assert row.net_amount_eur == Decimal("85")


@pytest.mark.asyncio
async def test_an_extra_withholding_charge_is_added(db):
    """The other direction: 15% withheld, a further 15% charged later."""
    await _security(db)
    await _ingest(db, [_div(DIV_DATE), _wht(DIV_DATE, "-15"),
                       _wht(DIV_DATE + timedelta(days=5), "-15", key="charge")])
    [row] = await _ibkr_rows(db)
    assert row.withholding_tax_eur == Decimal("30")
    assert row.net_amount_eur == Decimal("70")


@pytest.mark.asyncio
async def test_the_nearest_preceding_dividend_takes_it(db):
    """A quarterly payer: the refund corrects the latest dividend before it."""
    await _security(db)
    later = DIV_DATE + timedelta(days=91)
    await _ingest(db, DIVIDEND + [_div(later), _wht(later, "-30"),
                                  _wht(later + timedelta(days=10), "15", key="r")])
    first, second = await _ibkr_rows(db)
    assert first.withholding_tax_eur == Decimal("30")
    assert second.withholding_tax_eur == Decimal("15")


@pytest.mark.asyncio
async def test_an_adjustment_with_no_dividend_in_reach_is_counted_and_named(db):
    """
    Across the tax year (a January refund of December's tax) and beyond the lookback
    the line is not folded into anything — and says so, with a count and a warning
    naming it, rather than vanishing as it used to.
    """
    await _security(db)
    december = date(2025, 12, 15)
    january_refund = _wht(date(2026, 1, 20), "15", key="jan")
    too_late = _wht(DIV_DATE + timedelta(days=WHT_ADJUSTMENT_MAX_LOOKBACK_DAYS + 1), "15",
                    key="late")
    result = await _ingest(db, [_div(december), _wht(december, "-30"),
                                *DIVIDEND, january_refund, too_late])

    assert result["withholding_adjustments_unmatched"] == 2
    assert result["withholding_adjustments_applied"] == 0
    [warning] = result["warnings"]
    assert "AAA 2026-01-20" in warning and "2 IBKR withholding adjustment" in warning
    for row in await _ibkr_rows(db):
        assert row.withholding_tax_eur == Decimal("30")


@pytest.mark.asyncio
async def test_a_refund_larger_than_the_tax_withheld_is_refused(db):
    """Withholding below zero would publish income above gross: not this dividend's."""
    await _security(db)
    result = await _ingest(db, DIVIDEND + [_wht(DIV_DATE + timedelta(days=3), "40", key="x")])
    [row] = await _ibkr_rows(db)
    assert row.withholding_tax_eur == Decimal("30")
    assert result["withholding_adjustments_unmatched"] == 1
    assert result["warnings"]


@pytest.mark.asyncio
async def test_a_reversal_without_its_repayment_is_named(db):
    """A negative dividend alone on its day: counted and warned, never applied."""
    await _security(db)
    result = await _ingest(db, DIVIDEND + [_div(DIV_DATE + timedelta(days=4), "-100")])
    [row] = await _ibkr_rows(db)
    assert row.gross_amount_eur == Decimal("100")
    assert result["dividend_reversals_skipped"] == 1
    assert any("reversal" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_a_refused_line_warns_on_the_ingest_that_first_records_it_only(db):
    """
    The ~30-day window re-delivers a refused line on every statement for a month, and a
    warning that is always present teaches the reader to skip it. So each ingest still
    counts it, but only the first names it in `warnings[]` — for a cross-year refund, an
    oversized refund and a lone reversal alike.
    """
    await _security(db)
    statement = DIVIDEND + [
        _wht(date(2027, 1, 20), "15", key="cross-year"),
        _wht(DIV_DATE + timedelta(days=3), "40", key="oversized"),
        _div(DIV_DATE + timedelta(days=4), "-100"),
    ]

    first = await _ingest(db, statement)
    assert first["withholding_adjustments_unmatched"] == 2
    assert first["dividend_reversals_skipped"] == 1
    assert len(first["warnings"]) == 2
    assert "AAA 2027-01-20" in first["warnings"][0] and "reversal" in first["warnings"][1]

    second = await _ingest(db, statement)
    assert second["withholding_adjustments_unmatched"] == 2
    assert second["dividend_reversals_skipped"] == 1
    assert second["warnings"] == []

    # A new refused line in a later statement is warned on its own, the old ones not.
    third = await _ingest(db, statement + [_wht(DIV_DATE + timedelta(days=5), "50", key="new")])
    assert third["withholding_adjustments_unmatched"] == 3
    [warning] = third["warnings"]
    assert warning.startswith("1 IBKR withholding adjustment") and "2026-03-15" in warning
    [row] = await _ibkr_rows(db)
    assert row.withholding_tax_eur == Decimal("30")


@pytest.mark.asyncio
async def test_an_unconvertible_adjustment_is_not_recorded_and_retried(db):
    """No FX rate: skipped and counted, and NOT recorded, so a later statement retries."""
    await _security(db)

    class NoRates:
        async def convert_to_eur(self, amount, from_currency, target_date):
            raise ValueError("no rate")

    svc = DividendService(db)
    svc.currency_service = NoRates()
    usd_refund = dict(REFUND, currency="USD")
    result = await svc.sync_dividends_from_cash_transactions(DIVIDEND + [usd_refund], CONIDS)
    assert result["withholding_adjustments_skipped"] == 1
    assert result["warnings"]
    assert (await db.execute(select(DividendWithholdingAdjustment))).scalars().all() == []
    [row] = await _ibkr_rows(db)
    assert row.withholding_tax_eur == Decimal("30")
