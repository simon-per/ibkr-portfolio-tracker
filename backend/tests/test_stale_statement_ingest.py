"""
A Flex statement OLDER than the book must not rewind it (GitHub issue #13).

`reconcile_taxlots` treats OpenPositions as the truth *now*, but an older statement's
OpenPositions are the truth on its own `to_date`. Applying them used to:

- close a position bought after the old `to_date` as a heuristic "sale" stamped with the
  OLD to_date (before its own open date), cost not conserved;
- reopen a position sold after the old `to_date` next to its closed lot;
- move `last_sync_to_date` backwards, so the next real sync's trade window reached back
  over the sale and closed it a second time (double realized P&L).

`<OpenDividendAccruals>` is a snapshot too, stored by `replace_all`.

The path is the shared one: `app/cli/ingest_flex_xml.py` (a prior-range Client Portal
download) and the API sync (the day after a custom range ending today) both go through
`ingest_flex_statement`. The additive ledgers of an older statement are still ingested.

Also here: the gap alarm keys on what a run covered, not on when it finished.
Offline: EUR-only rows, so no FX lookup leaves the process.
"""
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models.dividend_accrual import DividendAccrual
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.ibkr_service import IBKRService
from app.services.sync_helper import ingest_flex_statement

# A bought after June, B held throughout, C sold in July, D bought on the newest day only.
A, B, C, D = "1001", "1002", "1003", "1004"


def _sec(conid, symbol, isin):
    return (f'<SecurityInfo assetCategory="STK" subCategory="COMMON" symbol="{symbol}" '
            f'conid="{conid}" isin="{isin}" description="{symbol}" currency="EUR" '
            f'listingExchange="IBIS" />')


def _pos(conid, symbol, isin, qty, cost, opened, report):
    return (f'<OpenPosition assetCategory="STK" subCategory="COMMON" symbol="{symbol}" '
            f'conid="{conid}" isin="{isin}" description="{symbol}" currency="EUR" '
            f'listingExchange="IBIS" position="{qty}" costBasisPrice="{cost / qty}" '
            f'costBasisMoney="{cost}" openDateTime="{opened}" levelOfDetail="LOT" '
            f'reportDate="{report}" />')


def _trade(conid, symbol, tx, day, side, qty, price):
    return (f'<Trade assetCategory="STK" subCategory="COMMON" conid="{conid}" '
            f'symbol="{symbol}" transactionID="{tx}" tradeID="TR-{tx}" tradeDate="{day}" '
            f'reportDate="{day}" buySell="{side}" quantity="{qty}" tradePrice="{price}" '
            f'proceeds="{-qty * price}" ibCommission="-1" currency="EUR" '
            f'fifoPnlRealized="0" levelOfDetail="EXECUTION" />')


def _accrual(conid, symbol, ex_date, pay_date, gross):
    return (f'<OpenDividendAccrual assetCategory="STK" subCategory="COMMON" '
            f'conid="{conid}" symbol="{symbol}" currency="EUR" exDate="{ex_date}" '
            f'payDate="{pay_date}" quantity="5" grossAmount="{gross}" tax="0" '
            f'netAmount="{gross}" />')


def _statement(to_date: str, positions: str, trades: str = "", cash: str = "",
               accruals: str = "") -> bytes:
    return f"""<FlexQueryResponse queryName="Portfolio" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="2026-01-01" toDate="{to_date}"
 period="Custom" whenGenerated="{to_date};120000">
 <SecuritiesInfo>
  {_sec(A, "AAA", "DE000000000A")}
  {_sec(B, "BBB", "DE000000000B")}
  {_sec(C, "CCC", "DE000000000C")}
  {_sec(D, "DDD", "DE000000000D")}
 </SecuritiesInfo>
 <OpenPositions>{positions}</OpenPositions>
 <Trades>{trades}</Trades>
 <CashTransactions>{cash}</CashTransactions>
 <OpenDividendAccruals>{accruals}</OpenDividendAccruals>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>""".encode()


JUNE_BOOK = (_pos(B, "BBB", "DE000000000B", 5, 500, "20260105", "2026-06-30")
             + _pos(C, "CCC", "DE000000000C", 8, 800, "20260105", "2026-06-30"))
# The book on 2026-06-30: B and C held.
JUNE = _statement("2026-06-30", JUNE_BOOK)
# The same period downloaded again later, carrying history the book has not seen yet: a
# trade, a dividend, and an accrual for C that was announced then and has since lapsed.
JUNE_WITH_HISTORY = _statement(
    "2026-06-30", JUNE_BOOK,
    trades=_trade(B, "BBB", "TX-B", "2026-01-05", "BUY", 5, 100),
    cash=('<CashTransaction type="Dividends" assetCategory="STK" subCategory="COMMON" '
          f'conid="{B}" symbol="BBB" transactionID="CT-DIV-B" dateTime="2026-06-15" '
          'settleDate="2026-06-15" amount="7.50" currency="EUR" '
          'description="BBB CASH DIVIDEND" levelOfDetail="DETAIL" />'),
    accruals=_accrual(C, "CCC", "2026-06-25", "2026-07-20", 4),
)
# The book on 2026-07-24: A bought 07-10, B unchanged, C sold 07-15 (SELL on record).
SELL_C = _trade(C, "CCC", "TX-C", "2026-07-15", "SELL", -8, 120)
BUY_A = _trade(A, "AAA", "TX-A", "2026-07-10", "BUY", 10, 100)


def _july(to_date, extra_positions="", extra_trades=""):
    return _statement(
        to_date,
        _pos(A, "AAA", "DE000000000A", 10, 1000, "20260710", to_date)
        + _pos(B, "BBB", "DE000000000B", 5, 500, "20260105", to_date)
        + extra_positions,
        BUY_A + SELL_C + extra_trades,
        accruals=_accrual(B, "BBB", "2026-07-20", "2026-08-15", 6),
    )


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, AsyncSession(engine, expire_on_commit=False)


async def _ingest(session, xml):
    flex_data = IBKRService(token="t", query_id="q").parse_flex_xml(xml)
    result = await ingest_flex_statement(session, flex_data)
    await session.commit()
    return result


async def _lots(session):
    rows = (await session.execute(
        select(Security.symbol, TaxLot.is_open, TaxLot.quantity, TaxLot.close_date,
               TaxLot.close_source)
        .join(Security, Security.id == TaxLot.security_id)
        .order_by(Security.symbol, TaxLot.is_open)
    )).all()
    return [tuple(r) for r in rows]


async def _accruals(session):
    rows = (await session.execute(
        select(Security.symbol, DividendAccrual.pay_date, DividendAccrual.net_amount_eur)
        .join(Security, Security.id == DividendAccrual.security_id)
        .order_by(Security.symbol)
    )).all()
    return [tuple(r) for r in rows]


async def _count(session, model, *where):
    return (await session.execute(select(func.count()).select_from(model).where(*where))
            ).scalar_one()


@pytest.mark.asyncio
async def test_an_older_statement_does_not_rewind_the_book():
    engine, session = await _session()
    try:
        await _ingest(session, JUNE)
        await _ingest(session, _july("2026-07-24"))
        before = await _lots(session)
        accruals_before = await _accruals(session)
        # Sanity: C closed once by its SELL, A and B open; July's accrual is B's.
        assert [(s, o) for s, o, *_ in before] == [
            ("AAA", True), ("BBB", True), ("CCC", False)
        ]
        assert [s for s, *_ in accruals_before] == ["BBB"]

        # A Client Portal download of an older range, fed through the shared path.
        result = await _ingest(session, JUNE_WITH_HISTORY)
        after_old = await _lots(session)
        last_sync = await AppSettingsRepository(session).get_last_sync_to_date()

        assert last_sync == date(2026, 7, 24), "last_sync_to_date moved backwards"
        assert after_old == before, "an older statement rewrote the open book"
        assert await _accruals(session) == accruals_before, \
            "an older statement's accrual snapshot replaced the current one"
        assert any("not applied" in w for w in result["warnings"]), result["warnings"]
        assert result["taxlots_synced"] == 0
        assert result["total_cost_basis_eur"] is None
        # The additive ledgers are what a prior-range download is for.
        assert await _count(session, Trade, Trade.ib_key == "TX-B") == 1
        assert await _count(session, DividendPayment) == 1

        # The next scheduled API statement.
        await _ingest(session, _july("2026-07-27"))
        after_next = await _lots(session)

        closed_c = [r for r in after_next if r[0] == "CCC" and not r[1]]
        assert len(closed_c) == 1, "C's sale was booked twice"
        phantom_a = [r for r in after_next if r[0] == "AAA" and not r[1]]
        assert phantom_a == [], "A got a phantom heuristic disposal"
        assert [r for r in after_next if r[1]] == [
            ("AAA", True, 10, None, None), ("BBB", True, 5, None, None)
        ]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_api_statement_after_a_custom_range_to_today_keeps_that_days_buy():
    """
    The second route in: a custom-range download ending today (D) is ingested, and the
    next scheduled statement comes back with `to_date = D-1`. Without the guard, D's
    buy was absent from it and closed as a heuristic sale.
    """
    buy_d = _trade(D, "DDD", "TX-D", "2026-07-24", "BUY", 3, 50)
    pos_d = _pos(D, "DDD", "DE000000000D", 3, 150, "20260724", "2026-07-24")
    engine, session = await _session()
    try:
        await _ingest(session, JUNE)
        await _ingest(session, _july("2026-07-24", pos_d, buy_d))     # custom range, to today
        before = await _lots(session)

        result = await _ingest(session, _july("2026-07-23"))           # the API's, a day older

        assert await _lots(session) == before
        assert ("DDD", True, 3, None, None) in before
        assert await AppSettingsRepository(session).get_last_sync_to_date() == date(2026, 7, 24)
        assert any("not applied" in w for w in result["warnings"])
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_last_sync_date_setter_never_moves_back():
    engine, session = await _session()
    try:
        repo = AppSettingsRepository(session)
        await repo.set_last_sync_to_date(date(2026, 7, 24))
        await repo.set_last_sync_to_date(date(2026, 6, 30))
        assert await repo.get_last_sync_to_date() == date(2026, 7, 24)
        await repo.set_last_sync_to_date(date(2026, 7, 27))
        assert await repo.get_last_sync_to_date() == date(2026, 7, 27)
    finally:
        await session.close()
        await engine.dispose()


# --- the gap alarm keys on what a run covered, not on when it finished ----------------

@pytest.mark.asyncio
async def test_an_old_manual_statement_does_not_silence_the_gap_alarm():
    """
    `find_flex_generation_gap` used to count from the newest successful run's
    `finished_at`, and an `ibkr_manual_xml` ingest counts. A Client Portal download of an
    OLD range ingested today therefore read as "fetched today" while the trades since the
    last API statement were still at risk. `find_stale_ibkr_sync` reads the same clock.
    """
    from datetime import datetime, timedelta
    from app.models.sync_run import SyncRun
    from app.services.scheduler_service import SchedulerService

    now = datetime(2026, 8, 8, 12, 0, 0)
    engine, session = await _session()
    try:
        # Last API statement ten days ago, a 3-day window ending the day before it.
        session.add(SyncRun(sync_type="full_sync", status="success",
                            finished_at=now - timedelta(days=10),
                            details={"ibkr_result": {"data_from": "2026-07-26",
                                                     "data_to": "2026-07-28"}}))
        await session.flush()
        assert await SchedulerService().find_flex_generation_gap(session, as_of=now), \
            "precondition: ten days without a statement against a 3-day window warns"
        assert await SchedulerService().find_stale_ibkr_sync(session, as_of=now)

        # An hour ago: a prior-range download that covers nothing after June.
        session.add(SyncRun(sync_type="ibkr_manual_xml", status="success",
                            finished_at=now - timedelta(hours=1),
                            details={"data_from": "2026-01-01", "data_to": "2026-06-09"}))
        await session.flush()

        warnings = await SchedulerService().find_flex_generation_gap(session, as_of=now)
        assert warnings, "an old-range manual ingest silenced the gap alarm"
        assert "2026-07-29" in warnings[0]
        assert await SchedulerService().find_stale_ibkr_sync(session, as_of=now), \
            "an old-range manual ingest reset the staleness clock"

        # A manual download that does reach the present closes the gap, as before.
        session.add(SyncRun(sync_type="ibkr_manual_xml", status="success",
                            finished_at=now - timedelta(minutes=30),
                            details={"data_from": "2026-07-01", "data_to": "2026-08-07"}))
        await session.flush()
        assert await SchedulerService().find_flex_generation_gap(session, as_of=now) == []
        assert await SchedulerService().find_stale_ibkr_sync(session, as_of=now) == []
    finally:
        await session.close()
        await engine.dispose()
