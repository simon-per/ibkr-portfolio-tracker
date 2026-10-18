"""
A held currency that no FX provider can serve reaches the sync record, and — once the
outage is long enough to matter — `warnings[]` (GitHub issue #17).

Two parts, on purpose (docs/pricing-and-fx.md, *the warm-up*):

- **Recorded, not warned, per run.** `sync_exchange_rates` keeps `frankfurter_failed`
  and `fallback_available` from `warm_rates`, so they land in each job's
  `details.fx_result`. One run's failure is healed by the next slot's re-ask, and a
  warning on every blip is the always-present warning CLAUDE.md says teaches readers
  to skip it.
- **Warned on staleness.** `find_stale_exchange_rates` (database only) names a held
  currency, or the selected base currency's `EUR -> base` pair, whose newest
  *published* rate has missed FX_STALE_PUBLICATION_DAYS ECB publication days. A
  stand-in (fallback or carried row) does not reset the age; a currency warmed only on
  spec never warns. All three jobs collect it.

Offline: httpx is a stub where Frankfurter answers 503 and the fallback is down too.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services import currency_service as cs
from app.services import scheduler_service as sched_mod
from app.clock import utc_today

CARRIED = f"frankfurter{cs.CurrencyService.CARRIED_SUFFIX}"


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _BothProvidersDown:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, **kw):
        return _Resp(503, {"message": "unavailable"})


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as db:
        db.add(Security(id=1, isin="US0378331005", symbol="AAPL", description="Apple",
                        currency="USD", conid=1, asset_category="STK", exchange="NASDAQ"))
        db.add(TaxLot(security_id=1, open_date=date(2025, 1, 2), quantity=Decimal("10"),
                      cost_basis=Decimal("1500"), price_per_unit=Decimal("150"),
                      currency="USD", cost_basis_eur=Decimal("1400"), is_open=True))
        await db.commit()
    yield engine
    await engine.dispose()


@pytest.fixture
def offline(monkeypatch, engine):
    monkeypatch.setattr(sched_mod, "AsyncSessionLocal",
                        async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(cs, "utcnow", lambda: datetime.combine(utc_today(), time(20, 0)))
    monkeypatch.setattr(cs.httpx, "AsyncClient", lambda *a, **k: _BothProvidersDown())
    return engine


def publication_day(missed: int) -> date:
    """The ECB publication day `missed` publication days before the latest one."""
    service = cs.CurrencyService(None)
    day = service._last_ecb_publication_day(utc_today())
    for _ in range(missed):
        day = service._last_ecb_publication_day(day - timedelta(days=1))
    return day


async def seed(engine, *rows):
    async with AsyncSession(engine) as db:
        for from_currency, to_currency, day, source in rows:
            db.add(ExchangeRate(date=day, from_currency=from_currency,
                                to_currency=to_currency, rate=Decimal("0.9"), source=source))
        await db.commit()


def stub_steps(monkeypatch, svc, recorded):
    async def _ok(*_a, **_k):
        return {"status": "success"}

    async def _record(result, _started):
        recorded.append(result)

    for name in ("sync_ibkr_data", "sync_market_data", "sync_dividends",
                 "sync_benchmark_prices", "record_dividend_forecast",
                 "refresh_lookthrough_data"):
        monkeypatch.setattr(svc, name, _ok)
    monkeypatch.setattr(svc, "_record_run", _record)


JOBS = ("_full_sync_job_locked", "_market_data_only_locked", "_ibkr_only_sync_locked")


@pytest.mark.asyncio
@pytest.mark.parametrize("job", JOBS)
async def test_one_failed_run_is_recorded_in_details_not_warned(offline, monkeypatch, job):
    """A blip: USD is one publication day behind, both providers refuse. The record names
    it; the next slot's re-ask heals it, so nothing warns."""
    await seed(offline, ("USD", "EUR", publication_day(1), "frankfurter"))
    svc = sched_mod.SchedulerService()
    recorded = []
    stub_steps(monkeypatch, svc, recorded)

    result = await getattr(svc, job)()

    fx = result["fx_result"]
    assert "USD" in fx["frankfurter_failed"]
    assert fx["fallback_available"] is False
    assert not result.get("warnings")
    assert recorded == [result]


@pytest.mark.asyncio
@pytest.mark.parametrize("job", JOBS)
async def test_a_stale_held_currency_warns_in_every_job(offline, monkeypatch, job):
    """Published rate past the threshold; a fallback and a carried row dated today are
    stand-ins and do not reset the age."""
    stale = publication_day(sched_mod.FX_STALE_PUBLICATION_DAYS)
    await seed(offline,
               ("USD", "EUR", stale, "frankfurter"),
               ("USD", "EUR", utc_today() - timedelta(days=1), cs.CurrencyService.FALLBACK_SOURCE),
               ("USD", "EUR", utc_today(), CARRIED))
    svc = sched_mod.SchedulerService()
    recorded = []
    stub_steps(monkeypatch, svc, recorded)

    result = await getattr(svc, job)()

    warnings = result.get("warnings") or []
    assert any(w.startswith("USD->EUR") and str(stale) in w for w in warnings), warnings
    assert recorded[0]["warnings"] == warnings


@pytest.mark.asyncio
async def test_one_day_short_of_the_threshold_is_quiet(offline):
    await seed(offline, ("USD", "EUR",
                         publication_day(sched_mod.FX_STALE_PUBLICATION_DAYS - 1),
                         "frankfurter"))
    async with AsyncSession(offline) as db:
        assert await sched_mod.SchedulerService().find_stale_exchange_rates(db) == []


@pytest.mark.asyncio
async def test_only_held_and_base_currencies_warn(offline):
    """TWD is warmed on spec (`WARM_CURRENCIES`) with no row at all, and a sold JPY
    security's rate is stale: neither warns. The selected base currency's pair does."""
    assert "TWD" in cs.CurrencyService.WARM_CURRENCIES
    stale = publication_day(sched_mod.FX_STALE_PUBLICATION_DAYS)
    async with AsyncSession(offline) as db:
        db.add(Security(id=2, isin="JP3633400001", symbol="7203", description="Toyota",
                        currency="JPY", conid=2, asset_category="STK", exchange="TSEJ"))
        db.add(TaxLot(security_id=2, open_date=date(2025, 1, 2), quantity=Decimal("100"),
                      cost_basis=Decimal("300000"), price_per_unit=Decimal("3000"),
                      currency="JPY", cost_basis_eur=Decimal("1800"), is_open=False,
                      close_date=date(2025, 6, 2)))
        await AppSettingsRepository(db).set_base_currency("CHF")
        await db.commit()
    await seed(offline,
               ("USD", "EUR", publication_day(0), "frankfurter"),
               ("JPY", "EUR", stale, "frankfurter"),
               ("EUR", "CHF", stale, "frankfurter"))

    async with AsyncSession(offline) as db:
        warnings = await sched_mod.SchedulerService().find_stale_exchange_rates(db)

    assert [w.split(":")[0] for w in warnings] == ["EUR->CHF"]


@pytest.mark.asyncio
async def test_a_non_ecb_held_currency_is_judged_on_its_fallback_rows(offline):
    """For TWD the fallback row is the only publication there will ever be."""
    async with AsyncSession(offline) as db:
        db.add(Security(id=3, isin="TW0002330008", symbol="2330", description="TSMC",
                        currency="TWD", conid=3, asset_category="STK", exchange="TWSE"))
        db.add(TaxLot(security_id=3, open_date=date(2025, 1, 2), quantity=Decimal("1000"),
                      cost_basis=Decimal("600000"), price_per_unit=Decimal("600"),
                      currency="TWD", cost_basis_eur=Decimal("17000"), is_open=True))
        await db.commit()
    await seed(offline,
               ("USD", "EUR", publication_day(0), "frankfurter"),
               ("TWD", "EUR", utc_today(), cs.CurrencyService.FALLBACK_SOURCE))

    async with AsyncSession(offline) as db:
        assert await sched_mod.SchedulerService().find_stale_exchange_rates(db) == []
