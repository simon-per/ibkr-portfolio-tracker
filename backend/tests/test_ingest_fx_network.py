"""
What network `ingest_flex_statement` may touch (GitHub issue #41).

It was documented as "no network I/O", but a lot in a currency/date the `exchange_rates`
cache lacks makes `CurrencyService.convert_to_eur` -> `get_exchange_rate` ask Frankfurter
(and, for recent dates, the fallback provider) from inside the ingest's open transaction.
The texts now say so. What stays load-bearing, and is pinned here: no Flex request and no
Yahoo — rules 1 and 2 never apply to the ingest.

Offline: httpx, requests and yfinance are recorders that refuse.
"""
import pytest
import requests
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.services import currency_service as cs
from app.services.ibkr_service import IBKRService
from app.services.sync_helper import ingest_flex_statement

XML = b"""<FlexQueryResponse queryName="Portfolio" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="2026-07-22" toDate="2026-07-24"
 period="Custom" whenGenerated="2026-07-24;120000">
 <SecuritiesInfo>
  <SecurityInfo assetCategory="STK" subCategory="COMMON" symbol="AAPL" conid="265598"
   isin="US0378331005" description="APPLE INC" currency="USD" listingExchange="NASDAQ" />
 </SecuritiesInfo>
 <OpenPositions>
  <OpenPosition assetCategory="STK" subCategory="COMMON" symbol="AAPL" conid="265598"
   isin="US0378331005" description="APPLE INC" currency="USD" listingExchange="NASDAQ"
   position="2" costBasisPrice="200" costBasisMoney="400" openDateTime="20260723"
   levelOfDetail="LOT" reportDate="2026-07-24" />
 </OpenPositions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


@pytest.mark.asyncio
async def test_the_ingest_reaches_only_the_fx_hosts(monkeypatch):
    fx_calls, other_calls = [], []

    class Recorder:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, **kw):
            fx_calls.append(url)
            raise RuntimeError("network")      # an offline host: refused

    def refuse(name):
        def _refuse(*a, **kw):
            other_calls.append((name, a[:2]))
            raise AssertionError(f"{name} called from the Flex ingest")
        return _refuse

    monkeypatch.setattr(cs.httpx, "AsyncClient", lambda *a, **k: Recorder())
    # The Flex client's transport (`ibkr_service._flex_get` -> requests.get).
    monkeypatch.setattr(requests, "get", refuse("requests.get"))
    monkeypatch.setattr(requests.Session, "request", refuse("requests.Session.request"))
    yf = pytest.importorskip("yfinance")
    monkeypatch.setattr(yf, "Ticker", refuse("yfinance.Ticker"))
    monkeypatch.setattr(yf, "download", refuse("yfinance.download"))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        flex_data = IBKRService(token="t", query_id="q").parse_flex_xml(XML)
        result = await ingest_flex_statement(session, flex_data)
    finally:
        await session.close()
        await engine.dispose()

    assert other_calls == []
    # An empty rate cache does go to the network — the documented behaviour — and only
    # to the two FX providers.
    assert fx_calls
    fx_hosts = (cs.CurrencyService.FRANKFURTER_API_URL, cs.CurrencyService.FALLBACK_API_URL)
    assert all(url.startswith(fx_hosts) for url in fx_calls), fx_calls
    # A provider outage is not an unsupported currency.
    skipped = [w for w in result["warnings"] if "taxlot" in w]
    assert skipped and "no FX rate" in skipped[0] and "unsupported" not in skipped[0]
