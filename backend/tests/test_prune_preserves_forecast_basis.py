"""
`prune_empty_dividends` must not delete the rows the forecast infers cadence from.

The two features were built against different definitions of "useful row" and the
definitions now collide:

  * `prune_empty_dividends._is_empty()` deletes any *computed* row carrying no income,
    on the stated grounds that it "deletes only rows the readers already ignore".
  * `_forecast_inputs()` is built from the RAW history precisely because "a payout from
    before we owned the share still evidences the schedule, and its per-share amount
    still sizes the next one" — keying on realized income was the bug that left TSMC,
    Samsung, SK Hynix, HPE and SOXQ forecasting nothing.

A pre-ownership yfinance row satisfies both: it carries `amount_per_share`, and it
carries no income. So the forecast is a reader that does *not* ignore it, and pruning
silently reverts the "20 payers project" fix toward the old 15.

The ingest window (`PRE_OWNERSHIP_HISTORY_YEARS`) makes this worse rather than better:
it now keeps exactly three years of pre-ownership history *on purpose*, so almost every
row prune can still reach is one deliberately retained for cadence.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli.prune_empty_dividends import _is_empty
import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.dividend_repository import DividendRepository
from app.services.dividend_service import PRE_OWNERSHIP_HISTORY_YEARS, DividendService
from tests.memory_db import memory_engine

AS_OF = date(2026, 5, 1)


async def _keep_from(session, security_id: int):
    """The cutoff the CLI computes — same rule `sync_dividend_data` ingests by."""
    first_lot = await DividendService(session)._first_lot_dates()
    owned = first_lot.get(security_id)
    return owned - timedelta(days=365 * PRE_OWNERSHIP_HISTORY_YEARS) if owned else None


async def _make_session():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(id=1, isin="US0000000001", symbol="TSMC", description="Taiwan Semi",
                         currency="EUR", conid=100, asset_category="STK", exchange="XETRA"))
    await session.flush()
    return engine, session


async def _seed_recently_bought_payer(session):
    """
    The exact shape the forecast fix was written for: bought weeks ago, so every
    dividend on record predates ownership and pays zero income.
    """
    session.add(TaxLot(
        security_id=1, open_date=date(2026, 4, 15), quantity=Decimal("12"),
        cost_basis=Decimal("1200"), cost_basis_eur=Decimal("1200"),
        price_per_unit=Decimal("100"), currency="EUR", is_open=True,
    ))
    repo = DividendRepository(session)
    # Four quarterly ex-dates, all before the lot opened → zero shares held, zero income,
    # but a per-share amount that establishes a ~91-day cadence.
    for ex in [date(2025, 5, 8), date(2025, 8, 7), date(2025, 11, 6), date(2026, 2, 5)]:
        await repo.upsert_payment({
            "security_id": 1, "ex_date": ex, "pay_date": ex,
            "currency": "EUR", "shares_held": Decimal("0"),
            "amount_per_share": Decimal("0.55"),
            "gross_amount_eur": Decimal("0"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("0"), "source": "yfinance_estimate",
        })
    await session.commit()


@pytest.mark.asyncio
async def test_a_pre_ownership_row_carries_the_cadence_the_forecast_needs():
    """Baseline: with those rows present, the payer projects."""
    engine, session = await _make_session()
    try:
        await _seed_recently_bought_payer(session)
        bd = await DividendService(session).get_dividend_breakdown(include_forecast=True, as_of=AS_OF)
        row = next(r for r in bd["securities"] if r["symbol"] == "TSMC")
        assert row["payouts"] == 0, "no income was ever received"
        assert row["forecast_payouts"] > 0, "but the schedule is inferable and must project"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_prune_must_not_delete_a_row_that_carries_a_per_share_amount():
    """
    The defect. Every one of those four rows is 'empty' by the prune predicate, so the
    documented cleanup deletes the entire basis for the projection above.
    """
    engine, session = await _make_session()
    try:
        await _seed_recently_bought_payer(session)
        payments = await DividendRepository(session).get_computed_dividends()
        cadence_rows = [p for p in payments if p.amount_per_share is not None]
        assert len(cadence_rows) == 4, "fixture sanity"

        keep_from = await _keep_from(session, 1)
        doomed = [p for p in cadence_rows if _is_empty(p, keep_from)]
        assert not doomed, (
            f"{len(doomed)} rows carrying amount_per_share would be deleted by "
            f"prune_empty_dividends. Those are the forecast's cadence basis — the CLI's "
            f"claim that it 'deletes only rows the readers already ignore' does not hold "
            f"for _forecast_inputs(), which reads the RAW history by design."
        )
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_genuine_bookkeeping_row_is_still_prunable():
    """
    The prune must keep working for what it was written for: yfinance's decades of
    pre-portfolio history, written as zero rows purely to mark them processed. Those
    carry no per-share amount once they fall outside the ingest window, and nothing
    reads them.
    """
    engine, session = await _make_session()
    try:
        await _seed_recently_bought_payer(session)
        await DividendRepository(session).upsert_payment({
            "security_id": 1, "ex_date": date(1998, 3, 2), "pay_date": date(1998, 3, 2),
            "currency": "EUR", "shares_held": Decimal("0"),
            "amount_per_share": None,
            "gross_amount_eur": Decimal("0"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("0"), "source": "yfinance_estimate",
        })
        await session.commit()

        payments = await DividendRepository(session).get_computed_dividends()
        keep_from = await _keep_from(session, 1)
        junk = [p for p in payments if p.ex_date == date(1998, 3, 2)]
        assert len(junk) == 1
        assert _is_empty(junk[0], keep_from), (
            "a decades-old income-free row is outside the ingest window and still prunable"
        )
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_security_with_no_lots_is_left_entirely_alone():
    """
    Mirrors the ingest exactly: with no lots there is no cutoff to infer, so
    `sync_dividend_data` keeps the whole history and prune must not second-guess it.
    Deleting here would remove the cadence data the moment before it becomes useful —
    a security's statement can arrive before its first purchase.
    """
    engine, session = await _make_session()
    try:
        await DividendRepository(session).upsert_payment({
            "security_id": 1, "ex_date": date(1985, 8, 12), "pay_date": date(1985, 8, 12),
            "currency": "EUR", "shares_held": Decimal("0"),
            "amount_per_share": Decimal("0.02"),
            "gross_amount_eur": Decimal("0"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("0"), "source": "yfinance_estimate",
        })
        await session.commit()

        keep_from = await _keep_from(session, 1)
        assert keep_from is None, "no lots means no cutoff"

        payments = await DividendRepository(session).get_computed_dividends()
        assert not [p for p in payments if _is_empty(p, keep_from)]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_row_awaiting_computation_is_never_pruned():
    """Unchanged guarantee: NULL shares_held means 'not computed yet', not 'no income'."""
    engine, session = await _make_session()
    try:
        await _seed_recently_bought_payer(session)
        await DividendRepository(session).upsert_payment({
            "security_id": 1, "ex_date": date(2026, 5, 7), "pay_date": date(2026, 5, 7),
            "currency": "EUR", "amount_per_share": Decimal("0.55"),
            "source": "yfinance_estimate",
        })
        await session.commit()

        payments = await DividendRepository(session).get_computed_dividends()
        keep_from = await _keep_from(session, 1)
        pending = [p for p in payments if p.ex_date == date(2026, 5, 7)]
        for p in pending:
            assert not _is_empty(p, keep_from)
    finally:
        await session.close()
        await engine.dispose()
