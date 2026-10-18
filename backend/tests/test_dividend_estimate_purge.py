"""
Purging a security's yfinance dividend estimates.

Modelled on the real SBI shape (SERABI GOLD PLC, CAD/Toronto): two estimate rows
written under a wrong bare-ticker mapping, plus one authoritative IBKR payment.
The mapping was corrected and the prices purged, but nothing invalidated the
dividend rows — and because _forecast_inputs() lets whichever series carries
amount_per_share define the cadence (skipping the IBKR rows when one exists),
those two rows alone projected a monthly schedule the company does not have.

The two properties that make the fix safe, both asserted here:
  * income is byte-identical before and after (the era splice already dropped
    the estimates, which is why the bug was invisible), and
  * the forecast collapses to nothing rather than to a different wrong number.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.app_settings import AppSetting
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.cli import purge_dividend_estimates as cli
from app.services.dividend_service import DividendService
from tests.memory_db import memory_engine


SBI_ID = 33


async def _make_db(monkeypatch, *, dual_listed=False):
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)

    session.add(Security(id=SBI_ID, isin="GB00BG5NDX91", symbol="SBI",
                         description="SERABI GOLD PLC", currency="CAD", conid=322447809,
                         asset_category="STK", exchange="TSE"))
    # A second payer whose IBKR ledger starts in February, because the era
    # boundary is GLOBAL. Without it `ibkr_from` would be SBI's own July payment
    # and its June estimate would still count as income — the opposite of
    # production, where the ledger begins 2026-02-18 and both estimates fall
    # after it. The fixture has to reproduce that or it proves nothing.
    session.add(Security(id=2, isin="US0000000002", symbol="AAA", description="A Co",
                         currency="EUR", conid=200, asset_category="STK", exchange="XETRA"))
    session.add(TaxLot(security_id=2, open_date=date(2026, 1, 5), quantity=Decimal("10"),
                       cost_basis=Decimal("100"), price_per_unit=Decimal("10"),
                       cost_basis_eur=Decimal("100"), currency="EUR", is_open=True))
    session.add(DividendPayment(
        security_id=2, ex_date=date(2026, 2, 18), pay_date=date(2026, 2, 18),
        source="ibkr", currency="EUR", shares_held=Decimal("0"),
        gross_amount_eur=Decimal("5"), withholding_tax_eur=Decimal("0"),
        net_amount_eur=Decimal("5"),
    ))
    if dual_listed:
        session.add(Security(id=99, isin="GB00BG5NDX91", symbol="SBI",
                             description="SERABI GOLD PLC", currency="GBP", conid=1,
                             asset_category="STK", exchange="LSE"))

    # The real lots: 50 shares from 2026-06-12, 50 more from 2026-07-07.
    for open_date, qty, cost, unit in (
        (date(2026, 6, 12), Decimal("50"), Decimal("188.50"), Decimal("3.77")),
        (date(2026, 7, 7), Decimal("50"), Decimal("175.02"), Decimal("3.50")),
    ):
        session.add(TaxLot(security_id=SBI_ID, open_date=open_date, quantity=qty,
                           cost_basis=cost, price_per_unit=unit, cost_basis_eur=cost,
                           currency="CAD", is_open=True))

    # Two poisoned estimates, 31 days apart -> inferred monthly.
    session.add(DividendPayment(
        security_id=SBI_ID, ex_date=date(2026, 6, 23), source="yfinance_estimate",
        amount_per_share=Decimal("0.042"), currency="CAD", shares_held=Decimal("50"),
        gross_amount_eur=Decimal("1.299354"),
    ))
    session.add(DividendPayment(
        security_id=SBI_ID, ex_date=date(2026, 7, 24), source="yfinance_estimate",
        amount_per_share=Decimal("0.042"), currency="CAD", shares_held=Decimal("100"),
        gross_amount_eur=Decimal("2.620758"), withholding_tax_eur=Decimal("0"),
        net_amount_eur=Decimal("2.620758"),
    ))
    # The one real payment. IBKR rows store the pay date in ex_date and carry a
    # 0 shares_held sentinel with no per-share figure.
    session.add(DividendPayment(
        security_id=SBI_ID, ex_date=date(2026, 7, 10), pay_date=date(2026, 7, 10),
        source="ibkr", amount_per_share=None, currency="CAD", shares_held=Decimal("0"),
        gross_amount_eur=Decimal("2.905352"), withholding_tax_eur=Decimal("0"),
        net_amount_eur=Decimal("2.905352"),
    ))
    session.add(AppSetting(key="base_currency", value="EUR"))
    await session.flush()
    await session.commit()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: _Ctx())
    return engine, session


async def _rows(session, security_id=SBI_ID):
    result = (await session.execute(
        select(DividendPayment).where(DividendPayment.security_id == security_id)
    )).scalars().all()
    return sorted((p.ex_date, p.source) for p in result)


async def _sbi_breakdown(session, year=2026):
    bd = await DividendService(session).get_dividend_breakdown(
        year=year, include_forecast=True, as_of=date(2026, 7, 30)
    )
    return next(r for r in bd["securities"] if r["symbol"] == "SBI")


@pytest.mark.asyncio
async def test_dry_run_deletes_nothing(monkeypatch, capsys):
    engine, session = await _make_db(monkeypatch)
    try:
        assert await cli.purge("SBI", "TSE", dry_run=True) == 0
        assert len(await _rows(session)) == 3
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "2 yfinance_estimate row(s) to delete" in out
        assert "1 IBKR row(s) kept" in out
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_ibkr_row_survives_and_only_estimates_go(monkeypatch):
    engine, session = await _make_db(monkeypatch)
    try:
        assert await cli.purge("SBI", "TSE", dry_run=False) == 0
        assert await _rows(session) == [(date(2026, 7, 10), "ibkr")]
        # Scoped: the other security's ledger is untouched.
        assert await _rows(session, security_id=2) == [(date(2026, 2, 18), "ibkr")]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_income_is_unchanged_but_the_phantom_forecast_goes(monkeypatch):
    """
    The whole reason this purge is safe. The era splice already excluded the
    estimates from income, so realized figures must not move by a cent — while
    the forecast, which read those same rows for its cadence, must collapse.
    """
    engine, session = await _make_db(monkeypatch)
    try:
        before = await _sbi_breakdown(session)
        # The poisoned pair projects a monthly schedule off a 31-day gap.
        assert before["forecast_payouts"] >= 4
        assert before["forecast_net_eur"] > 0

        assert await cli.purge("SBI", "TSE", dry_run=False) == 0
        after = await _sbi_breakdown(session)

        # Income: identical, to the cent.
        assert after["payouts"] == before["payouts"] == 1
        assert after["net_eur"] == before["net_eur"]
        assert after["gross_eur"] == before["gross_eur"]

        # Forecast: gone, not merely different. One IBKR payment cannot establish
        # a cadence, and inventing one is what caused this.
        assert after["forecast_payouts"] == 0
        assert after["forecast_net_eur"] == 0
        assert after["next_pay_date"] is None
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_dual_listed_symbol_is_refused_not_guessed(monkeypatch, capsys):
    engine, session = await _make_db(monkeypatch, dual_listed=True)
    try:
        assert await cli.purge("SBI", None, dry_run=False) == 2
        assert len(await _rows(session)) == 3          # nothing deleted
        assert "matches 2 securities" in capsys.readouterr().err
        # ...and the id resolves it.
        assert await cli.purge(security_id=SBI_ID, dry_run=False) == 0
        assert await _rows(session) == [(date(2026, 7, 10), "ibkr")]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_security_with_no_estimates_is_a_clean_noop(monkeypatch, capsys):
    engine, session = await _make_db(monkeypatch)
    try:
        assert await cli.purge("SBI", "TSE", dry_run=False) == 0
        assert await cli.purge("SBI", "TSE", dry_run=False) == 0   # idempotent
        assert await _rows(session) == [(date(2026, 7, 10), "ibkr")]
        assert "Nothing to purge" in capsys.readouterr().out
    finally:
        await session.close()
        await engine.dispose()
