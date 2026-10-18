"""
A security Yahoo cannot serve is asked once per back-off period, not every pass (issue #66).

An empty `.info` (or one carrying only Nones) or a non-429 error wrote no row and stamped
nothing, so the security stayed "missing" and was asked again on every daily pass — three
to six Yahoo requests each, for good. Now a miss stamps `fundamentals_checked_at` and
`fundamentals_miss_count` on the security and the pass backs off by the price loop's rule
(`miss_backoff`: one day, doubling, capped at 30). No row is written, so the tab and
`/status` show the security absent — not data, and not "no data yet" either.

Offline: `_fetch_yahoo_data` is faked, and the clock moves by patching `utcnow`.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.clock import utcnow
import app.models  # noqa: F401  register all mappers
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories import fundamentals_repository as fm_repo
from app.routers import fundamentals as fm_router
from app.services import fundamentals_service as fs_mod
from app.services.fundamentals_service import FundamentalsService

T0 = utcnow().replace(microsecond=0)
INFO = {"quoteType": "EQUITY", "trailingPE": 20.0, "currency": "USD"}


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    async def _sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(fs_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(fs_mod.random, "uniform", lambda *_a: 0)


@pytest.fixture
def clock(monkeypatch):
    """One 'now' for the service and the repository, moved by the test."""
    state = {"now": T0}
    monkeypatch.setattr(fs_mod, "utcnow", lambda: state["now"])
    monkeypatch.setattr(fm_repo, "utcnow", lambda: state["now"])
    return state


def _held(db, security_id=1):
    db.add(Security(id=security_id, isin=f"US000000000{security_id}", symbol=f"S{security_id}",
                    description="S", currency="USD", conid=security_id,
                    asset_category="STK", exchange="NASDAQ"))
    db.add(TaxLot(security_id=security_id, open_date=date(2025, 1, 2), is_open=True,
                  quantity=Decimal("10"), cost_basis=Decimal("1000"),
                  price_per_unit=Decimal("100"), currency="USD",
                  cost_basis_eur=Decimal("900")))


def _yahoo(monkeypatch, answer):
    """Yahoo answering `answer` ('empty' | 'nones' | '404' | '429' | 'data') every time;
    the returned list records each ask. `answer` may be a one-item list to change later."""
    asked = []
    box = answer if isinstance(answer, list) else [answer]

    async def _fetch(_self, ticker):
        asked.append(ticker)
        if box[0] == "404":
            raise Exception("HTTP Error 404: Quote not found for symbol")
        if box[0] == "429":
            raise Exception("429 Client Error: Too Many Requests")
        info = {"empty": {}, "nones": {"trailingPegRatio": None}, "data": dict(INFO)}[box[0]]
        return info, None, None, None, None

    monkeypatch.setattr(FundamentalsService, "_fetch_yahoo_data", _fetch)
    return asked


async def _rows(db) -> int:
    return (await db.execute(select(func.count(FundamentalMetrics.id)))).scalar()


@pytest.mark.parametrize("answer", ["empty", "nones", "404"])
@pytest.mark.asyncio
async def test_an_unservable_security_is_asked_once_per_backoff_period(
    db, monkeypatch, clock, answer
):
    asked = _yahoo(monkeypatch, answer)
    _held(db)
    await db.commit()

    for _ in range(5):  # the repro: five passes, the first one asks
        await FundamentalsService(db).sync_stale_fundamentals()
    assert len(asked) == 1

    # Daily passes at the same slot: due again after 1, then 2, then 4 days.
    asked_on = [0]
    for day in range(1, 8):
        clock["now"] = T0 + timedelta(days=day)
        before = len(asked)
        await FundamentalsService(db).sync_stale_fundamentals()
        if len(asked) > before:
            asked_on.append(day)
    assert asked_on == [0, 1, 3, 7]

    assert await _rows(db) == 0  # absent, never a row of stand-ins
    security = await db.get(Security, 1)
    assert security.fundamentals_miss_count == 4
    assert security.fundamentals_checked_at == T0 + timedelta(days=7)


@pytest.mark.asyncio
async def test_a_miss_is_neither_data_nor_no_data_yet(db, monkeypatch, clock):
    _yahoo(monkeypatch, "empty")
    _held(db)
    await db.commit()

    status = await fm_router.get_fundamentals_status(db=db)
    assert (status["securities_with_data"], status["securities_without_data"],
            status["securities_unavailable"]) == (0, 1, 0)

    result = await FundamentalsService(db).sync_stale_fundamentals()
    assert result["no_data"] == 1 and result["errors"] == 0

    status = await fm_router.get_fundamentals_status(db=db)
    assert (status["total_securities"], status["securities_with_data"],
            status["securities_without_data"], status["securities_unavailable"]) == (1, 0, 0, 1)
    assert status["newest_update"] is None  # a miss writes no row, so moves no as-of
    [entry] = await FundamentalsService(db).get_fundamentals_for_portfolio()
    assert entry["quote_type"] is None and entry["trailing_pe"] is None
    assert entry["last_updated"] is None


@pytest.mark.asyncio
async def test_a_data_answer_ends_the_backoff(db, monkeypatch, clock):
    answer = ["empty"]
    _yahoo(monkeypatch, answer)
    _held(db)
    await db.commit()
    await FundamentalsService(db).sync_stale_fundamentals()

    answer[0] = "data"
    clock["now"] = T0 + timedelta(days=1)
    result = await FundamentalsService(db).sync_stale_fundamentals()

    assert result["metrics_updated"] == 1
    security = await db.get(Security, 1)
    assert security.fundamentals_checked_at is None and security.fundamentals_miss_count is None
    status = await fm_router.get_fundamentals_status(db=db)
    assert (status["securities_with_data"], status["securities_unavailable"]) == (1, 0)


@pytest.mark.asyncio
async def test_a_failing_refresh_keeps_the_figures_and_status_counts_what_the_sync_asks(
    db, monkeypatch, clock
):
    """A row Yahoo now refuses (404) keeps its figures and backs off; `/status`'s stale
    count leaves it out exactly as the sync does (one predicate, `backing_off`)."""
    asked = _yahoo(monkeypatch, "404")
    _held(db)
    written = T0 - timedelta(days=2)
    db.add(FundamentalMetrics(security_id=1, trailing_pe=20.0, quote_type="EQUITY",
                              last_updated=written))
    await db.commit()
    assert (await fm_router.get_fundamentals_status(db=db))["stale_metrics"] == 1

    result = await FundamentalsService(db).sync_stale_fundamentals()
    assert result["errors"] == 1 and len(asked) == 1

    row = (await db.execute(select(FundamentalMetrics))).scalar_one()
    assert row.trailing_pe == 20.0 and row.last_updated == written
    assert (await fm_router.get_fundamentals_status(db=db))["stale_metrics"] == 0
    await FundamentalsService(db).sync_stale_fundamentals()
    assert len(asked) == 1

    clock["now"] = T0 + timedelta(days=1)
    assert (await fm_router.get_fundamentals_status(db=db))["stale_metrics"] == 1
    await FundamentalsService(db).sync_stale_fundamentals()
    assert len(asked) == 2


@pytest.mark.asyncio
async def test_a_rate_limit_is_not_a_miss(db, monkeypatch, clock):
    _yahoo(monkeypatch, "429")
    _held(db)
    await db.commit()

    result = await FundamentalsService(db).sync_stale_fundamentals()

    assert result["rate_limited"]
    security = await db.get(Security, 1)
    assert security.fundamentals_checked_at is None and security.fundamentals_miss_count is None


@pytest.mark.asyncio
async def test_a_forced_refresh_asks_a_security_backing_off(db, monkeypatch, clock):
    asked = _yahoo(monkeypatch, "empty")
    _held(db)
    await db.commit()
    await FundamentalsService(db).sync_stale_fundamentals()
    await FundamentalsService(db).sync_stale_fundamentals()
    assert len(asked) == 1

    await FundamentalsService(db).sync_fundamentals_data(force_refresh=True)
    assert len(asked) == 2
