"""
A watchlist note or target price can be cleared (review item U2, 2026-11-08).

The client turned an emptied field into "not sent" and the PATCH route skipped None, so
neither could ever be removed. The route now applies what the request sent
(`model_fields_set`): an empty note and a null target clear; a field left out stays.
"""
import pytest

import app.models  # noqa: F401
from app.models.watchlist_item import WatchlistItem
from app.routers.watchlist import update_watchlist_item
from app.schemas.portfolio import UpdateWatchlistItemRequest


@pytest.mark.asyncio
async def test_an_emptied_note_and_a_null_target_clear(db):
    item = WatchlistItem(yahoo_ticker="AAA", notes="buy below 90", target_price=90.0)
    db.add(item)
    await db.commit()

    row = await update_watchlist_item(
        item.id, UpdateWatchlistItemRequest(notes="", target_price=None), db=db)
    assert (row.notes, row.target_price) == (None, None)


@pytest.mark.asyncio
async def test_a_field_left_out_is_left_alone(db):
    item = WatchlistItem(yahoo_ticker="AAA", notes="keep", target_price=90.0)
    db.add(item)
    await db.commit()

    row = await update_watchlist_item(
        item.id, UpdateWatchlistItemRequest(target_price=95.0), db=db)
    assert (row.notes, row.target_price) == ("keep", 95.0)
