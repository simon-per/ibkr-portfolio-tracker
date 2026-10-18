"""
The FX warm-up asks Frankfurter only for what is missing, in one request (issue #2).

`sync_exchange_rates` runs at every slot. It used to re-request a 7- or 30-day window for
each ECB currency separately, re-downloading rows already stored; the ECB publishes once
per working day, so most of those requests carried nothing new. Pinned here, offline, with
a fake that answers like Frankfurter v1's EUR-based range endpoint and records each call:

- every pair current to the ECB's last publication day: **no** request;
- after a new publication: **one** request, for every pair, over only the missing days;
- stored rows keep the direction readers expect (`X -> EUR`, `EUR -> CHF/USD`), and a rate
  cached before a warm-up reads the same after it.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.services import currency_service as cs
from app.services.currency_service import CurrencyService
from app.clock import utc_today

# EUR -> X as the ECB publishes it (Frankfurter, 2026-10-16), answered for every weekday.
ECB = {"USD": "1.1253", "CHF": "0.9272", "JPY": "177.82", "KRW": "1499.01",
       "CNY": "8.0108", "HKD": "8.7449", "GBP": "0.8412"}
FALLBACK = {"result": "success", "rates": {
    "EUR": 1, "USD": 1.13, "TWD": 36.27, "CNH": 7.50, "AED": 4.12, "SAR": 4.22,
    "QAR": 4.10, "KWD": 0.345, "RUB": 91.0, "CLP": 1060.0, "COP": 4400.0,
}}

WEDNESDAY, THURSDAY, FRIDAY = date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)
BASE_PAIRS = [("EUR", "CHF"), ("EUR", "USD")]
PAIRS = [("USD", "EUR"), ("JPY", "EUR")] + BASE_PAIRS


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(engine):
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db
    finally:
        await db.close()


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def install_http(monkeypatch, frankfurter_status=200):
    """Record every (url, params); answer Frankfurter from `ECB` and the fallback from
    `FALLBACK`."""
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, **kwargs):
            calls.append((url, dict(params or {})))
            if url == CurrencyService.FALLBACK_API_URL:
                return FakeResponse(FALLBACK)
            if frankfurter_status >= 400:
                return FakeResponse({"message": "unavailable"}, frankfurter_status)
            assert params["from"] == "EUR"
            start, end = (date.fromisoformat(d) for d in url.rsplit("/", 1)[1].split(".."))
            quotes = params["to"].split(",")
            rates, day = {}, start
            while day <= end:
                if day.weekday() < 5:
                    rates[day.isoformat()] = {q: float(ECB[q]) for q in quotes if q in ECB}
                day += timedelta(days=1)
            return FakeResponse({"base": "EUR", "rates": rates})

    monkeypatch.setattr(cs.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    return calls


def frankfurter(calls):
    return [c for c in calls if c[0].startswith(CurrencyService.FRANKFURTER_API_URL)]


def at(moment, monkeypatch):
    monkeypatch.setattr(cs, "utcnow", lambda: moment)


def seed(session, day, pairs=PAIRS, rate="1", source="frankfurter"):
    for from_currency, to_currency in pairs:
        session.add(ExchangeRate(date=day, from_currency=from_currency,
                                 to_currency=to_currency, rate=Decimal(rate), source=source))


async def rows(session):
    return (await session.execute(select(ExchangeRate))).scalars().all()


async def warm(session, days_back=7, target=FRIDAY):
    return await CurrencyService(session).warm_rates(
        ["USD", "JPY"], target_date=target, days_back=days_back, extra_pairs=BASE_PAIRS
    )


def inverse(eur_to_x):
    return (Decimal(1) / Decimal(eur_to_x)).quantize(CurrencyService.RATE_SCALE)


# --- requests --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_pair_current_costs_no_request(monkeypatch, session):
    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    calls = install_http(monkeypatch)
    seed(session, FRIDAY)
    await session.flush()

    summary = await warm(session)

    assert calls == []
    assert summary["frankfurter_requests"] == 0
    assert summary["frankfurter"] == len(PAIRS)
    assert "frankfurter_failed" not in summary


@pytest.mark.asyncio
async def test_a_new_publication_costs_one_request_for_the_missing_days_only(
    monkeypatch, session
):
    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    calls = install_http(monkeypatch)
    seed(session, WEDNESDAY)
    await session.flush()

    summary = await warm(session)

    assert len(calls) == 1
    url, params = calls[0]
    # From the newest day held (re-asked so the reply is never empty), not the window.
    assert url.endswith(f"/{WEDNESDAY}..{FRIDAY}")
    assert params == {"from": "EUR", "to": "CHF,JPY,USD"}
    assert summary["frankfurter_requests"] == 1
    assert summary["frankfurter"] == len(PAIRS)
    stored = {(r.from_currency, r.to_currency, r.date) for r in await rows(session)}
    assert stored == {(f, t, d) for f, t in PAIRS for d in (WEDNESDAY, THURSDAY, FRIDAY)}

    await warm(session)                     # the next slot: nothing new published

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_day_is_not_asked_for_before_the_ecb_publishes_it(monkeypatch, session):
    at(datetime(2026, 10, 16, 10, 0), monkeypatch)
    calls = install_http(monkeypatch)
    seed(session, THURSDAY)
    await session.flush()

    await warm(session)

    assert calls == []


@pytest.mark.asyncio
async def test_a_stand_in_does_not_make_a_pair_current(monkeypatch, session):
    """A fallback row or a carried copy is what a later fetch must replace, so counting
    it as current would make the stand-in permanent — the 2026-10-01 CHF bug."""
    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    calls = install_http(monkeypatch)
    seed(session, THURSDAY)
    seed(session, FRIDAY, pairs=[("USD", "EUR")], rate="0.885",
         source=CurrencyService.FALLBACK_SOURCE)
    seed(session, FRIDAY, pairs=[p for p in PAIRS if p != ("USD", "EUR")])
    await session.flush()

    await warm(session)

    assert [c[0].rsplit("/", 1)[1] for c in frankfurter(calls)] == [f"{THURSDAY}..{FRIDAY}"]
    usd = [r for r in await rows(session) if (r.from_currency, r.date) == ("USD", FRIDAY)]
    assert [(r.rate, r.source) for r in usd] == [(inverse(ECB["USD"]), "frankfurter")]


@pytest.mark.asyncio
async def test_a_history_short_of_the_window_gets_the_whole_window_once(monkeypatch, session):
    """The crypto book passes a long `days_back` until its history reaches the window's
    start; a current newest row must not stop that backfill."""
    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    calls = install_http(monkeypatch)
    seed(session, FRIDAY)
    await session.flush()

    await warm(session, days_back=30)
    await warm(session, days_back=30)

    assert [c[0].rsplit("/", 1)[1] for c in calls] == [f"{FRIDAY - timedelta(days=30)}..{FRIDAY}"]


@pytest.mark.asyncio
async def test_an_outage_still_falls_through_to_the_fallback(monkeypatch, session):
    today = utc_today()
    at(datetime.combine(today, time(18, 0)), monkeypatch)
    calls = install_http(monkeypatch, frankfurter_status=503)

    summary = await CurrencyService(session).warm_rates(
        ["USD", "TWD"], target_date=today, extra_pairs=[("EUR", "CHF")]
    )

    assert summary["frankfurter"] == 0
    assert summary["frankfurter_failed"] == ["USD", "EUR->CHF"]
    assert summary["fallback"] == 2
    assert len(frankfurter(calls)) == 1
    assert calls.count((CurrencyService.FALLBACK_API_URL, {})) == 1
    assert {(r.from_currency, r.source) for r in await rows(session)} == {
        ("USD", CurrencyService.FALLBACK_SOURCE), ("TWD", CurrencyService.FALLBACK_SOURCE),
    }


@pytest.mark.parametrize("now, target, expected", [
    (datetime(2026, 10, 16, 10, 0), date(2026, 10, 16), date(2026, 10, 15)),  # Fri, not yet
    (datetime(2026, 10, 16, 18, 0), date(2026, 10, 16), date(2026, 10, 16)),  # Fri, out
    (datetime(2026, 10, 18, 12, 0), date(2026, 10, 18), date(2026, 10, 16)),  # Sunday
    (datetime(2026, 10, 19, 9, 0), date(2026, 10, 19), date(2026, 10, 16)),   # Mon morning
    (datetime(2026, 12, 25, 18, 0), date(2026, 12, 25), date(2026, 12, 24)),  # Christmas
    (datetime(2027, 1, 1, 18, 0), date(2027, 1, 1), date(2026, 12, 31)),      # New Year
    (datetime(2026, 10, 19, 18, 0), date(2026, 10, 11), date(2026, 10, 9)),   # a past target
])
def test_the_last_ecb_publication_day(monkeypatch, now, target, expected):
    at(now, monkeypatch)
    assert CurrencyService(None)._last_ecb_publication_day(target) == expected


# --- stored direction ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cached_rate_reads_the_same_after_the_warm_up(monkeypatch, session):
    """One `base=EUR` reply comes back as EUR -> X; readers match the stored direction
    exactly. A rate cached before reads identically after, and the new days land as
    `X -> EUR` (the ECB figure's inverse) and `EUR -> CHF/USD` (the figure itself)."""
    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    install_http(monkeypatch)
    seed(session, WEDNESDAY, pairs=[("USD", "EUR")], rate="0.89")
    seed(session, WEDNESDAY, pairs=[("JPY", "EUR")], rate="0.00565")
    seed(session, WEDNESDAY, pairs=[("EUR", "CHF")], rate="0.94")
    seed(session, WEDNESDAY, pairs=[("EUR", "USD")], rate="1.12")
    await session.flush()
    service = CurrencyService(session)
    before = [
        await service.get_exchange_rate("USD", WEDNESDAY),
        await service.get_exchange_rate("JPY", WEDNESDAY),
        await service.get_exchange_rate("EUR", WEDNESDAY, "CHF"),
    ]

    await warm(session)                     # re-asks Wednesday with different figures

    assert [
        await service.get_exchange_rate("USD", WEDNESDAY),
        await service.get_exchange_rate("JPY", WEDNESDAY),
        await service.get_exchange_rate("EUR", WEDNESDAY, "CHF"),
    ] == before == [Decimal("0.89"), Decimal("0.00565"), Decimal("0.94")]
    assert await service.get_exchange_rate("USD", FRIDAY) == inverse(ECB["USD"])
    assert await service.get_exchange_rate("JPY", FRIDAY) == inverse(ECB["JPY"])
    assert await service.get_exchange_rate("EUR", FRIDAY, "CHF") == Decimal(ECB["CHF"])
    assert await service.get_exchange_rate("EUR", FRIDAY, "USD") == Decimal(ECB["USD"])
    assert {(r.from_currency, r.to_currency) for r in await rows(session)} == set(PAIRS)


@pytest.mark.asyncio
async def test_the_inverse_is_exact_where_a_non_eur_base_was_rounded(monkeypatch, session):
    """v1 rounds a non-EUR base to five decimals: KRW->EUR asked as such came back as
    0.00067, against the ECB's 1/1499.01 = 0.00066711."""
    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    install_http(monkeypatch)

    assert await CurrencyService(session)._batch_fetch_rates("KRW", FRIDAY, "EUR", days_back=1)

    (row,) = [r for r in await rows(session) if r.date == FRIDAY]
    assert row.rate == Decimal("0.00066711")


# --- the scheduled job -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sync_makes_one_frankfurter_request_and_then_none(monkeypatch, engine):
    """Held, quoted (market caps) and EUR->base pairs all ride in one request; the next
    slot, with nothing newly published, makes none — so the market-cap currencies no
    longer need a refresh throttle of their own."""
    from app.services import scheduler_service as sched_mod
    from app.services.market_cap import MARKET_CAP_CURRENCIES

    monkeypatch.setattr(sched_mod, "AsyncSessionLocal",
                        async_sessionmaker(engine, expire_on_commit=False))
    at(datetime.combine(utc_today(), time(20, 0)), monkeypatch)
    calls = install_http(monkeypatch)
    async with AsyncSession(engine) as db:
        db.add(Security(id=1, isin="US0378331005", symbol="AAPL", description="Apple",
                        currency="USD", conid=1, asset_category="STK", exchange="NASDAQ"))
        await db.commit()
    svc = sched_mod.SchedulerService()

    first = await svc.sync_exchange_rates(days_back=7)

    assert first["status"] == "success"
    (request,) = frankfurter(calls)
    asked = set(request[1]["to"].split(","))
    assert {"USD", "CHF"} | (MARKET_CAP_CURRENCIES & CurrencyService.SUPPORTED_CURRENCIES) == asked
    assert len(calls) == 2                  # plus the one fallback table (TWD and the rest)

    second = await svc.sync_exchange_rates(days_back=7)

    assert second["status"] == "success"
    assert len(calls) == 2
    async with AsyncSession(engine) as db:
        pairs = {(r.from_currency, r.to_currency)
                 for r in (await db.execute(select(ExchangeRate))).scalars().all()}
    assert {("USD", "EUR"), ("JPY", "EUR"), ("EUR", "CHF"), ("EUR", "USD")} <= pairs
