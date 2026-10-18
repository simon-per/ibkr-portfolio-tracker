"""
A split restates the cached price history, not just the share count.

`get_missing_dates()` only ever fetches dates we don't already have, so once a split
lands the pre-split closes sitting in `market_prices` are never refreshed — while IBKR
restates the tax lot's quantity immediately. The two halves of the same position then
disagree, and the cost-basis-vs-market-value chart gets a step change at the split date
that nothing detects and nothing self-heals.

So ingestion drops the invalidated rows and lets the next market-data sync refetch them.
The delicate part is *when*: purging on every sync would wipe and refetch the same history
daily, which is the shape of the loops CLAUDE.md rule 1 exists to prevent. It is therefore
keyed on the corporate action being newly inserted, and these tests pin that.

Offline: no IBKR, no Yahoo.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401  register all mappers
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.repositories.corporate_action_repository import CorporateActionRepository
from app.repositories.market_price_repository import MarketPriceRepository
from app.repositories.trade_repository import TradeRepository
from app.services.sync_helper import (
    PRICE_RESTATING_ACTIONS,
    SPLIT_LIKE_ACTIONS,
    invalidate_prices_for_splits,
    persist_transactions,
)
from tests.memory_db import memory_engine

CONID = "322447809"
SPLIT_DAY = date(2026, 6, 15)


async def _make_session():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)

    session.add(Security(
        id=1, isin="US0000000001", symbol="AAA", description="Test Co",
        currency="USD", conid=int(CONID), asset_category="STK", exchange="NASDAQ",
    ))
    # Three closes before/on the split and two after it.
    for day, price in [
        (date(2026, 6, 10), 300), (date(2026, 6, 12), 310), (SPLIT_DAY, 320),
        (date(2026, 6, 16), 33), (date(2026, 6, 17), 34),
    ]:
        session.add(MarketPrice(
            security_id=1, date=day, close_price=Decimal(price),
            currency="USD", source="yahoo_finance",
        ))
    await session.flush()
    return engine, session


def _action(action_type, ib_key="CA-1", action_date=SPLIT_DAY):
    return {
        "ib_key": ib_key,
        "conid": CONID,
        "symbol": "AAA",
        "action_date": action_date,
        "action_type": action_type,
        "quantity": Decimal("90"),
        "value": None,
        "proceeds": None,
        "currency": "USD",
        "description": f"AAA({CONID}) {action_type} 10 FOR 1",
    }


async def _remaining_dates(session):
    result = await session.execute(select(MarketPrice.date).order_by(MarketPrice.date))
    return [row[0] for row in result.all()]


async def _ingest(session, actions):
    """Run the same two steps ingest_flex_statement runs, and return the purge summary."""
    persisted = await persist_transactions(
        TradeRepository(session), CorporateActionRepository(session),
        {CONID: 1}, [], actions,
    )
    return await invalidate_prices_for_splits(
        MarketPriceRepository(session), persisted["newly_restating_actions"]
    )


def test_price_restating_actions_is_a_subset_of_the_split_like_set():
    """Prices are fetched with auto_adjust=False, and Yahoo restates raw `Close` for
    splits only — a spinoff or stock dividend changes the share count without rebasing
    it. Purging on those would throw away good rows and buy a refetch for nothing."""
    assert PRICE_RESTATING_ACTIONS < SPLIT_LIKE_ACTIONS
    assert "SPINOFF" not in PRICE_RESTATING_ACTIONS
    assert "STOCKDIV" not in PRICE_RESTATING_ACTIONS
    assert "ISSUECHANGE" not in PRICE_RESTATING_ACTIONS


@pytest.mark.asyncio
async def test_a_new_split_purges_prices_up_to_and_including_its_date():
    engine, session = await _make_session()
    try:
        summary = await _ingest(session, [_action("FORWARDSPLIT")])

        assert summary["prices_invalidated"] == 3
        assert summary["securities_purged"] == 1
        # The split day's own close is restated too, hence the inclusive bound.
        assert await _remaining_dates(session) == [date(2026, 6, 16), date(2026, 6, 17)]
        assert summary["notes"] and "FORWARDSPLIT" in summary["notes"][0]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_re_ingesting_the_same_split_purges_nothing():
    """The YTD statement resends every action on every sync. Without the newly-inserted
    check, each of the five daily runs would wipe the history and trigger a refetch —
    turning one repair into a permanent loop against Yahoo."""
    engine, session = await _make_session()
    try:
        await _ingest(session, [_action("FORWARDSPLIT")])
        after_first = await _remaining_dates(session)

        summary = await _ingest(session, [_action("FORWARDSPLIT")])

        assert summary["prices_invalidated"] == 0
        assert summary["notes"] == []
        assert await _remaining_dates(session) == after_first
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_spinoff_leaves_the_price_history_alone():
    engine, session = await _make_session()
    try:
        before = await _remaining_dates(session)

        summary = await _ingest(session, [_action("SPINOFF")])

        assert summary["prices_invalidated"] == 0
        assert await _remaining_dates(session) == before
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_splits_for_one_security_purge_to_the_later_date():
    """A statement can carry more than one action per security; the earlier cutoff must
    not be the one that wins, or rows the later split also restated would survive."""
    engine, session = await _make_session()
    try:
        summary = await _ingest(session, [
            _action("REVERSESPLIT", ib_key="CA-1", action_date=date(2026, 6, 10)),
            _action("FORWARDSPLIT", ib_key="CA-2", action_date=date(2026, 6, 16)),
        ])

        assert summary["securities_purged"] == 1      # one purge, not two
        assert summary["prices_invalidated"] == 4
        assert await _remaining_dates(session) == [date(2026, 6, 17)]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_unresolvable_security_is_skipped_rather_than_raising():
    """`security_id` is nullable — a corporate action can arrive for something we have
    never held. It must not take the sync down with it."""
    engine, session = await _make_session()
    try:
        before = await _remaining_dates(session)
        orphan = _action("FORWARDSPLIT")
        orphan["conid"] = "999999999"

        persisted = await persist_transactions(
            TradeRepository(session), CorporateActionRepository(session),
            {}, [], [orphan],
        )
        summary = await invalidate_prices_for_splits(
            MarketPriceRepository(session), persisted["newly_restating_actions"]
        )

        assert persisted["corporate_actions_saved"] == 1
        assert summary["prices_invalidated"] == 0
        assert await _remaining_dates(session) == before
    finally:
        await session.close()
        await engine.dispose()
