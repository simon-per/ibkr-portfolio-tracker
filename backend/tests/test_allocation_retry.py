"""
A security Yahoo has no allocation data for must not be re-fetched forever.

`sync_allocation_data` selects every security whose `allocation_last_updated` is
null or older than 7 days, and the **failure** path never set it. So a security
with no Yahoo `.info` was re-fetched on every sync indefinitely: a 1-3s wait, a
request against an IP-based rate limit, and a 2-4s inter-security delay, for an
answer already known.

That is the same unbounded-retry shape `HOLIDAY_GRACE_DAYS` closed for market
prices and `UPSTREAM_RETRY_COOLDOWN_SECONDS` closed for benchmarks.

The two halves have to move together. Stamping the attempt bounds the retry, but
`securities_without_data` counted securities by a *null timestamp* — so on its own
the fix would have reported a security as covered the moment we gave up on it, and
the tab's "needs updating" banner would have cleared while the data was still
missing. The count now keys on the data.

Offline: yfinance is never reached — every fetch is faked.
"""

from datetime import timedelta

import pytest

from app.clock import utcnow
import app.models  # noqa: F401  register all mappers
from app.models.security import Security
from app.services import allocation_service as alloc_mod
from app.services.allocation_service import AllocationService


def make_security(session, **kw) -> Security:
    defaults = dict(
        isin="US0000000001", symbol="NOPE", description="Untraceable",
        currency="USD", conid=1, asset_category="STK", exchange="NASDAQ",
    )
    defaults.update(kw)
    security = Security(**defaults)
    session.add(security)
    return security


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    """The service sleeps 1-3s per fetch and 2-4s between securities."""
    async def _sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(alloc_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(alloc_mod.random, "uniform", lambda *_a: 0)


@pytest.mark.asyncio
async def test_a_failed_fetch_records_the_attempt(db, monkeypatch):
    security = make_security(db)
    await db.commit()

    async def _always_fails(_self, _security):
        return {'success': False, 'error': 'No data'}

    monkeypatch.setattr(AllocationService, "fetch_allocation_for_security", _always_fails)

    result = await AllocationService(db).sync_allocation_data()

    assert result['errors'] == 1
    assert security.allocation_last_updated is not None, (
        "a failure left the timestamp null, so this security is re-fetched on "
        "every sync forever"
    )


@pytest.mark.asyncio
async def test_the_next_sync_does_not_retry_it(db, monkeypatch):
    """The consequence that matters: the retry is bounded by the 7-day cutoff."""
    make_security(db)
    await db.commit()

    calls = []

    async def _always_fails(_self, security):
        calls.append(security.symbol)
        return {'success': False, 'error': 'No data'}

    monkeypatch.setattr(AllocationService, "fetch_allocation_for_security", _always_fails)

    await AllocationService(db).sync_allocation_data()
    await AllocationService(db).sync_allocation_data()

    assert calls == ["NOPE"], f"re-fetched a known-hopeless security: {calls}"


@pytest.mark.asyncio
async def test_it_is_retried_once_the_data_goes_stale(db, monkeypatch):
    """Bounded, not abandoned — a delisting or a mapping fix should still land."""
    security = make_security(db)
    security.allocation_last_updated = utcnow() - timedelta(days=8)
    await db.commit()

    calls = []

    async def _succeeds(_self, sec):
        calls.append(sec.symbol)
        return {'success': True, 'asset_type': 'Stock', 'sector': 'Technology',
                'industry': 'Software', 'country': 'United States'}

    monkeypatch.setattr(AllocationService, "fetch_allocation_for_security", _succeeds)

    await AllocationService(db).sync_allocation_data()

    assert calls == ["NOPE"]
    assert security.sector == "Technology"


@pytest.mark.asyncio
async def test_a_failure_does_not_invent_allocation_data(db, monkeypatch):
    """
    The timestamp records an attempt; it must never imply data that was not
    fetched. This is what lets the status count stay honest.
    """
    security = make_security(db)
    await db.commit()

    async def _always_fails(_self, _security):
        return {'success': False, 'error': 'No ticker mapping'}

    monkeypatch.setattr(AllocationService, "fetch_allocation_for_security", _always_fails)
    await AllocationService(db).sync_allocation_data()

    assert security.sector is None
    assert security.country is None


@pytest.mark.asyncio
async def test_the_status_count_follows_the_data_not_the_timestamp(db, monkeypatch):
    """
    Without this the banner would clear the moment we gave up: the count used to
    key on `allocation_last_updated IS NOT NULL`, which a failed attempt now sets.
    """
    from app.routers.allocation import get_allocation_status

    make_security(db, symbol="NOPE", isin="US0000000001", conid=1)
    make_security(db, symbol="MSFT", isin="US5949181045", conid=2,
                  sector="Technology", country="United States")
    make_security(db, symbol="IWDA", isin="IE00B4L5Y983", conid=3, asset_type="ETF")
    await db.commit()

    async def _always_fails(_self, _security):
        return {'success': False, 'error': 'No data'}

    monkeypatch.setattr(AllocationService, "fetch_allocation_for_security", _always_fails)
    await AllocationService(db).sync_allocation_data()

    status = await get_allocation_status(db)

    assert status['total_securities'] == 3
    # MSFT has a sector; IWDA is an ETF, which legitimately has many sectors rather
    # than none. Only the untraceable one is genuinely uncovered.
    assert status['securities_with_data'] == 2
    assert status['securities_without_data'] == 1


def test_the_sync_and_the_status_endpoint_share_one_staleness_threshold():
    """
    One decision seen from both ends: the constant picks what the sync refreshes,
    and `/api/allocation/status` reports what needs refreshing. Both wrote
    `timedelta(days=7)` inline until 2026-08-01, so nothing would have caught them
    drifting — the tab would simply have described a different set than the sync
    touched.

    Structural, because the two live in different modules and the coupling is the
    point rather than either value.
    """
    import inspect

    from app.routers import allocation as allocation_router
    from app.services import allocation_service as allocation_svc

    assert allocation_svc.ALLOCATION_STALE_DAYS == 7

    for module in (allocation_router, allocation_svc):
        src = inspect.getsource(module)
        assert "timedelta(days=7)" not in src, (
            f"{module.__name__} hardcodes the staleness window again instead of "
            f"using ALLOCATION_STALE_DAYS"
        )
        assert "ALLOCATION_STALE_DAYS" in src
