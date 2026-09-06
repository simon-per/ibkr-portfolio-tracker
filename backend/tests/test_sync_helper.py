"""
Regression tests for reconcile_taxlots (backend/app/services/sync_helper.py).

Focus: a per-share cost-basis change (e.g. the S&P Global -> Mobility Global
spinoff, which makes IBKR re-allocate costBasisPrice for days) must NOT be
mistaken for a sale. Reconciliation is by quantity held per security, so price
drift creates no phantom closed lots. Genuine quantity reductions still close
lots FIFO.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers for relationship config
from app.accounts import IBKR
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.models.corporate_action import CorporateAction
from app.repositories.taxlot_repository import TaxLotRepository
from app.repositories.trade_repository import TradeRepository
from app.repositories.corporate_action_repository import CorporateActionRepository
from app.services.sync_helper import reconcile_taxlots, EmptyStatementError


class FakeCurrencyService:
    """Identity EUR conversion so cost_basis == cost_basis_eur in tests."""

    async def convert_to_eur(self, amount, from_currency, target_date):
        return Decimal(str(amount))


async def _make_repo():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    # Create only the tables this test needs. Full-metadata create_all trips a
    # pre-existing duplicate index in an unrelated model (analyst_ratings);
    # production is unaffected because it uses migrations / pre-existing tables.
    tables = [Security.__table__, TaxLot.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    session = AsyncSession(engine, expire_on_commit=False)
    return engine, session, TaxLotRepository(session)


async def _make_repos_with_txns():
    """Like _make_repo but also creates trades + corporate_actions tables and a Security row."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [Security.__table__, TaxLot.__table__, Trade.__table__, CorporateAction.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    session = AsyncSession(engine, expire_on_commit=False)
    # Seed a security (id=1, conid=100) so reconcile can resolve security_id -> conid.
    session.add(Security(
        id=1, isin="US0000000001", symbol="AAA", description="Test Co",
        currency="USD", conid=100, asset_category="STK", exchange="NASDAQ",
    ))
    await session.flush()
    return (
        engine, session,
        TaxLotRepository(session),
        TradeRepository(session),
        CorporateActionRepository(session),
    )


async def _ensure_security(repo, security_id, account=IBKR, currency="USD"):
    """
    Give a seeded lot the `securities` row production guarantees it has.

    `taxlots.security_id` is a NOT NULL foreign key, so a lot without a security is
    not a state the app can reach -- but SQLite does not enforce FKs unless asked,
    so these fixtures used to create one anyway. That went unnoticed until
    `get_open_taxlots(account=...)` had to join `securities` to read the account,
    at which point the orphan lots vanished and the wipe guard stopped firing.
    """
    if await repo.session.get(Security, security_id) is not None:
        return
    repo.session.add(Security(
        id=security_id,
        isin=f"XX{security_id:010d}",
        symbol=f"SEC{security_id}",
        description=f"Security {security_id}",
        currency=currency,
        conid=security_id,
        exchange="NASDAQ",
        account=account,
    ))
    await repo.session.flush()


async def _seed_open_lot(repo, security_id, open_date, quantity, price, currency="USD",
                         account=IBKR):
    await _ensure_security(repo, security_id, account=account, currency=currency)
    q = Decimal(str(quantity))
    p = Decimal(str(price))
    cost = q * p
    return await repo.create({
        "security_id": security_id,
        "open_date": open_date,
        "quantity": q,
        "cost_basis": cost,
        "price_per_unit": p,
        "currency": currency,
        "cost_basis_eur": cost,  # identity conversion
        "is_open": True,
    })


def _incoming(conid, open_date, quantity, price, currency="USD"):
    q = Decimal(str(quantity))
    p = Decimal(str(price))
    return {
        "conid": conid,
        "open_date": open_date,
        "quantity": q,
        "cost_basis": q * p,
        "price_per_unit": p,
        "currency": currency,
        "is_open": True,
    }


@pytest.mark.asyncio
async def test_price_drift_same_quantity_creates_no_closed_lots():
    """The core bug: daily cost-basis drift after a spinoff must not close lots."""
    engine, session, repo = await _make_repo()
    try:
        # Existing open lot: 3 shares @ 492.47
        await _seed_open_lot(repo, security_id=1, open_date=date(2025, 11, 6),
                             quantity=3, price=492.47)

        # Incoming: same 3 shares but price drifted to 468.55 (spinoff reallocation)
        incoming = [_incoming("100", date(2025, 11, 6), 3, 468.55)]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 7, 3),
        )

        assert result["lots_closed_full"] == 0
        assert result["lots_closed_partial"] == 0
        # Exactly one open lot remains (the re-synced one), no closed lots at all.
        assert len(await repo.get_by_security_id(1, is_open=True)) == 1
        assert len(await repo.get_by_security_id(1, is_open=False)) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_sale_of_one_security_closes_it():
    """One security fully sold (absent from a non-empty incoming set) -> its whole position closes."""
    engine, session, repo = await _make_repo()
    try:
        # Two securities held; security 1 is fully sold, security 2 still reported.
        await _seed_open_lot(repo, security_id=1, open_date=date(2025, 11, 6),
                             quantity=3, price=236.38)
        await _seed_open_lot(repo, security_id=2, open_date=date(2025, 11, 6),
                             quantity=5, price=100.00)

        # Incoming still contains security 2 (conid "200"), so the statement is NOT empty.
        incoming = [_incoming("200", date(2025, 11, 6), 5, 100.00)]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"200": 2},  # security 1 no longer reported by IBKR
            taxlots_data=incoming,
            report_to_date=date(2026, 4, 17),
        )

        assert result["lots_closed_full"] == 1
        assert result["lots_closed_partial"] == 0
        closed = await repo.get_by_security_id(1, is_open=False)
        assert len(closed) == 1
        assert closed[0].quantity == Decimal("3")
        assert closed[0].close_date == date(2026, 4, 17)
        assert len(await repo.get_by_security_id(1, is_open=True)) == 0
        # Security 2 is untouched and still open.
        assert len(await repo.get_by_security_id(2, is_open=True)) == 1
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_statement_raises_and_preserves_open_lots():
    """An entirely empty incoming set while lots are held is a failed statement, not a liquidation."""
    engine, session, repo = await _make_repo()
    try:
        await _seed_open_lot(repo, security_id=1, open_date=date(2025, 11, 6),
                             quantity=3, price=236.38)

        with pytest.raises(EmptyStatementError):
            await reconcile_taxlots(
                repo, FakeCurrencyService(),
                conid_to_security_id={},   # nothing reported by IBKR (failed/empty statement)
                taxlots_data=[],
                report_to_date=date(2026, 4, 17),
            )

        # Nothing was deleted or closed — the open lot survives intact.
        open_lots = await repo.get_by_security_id(1, is_open=True)
        assert len(open_lots) == 1
        assert open_lots[0].quantity == Decimal("3")
        assert len(await repo.get_by_security_id(1, is_open=False)) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_sale_closes_sold_qty_fifo():
    """Selling 1 of 5 shares closes 1 share from the OLDEST lot (FIFO)."""
    engine, session, repo = await _make_repo()
    try:
        # Two lots: older 3 @ 492.47, newer 2 @ 530.31  (total 5)
        await _seed_open_lot(repo, 1, date(2025, 11, 6), 3, 492.47)
        await _seed_open_lot(repo, 1, date(2025, 12, 29), 2, 530.31)

        # Incoming total = 4 (sold 1). Lot composition is irrelevant; only total qty matters.
        incoming = [
            _incoming("100", date(2025, 11, 6), 2, 492.47),
            _incoming("100", date(2025, 12, 29), 2, 530.31),
        ]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 6, 22),
        )

        assert result["lots_closed_full"] == 0
        assert result["lots_closed_partial"] == 1
        closed = await repo.get_by_security_id(1, is_open=False)
        assert len(closed) == 1
        assert closed[0].quantity == Decimal("1")
        assert closed[0].open_date == date(2025, 11, 6)  # oldest lot consumed first
        # Proportional cost basis: 1/3 of the older lot's 1477.41 == 492.47
        assert float(closed[0].cost_basis_eur) == pytest.approx(492.47, abs=0.01)
        # 4 shares remain open
        open_lots = await repo.get_by_security_id(1, is_open=True)
        assert sum(l.quantity for l in open_lots) == Decimal("4")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_multiple_same_day_lots_preserved_on_drift():
    """Several distinct lots sharing (security, open_date) are not collapsed or closed."""
    engine, session, repo = await _make_repo()
    try:
        # 3 separate 1-share lots bought the same day at different prices
        await _seed_open_lot(repo, 1, date(2025, 6, 17), 1, 100.00)
        await _seed_open_lot(repo, 1, date(2025, 6, 17), 1, 101.00)
        await _seed_open_lot(repo, 1, date(2025, 6, 17), 1, 102.00)

        # Incoming: same 3 shares, prices drifted
        incoming = [
            _incoming("100", date(2025, 6, 17), 1, 99.50),
            _incoming("100", date(2025, 6, 17), 1, 100.50),
            _incoming("100", date(2025, 6, 17), 1, 101.50),
        ]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 7, 8),
        )

        assert result["lots_closed_full"] == 0
        assert result["lots_closed_partial"] == 0
        assert len(await repo.get_by_security_id(1, is_open=True)) == 3
        assert len(await repo.get_by_security_id(1, is_open=False)) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_forward_split_creates_no_closed_lots():
    """Forward split (share count increases, cost basis conserved) is not a sale."""
    engine, session, repo = await _make_repo()
    try:
        await _seed_open_lot(repo, 1, date(2025, 11, 6), 100, 5.00)  # cost 500
        # 2:1 split: 200 shares @ 2.50, same total cost
        incoming = [_incoming("100", date(2025, 11, 6), 200, 2.50)]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 7, 8),
        )

        assert result["lots_closed_full"] == 0
        assert result["lots_closed_partial"] == 0
        open_lots = await repo.get_by_security_id(1, is_open=True)
        assert sum(l.quantity for l in open_lots) == Decimal("200")
        assert len(await repo.get_by_security_id(1, is_open=False)) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_reverse_split_creates_no_closed_lots():
    """Reverse split (share count drops, cost basis conserved) must NOT record a sale."""
    engine, session, repo = await _make_repo()
    try:
        await _seed_open_lot(repo, 1, date(2025, 11, 6), 100, 5.00)  # cost 500
        # 1:10 reverse split: 10 shares @ 50, same total cost
        incoming = [_incoming("100", date(2025, 11, 6), 10, 50.00)]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 7, 8),
        )

        assert result["lots_closed_full"] == 0
        assert result["lots_closed_partial"] == 0
        open_lots = await repo.get_by_security_id(1, is_open=True)
        assert sum(l.quantity for l in open_lots) == Decimal("10")
        assert len(await repo.get_by_security_id(1, is_open=False)) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_reverse_split_with_cash_in_lieu_no_closure():
    """Reverse split with sub-1% cash-in-lieu of fractional shares is still a split, not a sale."""
    engine, session, repo = await _make_repo()
    try:
        await _seed_open_lot(repo, 1, date(2025, 11, 6), 105, 5.00)  # cost 525
        # 1:10 reverse split of 105 -> 10 shares; ~0.5 share paid as cash-in-lieu
        # remaining cost 522 (~99.4% of 525, within the 1% conservation band)
        incoming = [_incoming("100", date(2025, 11, 6), 10, 52.20)]  # cost 522

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 7, 8),
        )

        assert result["lots_closed_full"] == 0
        assert result["lots_closed_partial"] == 0
        assert len(await repo.get_by_security_id(1, is_open=False)) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_trade_driven_close_uses_real_trade_date_and_source():
    """A SELL trade in the window stamps the closure with its real date and close_source='trade'."""
    engine, session, repo, trade_repo, corp_repo = await _make_repos_with_txns()
    try:
        await _seed_open_lot(repo, 1, date(2025, 11, 6), 10, 50.00)  # cost 500
        # Authoritative SELL trade: sold 4 shares on 2026-06-10 (inside the window).
        await trade_repo.upsert({
            "ib_key": "TX1", "conid": "100", "security_id": 1, "symbol": "AAA",
            "trade_date": date(2026, 6, 10), "buy_sell": "SELL", "quantity": Decimal("-4"),
            "price": Decimal("50"), "proceeds": Decimal("200"), "commission": Decimal("-1"),
            "currency": "USD", "realized_pnl": Decimal("0"), "asset_category": "STK",
        })
        # Incoming reflects 6 shares remaining (cost 300, dropped 40% -> a real sale).
        incoming = [_incoming("100", date(2025, 11, 6), 6, 50.00)]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 6, 30),
            trade_repo=trade_repo,
            corp_action_repo=corp_repo,
            last_sync_date=date(2026, 6, 1),
        )

        assert result["lots_closed_full"] + result["lots_closed_partial"] == 1
        closed = await repo.get_by_security_id(1, is_open=False)
        assert len(closed) == 1
        assert closed[0].quantity == Decimal("4")
        # Real trade date used, NOT the report_to_date (2026-06-30).
        assert closed[0].close_date == date(2026, 6, 10)
        assert closed[0].close_source == "trade"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_corporate_action_reclassifies_even_when_cost_not_conserved():
    """A reverse split in the window suppresses a phantom sale even when cost isn't within the 1% heuristic band."""
    engine, session, repo, trade_repo, corp_repo = await _make_repos_with_txns()
    try:
        await _seed_open_lot(repo, 1, date(2025, 11, 6), 100, 5.00)  # cost 500
        # Reverse split recorded by IBKR in the window.
        await corp_repo.upsert({
            "ib_key": "CA1", "conid": "100", "security_id": 1, "symbol": "AAA",
            "action_date": date(2026, 6, 15), "action_type": "REVERSESPLIT",
            "quantity": Decimal("-90"), "value": Decimal("0"), "proceeds": Decimal("0"),
            "currency": "USD", "description": "AAA 1-for-10 reverse split",
        })
        # Incoming: 10 shares but cost dropped to 480 (96%) -> BELOW the 99% conservation
        # band, so the heuristic alone would wrongly book a sale. The corporate action wins.
        incoming = [_incoming("100", date(2025, 11, 6), 10, 48.00)]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 6, 30),
            trade_repo=trade_repo,
            corp_action_repo=corp_repo,
            last_sync_date=date(2026, 6, 1),
        )

        assert result["lots_closed_full"] == 0
        assert result["lots_closed_partial"] == 0
        assert len(await repo.get_by_security_id(1, is_open=False)) == 0
        open_lots = await repo.get_by_security_id(1, is_open=True)
        assert sum(l.quantity for l in open_lots) == Decimal("10")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_genuine_sale_still_closes_when_cost_drops():
    """A real partial sale (share count AND cost basis drop together) still closes a lot."""
    engine, session, repo = await _make_repo()
    try:
        await _seed_open_lot(repo, 1, date(2025, 11, 6), 100, 5.00)  # cost 500
        # Sold 40 shares at the same price: 60 @ 5 remain (cost 300, dropped 40%)
        incoming = [_incoming("100", date(2025, 11, 6), 60, 5.00)]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 7, 8),
        )

        assert result["lots_closed_full"] + result["lots_closed_partial"] == 1
        closed = await repo.get_by_security_id(1, is_open=False)
        assert len(closed) == 1
        assert closed[0].quantity == Decimal("40")
        open_lots = await repo.get_by_security_id(1, is_open=True)
        assert sum(l.quantity for l in open_lots) == Decimal("60")
    finally:
        await session.close()
        await engine.dispose()

@pytest.mark.asyncio
async def test_a_small_sale_with_a_real_trade_beats_the_cost_conserved_heuristic():
    """
    The cost-conservation heuristic is a *fallback*, and it used to run ahead of
    the SELL-trade lookup. A trim of <=1% of cost basis therefore looked like a
    consolidation and recorded no closure at all, even with IBKR's own SELL
    execution sitting in `trades` for that window — losing the disposal from
    XIRR's proceeds inflow, the attribution's disposal term and the tax report.
    """
    engine, session, repo, trade_repo, corp_repo = await _make_repos_with_txns()
    try:
        await _seed_open_lot(repo, 1, date(2025, 11, 6), 200, 100.00)  # cost 20,000
        await trade_repo.upsert({
            "ib_key": "TX-TRIM", "conid": "100", "security_id": 1, "symbol": "AAA",
            "trade_date": date(2026, 6, 12), "buy_sell": "SELL", "quantity": Decimal("-2"),
            "price": Decimal("100"), "proceeds": Decimal("200"), "commission": Decimal("-1"),
            "currency": "USD", "realized_pnl": Decimal("5"), "asset_category": "STK",
        })
        # 198 @ 100 = 19,800, which is exactly 99% of 20,000 -> inside the band.
        incoming = [_incoming("100", date(2025, 11, 6), 198, 100.00)]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 6, 30),
            trade_repo=trade_repo,
            corp_action_repo=corp_repo,
            last_sync_date=date(2026, 6, 1),
        )

        assert result["lots_closed_full"] + result["lots_closed_partial"] == 1
        closed = await repo.get_by_security_id(1, is_open=False)
        assert len(closed) == 1
        assert closed[0].quantity == Decimal("2")
        assert closed[0].close_date == date(2026, 6, 12)   # the real trade date
        assert closed[0].close_source == "trade"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_cost_conservation_still_suppresses_a_sale_with_no_sell_on_record():
    """
    The reorder must not make the heuristic unreachable: with the trade repo
    wired but holding no SELL for the window, a conserved cost basis is still a
    consolidation and must record no closure.
    """
    engine, session, repo, trade_repo, corp_repo = await _make_repos_with_txns()
    try:
        await _seed_open_lot(repo, 1, date(2025, 11, 6), 200, 100.00)  # cost 20,000
        # A BUY in the window, and a SELL of a *different* security: neither counts.
        await trade_repo.upsert({
            "ib_key": "TX-BUY", "conid": "100", "security_id": 1, "symbol": "AAA",
            "trade_date": date(2026, 6, 12), "buy_sell": "BUY", "quantity": Decimal("1"),
            "price": Decimal("100"), "proceeds": Decimal("-100"), "commission": Decimal("-1"),
            "currency": "USD", "realized_pnl": Decimal("0"), "asset_category": "STK",
        })
        await trade_repo.upsert({
            "ib_key": "TX-OTHER", "conid": "999", "security_id": None, "symbol": "ZZZ",
            "trade_date": date(2026, 6, 12), "buy_sell": "SELL", "quantity": Decimal("-9"),
            "price": Decimal("10"), "proceeds": Decimal("90"), "commission": Decimal("-1"),
            "currency": "USD", "realized_pnl": Decimal("0"), "asset_category": "STK",
        })
        incoming = [_incoming("100", date(2025, 11, 6), 198, 100.00)]

        result = await reconcile_taxlots(
            repo, FakeCurrencyService(),
            conid_to_security_id={"100": 1},
            taxlots_data=incoming,
            report_to_date=date(2026, 6, 30),
            trade_repo=trade_repo,
            corp_action_repo=corp_repo,
            last_sync_date=date(2026, 6, 1),
        )

        assert result["lots_closed_full"] == 0
        assert result["lots_closed_partial"] == 0
        assert len(await repo.get_by_security_id(1, is_open=False)) == 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("n_securities,lots_each", [(10, 3), (30, 1)])
async def test_reconcile_round_trips_do_not_scale_with_security_count(n_securities, lots_each):
    """
    Phase A issued one get_by_security_id() per security for rows
    get_open_taxlots() had already returned; Phase B one delete per security, each
    of which itself SELECTed and then ORM-deleted row by row; and Phase C refreshed
    every lot it wrote, for an object both call sites discard.

    Measured on the same 30 lots, before -> after:
        10 securities x 3 lots:  51 SELECT + 10 DELETE + 30 INSERT (91) -> 1 + 1 + 30 (32)
        30 securities x 1 lot:   91 SELECT + 30 DELETE + 30 INSERT (151) -> 1 + 1 + 30 (32)

    Both shapes now cost the same, which is the real property: nothing scales with
    the number of securities any more. Counted, not timed — a wall-clock assertion
    would be flaky and would not say why.
    """
    from sqlalchemy import event

    engine, session, repo = await _make_repo()
    try:
        conid_map = {}
        for sec_id in range(1, n_securities + 1):
            conid_map[str(100 + sec_id)] = sec_id
            for i in range(lots_each):
                await _seed_open_lot(repo, sec_id, date(2025, 11, 5 + i * 10), 10, 5.00)
        await session.commit()

        statements = []

        def _on(conn, cursor, statement, params, context, executemany):
            statements.append(statement.lstrip().split()[0].upper())

        event.listen(engine.sync_engine, "before_cursor_execute", _on)
        try:
            # Same quantities back, so no closures: this isolates Phases A-C.
            incoming = [
                _incoming(str(100 + sec_id), date(2025, 11, 5 + i * 10), 10, 5.00)
                for sec_id in range(1, n_securities + 1) for i in range(lots_each)
            ]
            await reconcile_taxlots(
                repo, FakeCurrencyService(),
                conid_to_security_id=conid_map,
                taxlots_data=incoming,
                report_to_date=date(2026, 6, 30),
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _on)

        total_lots = n_securities * lots_each
        assert statements.count("SELECT") == 1, "the open-lot load, and nothing per-security"
        assert statements.count("DELETE") == 1, "one batched delete for every security"
        # One INSERT per lot remains (the ORM emits row-wise inserts); what is gone
        # is the refresh SELECT that used to accompany each one.
        assert statements.count("INSERT") == total_lots
        assert len(statements) == total_lots + 2
    finally:
        await session.close()
        await engine.dispose()
