"""
GitHub issue #32: what one watchlist response costs. The ticker index resolved every
Yahoo-eligible security ever held through `_get_yahoo_ticker`, one `ticker_mappings` SELECT
each, so a 3-item watchlist cost 14 statements over 10 securities and 204 over 200; and
`isins_by_yahoo_ticker` read every basket line into Python. Now the index resolves through
`MarketDataService.yahoo_tickers_for` (mappings read once) and the basket read is narrowed in
SQL. Counted with a cursor listener; the route functions are called directly (no lifespan, no
network).

Also pins the tie: a sold security and a newer held one resolving to the same Yahoo ticker
give the row the HELD one's id (it was `Security.id` order, so the sold one's).
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.etf_basket import EtfHolding
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.watchlist_item import WatchlistItem
from app.routers.watchlist import _responses, get_watchlist
from app.services.watchlist_service import WatchlistService
from tests.memory_db import memory_engine

# Statements one GET issues for the fixed 3-item watchlist below, whatever the security
# count: base currency, the items, the securities, the mappings, the basket candidates.
GET_STATEMENTS = 5


async def _session():
    engine = await memory_engine()
    return engine, AsyncSession(engine, expire_on_commit=False)


def _security(s, sid, symbol, sold):
    s.add(Security(id=sid, isin=f"US{sid:010d}", symbol=symbol, description="x",
                   currency="USD", conid=1000 + sid, asset_category="STK", exchange="NASDAQ"))
    s.add(TaxLot(security_id=sid, open_date=date(2026, 1, 5),
                 close_date=date(2026, 2, 5) if sold else None, is_open=not sold,
                 quantity=Decimal("1"), cost_basis=Decimal("1"), cost_basis_eur=Decimal("1"),
                 price_per_unit=Decimal("1"), currency="USD"))


async def _seed(s, n_securities: int, n_items: int = 3, n_holdings: int = 200):
    for i in range(1, n_securities + 1):
        _security(s, i, f"S{i}", sold=i % 2 == 0)
    for j in range(n_items):
        s.add(WatchlistItem(yahoo_ticker=f"W{j}"))
    for k in range(n_holdings):
        s.add(EtfHolding(fund_isin="IE00B4L5Y983", line_no=k + 1, constituent_ticker=f"C{k}",
                         constituent_isin=f"US9{k:09d}", constituent_name="c",
                         weight_pct=Decimal("0.01")))
    await s.commit()


class _Counter:
    def __init__(self, engine):
        self.engine, self.statements = engine, []

    def _count(self, conn, cursor, statement, *_):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(self.engine.sync_engine, "before_cursor_execute", self._count)
        return self

    def __exit__(self, *_):
        event.remove(self.engine.sync_engine, "before_cursor_execute", self._count)


async def _measure(n_securities: int):
    engine, s = await _session()
    try:
        await _seed(s, n_securities)
        with _Counter(engine) as counter:
            rows = await get_watchlist(db=s)
    finally:
        await s.close()
        await engine.dispose()
    assert len(rows) == 3
    mapping_selects = sum("ticker_mappings" in st for st in counter.statements)
    return len(counter.statements), mapping_selects


@pytest.mark.asyncio
@pytest.mark.parametrize("n_securities", [10, 200])
async def test_a_watchlist_read_costs_the_same_whatever_was_ever_held(n_securities):
    statements, mapping_selects = await _measure(n_securities)
    assert mapping_selects == 1, "the mappings are read once, not once per security"
    assert statements == GET_STATEMENTS, (
        f"GET /api/watchlist issued {statements} statements over {n_securities} securities"
    )


@pytest.mark.asyncio
async def test_a_post_reuses_the_index_its_sync_built():
    """POST resolves the held index for the RSI (`sync_item` -> `_held_closes`) and then
    serves the row; one service, so the securities and mappings are read once."""
    engine, s = await _session()
    try:
        await _seed(s, 10)
        service = WatchlistService(s)
        items = await service.get_all_items()
        await service.security_ids_by_ticker(held_only=True)
        with _Counter(engine) as counter:
            await _responses(s, items, service)
    finally:
        await s.close()
        await engine.dispose()
    rebuilt = [st for st in counter.statements
               if "ticker_mappings" in st or "FROM securities" in st]
    assert not rebuilt, f"the response rebuilt the ticker index: {rebuilt}"


@pytest.mark.asyncio
async def test_a_sold_and_a_held_security_sharing_a_ticker_resolve_to_the_held_one():
    engine, s = await _session()
    try:
        _security(s, 1, "ABC", sold=True)
        _security(s, 2, "ABC", sold=False)
        s.add(WatchlistItem(yahoo_ticker="ABC"))
        await s.commit()
        rows = await get_watchlist(db=s)
        assert rows[0].security_id == 2, "the row points at the sold security, not the held one"
        service = WatchlistService(s)
        assert (await service.security_ids_by_ticker(held_only=True)) == {"ABC": 2}
    finally:
        await s.close()
        await engine.dispose()
