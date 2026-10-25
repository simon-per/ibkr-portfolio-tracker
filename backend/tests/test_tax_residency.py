"""
Swiss residency (docs/tax.md, *Swiss residency*): the report counts only the days the owner
was a Swiss taxpayer.

The fixture owner becomes a Swiss taxpayer mid-year (MOVED). The Swiss report used to treat
the whole calendar year as Swiss, so the move year carried months of dividends and sales
that belong to the previous country's return, and the year before -- no Swiss tax
at all -- read as a Swiss report. `app_settings.swiss_tax_residency_from` moves the start of
the window; unset keeps the old behaviour.

EUR base, so no FX. Offline: no network.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.accounts import IBKR
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.models.trade import Trade
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.tax_service import TaxService

YEAR = 2026
MOVED = date(YEAR, 7, 12)


def _pay(day: date, gross: str = "100", wht: str = "15") -> DividendPayment:
    return DividendPayment(
        security_id=1, ex_date=day, pay_date=day, currency="EUR", shares_held=Decimal("0"),
        gross_amount_eur=Decimal(gross), withholding_tax_eur=Decimal(wht),
        net_amount_eur=Decimal(gross) - Decimal(wht), source="ibkr", withholding_country="US",
    )


def _trade(key: str, day: date, side: str, qty: str, price: str, pnl: str) -> Trade:
    proceeds = Decimal(qty) * Decimal(price)
    return Trade(
        ib_key=key, conid="1", security_id=1, symbol="AAA", trade_date=day, buy_sell=side,
        quantity=Decimal(qty), price=Decimal(price),
        proceeds=-proceeds if side == "BUY" else abs(proceeds),
        commission=Decimal("0"), realized_pnl=Decimal(pnl), currency="EUR", account=IBKR,
    )


async def _book(db) -> None:
    db.add(Security(id=1, isin="US1111111111", symbol="AAA", description="US Co",
                    currency="EUR", conid=1, exchange="NASDAQ", account=IBKR))
    await db.flush()
    db.add_all([
        # Before the move.
        _pay(date(YEAR, 3, 10)),
        _pay(MOVED.replace(day=11)),
        _trade("buy-it", date(YEAR, 7, 5), "BUY", "10", "100", "0"),
        _trade("sell-it", date(YEAR, 7, 10), "SELL", "-2", "120", "40"),
        # Swiss period, the first day included.
        _pay(MOVED, gross="50", wht="7.5"),
        _pay(date(YEAR, 9, 1), gross="30", wht="4.5"),
        _trade("buy-ch", date(YEAR, 8, 1), "BUY", "5", "100", "0"),
        _trade("sell-ch", date(YEAR, 7, 20), "SELL", "-1", "130", "25"),
    ])
    await db.commit()


async def _moved_on(db, day) -> None:
    await AppSettingsRepository(db).set_swiss_tax_residency_from(day)
    await db.commit()


@pytest.mark.asyncio
async def test_unset_counts_the_whole_year_as_before(db):
    await _book(db)
    report = await TaxService(db).get_tax_report(YEAR)
    assert report["residency"] == {"swiss_from": None, "covered_from": "2026-01-01"}
    assert report["dividend_totals"]["gross"] == 280.0
    assert len(report["realized_gains"]) == 2


@pytest.mark.asyncio
async def test_the_arrival_year_counts_from_the_first_swiss_day(db):
    await _book(db)
    await _moved_on(db, MOVED)
    report = await TaxService(db).get_tax_report(YEAR)

    assert report["residency"] == {"swiss_from": "2026-07-12", "covered_from": "2026-07-12"}
    # The pay date of 2026-07-12 is the first Swiss day and counts; the day before does not.
    assert [d["pay_date"] for d in report["dividend_income"]] == ["2026-07-12", "2026-09-01"]
    assert report["dividend_totals"]["gross"] == 80.0
    assert report["dividend_totals"]["withholding"] == 12.0
    (country,) = report["dividend_by_country"]
    assert (country["country"], country["gross"], country["withholding"]) == ("US", 80.0, 12.0)
    # Sales: the pre-move SELL is out, the Swiss one is in.
    assert [r["trade_date"] for r in report["realized_gains"]] == ["2026-07-20"]
    assert report["realized_totals"]["gain_loss"] == 25.0


@pytest.mark.asyncio
async def test_the_year_end_base_is_not_cut_by_the_move(db):
    """Wealth is valued at 31 December whenever the owner arrived."""
    await _book(db)
    full = await TaxService(db).get_tax_report(YEAR)
    await _moved_on(db, MOVED)
    moved = await TaxService(db).get_tax_report(YEAR)
    assert moved["holdings_as_of"] == full["holdings_as_of"]
    assert moved["holdings_snapshot"] == full["holdings_snapshot"]
    assert moved["holdings_snapshot_total"] == full["holdings_snapshot_total"]


@pytest.mark.asyncio
async def test_the_dealer_indicators_are_measured_from_the_move(db):
    await _book(db)
    await _moved_on(db, MOVED)
    criteria = (await TaxService(db).get_tax_report(YEAR))["trader_criteria"]
    # The opening base is the day before the move, and only the Swiss BUY is a purchase.
    assert criteria["opening_as_of"] == "2026-07-11"
    assert criteria["purchases"] == 500.0
    assert criteria["sales"] == 1


@pytest.mark.asyncio
async def test_a_year_before_the_move_is_not_a_swiss_report(db):
    await _book(db)
    await _moved_on(db, MOVED)
    service = TaxService(db)
    report = await service.get_tax_report(YEAR - 1)

    assert report["residency"] == {"swiss_from": "2026-07-12", "covered_from": None}
    assert report["dividend_income"] == [] and report["realized_gains"] == []
    assert report["dividend_source"] is None and report["realized_source"] is None
    # A wealth-tax base of a year with no Swiss wealth tax is absent, not zero.
    assert report["holdings_snapshot_total"] is None and report["cash_balance"] is None
    assert report["trader_criteria"] is None and report["pillar3a"] is None
    (warning,) = report["warnings"]
    assert "2026-07-12" in warning and "2025" in warning

    csv_text = service.to_csv(report)
    assert "Swiss tax residence began 2026-07-12" in csv_text
    assert "Dividend income" not in csv_text


@pytest.mark.asyncio
async def test_a_year_before_the_move_answers_the_whole_report_shape(db):
    """The early answer is built apart from the full one; their keys must not drift."""
    await _book(db)
    await _moved_on(db, MOVED)
    service = TaxService(db)
    before = await service.get_tax_report(YEAR - 1)
    during = await service.get_tax_report(YEAR)
    assert set(before) == set(during)
    assert set(before["dividend_totals"]) == set(during["dividend_totals"])
    assert set(before["realized_totals"]) == set(during["realized_totals"])


@pytest.mark.asyncio
async def test_a_later_year_is_unaffected_by_the_move(db):
    await _book(db)
    await _moved_on(db, MOVED)
    report = await TaxService(db).get_tax_report(YEAR + 1)
    assert report["residency"] == {"swiss_from": "2026-07-12", "covered_from": "2027-01-01"}


@pytest.mark.asyncio
async def test_the_csv_names_the_window(db):
    await _book(db)
    await _moved_on(db, MOVED)
    service = TaxService(db)
    assert "Swiss tax residence from 2026-07-12,figures count from 2026-07-12" in (
        service.to_csv(await service.get_tax_report(YEAR))
    )


@pytest.mark.asyncio
async def test_an_unreadable_stored_date_is_unset(db):
    await AppSettingsRepository(db).set("swiss_tax_residency_from", "not-a-date")
    await db.commit()
    assert await AppSettingsRepository(db).get_swiss_tax_residency_from() is None


@pytest.mark.asyncio
async def test_clearing_the_date_restores_the_whole_year(db):
    await _book(db)
    await _moved_on(db, MOVED)
    await _moved_on(db, None)
    report = await TaxService(db).get_tax_report(YEAR)
    assert report["residency"]["swiss_from"] is None
    assert report["dividend_totals"]["gross"] == 280.0
