"""
Tests for TaxService.get_tax_report — dividend income (net of withholding) and
realized capital gains, assembled from the authoritative tables. EUR base so no
FX data is needed, and market prices are seeded only where a test needs a closed
lot to be valuable.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.models.dividend_payment import DividendPayment
from app.models.app_settings import AppSetting
from app.models.market_price import MarketPrice
from app.models.corporate_action import CorporateAction
from app.models.cash_flow import CashFlow
from app.models.cash_balance import CashBalance
from app.repositories.trade_repository import TradeRepository
from app.repositories.dividend_repository import DividendRepository
from app.services.tax_service import TaxService
from app.clock import utc_today
from tests.memory_db import memory_engine


async def _make_session():
    # corporate_actions comes along because the holdings snapshot asks it when a position
    # actually began (a spun-off line's lots predate its listing); the two cash tables
    # because the IBKR cash line beside the holdings reads every cash ledger.
    tables = [Security.__table__, TaxLot.__table__, Trade.__table__,
              DividendPayment.__table__, AppSetting.__table__, MarketPrice.__table__,
              CorporateAction.__table__, CashFlow.__table__, CashBalance.__table__]
    engine = await memory_engine(tables=tables)
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(
        id=1, isin="US0000000001", symbol="AAA", description="Test Co",
        currency="EUR", conid=100, asset_category="STK", exchange="XETRA",
    ))
    await session.flush()
    return engine, session


@pytest.mark.asyncio
async def test_tax_report_dividends_and_realized_for_year():
    engine, session = await _make_session()
    try:
        # A realized SELL trade in 2025 (EUR): proceeds 200, realized 30 -> cost 170.
        await TradeRepository(session).upsert({
            "ib_key": "TX1", "conid": "100", "security_id": 1, "symbol": "AAA",
            "trade_date": date(2025, 6, 10), "buy_sell": "SELL", "quantity": Decimal("-4"),
            "price": Decimal("50"), "proceeds": Decimal("200"), "commission": Decimal("-1"),
            "currency": "EUR", "realized_pnl": Decimal("30"), "asset_category": "STK",
        })
        # A trade in 2024 must NOT count toward 2025.
        await TradeRepository(session).upsert({
            "ib_key": "TX0", "conid": "100", "security_id": 1, "symbol": "AAA",
            "trade_date": date(2024, 3, 1), "buy_sell": "SELL", "quantity": Decimal("-1"),
            "price": Decimal("10"), "proceeds": Decimal("10"), "commission": Decimal("0"),
            "currency": "EUR", "realized_pnl": Decimal("5"), "asset_category": "STK",
        })
        # An IBKR dividend in 2025: gross 100, withholding 15, net 85.
        await DividendRepository(session).upsert_payment({
            "security_id": 1, "ex_date": date(2025, 5, 2), "pay_date": date(2025, 5, 2),
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("100"), "withholding_tax_eur": Decimal("15"),
            "net_amount_eur": Decimal("85"), "source": "ibkr", "last_computed": datetime.now(),
        })
        await session.commit()

        report = await TaxService(session).get_tax_report(2025)

        assert report["year"] == 2025
        assert report["base_currency"] == "EUR"
        assert report["dividend_source"] == "ibkr"

        assert report["dividend_totals"] == {
            "gross": 100.0, "withholding": 15.0, "net": 85.0, "estimated_rows": 0,
        }
        assert len(report["dividend_income"]) == 1

        # Only the 2025 sell counts.
        assert len(report["realized_gains"]) == 1
        assert report["realized_source"] == "trades"
        assert report["realized_totals"] == {"proceeds": 200.0, "cost_basis": 170.0, "gain_loss": 30.0}

        csv_text = TaxService(session).to_csv(report)
        assert "Dividend income" in csv_text
        assert "Realized capital gains" in csv_text
        assert "AAA" in csv_text
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_withholding_is_grouped_by_country_for_da1():
    """DA-1 is filed per source country, derived from the ISIN prefix."""
    engine, session = await _make_session()
    try:
        # Second security in a different domicile (CH), same base currency.
        session.add(Security(
            id=2, isin="CH0000000002", symbol="BBB", description="Swiss Co",
            currency="EUR", conid=200, asset_category="STK", exchange="EBS",
        ))
        await session.flush()

        repo = DividendRepository(session)
        # US: 100 gross / 15 withheld
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2025, 5, 2), "pay_date": date(2025, 5, 2),
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("100"), "withholding_tax_eur": Decimal("15"),
            "net_amount_eur": Decimal("85"), "source": "ibkr", "last_computed": datetime.now(),
        })
        # CH: 50 gross / 17.5 withheld
        await repo.upsert_payment({
            "security_id": 2, "ex_date": date(2025, 7, 1), "pay_date": date(2025, 7, 1),
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("50"), "withholding_tax_eur": Decimal("17.5"),
            "net_amount_eur": Decimal("32.5"), "source": "ibkr", "last_computed": datetime.now(),
        })
        await session.commit()

        report = await TaxService(session).get_tax_report(2025)

        by_country = {c["country"]: c for c in report["dividend_by_country"]}
        assert set(by_country) == {"US", "CH"}
        assert by_country["US"]["gross"] == 100.0
        assert by_country["US"]["withholding"] == 15.0
        assert by_country["CH"]["withholding"] == 17.5
        assert by_country["CH"]["positions"] == 1
        # Sorted by withholding, largest first — the biggest reclaim leads.
        assert report["dividend_by_country"][0]["country"] == "CH"
        # Country totals must reconcile with the overall dividend totals.
        assert sum(c["withholding"] for c in report["dividend_by_country"]) == \
            report["dividend_totals"]["withholding"]

        assert "Withholding by country (DA-1)" in TaxService(session).to_csv(report)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_holdings_snapshot_is_reconstructed_at_year_end_for_past_years():
    """
    The Swiss wealth-tax base (Steuerwert) is the 31 December value, so a past year must
    be valued at that date — not from today's positions, which is what it used to do.
    """
    engine, session = await _make_session()
    try:
        # Held across 2025-12-31 -> counts.
        session.add(TaxLot(
            security_id=1, open_date=date(2025, 3, 10), close_date=None,
            quantity=Decimal("10"), cost_basis=Decimal("800"), price_per_unit=Decimal("80"),
            currency="EUR", cost_basis_eur=Decimal("800"), is_open=True,
        ))
        # Closed mid-2025 -> must NOT count at year end.
        session.add(TaxLot(
            security_id=1, open_date=date(2025, 1, 5), close_date=date(2025, 6, 30),
            quantity=Decimal("4"), cost_basis=Decimal("300"), price_per_unit=Decimal("75"),
            currency="EUR", cost_basis_eur=Decimal("300"), is_open=False,
        ))
        # Bought after the year end -> must NOT count in 2025.
        session.add(TaxLot(
            security_id=1, open_date=date(2026, 2, 1), close_date=None,
            quantity=Decimal("7"), cost_basis=Decimal("700"), price_per_unit=Decimal("100"),
            currency="EUR", cost_basis_eur=Decimal("700"), is_open=True,
        ))
        session.add(MarketPrice(
            security_id=1, date=date(2025, 12, 31), close_price=Decimal("120"),
            currency="EUR", source="test",
        ))
        await session.commit()

        report = await TaxService(session).get_tax_report(2025)

        assert report["holdings_as_of"] == "2025-12-31"
        assert len(report["holdings_snapshot"]) == 1
        row = report["holdings_snapshot"][0]
        assert row["quantity"] == 10.0          # only the lot held on 31 Dec
        assert row["market_value"] == 1200.0    # 10 x 120 at the year-end price
        assert row["cost_basis"] == 800.0
        assert report["holdings_snapshot_total"] == 1200.0
        assert "2025-12-31" in TaxService(session).to_csv(report)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_holdings_snapshot_uses_today_for_the_current_year():
    """No 31 December yet, so today's positions are the correct answer."""
    engine, session = await _make_session()
    try:
        session.add(TaxLot(
            security_id=1, open_date=date(2026, 1, 15), close_date=None,
            quantity=Decimal("5"), cost_basis=Decimal("500"), price_per_unit=Decimal("100"),
            currency="EUR", cost_basis_eur=Decimal("500"), is_open=True,
        ))
        await session.commit()

        report = await TaxService(session).get_tax_report(utc_today().year)

        assert report["holdings_as_of"] == utc_today().isoformat()
        assert "current year" in report["holdings_snapshot_note"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_unpriceable_holding_is_omitted_not_counted_as_zero():
    engine, session = await _make_session()
    try:
        session.add(TaxLot(
            security_id=1, open_date=date(2025, 3, 10), close_date=None,
            quantity=Decimal("10"), cost_basis=Decimal("800"), price_per_unit=Decimal("80"),
            currency="EUR", cost_basis_eur=Decimal("800"), is_open=True,
        ))
        await session.commit()  # no MarketPrice rows at all

        report = await TaxService(session).get_tax_report(2025)

        assert report["holdings_snapshot"] == []
        assert report["holdings_snapshot_total"] == 0.0
        assert "omitted" in report["holdings_snapshot_note"]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_dividend_source_is_decided_per_year_not_globally():
    """
    A year-to-date Flex Query only carries the current year's cash transactions, so
    earlier years keep yfinance estimates. Deciding the preference globally made the 2025
    report filter to source='ibkr', match nothing, and present 0.00 *labelled ibkr* — an
    estimate silently replaced by a confident zero, which is what the flag exists to stop.
    """
    engine, session = await _make_session()
    try:
        repo = DividendRepository(session)
        # Authoritative row, current year only.
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2026, 5, 2), "pay_date": date(2026, 5, 2),
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("100"), "withholding_tax_eur": Decimal("15"),
            "net_amount_eur": Decimal("85"), "source": "ibkr", "last_computed": datetime.now(),
        })
        # Estimate for the prior year — all 2025 has.
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2025, 5, 2), "pay_date": date(2025, 5, 2),
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("40"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("40"), "source": "yfinance_estimate",
            "last_computed": datetime.now(),
        })
        await session.commit()

        prior = await TaxService(session).get_tax_report(2025)
        assert prior["dividend_source"] == "yfinance_estimate"
        assert prior["dividend_totals"]["gross"] == 40.0
        assert len(prior["dividend_income"]) == 1

        current = await TaxService(session).get_tax_report(2026)
        assert current["dividend_source"] == "ibkr"
        assert current["dividend_totals"] == {
            "gross": 100.0, "withholding": 15.0, "net": 85.0, "estimated_rows": 0,
        }
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_estimate_only_year_states_no_withholding_anywhere():
    """
    Every payment an estimate: no withholding is on record, so none is reported — not
    0.00 per row, per country, in the total, or in the CSV a DA-1 is filled from. A zero
    there reads as "nothing was withheld", which is a reclaim of nothing.
    """
    engine, session = await _make_session()
    try:
        await DividendRepository(session).upsert_payment({
            "security_id": 1, "ex_date": date(2025, 5, 2), "pay_date": date(2025, 5, 2),
            "currency": "EUR", "shares_held": Decimal("10"),
            "gross_amount_eur": Decimal("40"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("40"), "source": "yfinance_estimate",
            "last_computed": datetime.now(),
        })
        await session.commit()

        report = await TaxService(session).get_tax_report(2025)

        assert report["dividend_income"][0]["withholding"] is None
        assert report["dividend_totals"]["withholding"] is None
        assert report["dividend_totals"]["estimated_rows"] == 1
        (country,) = report["dividend_by_country"]
        assert country["withholding"] is None
        assert (country["payments"], country["estimated_payments"]) == (1, 1)

        lines = TaxService(session).to_csv(report).splitlines()
        total = next(l for l in lines if l.startswith("TOTAL,") and "est." in l)
        assert total.startswith("TOTAL,,,,40.0,,40.0,"), total
        us = next(l for l in lines if l.startswith("US,"))
        # Withholding, rate, creditable and excess all blank -- nothing is recorded --
        # while the treaty rate is a fact about the country, known either way.
        assert us == "US,1,40.0,,,15.0,,,40.0,1", us
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_estimates_are_never_summed_with_authoritative_rows():
    """Within one year the two sources must never be added together."""
    engine, session = await _make_session()
    try:
        repo = DividendRepository(session)
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2026, 3, 1), "pay_date": date(2026, 3, 1),
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("100"), "withholding_tax_eur": Decimal("15"),
            "net_amount_eur": Decimal("85"), "source": "ibkr", "last_computed": datetime.now(),
        })
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2026, 3, 2), "pay_date": date(2026, 3, 2),
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("99"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("99"), "source": "yfinance_estimate",
            "last_computed": datetime.now(),
        })
        await session.commit()

        report = await TaxService(session).get_tax_report(2026)

        assert report["dividend_source"] == "ibkr"
        assert report["dividend_totals"]["gross"] == 100.0  # not 199
        assert len(report["dividend_income"]) == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_realized_falls_back_to_closed_lots_when_no_sell_trades():
    """
    Without ingested <Trades>, the tax report must still report the lots the DB
    knows were closed — reported as 0 while the portfolio view showed a figure was
    a real inconsistency between the two views. Flagged via realized_source.
    """
    engine, session = await _make_session()
    try:
        # A lot opened and closed in 2025, closed by the heuristic path (no Trade row).
        session.add(TaxLot(
            security_id=1, open_date=date(2025, 2, 1), close_date=date(2025, 9, 15),
            quantity=Decimal("3"), cost_basis=Decimal("300"), price_per_unit=Decimal("100"),
            currency="EUR", cost_basis_eur=Decimal("300"), is_open=False,
        ))
        # A lot closed in 2024 must not leak into 2025.
        session.add(TaxLot(
            security_id=1, open_date=date(2024, 1, 5), close_date=date(2024, 6, 1),
            quantity=Decimal("1"), cost_basis=Decimal("50"), price_per_unit=Decimal("50"),
            currency="EUR", cost_basis_eur=Decimal("50"), is_open=False,
        ))
        # Close-date price so the lot can actually be valued: 3 x 140 = 420 proceeds
        # against 300 cost -> +120 realized.
        session.add(MarketPrice(
            security_id=1, date=date(2025, 9, 15), close_price=Decimal("140"),
            currency="EUR", source="test",
        ))
        await session.commit()

        report = await TaxService(session).get_tax_report(2025)

        assert report["realized_source"] == "closed_lot_estimate"
        assert len(report["realized_gains"]) == 1  # the 2024 lot is excluded
        row = report["realized_gains"][0]
        assert row["symbol"] == "AAA"
        assert row["trade_date"] == "2025-09-15"
        assert row["quantity"] == 3.0
        assert report["realized_totals"] == {
            "proceeds": 420.0, "cost_basis": 300.0, "gain_loss": 120.0,
        }
        assert "closed_lot_estimate" in TaxService(session).to_csv(report)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_pre_ownership_zero_rows_do_not_pad_the_report():
    """
    yfinance stores a security's whole dividend history, so ex-dates from before
    it was bought sit in the table as zero rows. On this account 70 of the 96
    rows the 2025 report listed were 0.00 — noise in a filing aid.
    """
    engine, session = await _make_session()
    try:
        repo = DividendRepository(session)
        # Bought later: this ex-date earned nothing.
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2025, 2, 10), "pay_date": date(2025, 2, 10),
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("0"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("0"), "source": "yfinance_estimate",
        })
        # Real income, legacy shape (net NULL) — must survive and use gross.
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2025, 6, 12), "pay_date": date(2025, 6, 12),
            "currency": "EUR", "shares_held": Decimal("10"),
            "gross_amount_eur": Decimal("8"), "withholding_tax_eur": None,
            "net_amount_eur": None, "source": "yfinance_estimate",
        })
        await session.commit()

        report = await TaxService(session).get_tax_report(2025)
        rows = report["dividend_income"]
        assert [r["pay_date"] for r in rows] == ["2025-06-12"]
        assert rows[0]["net"] == 8.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_boundary_year_keeps_pre_ibkr_estimates_and_labels_mixed():
    """
    The cash-transaction ledger starts mid-year, so the boundary year holds real
    January income that exists only as estimates. Filtering the whole year to
    `ibkr` silently understated a filing aid while badging it authoritative. The
    era splice keeps estimates strictly before the first IBKR payment, IBKR rows
    from there on — and a post-boundary estimate is still the double-count it
    always was.
    """
    engine, session = await _make_session()
    try:
        repo = DividendRepository(session)
        # Pre-boundary estimate: real income from the weeks before the ledger.
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2026, 1, 15), "pay_date": date(2026, 1, 15),
            "currency": "EUR", "shares_held": Decimal("10"),
            "gross_amount_eur": Decimal("40"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("40"), "source": "yfinance_estimate",
            "last_computed": datetime.now(),
        })
        # First IBKR payment — the boundary.
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2026, 3, 1), "pay_date": date(2026, 3, 1),
            "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("100"), "withholding_tax_eur": Decimal("15"),
            "net_amount_eur": Decimal("85"), "source": "ibkr", "last_computed": datetime.now(),
        })
        # Post-boundary estimate: the same dividend seen through yfinance — drop it.
        await repo.upsert_payment({
            "security_id": 1, "ex_date": date(2026, 4, 1), "pay_date": date(2026, 4, 1),
            "currency": "EUR", "shares_held": Decimal("10"),
            "gross_amount_eur": Decimal("99"), "withholding_tax_eur": Decimal("0"),
            "net_amount_eur": Decimal("99"), "source": "yfinance_estimate",
            "last_computed": datetime.now(),
        })
        await session.commit()

        report = await TaxService(session).get_tax_report(2026)

        assert report["dividend_source"] == "mixed"
        assert report["dividend_ibkr_from"] == "2026-03-01"
        assert [r["pay_date"] for r in report["dividend_income"]] == \
            ["2026-01-15", "2026-03-01"]
        assert [r["source"] for r in report["dividend_income"]] == \
            ["yfinance_estimate", "ibkr"]
        # Gross and net include the estimate (its net falls back to its gross); the
        # withholding is the recorded one, and the count says what it leaves out.
        assert report["dividend_totals"] == {
            "gross": 140.0, "withholding": 15.0, "net": 125.0, "estimated_rows": 1,
        }
        # The estimate's withholding is not known, so it is absent rather than 0.00.
        assert [r["withholding"] for r in report["dividend_income"]] == [None, 15.0]
        (country,) = report["dividend_by_country"]
        assert (country["payments"], country["estimated_payments"]) == (2, 1)
        assert country["withholding"] == 15.0

        csv_text = TaxService(session).to_csv(report)
        assert "source: mixed" in csv_text
        assert "Estimates before 2026-03-01" in csv_text
        lines = csv_text.splitlines()
        estimate_line = next(l for l in lines if l.startswith("AAA,") and "2026-01-15" in l)
        assert estimate_line.endswith(",40.0,,40.0,est."), estimate_line
        assert any("withholding excludes, gross and net include 1 est." in l for l in lines)
    finally:
        await session.close()
        await engine.dispose()


# ── The report must not report numbers it cannot justify ────────────────
#
# Two swallowed failures used to produce plausible figures in a filing aid:
# _to_eur returned the *unconverted* foreign amount when FX failed (a TWD sale
# reads ~35x high, still badged realized_source='trades'), and a bare
# `except Exception: pass` turned any holdings-snapshot failure into a
# Steuerwert of 0.00 served with the note still claiming it was valued at that
# date's closes.


async def _twd_session():
    """A TWD security with one TWD SELL, and no exchange_rates table at all."""
    engine, session = await _make_session()
    session.add(Security(
        id=2, isin="TW0002330008", symbol="2330", description="TSMC",
        currency="TWD", conid=200, asset_category="STK", exchange="TWSE",
    ))
    await session.flush()
    await TradeRepository(session).upsert({
        "ib_key": "TX-TWD", "conid": "200", "security_id": 2, "symbol": "2330",
        "trade_date": date(2026, 3, 10), "buy_sell": "SELL", "quantity": Decimal("-12"),
        "price": Decimal("1000"), "proceeds": Decimal("12000"), "commission": Decimal("-10"),
        "currency": "TWD", "realized_pnl": Decimal("1500"), "asset_category": "STK",
    })
    return engine, session


@pytest.mark.asyncio
async def test_an_unconvertible_sale_is_omitted_and_reported_not_mis_scaled():
    engine, session = await _twd_session()
    try:
        report = await TaxService(session).get_tax_report(2026)

        # 12,000 TWD is ~340 EUR. Reporting 12000 would be the old behaviour.
        assert report["realized_totals"]["proceeds"] != 12000
        assert not any(r["symbol"] == "2330" for r in report["realized_gains"])
        assert report["warnings"], "an omitted sale must be reported, not silent"
        assert "TWD" in " ".join(report["warnings"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_convertible_sale_alongside_an_unconvertible_one_still_counts():
    engine, session = await _twd_session()
    try:
        # An EUR sale in the same year: it must survive its TWD neighbour.
        await TradeRepository(session).upsert({
            "ib_key": "TX-EUR", "conid": "100", "security_id": 1, "symbol": "AAA",
            "trade_date": date(2026, 4, 1), "buy_sell": "SELL", "quantity": Decimal("-2"),
            "price": Decimal("50"), "proceeds": Decimal("100"), "commission": Decimal("-1"),
            "currency": "EUR", "realized_pnl": Decimal("10"), "asset_category": "STK",
        })

        report = await TaxService(session).get_tax_report(2026)

        assert [r["symbol"] for r in report["realized_gains"]] == ["AAA"]
        assert report["realized_totals"]["proceeds"] == 100.0
        assert report["realized_source"] == "trades"
        assert "understate" in " ".join(report["warnings"])
    finally:
        await session.close()
        await engine.dispose()


# ── SELL trades convert through NativeToBase (GitHub issue #46) ─────────
#
# The realized loop was a hand-written copy of the native->EUR->base two-step: two rate
# lookups per sale, and a sale in the base currency round-tripped through EUR. A foreign
# sale's figures must not move; a base-currency sale's become its own amounts exactly.

SALE_DAY = date(2026, 5, 4)
#: A CHF->EUR rate and an EUR->CHF rate that are not exact inverses (0.9375 * 1.0640 =
#: 0.99750): the two separately rounded series every real round trip goes through.
CHF_TO_EUR = Decimal("0.9375")
EUR_TO_CHF = Decimal("1.0640")
USD_TO_EUR = Decimal("0.9100")


class _CountingRates:
    """A `CurrencyService` stand-in that counts lookups, keyed by native currency."""

    def __init__(self, rates):
        self.rates = rates
        self.calls = 0

    async def get_exchange_rate(self, currency, on_date, to_currency="EUR"):
        self.calls += 1
        return self.rates[currency]


async def _report_under_chf(monkeypatch, session, rates):
    from app.services.base_fx import BaseFx
    from app.services.portfolio_service import PortfolioService

    async def _chf(self):
        return BaseFx("CHF", {SALE_DAY: EUR_TO_CHF})

    monkeypatch.setattr(PortfolioService, "_load_base_fx", _chf)
    service = TaxService(session)
    service.currency_service = rates
    return await service.get_tax_report(SALE_DAY.year)


async def _sell(session, key, currency, proceeds, pnl):
    await TradeRepository(session).upsert({
        "ib_key": key, "conid": "100", "security_id": 1, "symbol": "AAA",
        "trade_date": SALE_DAY, "buy_sell": "SELL", "quantity": Decimal("-1"),
        "price": proceeds, "proceeds": proceeds, "commission": Decimal("0"),
        "currency": currency, "realized_pnl": pnl, "asset_category": "STK",
    })


@pytest.mark.asyncio
async def test_a_foreign_sale_converts_exactly_as_the_old_two_step_did(monkeypatch):
    engine, session = await _make_session()
    try:
        await _sell(session, "TX-USD", "USD", Decimal("1234.56"), Decimal("210.98"))
        await session.commit()

        report = await _report_under_chf(monkeypatch, session, _CountingRates({"USD": USD_TO_EUR}))

        # The removed code, written out: to EUR at the trade date, then EUR -> CHF.
        old_proceeds = Decimal("1234.56") * USD_TO_EUR * EUR_TO_CHF
        old_gain = Decimal("210.98") * USD_TO_EUR * EUR_TO_CHF
        old_cost = (Decimal("1234.56") - Decimal("210.98")) * USD_TO_EUR * EUR_TO_CHF
        (row,) = report["realized_gains"]
        assert (row["proceeds"], row["cost_basis"], row["gain_loss"]) == (
            round(float(old_proceeds), 2), round(float(old_cost), 2), round(float(old_gain), 2),
        ) == (1195.35, 991.07, 204.28)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_sale_in_the_base_currency_is_its_own_amount_not_a_round_trip(monkeypatch):
    engine, session = await _make_session()
    try:
        await _sell(session, "TX-CHF", "CHF", Decimal("4500.00"), Decimal("300.00"))
        await session.commit()

        rates = _CountingRates({"CHF": CHF_TO_EUR})
        report = await _report_under_chf(monkeypatch, session, rates)

        (row,) = report["realized_gains"]
        assert (row["proceeds"], row["cost_basis"], row["gain_loss"]) == (4500.0, 4200.0, 300.0)
        # The old round trip read 4,488.75 / 4,189.50 / 299.25 — 0.25% low on every
        # figure, on the line a reader checks against the broker statement.
        assert round(float(Decimal("4500.00") * CHF_TO_EUR * EUR_TO_CHF), 2) == 4488.75
        # And it asked for no rate at all.
        assert rates.calls == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_sales_on_one_currency_and_date_cost_one_rate_lookup(monkeypatch):
    engine, session = await _make_session()
    try:
        for i in range(5):
            await _sell(session, f"TX-USD-{i}", "USD", Decimal("100"), Decimal("10"))
        await session.commit()

        rates = _CountingRates({"USD": USD_TO_EUR})
        report = await _report_under_chf(monkeypatch, session, rates)

        assert len(report["realized_gains"]) == 5
        # Two lookups per sale (proceeds and gain) before NativeToBase's memo: 10.
        assert rates.calls == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_failed_holdings_snapshot_is_flagged_not_reported_as_zero(monkeypatch):
    engine, session = await _make_session()
    try:
        from app.services.portfolio_service import PortfolioService

        async def _boom(self, base_fx, as_of):
            raise RuntimeError("price currency cache blew up")

        monkeypatch.setattr(PortfolioService, "holdings_snapshot_as_of", _boom)

        report = await TaxService(session).get_tax_report(2025)

        assert report["holdings_snapshot_error"] is True
        # None, not 0.00 — a zero here reads as a computed wealth-tax base.
        assert report["holdings_snapshot_total"] is None
        assert report["holdings_snapshot"] == []
        assert "not zero" in " ".join(report["warnings"])
        # And the note must stop claiming a valuation that never happened.
        assert "valued at that date's closing prices" not in report["holdings_snapshot_note"]

        csv_text = TaxService(session).to_csv(report)
        assert "WARNINGS" in csv_text
        assert "This is not zero." in csv_text
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_healthy_report_carries_no_warnings_and_a_real_total():
    engine, session = await _make_session()
    try:
        report = await TaxService(session).get_tax_report(2025)

        assert report["warnings"] == []
        assert report["holdings_snapshot_error"] is False
        assert report["holdings_snapshot_total"] is not None
        assert "valued at that date's closing prices" in report["holdings_snapshot_note"]
    finally:
        await session.close()
        await engine.dispose()
