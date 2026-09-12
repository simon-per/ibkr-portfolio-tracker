"""
Pricing a pension-only fund tranche from a sibling share class.

The Swisscanto EM tranche held in the 3a account has no public quote; its provider
prints a NAV only on transaction rows, so the previous design carried the last NAV
45 business days and then let the position go *unpriced* — dropping ~0.6% of the book
out of the total to avoid a stale-price error of a fraction of that. Another share
class of the same fund is quoted daily (`0P0000S0OE.SW`, ~49% higher in level), so:

    price(t) = NAV(anchor) × close_sibling(t) / close_sibling(anchor)

Offline: `_try_fetch_yahoo` is replaced on the service instance.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.market_price import MarketPrice, PRICE_ROW_SOURCE_SIBLING_SCALED
from app.models.security import PRICE_SOURCE_MANUAL, PRICE_SOURCE_SIBLING, Security
from app.repositories.ticker_mapping_repository import TickerMappingRepository
from app.services import market_data_service as mds
from app.services.finpension_ingest import PRICE_SOURCE_STATEMENT
from app.services.market_data_service import MarketDataService

SIBLING = "0P0000S0OE.SW"
NAV = Decimal("121.201101")


def _weekday(days_ago: int) -> date:
    d = date.today() - timedelta(days=days_ago)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


ANCHOR = _weekday(12)


async def _session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, AsyncSession(engine, expire_on_commit=False)


async def _fund(session, *, price_source=PRICE_SOURCE_SIBLING, mapped=True, navs=None):
    session.add(Security(
        id=9, isin="CH1529078078", symbol="CH1529078078", description="Swisscanto EM",
        currency="CHF", conid=None, asset_category="STK", exchange="FUND",
        account="pillar3a", price_source=price_source,
    ))
    for when, price in (navs or [(ANCHOR, NAV)]):
        session.add(MarketPrice(
            security_id=9, date=when, close_price=price, currency="CHF",
            source=PRICE_SOURCE_STATEMENT,
        ))
    await session.flush()
    if mapped:
        await TickerMappingRepository(session).upsert_mapping(
            ibkr_symbol="CH1529078078", ibkr_exchange="FUND", yahoo_ticker=SIBLING,
            source="manual", notes="sibling class",
        )
    await session.commit()
    return await session.get(Security, 9)


def _sibling_closes(start: date, end: date, base=Decimal("180"), currency="CHF"):
    """A weekday close series that drifts 0.5% a day, so every date has a distinct ratio."""
    rows, d, i = [], start, 0
    while d <= end:
        if d.weekday() < 5:
            rows.append({
                "date": d, "close_price": (base * (Decimal("1.005") ** i)).quantize(Decimal("0.000001")),
                "currency": currency, "source": "yahoo_finance",
            })
            i += 1
        d += timedelta(days=1)
    return rows


def _stub_fetch(monkeypatch, service, rows, rate_limited=False, record=None):
    async def fake(ticker, security, start, end):
        if record is not None:
            record.append((ticker, start, end))
        return [r for r in rows if start <= r["date"] <= end], rate_limited

    monkeypatch.setattr(service, "_try_fetch_yahoo", fake)
    monkeypatch.setattr(mds.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(mds.random, "uniform", lambda *_: 0)


async def _no_sleep(*_a, **_k):
    return None


async def _rows(session, source=None):
    q = select(MarketPrice).where(MarketPrice.security_id == 9).order_by(MarketPrice.date)
    if source:
        q = q.where(MarketPrice.source == source)
    return list((await session.execute(q)).scalars().all())


@pytest.mark.asyncio
async def test_prices_are_the_sibling_scaled_to_the_statement_nav(monkeypatch):
    engine, session = await _session()
    try:
        security = await _fund(session)
        service = MarketDataService(session)
        closes = _sibling_closes(ANCHOR - timedelta(days=10), date.today())
        calls = []
        _stub_fetch(monkeypatch, service, closes, record=calls)

        written = await service.sync_sibling_prices(security, days_back=730)

        by_date = {r["date"]: r["close_price"] for r in closes}
        factor = NAV / by_date[ANCHOR]
        derived = await _rows(session, PRICE_ROW_SOURCE_SIBLING_SCALED)
        assert written == len(derived) > 3
        for row in derived:
            assert row.date > ANCHOR, "nothing is derived before the first observed NAV"
            assert row.close_price == (by_date[row.date] * factor).quantize(Decimal("0.000001"))
            assert row.currency == "CHF"
        # The anchor row is the provider's, untouched.
        statement = await _rows(session, PRICE_SOURCE_STATEMENT)
        assert [(r.date, r.close_price) for r in statement] == [(ANCHOR, NAV)]
        # The fetch asked the sibling, and reached back past the anchor for its close.
        assert calls and calls[0][0] == SIBLING and calls[0][1] < ANCHOR
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_anchor_day_without_a_sibling_close_uses_the_nearest_prior_one(monkeypatch):
    """A NAV struck on a Swiss holiday: the sibling has no bar that day."""
    engine, session = await _session()
    try:
        security = await _fund(session)
        service = MarketDataService(session)
        closes = [r for r in _sibling_closes(ANCHOR - timedelta(days=10), date.today())
                  if r["date"] != ANCHOR]
        _stub_fetch(monkeypatch, service, closes)

        await service.sync_sibling_prices(security)

        prior = max(r["date"] for r in closes if r["date"] < ANCHOR)
        by_date = {r["date"]: r["close_price"] for r in closes}
        factor = NAV / by_date[prior]
        derived = await _rows(session, PRICE_ROW_SOURCE_SIBLING_SCALED)
        assert derived
        assert derived[0].close_price == (by_date[derived[0].date] * factor).quantize(Decimal("0.000001"))
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_second_run_writes_only_what_is_missing(monkeypatch):
    engine, session = await _session()
    try:
        security = await _fund(session)
        service = MarketDataService(session)
        closes = _sibling_closes(ANCHOR - timedelta(days=10), date.today())
        _stub_fetch(monkeypatch, service, closes)

        first = await service.sync_sibling_prices(security)
        second = await service.sync_sibling_prices(security)

        # Only the provisional tail (PROVISIONAL_PRICE_DAYS) is re-stated on a warm cache.
        assert first > second
        assert second <= 3
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_sibling_in_another_currency_is_refused_whole(monkeypatch):
    """An FX move would ride into the ratio and be read as the fund moving."""
    engine, session = await _session()
    try:
        security = await _fund(session)
        service = MarketDataService(session)
        _stub_fetch(monkeypatch, service,
                    _sibling_closes(ANCHOR - timedelta(days=10), date.today(), currency="USD"))

        with pytest.raises(ValueError, match="quoted in USD"):
            await service.sync_sibling_prices(security)
        assert await _rows(session, PRICE_ROW_SOURCE_SIBLING_SCALED) == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_sibling_is_declared_never_inferred(monkeypatch):
    engine, session = await _session()
    try:
        security = await _fund(session, mapped=False)
        service = MarketDataService(session)
        calls = []
        _stub_fetch(monkeypatch, service, [], record=calls)

        with pytest.raises(ValueError, match="no active ticker mapping"):
            await service.sync_sibling_prices(security)
        assert calls == [], "the suffix logic must never pick a sibling"
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_rate_limit_abandons_the_pass_like_every_other_yahoo_caller(monkeypatch):
    engine, session = await _session()
    try:
        security = await _fund(session)
        service = MarketDataService(session)
        _stub_fetch(monkeypatch, service, [], rate_limited=True)

        assert await service.sync_sibling_prices(security) == 0
        assert service.rate_limited is True
        assert await _rows(session, PRICE_ROW_SOURCE_SIBLING_SCALED) == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_loop_routes_a_sibling_security_and_still_skips_a_manual_one(monkeypatch):
    engine, session = await _session()
    try:
        security = await _fund(session)
        session.add(Security(
            id=10, isin="CH0117044948", symbol="CH0117044948", description="World ex CH",
            currency="CHF", conid=None, asset_category="STK", exchange="FUND",
            account="pillar3a", price_source=PRICE_SOURCE_MANUAL,
        ))
        await session.commit()
        manual = await session.get(Security, 10)
        service = MarketDataService(session)
        _stub_fetch(monkeypatch, service, _sibling_closes(ANCHOR - timedelta(days=10), date.today()))

        result = await service.sync_securities([security, manual], days_back=30)

        assert result["processed"] == 1
        assert result["skipped_not_yahoo"] == 1
        assert result["errors"] == []
        assert result["total_prices"] > 0
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_refusal_reaches_the_loops_errors_rather_than_vanishing(monkeypatch):
    engine, session = await _session()
    try:
        security = await _fund(session, mapped=False)
        service = MarketDataService(session)
        _stub_fetch(monkeypatch, service, [])

        result = await service.sync_securities([security], days_back=30)

        assert result["processed"] == 0
        assert len(result["errors"]) == 1 and "no active ticker mapping" in result["errors"][0]
    finally:
        await session.close()
        await engine.dispose()
