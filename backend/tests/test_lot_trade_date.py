"""
A lot opened by a BUY carries that trade's own trade date (review item C1, 2026-11-08).

`openDateTime` is US Eastern and `tradeDate` exchange-local: a KRX buy at 09:05 in Seoul
executes at 20:05 the evening before in New York. The lot was dated by the evening, the
cash by the trade date, so for one day the account held the shares and the money that
paid for them — the Total Value spiked by the purchase. The ingest now dates the lot by
the BUY executed at its `openDateTime` (`date_lots_by_their_buys`), keeps that date once
the trade has left the statement window, and the first sync that redates a lot books no
phantom sale. Offline: EUR-only rows, so no FX lookup leaves the process.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.cash_flow import DEPOSIT_WITHDRAW, CashFlow
from app.models.market_price import MarketPrice
from app.models.taxlot import TaxLot
from app.models.trade import Trade, buy_sign
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.ibkr_service import IBKRService
from app.services.portfolio_service import PortfolioService
from app.services.sync_helper import ingest_flex_statement
from tests.memory_db import memory_engine

HYNIX = "1009"


def _pos(qty, cost, opened, report):
    return (
        f'<OpenPosition assetCategory="STK" subCategory="COMMON" symbol="000660" '
        f'conid="{HYNIX}" isin="KR7000660001" description="SK HYNIX" currency="EUR" '
        f'listingExchange="KSE" position="{qty}" costBasisPrice="{cost / qty}" '
        f'costBasisMoney="{cost}" openDateTime="{opened}" levelOfDetail="LOT" '
        f'reportDate="{report}" />'
    )


def _trade(tx, day, side, qty, price, executed=None):
    when = f'dateTime="{executed}" ' if executed else ""
    return (
        f'<Trade assetCategory="STK" subCategory="COMMON" conid="{HYNIX}" '
        f'symbol="000660" transactionID="{tx}" tradeID="TR-{tx}" tradeDate="{day}" '
        f'{when}reportDate="{day}" buySell="{side}" quantity="{qty}" '
        f'tradePrice="{price}" proceeds="{-qty * price}" ibCommission="0" '
        f'currency="EUR" fifoPnlRealized="0" levelOfDetail="EXECUTION" />'
    )


def _statement(to_date, positions, trades=""):
    return f"""<FlexQueryResponse queryName="Portfolio" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="2026-09-20" toDate="{to_date}"
 period="Custom" whenGenerated="{to_date};120000">
 <SecuritiesInfo>
  <SecurityInfo assetCategory="STK" subCategory="COMMON" symbol="000660" conid="{HYNIX}"
   isin="KR7000660001" description="SK HYNIX" currency="EUR" listingExchange="KSE" />
 </SecuritiesInfo>
 <OpenPositions>{positions}</OpenPositions>
 <Trades>{trades}</Trades>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>""".encode()


# The buy: 09:05 in Seoul on 13 October, 20:05 in New York on the 12th.
EVENING = "20261012;200512"
BUY = dict(tx="TX-BUY", day="2026-10-13", side="BUY", qty=10, price=90)


async def _ingest(session, xml):
    flex_data = IBKRService(token="t", query_id="q").parse_flex_xml(xml)
    await ingest_flex_statement(session, flex_data)
    await session.commit()


async def _lots(session):
    rows = (
        await session.execute(
            select(
                TaxLot.open_date, TaxLot.quantity, TaxLot.is_open, TaxLot.close_source
            ).order_by(TaxLot.open_date, TaxLot.is_open)
        )
    ).all()
    return [(d, int(q), o, s) for d, q, o, s in rows]


@pytest.mark.asyncio
async def test_a_lot_opened_by_a_buy_carries_the_trades_date():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        await _ingest(
            session,
            _statement(
                "2026-10-14",
                _pos(10, 900, EVENING, "2026-10-14"),
                _trade(**BUY, executed=EVENING),
            ),
        )
        assert await _lots(session) == [(date(2026, 10, 13), 10, True, None)]
        trade = (await session.execute(select(Trade))).scalar_one()
        # 20:05 EDT is 00:05 UTC the next day.
        assert trade.executed_at == datetime(2026, 10, 13, 0, 5, 12)

        # A later statement whose window no longer holds the trade: the date stays.
        await _ingest(
            session, _statement("2026-11-20", _pos(10, 900, EVENING, "2026-11-20"))
        )
        assert await _lots(session) == [(date(2026, 10, 13), 10, True, None)]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_sync_that_first_redates_a_lot_books_no_phantom_sale():
    """
    The book before the fix: the lot under its evening date, the trade stored without its
    instant. The next statement delivers the instant and also a real sale of another lot
    of the same security, so the reconcile classifies every missing share against a SELL
    on record — the redated lot must not be among them.
    """
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        older = _pos(5, 400, "20260921;010000", "2026-10-14")
        await _ingest(
            session,
            _statement(
                "2026-10-14",
                older + _pos(10, 900, EVENING, "2026-10-14"),
                _trade(**BUY),
            ),
        )
        assert await _lots(session) == [
            (date(2026, 9, 21), 5, True, None),
            (date(2026, 10, 12), 10, True, None),
        ]

        await _ingest(
            session,
            _statement(
                "2026-10-21",
                _pos(10, 900, EVENING, "2026-10-21"),
                _trade(**BUY, executed=EVENING)
                + _trade("TX-SELL", "2026-10-20", "SELL", -5, 95),
            ),
        )
        assert await _lots(session) == [
            (date(2026, 9, 21), 5, False, "trade"),
            (date(2026, 10, 13), 10, True, None),
        ]
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_every_buy_opened_lot_carries_its_trade_date_and_the_value_stays_flat():
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        await _ingest(
            session,
            _statement(
                "2026-10-14",
                _pos(10, 900, EVENING, "2026-10-14"),
                _trade(**BUY, executed=EVENING),
            ),
        )
        # The family question, over what the ingest stored.
        buys = {
            (t.executed_at, t.trade_date)
            for t in (await session.execute(select(Trade))).scalars()
            if buy_sign(t) == 1
        }
        lots = (await session.execute(select(TaxLot))).scalars().all()
        assert {lot.open_date for lot in lots} <= {d for _, d in buys}

        # The money for it, and a price on every day around the buy.
        session.add(
            CashFlow(
                ib_key="D1",
                flow_date=date(2026, 10, 9),
                flow_type=DEPOSIT_WITHDRAW,
                amount=Decimal("1000"),
                currency="EUR",
                amount_eur=Decimal("1000"),
            )
        )
        for day in (9, 12, 13, 14):
            session.add(
                MarketPrice(
                    security_id=lots[0].security_id,
                    date=date(2026, 10, day),
                    close_price=Decimal("90"),
                    currency="EUR",
                    source="test",
                )
            )
        await session.flush()
        await AppSettingsRepository(session).widen_cash_flows_covered_from(
            date(2026, 10, 9)
        )
        await session.commit()
        timeline = await PortfolioService(session).get_portfolio_value_over_time(
            date(2026, 10, 9),
            date(2026, 10, 14),
        )
        values = {r["date"]: r["total_value_eur"] for r in timeline}
        assert values["2026-10-12"] == pytest.approx(1000)
        assert values["2026-10-13"] == pytest.approx(1000)
    finally:
        await session.close()
        await engine.dispose()
