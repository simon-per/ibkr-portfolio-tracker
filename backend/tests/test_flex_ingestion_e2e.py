"""
End-to-end ingestion test over a real-shaped Flex document: sanitize -> ibflex ->
extractors -> persistence -> tax report. No network, no IBKR credentials.

The <Trades> rows mirror actual executions read back from the IBKR account
(GOOGL BUY 2 @ 319.68 on 2026-07-24; CSU SELL 0.1 @ 2742 with fifoPnlRealized
30.269944 on 2026-07-06), including the `subCategory` attribute that the pinned
ibflex cannot model and a duplicate ORDER-level row of the same fill. Everything
is denominated in EUR so the assertions pin the ingestion wiring rather than FX
conversion (covered elsewhere) — no exchange-rate tables are needed.

This is what "trades + withholding land correctly" means, checkable without the
Flex Web Service being reachable.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from ibflex import parser

from app.database import Base
import app.models  # noqa: F401
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.models.corporate_action import CorporateAction
from app.models.dividend_payment import DividendPayment
from app.models.app_settings import AppSetting
from app.models.market_price import MarketPrice
from app.models.cash_flow import CashFlow
from app.models.cash_balance import CashBalance
from app.repositories.security_repository import SecurityRepository
from app.repositories.trade_repository import TradeRepository
from app.repositories.corporate_action_repository import CorporateActionRepository
from app.services.ibkr_service import IBKRService
from app.services.sync_helper import persist_transactions
from app.services.dividend_service import DividendService
from app.services.tax_service import TaxService


GOOGL_CONID = "208813719"
CSU_CONID = "11996609"   # fully sold, so absent from OpenPositions on purpose

FLEX_XML = f"""<FlexQueryResponse queryName="Portfolio" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="2026-01-01" toDate="2026-07-24"
 period="YearToDate" whenGenerated="2026-07-24;120000">
 <SecuritiesInfo>
  <SecurityInfo assetCategory="STK" subCategory="COMMON" symbol="GOOGL" conid="{GOOGL_CONID}"
   isin="US02079K3059" description="ALPHABET INC-CL A" currency="EUR" listingExchange="NASDAQ" />
 </SecuritiesInfo>
 <OpenPositions>
  <OpenPosition assetCategory="STK" subCategory="COMMON" symbol="GOOGL" conid="{GOOGL_CONID}"
   isin="US02079K3059" description="ALPHABET INC-CL A" currency="EUR" listingExchange="NASDAQ"
   position="12" costBasisPrice="204.58" costBasisMoney="2455" openDateTime="20260724"
   levelOfDetail="LOT" reportDate="2026-07-24" />
 </OpenPositions>
 <Trades>
  <Trade assetCategory="STK" subCategory="COMMON" conid="{GOOGL_CONID}" symbol="GOOGL"
   transactionID="TX-G-EXEC" tradeID="TR-G" tradeDate="2026-07-24" reportDate="2026-07-24"
   buySell="BUY" quantity="2" tradePrice="319.68" proceeds="-639.36" ibCommission="-0.350663"
   currency="EUR" fifoPnlRealized="0" levelOfDetail="EXECUTION" />
  <Trade assetCategory="STK" subCategory="COMMON" conid="{GOOGL_CONID}" symbol="GOOGL"
   transactionID="TX-G-ORDER" tradeID="TR-G" tradeDate="2026-07-24" reportDate="2026-07-24"
   buySell="BUY" quantity="2" tradePrice="319.68" proceeds="-639.36" ibCommission="-0.350663"
   currency="EUR" fifoPnlRealized="0" levelOfDetail="ORDER" />
  <Trade assetCategory="STK" subCategory="COMMON" conid="{CSU_CONID}" symbol="CSU"
   transactionID="TX-CSU" tradeID="TR-CSU" tradeDate="2026-07-06" reportDate="2026-07-06"
   buySell="SELL" quantity="-0.1" tradePrice="2742" proceeds="274.2" ibCommission="-1.000028"
   currency="EUR" fifoPnlRealized="30.269944" levelOfDetail="EXECUTION" />
  <Trade assetCategory="OPT" subCategory="C" conid="999999" symbol="NVDA 260918C00200000"
   transactionID="TX-OPT" tradeDate="2026-07-10" buySell="BUY" quantity="1" tradePrice="5"
   proceeds="-500" currency="EUR" levelOfDetail="EXECUTION" />
 </Trades>
 <CorporateActions>
  <CorporateAction assetCategory="STK" subCategory="COMMON" type="FS" conid="{GOOGL_CONID}"
   symbol="GOOGL" transactionID="CA-1" dateTime="2026-03-02" reportDate="2026-03-02"
   quantity="6" value="0" proceeds="0" currency="EUR"
   actionDescription="ALPHABET INC-CL A SPLIT 2 FOR 1" levelOfDetail="DETAIL" />
 </CorporateActions>
 <CashTransactions>
  <CashTransaction type="Dividends" assetCategory="STK" subCategory="COMMON" conid="{GOOGL_CONID}"
   symbol="GOOGL" transactionID="CT-DIV" dateTime="2026-05-01" settleDate="2026-05-02"
   amount="42.50" currency="EUR" description="GOOGL CASH DIVIDEND" levelOfDetail="DETAIL" />
  <CashTransaction type="Withholding Tax" assetCategory="STK" subCategory="COMMON"
   conid="{GOOGL_CONID}" symbol="GOOGL" transactionID="CT-WHT" dateTime="2026-05-01"
   settleDate="2026-05-02" amount="-6.38" currency="EUR"
   description="GOOGL WITHHOLDING @ 15%" levelOfDetail="DETAIL" />
 </CashTransactions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>""".encode()


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    tables = [Security.__table__, TaxLot.__table__, Trade.__table__,
              CorporateAction.__table__, DividendPayment.__table__,
              AppSetting.__table__, MarketPrice.__table__,
              CashFlow.__table__, CashBalance.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    return engine, AsyncSession(engine, expire_on_commit=False)


async def _ingest(session, xml: bytes = FLEX_XML) -> dict:
    """Mirror the production sync flow for the Flex-derived tables."""
    svc = IBKRService(token="t", query_id="q")

    sanitized, warnings = svc._sanitize_flex_xml(xml)
    statement = parser.parse(sanitized).FlexStatements[0]
    flex_data = {"statement": statement}

    conid_to_security_id = {}
    for sec in await svc.extract_securities(flex_data):
        saved = await SecurityRepository(session).upsert(sec)
        conid_to_security_id[sec["conid"]] = saved.id

    await persist_transactions(
        TradeRepository(session),
        CorporateActionRepository(session),
        conid_to_security_id,
        await svc.extract_trades(flex_data),
        await svc.extract_corporate_actions(flex_data),
    )
    await DividendService(session).sync_dividends_from_cash_transactions(
        await svc.extract_cash_transactions(flex_data), conid_to_security_id
    )
    await session.commit()
    # `parse_flex_xml` carries the sanitizer's two channels separately — drift that can
    # change ingested data, and drift that provably cannot. This helper assembles
    # `flex_data` by hand, so it has to mirror that split or the distinction goes
    # untested on the one path that parses a real IBKR document.
    return {"warnings": warnings, "flex_schema_notes": svc.last_schema_notes}


@pytest.mark.asyncio
async def test_real_flex_document_ingests_trades_and_withholding():
    engine, session = await _make_session()
    try:
        result = await _ingest(session)

        # The document only parses at all because the sanitizer removed subCategory —
        # but no extractor reads that field, so it is recorded in the run's details
        # rather than raised as a warning nobody could ever act on.
        assert any("subCategory" in n for n in result["flex_schema_notes"])
        assert not any("subCategory" in w for w in result["warnings"])
        # Dropping whole rows IS worth a warning: it changes what was ingested.
        assert any("aggregate <Trade> row(s)" in w for w in result["warnings"])

        trades = (await session.execute(select(Trade).order_by(Trade.trade_date))).scalars().all()
        by_key = {t.ib_key: t for t in trades}

        # Two STK executions: the OPT trade is filtered, and the duplicate
        # ORDER-level row of the GOOGL fill never reaches the DB.
        assert set(by_key) == {"TX-CSU", "TX-G-EXEC"}

        # The SELL carries IBKR's own FIFO realized P&L, unrounded.
        csu = by_key["TX-CSU"]
        assert csu.buy_sell == "SELL"
        assert csu.quantity == Decimal("-0.1")
        assert csu.realized_pnl == Decimal("30.269944")
        assert csu.proceeds == Decimal("274.2")
        # CSU is fully sold so it isn't in OpenPositions — security_id stays null.
        assert csu.security_id is None

        googl = by_key["TX-G-EXEC"]
        assert googl.buy_sell == "BUY"
        assert googl.quantity == Decimal("2")
        assert googl.security_id is not None  # resolved via conid

        # Corporate action landed and is linked to the security.
        actions = (await session.execute(select(CorporateAction))).scalars().all()
        assert len(actions) == 1
        assert actions[0].action_type == "FORWARDSPLIT"
        assert actions[0].quantity == Decimal("6")

        # Withholding: gross 42.50 - 6.38 = 36.12 net, marked authoritative.
        dividends = (await session.execute(select(DividendPayment))).scalars().all()
        assert len(dividends) == 1
        dp = dividends[0]
        assert dp.source == "ibkr"
        assert dp.gross_amount_eur == Decimal("42.50")
        assert dp.withholding_tax_eur == Decimal("6.38")
        assert dp.net_amount_eur == Decimal("36.12")
        assert dp.pay_date == date(2026, 5, 2)  # settleDate, not dateTime
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_tax_report_uses_ingested_trades_and_withholding():
    engine, session = await _make_session()
    try:
        await _ingest(session)

        report = await TaxService(session).get_tax_report(2026)

        # Both sources flip to authoritative once the Flex sections are ingested.
        assert report["dividend_source"] == "ibkr"
        assert report["realized_source"] == "trades"

        assert report["dividend_totals"] == {
            "gross": 42.5, "withholding": 6.38, "net": 36.12, "estimated_rows": 0,
        }
        assert len(report["dividend_income"]) == 1
        assert report["dividend_income"][0]["pay_date"] == "2026-05-02"

        # Only the SELL produces a realized row, at IBKR's own figure.
        assert len(report["realized_gains"]) == 1
        row = report["realized_gains"][0]
        assert row["symbol"] == "CSU"
        assert row["trade_date"] == "2026-07-06"
        assert row["gain_loss"] == 30.27
        assert report["realized_totals"]["gain_loss"] == 30.27
        # cost basis is derived as proceeds - gain
        assert report["realized_totals"]["proceeds"] == 274.2
        assert report["realized_totals"]["cost_basis"] == round(274.2 - 30.269944, 2)

        csv_text = TaxService(session).to_csv(report)
        assert "source: ibkr" in csv_text
        assert "source: trades" in csv_text
        assert "CSU" in csv_text
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_reingested_statement_backfills_the_withholding_country():
    """
    The withholding line names the country that withheld (GitHub issue #62). A row
    stored before the column existed fills in when a statement covering it is ingested
    again -- `app.cli.ingest_flex_xml` from a Client Portal download, no token spend.
    """
    engine, session = await _make_session()
    try:
        await _ingest(session)          # this fixture's line names no country
        (dp,) = (await session.execute(select(DividendPayment))).scalars().all()
        assert dp.withholding_country is None

        await _ingest(session, FLEX_XML.replace(
            b"GOOGL WITHHOLDING @ 15%",
            b"GOOGL(US02079K3059) CASH DIVIDEND EUR 0.21 PER SHARE - US TAX",
        ))
        (dp,) = (await session.execute(select(DividendPayment))).scalars().all()
        await session.refresh(dp)
        assert dp.withholding_country == "US"
        assert dp.withholding_tax_eur == Decimal("6.38")
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_ingestion_is_idempotent():
    """Daily syncs re-send overlapping statements; nothing may duplicate."""
    engine, session = await _make_session()
    try:
        await _ingest(session)
        await _ingest(session)

        trades = (await session.execute(select(Trade))).scalars().all()
        actions = (await session.execute(select(CorporateAction))).scalars().all()
        dividends = (await session.execute(select(DividendPayment))).scalars().all()

        assert len(trades) == 2
        assert len(actions) == 1
        assert len(dividends) == 1
        assert dividends[0].withholding_tax_eur == Decimal("6.38")  # not doubled
    finally:
        await session.close()
        await engine.dispose()
