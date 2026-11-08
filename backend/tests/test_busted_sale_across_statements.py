"""
A sale IBKR busts in a later statement takes its closed lot with it (review item I3,
2026-11-08).

The SELL closes its lot in the sync that carries it. The CANCELSELL arrives one statement
later with the sale's trade date, and the lot is reported open again under its old open
date — the closed lot stayed beside it, so the shares were held twice on every day before
the "sale". Offline, through `reconcile_taxlots` and the helpers of test_sync_helper.
"""

from datetime import date

import pytest

from app.services.sync_helper import reconcile_taxlots
from tests.test_sync_helper import (
    FakeCurrencyService,
    _all_lots,
    _held_on,
    _incoming,
    _make_repos_with_txns,
    _seed_open_lot,
    _trade,
)

OPENED = date(2025, 1, 6)
SOLD = date(2026, 6, 10)


async def _sync(repo, trade_repo, corp_repo, incoming, start, end):
    return await reconcile_taxlots(
        repo,
        FakeCurrencyService(),
        conid_to_security_id={"100": 1, "2": 2},
        taxlots_data=incoming,
        report_to_date=end,
        trade_repo=trade_repo,
        corp_action_repo=corp_repo,
        last_sync_date=start,
    )


def _shape(lots):
    return sorted(
        (lot.open_date, int(lot.quantity), lot.is_open, lot.close_date) for lot in lots
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sold, kept_open", [(10, 0), (4, 6)])
async def test_a_bust_in_the_next_statement_reopens_the_lot_once(sold, kept_open):
    engine, session, repo, trade_repo, corp_repo = await _make_repos_with_txns()
    try:
        await _seed_open_lot(repo, 1, OPENED, 10, 100)
        await _seed_open_lot(repo, 2, OPENED, 5, 100)
        other = _incoming("2", OPENED, 5, 100)

        await _trade(trade_repo, "S1", "SELL", SOLD, sold, 120)
        first = [other] + (
            [_incoming("100", OPENED, kept_open, 100)] if kept_open else []
        )
        await _sync(
            repo, trade_repo, corp_repo, first, date(2026, 6, 9), date(2026, 6, 12)
        )
        assert _held_on(await _all_lots(repo), date(2026, 1, 1)) == 10

        await _trade(trade_repo, "S1c", "CANCELSELL", SOLD, sold, 120)
        result = await _sync(
            repo,
            trade_repo,
            corp_repo,
            [_incoming("100", OPENED, 10, 100), other],
            date(2026, 6, 12),
            date(2026, 6, 15),
        )
        lots = await _all_lots(repo)
        assert _shape(lots) == [(OPENED, 10, True, None)]
        assert _held_on(lots, date(2026, 1, 1)) == 10
        assert result["lots_unclosed"] == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_sale_that_still_stands_keeps_its_closed_lot():
    """Two sales the same day, one busted: the one that stands keeps its closure."""
    engine, session, repo, trade_repo, corp_repo = await _make_repos_with_txns()
    try:
        await _seed_open_lot(repo, 1, OPENED, 10, 100)
        await _trade(trade_repo, "S1", "SELL", SOLD, 3, 120)
        await _trade(trade_repo, "S2", "SELL", SOLD, 4, 121)
        await _sync(
            repo,
            trade_repo,
            corp_repo,
            [_incoming("100", OPENED, 3, 100)],
            date(2026, 6, 9),
            date(2026, 6, 12),
        )
        assert _shape(await _all_lots(repo)) == [
            (OPENED, 3, True, None),
            (OPENED, 7, False, SOLD),
        ]

        await _trade(trade_repo, "S2c", "CANCELSELL", SOLD, 4, 121)
        await _sync(
            repo,
            trade_repo,
            corp_repo,
            [_incoming("100", OPENED, 7, 100)],
            date(2026, 6, 12),
            date(2026, 6, 15),
        )
        lots = await _all_lots(repo)
        assert _shape(lots) == [(OPENED, 3, False, SOLD), (OPENED, 7, True, None)]
        assert _held_on(lots, date(2026, 1, 1)) == 10
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_rebuy_on_a_new_date_does_not_undo_a_sale():
    engine, session, repo, trade_repo, corp_repo = await _make_repos_with_txns()
    try:
        await _seed_open_lot(repo, 1, OPENED, 10, 100)
        await _trade(trade_repo, "S1", "SELL", SOLD, 10, 120)
        await _seed_open_lot(repo, 2, OPENED, 5, 100)
        other = _incoming("2", OPENED, 5, 100)
        await _sync(
            repo, trade_repo, corp_repo, [other], date(2026, 6, 9), date(2026, 6, 12)
        )
        await _trade(trade_repo, "B2", "BUY", date(2026, 6, 14), 10, 110)
        await _sync(
            repo,
            trade_repo,
            corp_repo,
            [_incoming("100", date(2026, 6, 14), 10, 110), other],
            date(2026, 6, 12),
            date(2026, 6, 15),
        )
        assert _shape(await _all_lots(repo)) == [
            (OPENED, 10, False, SOLD),
            (date(2026, 6, 14), 10, True, None),
        ]
    finally:
        await session.close()
        await engine.dispose()
