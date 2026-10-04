"""
Tests for IBKR cash-transaction dividend ingestion (real gross / withholding /
net) in DividendService. Uses EUR amounts so no FX data is required.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register mappers
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.dividend_payment import DividendPayment
from app.models.app_settings import AppSetting
from app.repositories.dividend_repository import DividendRepository
from app.services.dividend_service import DividendService


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [Security.__table__, TaxLot.__table__, DividendPayment.__table__, AppSetting.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    session = AsyncSession(engine, expire_on_commit=False)
    session.add(Security(
        id=1, isin="US0000000001", symbol="AAA", description="Test Co",
        currency="EUR", conid=100, asset_category="STK", exchange="XETRA",
    ))
    await session.flush()
    return engine, session


def _ct(ct_type, amount, conid="100", pay_date=date(2026, 5, 2)):
    return {
        "ib_key": f"{ct_type}-{amount}", "conid": conid, "symbol": "AAA",
        "pay_date": pay_date, "type": ct_type, "amount": Decimal(str(amount)),
        "currency": "EUR", "description": "AAA dividend",
    }


@pytest.mark.asyncio
async def test_ibkr_dividend_computes_net_of_withholding():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        cash_txns = [_ct("DIVIDEND", 100), _ct("WHTAX", -15)]

        result = await svc.sync_dividends_from_cash_transactions(cash_txns, {"100": 1})
        await session.commit()
        assert result["ibkr_dividends"] == 1

        rows = await svc.repo.get_by_security(1)
        assert len(rows) == 1
        dp = rows[0]
        assert dp.source == "ibkr"
        assert dp.gross_amount_eur == Decimal("100")
        assert dp.withholding_tax_eur == Decimal("15")   # stored positive
        assert dp.net_amount_eur == Decimal("85")

        summary = await svc.get_dividend_summary()
        assert summary["source"] == "ibkr"
        assert summary["total_gross_eur"] == 100.0
        assert summary["total_withholding_eur"] == 15.0
        assert summary["total_net_eur"] == 85.0
        assert summary["total_eur"] == 85.0  # back-compat key is NET
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ibkr_dividend_aggregates_payment_in_lieu():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        # Dividend + payment-in-lieu on the same date sum into gross; withholding applies.
        cash_txns = [_ct("DIVIDEND", 60), _ct("PAYMENTINLIEU", 40), _ct("WHTAX", -15)]

        await svc.sync_dividends_from_cash_transactions(cash_txns, {"100": 1})
        await session.commit()

        rows = await svc.repo.get_by_security(1)
        assert len(rows) == 1
        assert rows[0].gross_amount_eur == Decimal("100")
        assert rows[0].net_amount_eur == Decimal("85")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_cash_transactions_is_noop():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        result = await svc.sync_dividends_from_cash_transactions([], {"100": 1})
        assert result["ibkr_dividends"] == 0
        assert await svc.repo.get_by_security(1) == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_last_updated_is_serialized_with_an_explicit_utc_offset():
    """
    last_computed is stamped naive (the container clock is UTC). A bare
    isoformat() makes the browser parse it as local time — the misread
    utc_iso() exists to prevent. The API must always emit the offset.
    """
    from datetime import datetime

    engine, session = await _make_session()
    try:
        session.add(DividendPayment(
            security_id=1, ex_date=date(2026, 3, 2), source="yfinance_estimate",
            amount_per_share=Decimal("1"), currency="EUR",
            shares_held=Decimal("5"), gross_amount_eur=Decimal("5"),
            withholding_tax_eur=Decimal("0"), net_amount_eur=Decimal("5"),
            last_computed=datetime(2026, 3, 2, 6, 2, 46),  # naive, as stored
        ))
        await session.commit()

        summary = await DividendService(session).get_dividend_summary()
        assert summary["last_updated"] == "2026-03-02T06:02:46+00:00"
    finally:
        await session.close()
        await engine.dispose()


def test_staleness_check_reads_aware_and_legacy_naive_timestamps():
    """The router's parser must survive both serialization eras without a 500."""
    from datetime import datetime, timedelta, timezone

    from app.routers.dividends import _is_summary_stale

    fresh_aware = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    old_aware = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    old_naive = (datetime.now(timezone.utc) - timedelta(days=3)).replace(tzinfo=None).isoformat()

    base = {"monthly": [{"month": "2026-03", "amount_eur": 5.0}]}
    assert _is_summary_stale({**base, "last_updated": fresh_aware}) is False
    assert _is_summary_stale({**base, "last_updated": old_aware}) is True
    assert _is_summary_stale({**base, "last_updated": old_naive}) is True
    assert _is_summary_stale({**base, "last_updated": "not-a-date"}) is True


# ── The summary card's provenance flag ──────────────────────────────────
#
# The card's figures are NET and era-spliced, but it reported a flat
# source='ibkr' the moment any IBKR row existed — while still carrying the
# estimated months that precede the ledger. The UI's footnote read off that
# flag, so it claimed either real withholding for a period with none or, in the
# other direction, "estimated gross via Yahoo, withholding not reflected" over
# IBKR actuals net of real tax.


@pytest.mark.asyncio
async def test_the_boundary_era_reports_mixed_not_ibkr():
    engine, session = await _make_session()
    try:
        # An estimate in January, real IBKR rows from May: both survive the splice.
        session.add(DividendPayment(
            security_id=1, ex_date=date(2026, 1, 15), source="yfinance_estimate",
            amount_per_share=Decimal("1"), currency="EUR", shares_held=Decimal("5"),
            gross_amount_eur=Decimal("5"), withholding_tax_eur=Decimal("0"),
            net_amount_eur=Decimal("5"),
        ))
        await session.flush()
        svc = DividendService(session)
        await svc.sync_dividends_from_cash_transactions(
            [_ct("DIVIDEND", 100), _ct("WHTAX", -15)], {"100": 1}
        )
        await session.commit()

        summary = await svc.get_dividend_summary()

        assert summary["source"] == "mixed"
        assert summary["ibkr_from"] == "2026-05-02"
        # Both eras contribute: 5 estimated + 85 net actual.
        assert summary["total_net_eur"] == 90.0
        assert summary["total_withholding_eur"] == 15.0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_estimate_only_history_reports_yfinance_estimate():
    engine, session = await _make_session()
    try:
        session.add(DividendPayment(
            security_id=1, ex_date=date(2026, 1, 15), source="yfinance_estimate",
            amount_per_share=Decimal("1"), currency="EUR", shares_held=Decimal("5"),
            gross_amount_eur=Decimal("5"), withholding_tax_eur=Decimal("0"),
            net_amount_eur=Decimal("5"),
        ))
        await session.commit()

        summary = await DividendService(session).get_dividend_summary()

        assert summary["source"] == "yfinance_estimate"
        assert summary["ibkr_from"] is None
        assert summary["total_withholding_eur"] == 0.0
    finally:
        await session.close()
        await engine.dispose()


# ── The two sources no longer collide on one unique key ──────────────────
#
# `ex_date` means a real ex-date for yfinance rows and the *pay* date for IBKR
# ones. Under the old (security_id, ex_date) key those shared one slot, so a
# payer whose ex-to-pay lag landed on another record's date had the two rows
# overwrite each other — relabelling real withholding as an estimate, or nulling
# amount_per_share and dropping the security below the two-sample threshold the
# forecast needs. Mastercard's 29-day lag already exceeds a monthly cycle.


@pytest.mark.asyncio
async def test_an_ibkr_pay_date_and_a_yfinance_ex_date_can_share_a_day():
    engine, session = await _make_session()
    try:
        repo = DividendRepository(session)
        collision = date(2026, 5, 2)

        await repo.upsert_payment({
            "security_id": 1, "ex_date": collision, "source": "yfinance_estimate",
            "amount_per_share": Decimal("0.5"), "currency": "EUR",
            "shares_held": Decimal("10"), "gross_amount_eur": Decimal("5"),
        })
        await repo.upsert_payment({
            "security_id": 1, "ex_date": collision, "pay_date": collision,
            "source": "ibkr", "currency": "EUR", "shares_held": Decimal("0"),
            "gross_amount_eur": Decimal("7"), "withholding_tax_eur": Decimal("1"),
            "net_amount_eur": Decimal("6"),
        })
        await session.commit()

        rows = sorted(
            (r.source, r.gross_amount_eur, r.amount_per_share)
            for r in await repo.get_by_security(1)
        )
        assert len(rows) == 2, "the two sources must coexist, not overwrite"
        assert rows[0] == ("ibkr", Decimal("7"), None)
        # The estimate keeps its per-share figure, which the forecast's cadence needs.
        assert rows[1][0] == "yfinance_estimate"
        assert rows[1][2] == Decimal("0.5")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_upsert_still_updates_its_own_source_in_place():
    """Source-scoping must not turn every re-sync into a duplicate row."""
    engine, session = await _make_session()
    try:
        repo = DividendRepository(session)
        for gross in (Decimal("5"), Decimal("6")):
            await repo.upsert_payment({
                "security_id": 1, "ex_date": date(2026, 5, 2),
                "source": "yfinance_estimate", "amount_per_share": Decimal("0.5"),
                "currency": "EUR", "shares_held": Decimal("10"),
                "gross_amount_eur": gross,
            })
        await session.commit()

        rows = await repo.get_by_security(1)
        assert len(rows) == 1
        assert rows[0].gross_amount_eur == Decimal("6")
    finally:
        await session.close()
        await engine.dispose()


# ---- IBKR's stated rate and kind, parsed off the cash line (2026-10-04) -------------
# Real descriptions from a statement, ISINs replaced.

from app.services.dividend_service import parse_ibkr_dividend_description  # noqa: E402


def test_the_rate_currency_and_kind_are_read_off_a_real_cash_line():
    assert parse_ibkr_dividend_description(
        "VT(US0000000000) CASH DIVIDEND USD 0.4084 PER SHARE (Ordinary Dividend)"
    ) == ("USD", Decimal("0.4084"), "ordinary")
    assert parse_ibkr_dividend_description(
        "000660.KS(KR0000000000) CASH DIVIDEND KRW 375 PER SHARE (Ordinary Dividend)"
    ) == ("KRW", Decimal("375"), "ordinary")
    assert parse_ibkr_dividend_description(
        "XYZ(US0000000000) CASH DIVIDEND USD 1.50 PER SHARE (Special Dividend)"
    )[2] == "special"
    assert parse_ibkr_dividend_description(
        "XYZ(US0000000000) CASH DIVIDEND USD 0.10 PER SHARE (Return of Capital)"
    )[2] == "return_of_capital"


def test_an_unparseable_description_costs_the_figure_never_the_row():
    assert parse_ibkr_dividend_description(None) == (None, None, None)
    assert parse_ibkr_dividend_description("AAA dividend") == (None, None, None)
    # Rate without a kind label, and the ISIN's parenthesis is never taken for a kind.
    assert parse_ibkr_dividend_description(
        "AAA(US0000000000) CASH DIVIDEND EUR 0.25 PER SHARE"
    ) == ("EUR", Decimal("0.25"), None)


def _described(ct_type, amount, description, pay_date=date(2026, 5, 2)):
    ct = _ct(ct_type, amount, pay_date=pay_date)
    ct["description"] = description
    ct["ib_key"] = f"{ct_type}-{amount}-{description}"
    return ct


@pytest.mark.asyncio
async def test_the_ingest_stores_ibkrs_rate_and_kind_on_the_row():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        line = "AAA(US0000000001) CASH DIVIDEND EUR 0.25 PER SHARE"
        await svc.sync_dividends_from_cash_transactions([
            _described("DIVIDEND", 25, line + " (Ordinary Dividend)"),
            _described("WHTAX", -3.75, line + " - US TAX"),
        ], {"100": 1})
        await session.commit()
        (dp,) = await svc.repo.get_by_security(1)
        assert dp.per_share_native == Decimal("0.25")
        assert dp.dividend_kind == "ordinary"
        assert dp.amount_per_share is None     # never the Yahoo-series marker
        assert dp.withholding_tax_eur == Decimal("3.75")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_special_paid_beside_an_ordinary_dividend_keeps_the_ordinary_rate():
    """Two cash lines, one group: the row's rate is the one the schedule repeats."""
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        await svc.sync_dividends_from_cash_transactions([
            _described("DIVIDEND", 25,
                       "AAA(US0000000001) CASH DIVIDEND EUR 0.25 PER SHARE (Ordinary Dividend)"),
            _described("DIVIDEND", 100,
                       "AAA(US0000000001) CASH DIVIDEND EUR 1.00 PER SHARE (Special Dividend)"),
        ], {"100": 1})
        await session.commit()
        (dp,) = await svc.repo.get_by_security(1)
        assert dp.gross_amount_eur == Decimal("125")    # the money is all real
        assert dp.per_share_native == Decimal("0.25")
        assert dp.dividend_kind == "ordinary"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_reversal_cancels_its_rate_rather_than_doubling_it():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        line = "AAA(US0000000001) CASH DIVIDEND EUR 0.25 PER SHARE (Ordinary Dividend)"
        await svc.sync_dividends_from_cash_transactions([
            _described("DIVIDEND", 25, line),
            _described("DIVIDEND", -25, line),
            _described("DIVIDEND", 25, line + " "),
        ], {"100": 1})
        await session.commit()
        (dp,) = await svc.repo.get_by_security(1)
        assert dp.gross_amount_eur == Decimal("25")
        assert dp.per_share_native == Decimal("0.25")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_rate_in_another_currency_than_the_payment_is_dropped():
    engine, session = await _make_session()
    try:
        svc = DividendService(session)
        await svc.sync_dividends_from_cash_transactions([
            _described("DIVIDEND", 25,
                       "AAA(US0000000001) CASH DIVIDEND USD 0.27 PER SHARE (Ordinary Dividend)"),
        ], {"100": 1})
        await session.commit()
        (dp,) = await svc.repo.get_by_security(1)
        assert dp.per_share_native is None
        assert dp.dividend_kind == "ordinary"
    finally:
        await session.close()
        await engine.dispose()
