"""
Which securities a price pass asks about, and how often it re-asks one that never answers
(GitHub issue #10).

Every pass used to ask Yahoo about every row in `securities`, seven times a day. A sold
but still listed security cost a request a pass for prices nothing reads intraday. A
delisted one cost more: its trailing gap never closes (`get_missing_dates` keeps leading
and trailing gaps missing at any age, on purpose), so every pass retried the mapped
ticker, each variation and, for a US listing, Alpha Vantage — asking for `full`, which the
free tier answers with an `Information` notice and no data.

Now: the six 7-day passes ask about what is held or sold inside `SOLD_PRICE_GRACE_DAYS`;
the 18:00 pass also prices every other sold security (its held span up to the last close
while that has a gap, then to today for `post_sale_pct`); an empty answer from every
provider backs the security off for a day, doubling; Alpha Vantage gets `compact`, once a
security a UTC day, and is not asked again in a pass that hit its cap.

Offline: `yf.Ticker` and `httpx.AsyncClient` are recording stubs, the clock is patched,
and the database is in memory. Nothing reaches the network.
"""
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register all mappers
from app.accounts import PILLAR3A
from app.database import Base
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.market_price_repository import MarketPriceRepository
from app.services import market_data_service as mds
from app.services.market_data_service import (
    SOLD_PRICE_GRACE_DAYS,
    MarketDataService,
    price_backoff_until,
)
from app.services.portfolio_service import PRICE_LOOKBACK_DAYS
from app.clock import utc_today

TODAY = utc_today()
START = datetime(2026, 10, 5, 0, 0)  # naive UTC, like every stored stamp
SHORT, LONG = 7, 730
# Berlin's seven repricing slots, as one day of passes: six 7-day ones and the 18:00 one.
DAY_OF_PASSES = [(8, SHORT), (11, SHORT), (13, SHORT), (15, SHORT), (18, LONG),
                 (20, SHORT), (22, SHORT)]


def _weekdays(start: date, end: date):
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


class Upstream:
    """Yahoo and Alpha Vantage, as recording stubs. `served` tickers have every weekday."""

    def __init__(self, monkeypatch, served=(), av_body=None, rate_limited=False):
        self.served = set(served)
        self.av_body = av_body or (lambda params: {"Error Message": "Invalid API call."})
        self.rate_limited = rate_limited
        self.yahoo = []   # (ticker, start, end) — end as asked, i.e. exclusive
        self.av = []      # the query parameters of each call
        self.clock = START
        upstream = self

        class FakeTicker:
            def __init__(self, ticker):
                self.ticker = ticker
                self._price_history = SimpleNamespace(_history_metadata=None)

            def history(self, start, end, **_kw):
                upstream.yahoo.append((self.ticker, start, end))
                if upstream.rate_limited:
                    raise Exception("HTTP Error 429: Too Many Requests")
                if self.ticker not in upstream.served:
                    return pd.DataFrame()
                days = list(_weekdays(start, min(end - timedelta(days=1), TODAY)))
                self._price_history._history_metadata = {"currency": "USD"}
                return pd.DataFrame({"Close": [100.0] * len(days)},
                                    index=pd.to_datetime(days))

        class FakeAsyncClient:
            def __init__(self, *_a, **_kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, _url, params=None):
                upstream.av.append(dict(params))
                body = upstream.av_body(params)
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

        async def _no_sleep(*_a, **_kw):
            return None

        monkeypatch.setattr(mds.yf, "Ticker", FakeTicker)
        monkeypatch.setattr(mds.httpx, "AsyncClient", FakeAsyncClient)
        monkeypatch.setattr(mds.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(mds.random, "uniform", lambda *_: 0)
        monkeypatch.setattr(mds.settings, "alpha_vantage_api_key", "test-key")
        monkeypatch.setattr(mds, "utcnow", lambda: upstream.clock)

    def asked(self, symbol):
        """Yahoo requests whose ticker is `symbol` or one of its variations."""
        return [r for r in self.yahoo if r[0].split(".")[0] == symbol]


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def _security(session, sid, symbol, *, exchange="NASDAQ", currency="USD",
                    opened_days_ago=400, closed_days_ago=None, prices=None, **kw):
    """A security with one lot; `prices` = (from_days_ago, to_days_ago) of cached closes."""
    session.add(Security(
        id=sid, isin=f"US00000000{sid:02d}", symbol=symbol, description=symbol,
        currency=currency, conid=100 + sid, asset_category="STK", exchange=exchange, **kw,
    ))
    closed = TODAY - timedelta(days=closed_days_ago) if closed_days_ago is not None else None
    session.add(TaxLot(
        security_id=sid, open_date=TODAY - timedelta(days=opened_days_ago), close_date=closed,
        is_open=closed is None, quantity=Decimal("1"), cost_basis=Decimal("100"),
        price_per_unit=Decimal("100"), currency=currency, cost_basis_eur=Decimal("90"),
    ))
    if prices:
        for day in _weekdays(TODAY - timedelta(days=prices[0]), TODAY - timedelta(days=prices[1])):
            session.add(MarketPrice(security_id=sid, date=day, close_price=Decimal("100"),
                                    currency=currency, source="yahoo_finance"))
    await session.commit()


async def _pass(session, days_back):
    """One pass, as both callers run it: every security, a fresh service (fresh latches)."""
    securities = list((await session.execute(select(Security).order_by(Security.id))).scalars())
    return await MarketDataService(session).sync_securities(securities, days_back=days_back)


async def _written(session, sid):
    return [d for (d,) in (await session.execute(
        select(MarketPrice.date).where(MarketPrice.security_id == sid).order_by(MarketPrice.date)
    )).all()]


# --- scope ------------------------------------------------------------------------------


def test_the_grace_lets_the_close_date_price_settle_and_be_found():
    """The reader looks back PRICE_LOOKBACK_DAYS from the close date, and that close must
    have stopped being provisional before the security leaves the intraday passes."""
    assert SOLD_PRICE_GRACE_DAYS >= MarketPriceRepository.PROVISIONAL_PRICE_DAYS + PRICE_LOOKBACK_DAYS


@pytest.mark.asyncio
async def test_a_short_pass_asks_about_held_and_just_sold_securities_only(db, monkeypatch):
    up = Upstream(monkeypatch, served={"HLD", "NEW", "OLD"})
    await _security(db, 1, "HLD")
    # Sold inside the grace, and in the pillar 3a account: `held()` blends accounts.
    await _security(db, 2, "NEW", closed_days_ago=SOLD_PRICE_GRACE_DAYS - 1, account=PILLAR3A)
    await _security(db, 3, "OLD", closed_days_ago=SOLD_PRICE_GRACE_DAYS + 1)

    result = await _pass(db, SHORT)

    assert {r[0] for r in up.yahoo} == {"HLD", "NEW"}
    assert result["skipped_sold"] == 1
    assert result["planned"] == 2 and result["processed"] == 2


@pytest.mark.asyncio
async def test_a_sold_security_with_a_gap_in_its_held_span_is_fetched_to_its_last_close(
    db, monkeypatch,
):
    """First seen already closed (or purged): the long pass fills the span valuation reads,
    and stops there. Once it is complete the span is never asked for again."""
    up = Upstream(monkeypatch, served={"SLD"})
    await _security(db, 1, "SLD", opened_days_ago=200, closed_days_ago=60)
    last_close = TODAY - timedelta(days=60)

    await _pass(db, LONG)

    assert len(up.yahoo) == 1
    assert up.yahoo[0][2] == last_close + timedelta(days=1), "the request must end at the last close"
    span = await _written(db, 1)
    assert span and max(span) <= last_close

    # Span complete: the next long pass prices after the sale only (post_sale_pct).
    await _pass(db, LONG)

    assert len(up.yahoo) == 2
    assert up.yahoo[1][2] == TODAY + timedelta(days=1)
    after = [d for d in await _written(db, 1) if d not in span]
    assert after and min(after) > last_close, "the held span was asked for again"

    # And a short pass does not ask at all: outside the grace, nothing reads it intraday.
    assert (await _pass(db, SHORT))["skipped_sold"] == 1
    assert len(up.yahoo) == 2


@pytest.mark.asyncio
async def test_the_weekend_full_sync_asks_only_about_what_has_no_row(db, monkeypatch):
    """
    GitHub issue #9: on a Berlin weekend the 18:00 pass runs missing-only. A warm holding
    and a sold security with a complete span cost nothing; a security first seen in
    Saturday's statement still gets its history. A trading-day pass asks all three.
    """
    up = Upstream(monkeypatch, served={"HLD", "SLD", "NEW"})
    await _security(db, 1, "HLD", prices=(740, 0))
    await _security(db, 2, "SLD", closed_days_ago=60, prices=(740, 0))
    await _security(db, 3, "NEW", opened_days_ago=1)
    securities = list((await db.execute(select(Security).order_by(Security.id))).scalars())

    await MarketDataService(db).sync_securities(
        securities, days_back=LONG, refresh_provisional=False,
    )
    assert [r[0] for r in up.yahoo] == ["NEW"]

    await MarketDataService(db).sync_securities(securities, days_back=LONG)
    assert [r[0] for r in up.yahoo[1:]] == ["HLD", "SLD", "NEW"]


# --- back-off -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_answer_from_every_provider_backs_off_and_doubles(db, monkeypatch):
    up = Upstream(monkeypatch)  # Yahoo knows none of it: delisted
    await _security(db, 1, "DLX", exchange="XETRA", currency="EUR",
                    closed_days_ago=60, prices=(740, 60))

    await _pass(db, LONG)
    asked = len(up.yahoo)
    assert asked == 3, "DLX.DE, DLX.F and bare DLX — the cost every pass paid before"
    security = await db.get(Security, 1)
    assert security.price_miss_count == 1 and security.price_checked_at == START

    up.clock = START + timedelta(hours=6)
    result = await _pass(db, LONG)
    assert len(up.yahoo) == asked, "asked again inside the back-off"
    assert result["skipped_backoff"] == 1

    up.clock = START + timedelta(days=1)
    await _pass(db, LONG)
    assert len(up.yahoo) == asked + 3
    assert security.price_miss_count == 2

    up.clock = START + timedelta(days=2)  # one day after a second miss: still backing off
    await _pass(db, LONG)
    assert len(up.yahoo) == asked + 3


def test_a_holding_never_backs_off_and_a_sold_one_is_capped_at_a_month():
    stamped = START
    long_run = SimpleNamespace(price_checked_at=stamped, price_miss_count=12)

    sold_due = price_backoff_until(long_run, is_held=False)

    # A holding is asked at every pass: empty answers from a soft-throttling Yahoo must
    # not freeze the book's prices, and a broken mapping shows as a stale price.
    assert price_backoff_until(long_run, is_held=True) is None
    assert stamped + timedelta(days=29) < sold_due <= stamped + timedelta(days=30)
    assert price_backoff_until(SimpleNamespace(price_checked_at=None, price_miss_count=None),
                               is_held=False) is None


@pytest.mark.asyncio
async def test_a_rate_limited_pass_stamps_nothing(db, monkeypatch):
    up = Upstream(monkeypatch, rate_limited=True)
    await _security(db, 1, "HLD")

    result = await _pass(db, SHORT)

    assert result["rate_limited_after"] == "HLD"
    security = await db.get(Security, 1)
    assert security.price_checked_at is None and security.price_miss_count is None
    assert up.av == [], "a refused Yahoo must not fall through to Alpha Vantage"


@pytest.mark.asyncio
async def test_a_row_written_resets_the_back_off(db, monkeypatch):
    up = Upstream(monkeypatch, served={"SLD"})
    await _security(db, 1, "SLD", closed_days_ago=60, prices=(740, 60),
                    price_checked_at=START - timedelta(days=5), price_miss_count=3)

    await _pass(db, LONG)  # three misses: four days' back-off, which has run out

    assert len(up.yahoo) == 1
    security = await db.get(Security, 1)
    assert security.price_miss_count is None and security.price_checked_at is None


# --- Alpha Vantage ------------------------------------------------------------------------


def _av_series(params):
    return {"Time Series (Daily)": {
        d.isoformat(): {"4. close": "50.0"}
        for d in _weekdays(TODAY - timedelta(days=40), TODAY)
    }}


@pytest.mark.asyncio
async def test_alpha_vantage_is_asked_compact_and_once_a_day_per_security(db, monkeypatch):
    up = Upstream(monkeypatch, av_body=_av_series)  # only AV can price it
    await _security(db, 1, "AVU", prices=(740, 10))

    await _pass(db, SHORT)
    assert [p["outputsize"] for p in up.av] == ["compact"]
    assert (await db.get(Security, 1)).price_miss_count is None, "AV's rows count as an answer"

    up.clock = START + timedelta(hours=3)
    await _pass(db, SHORT)   # Yahoo still empty; AV already asked this UTC day
    up.clock = START + timedelta(hours=6)
    await _pass(db, SHORT)
    assert len(up.av) == 1

    up.clock = START + timedelta(days=1, hours=1)
    await _pass(db, SHORT)
    assert len(up.av) == 2, "a new UTC day may ask again"


@pytest.mark.asyncio
async def test_an_information_notice_is_logged_and_stops_alpha_vantage_for_the_pass(
    db, monkeypatch, caplog,
):
    up = Upstream(monkeypatch, av_body=lambda p: {
        "Information": "Thank you for using Alpha Vantage! Our standard API rate limit is "
                       "25 requests per day."})
    await _security(db, 1, "AAA", prices=(740, 10))
    await _security(db, 2, "BBB", prices=(740, 10))

    with caplog.at_level(logging.WARNING, logger=mds.__name__):
        await _pass(db, SHORT)

    assert len(up.av) == 1, "AV was asked again after saying its cap was reached"
    assert "Information" in caplog.text
    assert len(up.asked("BBB")) == 1, "Yahoo is still asked; only the fallback stops"


@pytest.mark.asyncio
async def test_alpha_vantage_is_not_asked_for_a_gap_its_free_tier_cannot_reach(db, monkeypatch):
    up = Upstream(monkeypatch, av_body=_av_series)
    await _security(db, 1, "AVU", prices=(740, 200))

    await _pass(db, LONG)

    assert up.av == []


# --- the budget, end to end ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_requests_per_day_for_held_sold_and_delisted_securities(db, monkeypatch):
    """
    Thirty days of the seven daily passes. Before this change every pass asked every
    security: the sold-but-listed one 7 Yahoo requests a day, the delisted German one 21
    (three tickers a pass), the delisted US one 7 plus 7 Alpha Vantage calls — 210, 630,
    210 + 210 over these thirty days.
    """
    up = Upstream(monkeypatch, served={"HLD", "SLD"})
    await _security(db, 1, "HLD", prices=(740, 1))
    await _security(db, 2, "SLD", closed_days_ago=60, prices=(740, 60))
    await _security(db, 3, "DLX", exchange="XETRA", currency="EUR",
                    closed_days_ago=60, prices=(740, 60))
    await _security(db, 4, "DLU", closed_days_ago=60, prices=(740, 60))

    per_day = []
    for day in range(30):
        before = {s: len(up.asked(s)) for s in ("HLD", "SLD", "DLX", "DLU")}
        av_before = len(up.av)
        for hour, days_back in DAY_OF_PASSES:
            up.clock = START + timedelta(days=day, hours=hour)
            await _pass(db, days_back)
        per_day.append({**{s: len(up.asked(s)) - n for s, n in before.items()},
                        "AV": len(up.av) - av_before})

    assert all(d["HLD"] == 7 for d in per_day), "a holding is still priced at every pass"
    assert all(d["SLD"] == 1 for d in per_day), "a sold, listed security: the 18:00 pass only"
    # Asked on days 0, 1, 3, 7 and 15 (one day, doubling), then not before day 31.
    asked_days = [i for i, d in enumerate(per_day) if d["DLX"]]
    assert asked_days == [0, 1, 3, 7, 15]
    assert sum(d["DLX"] for d in per_day) == 15
    assert sum(d["DLU"] for d in per_day) == 5
    assert sum(d["AV"] for d in per_day) == 5
    assert {p["outputsize"] for p in up.av} == {"compact"}
