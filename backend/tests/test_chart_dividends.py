"""
Dividends captured from the price chart (GitHub issue #11, item a).

Every price fetch already asks `v8/finance/chart` for `events=div,splits`, and its window
is re-read at every repricing slot, so the dedicated `.dividends` request — the whole
history — is kept only for the bootstrap, a mapping change, a split and a slow re-verify.
The two paths share one row builder, and `test_one_frame_through_both_paths_stores_the_
same_rows` feeds one frame through each.

Offline: `yf.Ticker` is a stub serving a fixed frame; `.dividends` records every read.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
import app.services.dividend_service as ds
import app.services.market_data_service as mds
from app.clock import utcnow
from app.database import Base
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.ticker_mapping import TickerMapping
from app.services.dividend_service import DIVIDENDS_RECHECK_AFTER, DividendService
from app.services.market_data_service import MarketDataService

TODAY = date.today()
FIRST_LOT = date(2025, 6, 2)  # pre-ownership cutoff: three years before, 2022-06-03


def _frame(rows):
    """A daily `history()` frame as yfinance 1.x returns it: exchange-tz midnight index."""
    index = pd.DatetimeIndex([pd.Timestamp(d) for d, *_ in rows]).tz_localize("America/New_York")
    closes = [c for _, c, _, _ in rows]
    return pd.DataFrame({
        "Open": closes, "High": closes, "Low": closes, "Close": closes, "Adj Close": closes,
        "Volume": [1000] * len(rows),
        "Dividends": [d for _, _, d, _ in rows],
        "Stock Splits": [s for _, _, _, s in rows],
    }, index=index)


def _recent(days_ago):
    return TODAY - timedelta(days=days_ago)


# One frame: a pre-ownership dividend (skipped by both), two recent ones, plain days.
FRAME = _frame([
    (date(2021, 3, 1), 50.0, 0.40, 0.0),
    (_recent(9), 100.0, 0.0, 0.0),
    (_recent(8), 101.0, 0.52, 0.0),
    (_recent(7), 102.0, 0.0, 0.0),
    (_recent(2), 103.0, 0.1375, 0.0),
    (_recent(1), 104.0, 0.0, 0.0),
])


class _Yahoo:
    """`yf.Ticker` stand-in. `frames[ticker] = (frame, reported currency)`."""

    def __init__(self):
        self.frames = {}
        self.dividend_reads = []
        self.history_reads = []
        yahoo = self

        class _Ticker:
            def __init__(self, ticker):
                self.ticker = ticker
                currency = yahoo.frames.get(ticker, (None, None))[1]
                self._price_history = SimpleNamespace(_history_metadata={"currency": currency})

            def history(self, **_kw):
                yahoo.history_reads.append(self.ticker)
                return yahoo.frames.get(self.ticker, (pd.DataFrame(), None))[0]

            @property
            def dividends(self):
                yahoo.dividend_reads.append(self.ticker)
                frame = yahoo.frames[self.ticker][0]
                column = frame["Dividends"]
                return column[column != 0]  # exactly yfinance's `get_dividends`

        self.Ticker = _Ticker


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


@pytest.fixture
def yahoo(monkeypatch):
    stub = _Yahoo()
    monkeypatch.setattr(mds.yf, "Ticker", stub.Ticker)  # one yfinance module for both
    for module in (mds, ds):
        monkeypatch.setattr(module.random, "uniform", lambda *_a, **_kw: 0)
    return stub


async def _security(db, symbol="PAY", exchange="NASDAQ", currency="USD", held=True, checked=None):
    security = Security(
        isin=f"US{abs(hash(symbol)) % 10**10:010d}", symbol=symbol, description=symbol,
        currency=currency, conid=abs(hash(symbol)) % 10**6, asset_category="STK",
        exchange=exchange, dividends_checked_at=checked,
    )
    db.add(security)
    await db.flush()
    db.add(TaxLot(
        security_id=security.id, open_date=FIRST_LOT, quantity=Decimal("10"),
        cost_basis=Decimal("100"), cost_basis_eur=Decimal("90"), price_per_unit=Decimal("10"),
        currency=currency, is_open=held, close_date=None if held else _recent(30),
    ))
    await db.commit()
    return security


async def _price_pass(db, security):
    return await MarketDataService(db).fetch_and_cache_prices(
        security, start_date=_recent(10), end_date=TODAY,
    )


async def _rows(db):
    db.expire_all()
    rows = (await db.execute(select(DividendPayment).order_by(DividendPayment.ex_date))).scalars()
    return [(r.ex_date, r.amount_per_share, r.currency, r.source) for r in rows]


async def _stamp(db, security_id):
    db.expire_all()
    return (await db.get(Security, security_id)).dividends_checked_at


# ── the same rows either way ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_frame_through_both_paths_stores_the_same_rows(db, yahoo):
    security = await _security(db)
    yahoo.frames["PAY"] = (FRAME, "USD")

    await _price_pass(db, security)
    from_chart = await _rows(db)
    assert yahoo.dividend_reads == []

    await db.execute(delete(DividendPayment))
    await db.commit()
    result = await DividendService(db).sync_dividend_data()  # stamp NULL: the bootstrap
    from_dividends = await _rows(db)

    assert yahoo.dividend_reads == ["PAY"]
    assert from_chart == from_dividends
    assert from_chart == [
        (_recent(8), Decimal("0.52"), "USD", "yfinance_estimate"),
        (_recent(2), Decimal("0.1375"), "USD", "yfinance_estimate"),
    ]
    assert result["pre_ownership_skipped"] == 1


# ── when `.dividends` is read ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_steady_state_never_reads_the_full_history(db, yahoo):
    checked = utcnow() - timedelta(days=1)
    security = await _security(db, checked=checked)
    sid = security.id
    yahoo.frames["PAY"] = (FRAME, "USD")

    await _price_pass(db, security)
    await DividendService(db).sync_dividend_data()

    assert yahoo.dividend_reads == []
    assert len(await _rows(db)) == 2, "the new ex-dates came from the price response"
    assert await _stamp(db, sid) == checked, "a window never stamps a full read"


@pytest.mark.asyncio
async def test_the_bootstrap_reads_it(db, yahoo):
    await _security(db, checked=None)
    yahoo.frames["PAY"] = (FRAME, "USD")

    await DividendService(db).sync_dividend_data()

    assert yahoo.dividend_reads == ["PAY"]


@pytest.mark.asyncio
async def test_the_re_verify_reads_it_once_the_interval_has_passed(db, yahoo):
    fresh = await _security(db, "FRESH", checked=utcnow() - DIVIDENDS_RECHECK_AFTER + timedelta(hours=1))
    stale = await _security(db, "STALE", checked=utcnow() - DIVIDENDS_RECHECK_AFTER - timedelta(hours=1))
    for s in (fresh, stale):
        yahoo.frames[s.symbol] = (FRAME, "USD")

    await DividendService(db).sync_dividend_data()

    assert yahoo.dividend_reads == ["STALE"]
    assert DIVIDENDS_RECHECK_AFTER == timedelta(days=30)


@pytest.mark.asyncio
async def test_a_mapping_change_after_the_read_re_arms_it(db, yahoo):
    checked = utcnow() - timedelta(days=2)
    security = await _security(db, "SBI", exchange="TSE", currency="CAD", checked=checked)
    sid = security.id
    yahoo.frames["SBI.TO"] = (FRAME, "CAD")
    db.add(TickerMapping(
        ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI.TO", source="manual",
        is_active=True, created_at=checked - timedelta(days=90), updated_at=utcnow(),
    ))
    await db.commit()

    await DividendService(db).sync_dividend_data()
    assert yahoo.dividend_reads == ["SBI.TO"]

    # Read after the change: no longer due.
    await DividendService(db).sync_dividend_data()
    assert yahoo.dividend_reads == ["SBI.TO"]
    assert await _stamp(db, sid) > checked


@pytest.mark.asyncio
async def test_a_mapping_older_than_the_read_does_not(db, yahoo):
    checked = utcnow() - timedelta(days=2)
    await _security(db, "SBI", exchange="TSE", currency="CAD", checked=checked)
    db.add(TickerMapping(
        ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI.TO", source="manual",
        is_active=True, updated_at=checked - timedelta(days=1),
    ))
    await db.commit()

    await DividendService(db).sync_dividend_data()
    assert yahoo.dividend_reads == []


@pytest.mark.asyncio
async def test_a_split_in_the_window_after_the_read_re_arms_it_once(db, yahoo):
    checked = utcnow() - timedelta(days=5)
    security = await _security(db, checked=checked)
    sid = security.id
    split = _frame([
        (_recent(9), 400.0, 0.0, 0.0),
        (_recent(3), 100.0, 0.0, 4.0),
        (_recent(1), 101.0, 0.13, 0.0),
    ])
    yahoo.frames["PAY"] = (split, "USD")

    await _price_pass(db, security)
    assert await _stamp(db, sid) is None
    await DividendService(db).sync_dividend_data()
    assert yahoo.dividend_reads == ["PAY"]

    # The same split seen again after the re-read does not re-arm.
    await _price_pass(db, await db.get(Security, sid))
    assert await _stamp(db, sid) is not None
    await DividendService(db).sync_dividend_data()
    assert yahoo.dividend_reads == ["PAY"]


@pytest.mark.asyncio
async def test_a_split_before_the_read_does_not(db, yahoo):
    checked = utcnow()
    security = await _security(db, checked=checked)
    sid = security.id
    yahoo.frames["PAY"] = (_frame([
        (_recent(9), 400.0, 0.0, 0.0), (_recent(3), 100.0, 0.0, 4.0), (_recent(1), 101.0, 0.0, 0.0),
    ]), "USD")

    await _price_pass(db, security)

    assert await _stamp(db, sid) == checked


# ── which response, which security ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_rejected_variation_contributes_no_dividend(db, yahoo):
    """`ABC.DE` is empty, `ABC.F` quotes USD for a EUR security (rejected), bare `ABC`
    is accepted: only its dividend may be stored."""
    security = await _security(db, "ABC", exchange="XETRA", currency="EUR", checked=utcnow())
    yahoo.frames["ABC.F"] = (_frame([(_recent(3), 10.0, 9.99, 0.0), (_recent(1), 10.0, 0.0, 0.0)]), "USD")
    yahoo.frames["ABC"] = (_frame([(_recent(4), 20.0, 0.25, 0.0), (_recent(1), 20.0, 0.0, 0.0)]), "EUR")

    await _price_pass(db, security)

    assert await _rows(db) == [(_recent(4), Decimal("0.25"), "EUR", "yfinance_estimate")]
    assert yahoo.history_reads == ["ABC.DE", "ABC.F", "ABC"]


@pytest.mark.asyncio
async def test_a_sold_security_gets_no_dividend_rows(db, yahoo):
    """The dividend pass asks about held securities only; the capture keeps that scope."""
    security = await _security(db, held=False)
    yahoo.frames["PAY"] = (FRAME, "USD")

    await _price_pass(db, security)

    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_an_unreadable_dividend_never_costs_the_prices(db, yahoo):
    security = await _security(db, checked=utcnow())
    odd = FRAME.copy()
    odd["Dividends"] = odd["Dividends"].astype(object)
    odd.iloc[2, odd.columns.get_loc("Dividends")] = "0.52 VND"  # yfinance's currency-mismatch shape
    yahoo.frames["PAY"] = (odd, "USD")

    written = await _price_pass(db, security)

    assert security.symbol == "PAY"  # still readable after the rollback, without an await
    assert written > 0
    assert await _rows(db) == []
