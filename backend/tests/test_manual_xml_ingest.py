"""
Tests for the offline XML ingest path (`app/cli/ingest_flex_xml.py`).

This path exists because IBKR's Flex Web Service and the Client Portal download button
serve the same statement over independent channels — only the API path spends the token's
request budget, so only it can trip a `Code=1025` lockout. Feeding a downloaded file works
while the token is blocked, and is the only way to reach a prior tax year (our query is
Year-to-Date).

The point of these tests is that the offline route is not a shortcut: it goes through the
same `ingest_flex_statement` as the endpoint and the scheduled jobs, so it must inherit the
empty-statement wipe guard and the idempotent upserts rather than quietly bypassing them.
No network, no credentials.
"""
import re
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.models.dividend_payment import DividendPayment
from app.services.ibkr_service import IBKRService
from app.services.sync_helper import ingest_flex_statement, EmptyStatementError

from tests.test_flex_ingestion_e2e import FLEX_XML, CSU_CONID
from tests.memory_db import memory_engine


async def _make_session():
    """Full schema: ingest_flex_statement touches securities, lots, trades, dividends,
    app_settings and the benchmark cache, so create everything rather than a subset."""
    engine = await memory_engine()
    return engine, AsyncSession(engine, expire_on_commit=False)


async def _ingest_bytes(session, xml: bytes) -> dict:
    flex_data = IBKRService(token="t", query_id="q").parse_flex_xml(xml)
    result = await ingest_flex_statement(session, flex_data)
    await session.commit()
    return result


@pytest.mark.asyncio
async def test_offline_ingest_populates_everything_the_api_path_would():
    engine, session = await _make_session()
    try:
        result = await _ingest_bytes(session, FLEX_XML)

        assert result["account_id"] == "U1234567"
        assert result["data_to"] == "2026-07-24"
        assert result["securities_synced"] == 1
        assert result["taxlots_synced"] == 1

        # The OPT row and the duplicate ORDER-level row are both excluded, so only the
        # two real stock executions land.
        trades = (await session.execute(select(Trade))).scalars().all()
        assert {t.ib_key for t in trades} == {"TX-G-EXEC", "TX-CSU"}

        # Real withholding, not a yfinance estimate.
        div = (await session.execute(
            select(DividendPayment).where(DividendPayment.source == "ibkr")
        )).scalars().first()
        assert div is not None
        assert div.gross_amount_eur == Decimal("42.50")
        assert div.withholding_tax_eur == Decimal("6.38")

        lot = (await session.execute(select(TaxLot))).scalars().one()
        assert lot.quantity == Decimal("12")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_re_ingesting_the_same_file_changes_nothing():
    """Re-running must be safe — upserts are keyed on ib_key and isin+exchange."""
    engine, session = await _make_session()
    try:
        await _ingest_bytes(session, FLEX_XML)
        first = {
            "securities": len((await session.execute(select(Security))).scalars().all()),
            "trades": len((await session.execute(select(Trade))).scalars().all()),
            "dividends": len((await session.execute(select(DividendPayment))).scalars().all()),
            "open_lots": len((await session.execute(
                select(TaxLot).where(TaxLot.is_open == True)  # noqa: E712
            )).scalars().all()),
        }

        await _ingest_bytes(session, FLEX_XML)
        second = {
            "securities": len((await session.execute(select(Security))).scalars().all()),
            "trades": len((await session.execute(select(Trade))).scalars().all()),
            "dividends": len((await session.execute(select(DividendPayment))).scalars().all()),
            "open_lots": len((await session.execute(
                select(TaxLot).where(TaxLot.is_open == True)  # noqa: E712
            )).scalars().all()),
        }

        assert first == second, "offline ingest must be idempotent, not additive"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_wipe_guard_still_fires_through_the_offline_path():
    """
    An empty statement must not be able to mark every holding sold just because it
    arrived as a file rather than over the API. The guard lives in reconcile_taxlots and
    the offline route must inherit it.
    """
    engine, session = await _make_session()
    try:
        await _ingest_bytes(session, FLEX_XML)  # establishes an open lot

        # A real "successful but empty" statement: the section is present but has no rows.
        empty = re.sub(
            rb"<OpenPositions>.*?</OpenPositions>",
            b"<OpenPositions></OpenPositions>",
            FLEX_XML,
            flags=re.DOTALL,
        )

        with pytest.raises(EmptyStatementError):
            await _ingest_bytes(session, empty)

        await session.rollback()
        # The holding survived.
        remaining = (await session.execute(
            select(TaxLot).where(TaxLot.is_open == True)  # noqa: E712
        )).scalars().all()
        assert len(remaining) == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_sold_out_security_gets_linked_through_this_path_too():
    """
    CSU is absent from OpenPositions (fully sold), so the statement's conid map can't
    resolve it. Once the security exists in the DB the trade must still link — this is the
    fix that turned 6 NULL security_id rows into 0.
    """
    engine, session = await _make_session()
    try:
        await _ingest_bytes(session, FLEX_XML)
        csu_trade = (await session.execute(
            select(Trade).where(Trade.ib_key == "TX-CSU")
        )).scalar_one()
        # No CSU security row exists here, so NULL is the correct answer...
        assert csu_trade.security_id is None

        # ...but once it does, a re-ingest links it.
        session.add(Security(
            isin="CA21037X1006", symbol="CSU", description="Constellation Software",
            currency="EUR", conid=int(CSU_CONID), asset_category="STK", exchange="TSE",
        ))
        await session.commit()

        await _ingest_bytes(session, FLEX_XML)
        csu_trade = (await session.execute(
            select(Trade).where(Trade.ib_key == "TX-CSU")
        )).scalar_one()
        assert csu_trade.security_id is not None
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Schema drift reaches the sync record by the right channel.
#
# `_sanitize_flex_xml` classifies a dropped attribute by whether the extractors read it,
# and `tests/test_flex_xml_sanitizer.py` pins that decision. What those tests cannot see
# is the wiring that carries it onward: sanitizer -> parse_flex_xml's `flex_notes` ->
# ingest_flex_statement's `flex_schema_notes`, with `warnings` left clean. That chain is
# what decides whether the banner in the header is empty, and it is only exercised by the
# two real entry points — which `_ingest_bytes` above is.
#
# Worth pinning here rather than trusting: production could not confirm it. The first
# IBKR sync after the fix deployed returned a routine `Code=1001`, so no statement was
# parsed and neither channel was populated.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harmless_schema_drift_reaches_details_and_not_the_warning_banner():
    """
    The real document carries `subCategory`, which no extractor reads. It must be
    recorded — losing it entirely would make "did a NEW kind of thing start being
    dropped?" unanswerable — and it must not reach `warnings`, because a banner that is
    always present is a banner nobody reads.
    """
    engine, session = await _make_session()
    try:
        result = await _ingest_bytes(session, FLEX_XML)

        notes = result["flex_schema_notes"]
        assert notes, "harmless drift vanished instead of being recorded"
        assert any("subCategory" in n for n in notes)
        # One compact line, not one entry per field: 27 fully-qualified names with
        # reasons is what made the original unreadable.
        assert len(notes) == 1
        assert "nothing was affected" in notes[0]

        assert not any("attribute" in w for w in result["warnings"]), result["warnings"]
        # And the ingest still worked — the point of dropping the attribute at all.
        assert result["securities_synced"] > 0
        assert result["trades_seen"] > 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_drift_on_a_field_the_ingest_reads_still_reaches_the_banner():
    """
    The half that must stay loud, through the same chain. An unparseable
    `CashTransaction.type` is dropped to None and the row is then skipped by
    extract_cash_transactions — a dividend that silently never arrives, which is the
    exact class of failure `warnings[]` exists to surface.
    """
    engine, session = await _make_session()
    try:
        # "Broker Fees" is selectable in the Flex Query and absent from ibflex's
        # CashAction enum — unlike "Bond Interest Paid", which is a *valid* member and
        # so gets converted rather than dropped. Picked deliberately: the first attempt
        # at this test used a legal value and passed nothing.
        broken = FLEX_XML.replace(b'type="Dividends"', b'type="Broker Fees"')
        assert broken != FLEX_XML, "fixture changed; the substitution no longer applies"

        result = await _ingest_bytes(session, broken)

        assert any("CashTransaction.type" in w for w in result["warnings"]), result["warnings"]
        assert any("data may be affected" in w for w in result["warnings"])
        # It must not be filed away as harmless.
        assert not any("CashTransaction.type" in n for n in result["flex_schema_notes"])
        # The dividend really is gone, which is what makes the warning worth having.
        assert (await session.execute(select(DividendPayment))).scalars().first() is None
    finally:
        await session.close()
        await engine.dispose()
