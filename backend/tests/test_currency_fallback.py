"""
Offline tests for the secondary FX provider.

Frankfurter republishes the ECB reference rates, so its currency list is fixed at the
ECB's 30 + EUR and will never contain TWD. Buying TSMC on TWSE therefore produced a
currency the app could not convert at all: get_exchange_rate() raised, reconcile_taxlots()
counted the lot in `taxlots_skipped`, and the holding silently disappeared from the
portfolio and the tax report.

The fallback closes that hole, but it serves *current* rates only — there is no free
historical endpoint. These tests pin the resulting rule: recent dates are answered,
older ones still raise so the caller keeps skipping rather than being handed an
invented historical rate.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.exchange_rate import ExchangeRate
from app.services import currency_service as cs
from app.services.currency_service import CurrencyService


# EUR-based snapshot, shaped like open.er-api.com's payload.
FALLBACK_PAYLOAD = {
    "result": "success",
    "base_code": "EUR",
    "rates": {"EUR": 1, "USD": 1.138971, "CHF": 0.930479, "TWD": 36.821601},
}


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[ExchangeRate.__table__])
        )
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db
    finally:
        await db.close()
        await engine.dispose()


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def install_fake_http(monkeypatch, payload=FALLBACK_PAYLOAD, status_code=200):
    """Capture every URL the service requests, answering all of them identically."""
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            calls.append(url)
            return FakeResponse(payload, status_code)

    monkeypatch.setattr(cs.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    return calls


def frankfurter_range_payload(rate, on=None):
    """Shaped like Frankfurter's /start..end range reply: {rates: {date: {CUR: rate}}}."""
    on = on or date.today()
    return {"rates": {on.isoformat(): {"EUR": rate}}}


def install_routed_http(monkeypatch, frankfurter, fallback):
    """
    Answer the two providers differently, so a test can fail one and not the other.

    That distinction is the whole subject of these tests: the fallback trades accuracy
    for availability, so exactly *when* it is reached is the behaviour worth pinning.
    """
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            calls.append(url)
            is_fallback = url == CurrencyService.FALLBACK_API_URL
            return FakeResponse(fallback if is_fallback else frankfurter)

    monkeypatch.setattr(cs.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    return calls


async def _rows(session):
    result = await session.execute(select(ExchangeRate))
    return result.scalars().all()


@pytest.mark.asyncio
async def test_currency_frankfurter_lacks_is_resolved_by_the_fallback(monkeypatch, session):
    calls = install_fake_http(monkeypatch)

    rate = await CurrencyService(session).get_exchange_rate("TWD", date.today())

    # Provider quotes "X per 1 EUR", so TWD->EUR is the reciprocal. The value comes
    # back through the Numeric(18, 8) column, so compare at the column's precision.
    assert abs(rate - Decimal("1") / Decimal("36.821601")) < Decimal("1e-8")
    assert calls == [CurrencyService.FALLBACK_API_URL]

    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].from_currency == "TWD"
    assert rows[0].source == CurrencyService.FALLBACK_SOURCE


@pytest.mark.asyncio
async def test_old_date_still_raises_rather_than_backdating_todays_rate(monkeypatch, session):
    """A latest-only provider cannot answer for 2025. Answering anyway would put a
    fabricated historical rate on an old tax lot and the tax report would show it as
    fact, so we keep the skip-with-warning behaviour instead."""
    calls = install_fake_http(monkeypatch)
    stale = date.today() - timedelta(days=CurrencyService.FALLBACK_MAX_AGE_DAYS + 1)

    with pytest.raises(ValueError):
        await CurrencyService(session).get_exchange_rate("TWD", stale)

    assert calls == []          # refused before spending a request
    assert await _rows(session) == []


@pytest.mark.asyncio
async def test_no_provider_switch_while_frankfurter_answers(monkeypatch, session):
    """The fallback must not creep in as a *replacement*. Its rates are today's applied
    to a recent date; the ECB's are the real thing for the date asked. So as long as
    Frankfurter answers, it wins and the fallback is never even contacted."""
    calls = install_routed_http(
        monkeypatch,
        frankfurter=frankfurter_range_payload(0.878),
        fallback=FALLBACK_PAYLOAD,
    )

    rate = await CurrencyService(session).get_exchange_rate("USD", date.today())

    assert rate == Decimal("0.878")
    assert CurrencyService.FALLBACK_API_URL not in calls
    rows = await _rows(session)
    assert [r.source for r in rows] == ["frankfurter"]


@pytest.mark.asyncio
async def test_frankfurter_outage_is_rescued_by_the_fallback_and_tagged(monkeypatch, session):
    """USD is in SUPPORTED_CURRENCIES, so this used to raise — and a raise means
    reconcile_taxlots() skips the lot and the position vanishes from the portfolio and
    the tax report. One provider being down should degrade the rate, not delete a
    holding, so the fallback is now the last resort for *any* currency. The tag is what
    keeps the approximation honest afterwards."""
    calls = install_routed_http(
        monkeypatch,
        frankfurter={"rates": {}},          # up, but has nothing for us
        fallback=FALLBACK_PAYLOAD,
    )

    rate = await CurrencyService(session).get_exchange_rate("USD", date.today())

    assert abs(rate - Decimal("1") / Decimal("1.138971")) < Decimal("1e-8")
    assert CurrencyService.FALLBACK_API_URL in calls
    rows = await _rows(session)
    assert [r.source for r in rows] == [CurrencyService.FALLBACK_SOURCE]


@pytest.mark.asyncio
async def test_frankfurter_outage_on_an_old_date_still_raises(monkeypatch, session):
    """The rescue is strictly for recent dates. Applying today's rate to a 2025 lot
    would write a fabricated historical rate the tax report then presents as fact, so an
    old date keeps raising even when the fallback could technically answer."""
    calls = install_routed_http(
        monkeypatch,
        frankfurter={"rates": {}},
        fallback=FALLBACK_PAYLOAD,
    )
    stale = date.today() - timedelta(days=CurrencyService.FALLBACK_MAX_AGE_DAYS + 1)

    with pytest.raises(ValueError):
        await CurrencyService(session).get_exchange_rate("USD", stale)

    # Frankfurter was asked; the fallback was refused on age before spending a request.
    assert calls and CurrencyService.FALLBACK_API_URL not in calls
    assert await _rows(session) == []


@pytest.mark.asyncio
async def test_carry_forward_preserves_the_provider_tag(monkeypatch, session):
    """A rate carried across a weekend must not be relabelled as Frankfurter data."""
    install_fake_http(monkeypatch, payload={"result": "error"})
    yesterday = date.today() - timedelta(days=1)
    session.add(ExchangeRate(
        date=yesterday, from_currency="TWD", to_currency="EUR",
        rate=Decimal("0.02716"), source=CurrencyService.FALLBACK_SOURCE,
    ))
    await session.flush()

    rate = await CurrencyService(session).get_exchange_rate("TWD", date.today())

    assert rate == Decimal("0.02716")
    carried = [r for r in await _rows(session) if r.date == date.today()]
    assert len(carried) == 1
    assert carried[0].source == CurrencyService.FALLBACK_SOURCE + CurrencyService.CARRIED_SUFFIX


@pytest.mark.asyncio
async def test_carry_forward_refuses_a_month_stale_rate(monkeypatch, session):
    """
    With both providers down, a rate older than the carry bound must raise —
    silently stamping it onto a new lot's persisted cost_basis_eur would be a
    permanent mis-valuation, while a skipped lot self-heals on the next sync.
    """
    install_fake_http(monkeypatch, payload={"result": "error"})
    stale_day = date.today() - timedelta(days=CurrencyService.CARRY_FORWARD_MAX_AGE_DAYS + 10)
    session.add(ExchangeRate(
        date=stale_day, from_currency="TWD", to_currency="EUR",
        rate=Decimal("0.02716"), source=CurrencyService.FALLBACK_SOURCE,
    ))
    await session.flush()

    with pytest.raises(ValueError):
        await CurrencyService(session).get_exchange_rate("TWD", date.today())


@pytest.mark.asyncio
async def test_carry_forward_covers_a_gap_inside_the_bound(monkeypatch, session):
    install_fake_http(monkeypatch, payload={"result": "error"})
    recent = date.today() - timedelta(days=CurrencyService.CARRY_FORWARD_MAX_AGE_DAYS - 1)
    session.add(ExchangeRate(
        date=recent, from_currency="TWD", to_currency="EUR",
        rate=Decimal("0.02716"), source=CurrencyService.FALLBACK_SOURCE,
    ))
    await session.flush()

    rate = await CurrencyService(session).get_exchange_rate("TWD", date.today())
    assert rate == Decimal("0.02716")


@pytest.mark.asyncio
async def test_provider_failure_leaves_the_cache_untouched(monkeypatch, session):
    """The fetcher must never raise on its own; get_exchange_rate() owns that decision."""
    install_fake_http(monkeypatch, payload={"result": "error"})

    with pytest.raises(ValueError):
        await CurrencyService(session).get_exchange_rate("TWD", date.today())

    assert await _rows(session) == []


# --- warm-up ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_rates_costs_one_request_for_the_whole_non_ecb_set(monkeypatch, session):
    """The provider returns all ~166 currencies in a single reply, so warming N of them
    must cost one request, not N. This is the only reason warming a watchlist daily is
    affordable — and daily is what builds the history, since there is no historical
    endpoint to ask later."""
    calls = install_routed_http(
        monkeypatch,
        frankfurter=frankfurter_range_payload(0.878),
        fallback=FALLBACK_PAYLOAD,
    )

    summary = await CurrencyService(session).warm_rates(["TWD", "CHF", "USD"])

    fallback_calls = [c for c in calls if c == CurrencyService.FALLBACK_API_URL]
    assert len(fallback_calls) == 1
    assert summary["frankfurter"] == 2          # CHF and USD via the ECB range endpoint
    assert summary["fallback"] == 1             # TWD from the shared table

    by_currency = {r.from_currency: r for r in await _rows(session)}
    assert by_currency["TWD"].source == CurrencyService.FALLBACK_SOURCE
    assert by_currency["USD"].source == "frankfurter"


@pytest.mark.asyncio
async def test_warm_rates_routes_a_frankfurter_failure_to_the_fallback(monkeypatch, session):
    """Same rule as get_exchange_rate: an outage should cost accuracy, not a whole day of
    history. It also has to be *reported* — a currency that failed both providers is the
    precondition for a position quietly disappearing."""
    install_routed_http(
        monkeypatch,
        frankfurter={"rates": {}},            # up, but returns nothing for USD
        fallback=FALLBACK_PAYLOAD,
    )

    summary = await CurrencyService(session).warm_rates(["USD"])

    assert summary["frankfurter"] == 0
    assert summary["frankfurter_failed"] == ["USD"]
    assert summary["fallback"] == 1
    rows = await _rows(session)
    assert [(r.from_currency, r.source) for r in rows] == [
        ("USD", CurrencyService.FALLBACK_SOURCE)
    ]


@pytest.mark.asyncio
async def test_a_clean_warm_up_reports_no_failures(monkeypatch, session):
    install_routed_http(
        monkeypatch, frankfurter=frankfurter_range_payload(0.878), fallback=FALLBACK_PAYLOAD
    )

    summary = await CurrencyService(session).warm_rates(["USD"])

    assert summary["frankfurter"] == 1
    assert "frankfurter_failed" not in summary


@pytest.mark.asyncio
async def test_warm_rates_survives_an_unavailable_fallback(monkeypatch, session):
    """Warm-up is bookkeeping for later; a provider being down must not fail the sync
    that called it."""
    install_routed_http(
        monkeypatch,
        frankfurter=frankfurter_range_payload(0.878),
        fallback={"result": "error"},
    )

    summary = await CurrencyService(session).warm_rates(["TWD", "USD"])

    assert summary["fallback"] == 0
    assert summary["fallback_available"] is False
    assert [r.from_currency for r in await _rows(session)] == ["USD"]


@pytest.mark.asyncio
async def test_warm_rates_skips_the_request_when_the_day_is_already_cached(
    monkeypatch, session
):
    """Five jobs a day call this. The provider is latest-only, so re-asking returns the
    same number — the second run of the day should cost nothing at all."""
    service = CurrencyService(session)
    calls = install_routed_http(
        monkeypatch, frankfurter=frankfurter_range_payload(0.878), fallback=FALLBACK_PAYLOAD
    )

    await service.warm_rates(["TWD"])
    assert calls == [CurrencyService.FALLBACK_API_URL]

    summary = await service.warm_rates(["TWD"])

    assert calls == [CurrencyService.FALLBACK_API_URL]   # still just the one
    assert summary["fallback"] == 0
    assert len(await _rows(session)) == 1


@pytest.mark.asyncio
async def test_warm_rates_does_not_claim_a_currency_neither_provider_carries(
    monkeypatch, session
):
    """Counting it as warmed would report success for a currency that still cannot be
    converted — and the symptom of that is a position quietly missing from the portfolio
    and the tax report, months later."""
    install_routed_http(
        monkeypatch, frankfurter=frankfurter_range_payload(0.878), fallback=FALLBACK_PAYLOAD
    )

    summary = await CurrencyService(session).warm_rates(["TWD", "XYZ"])

    assert summary["fallback"] == 1
    assert [r.from_currency for r in await _rows(session)] == ["TWD"]


@pytest.mark.asyncio
async def test_warm_rates_ignores_the_target_currency_itself(monkeypatch, session):
    """EUR->EUR is 1 by definition and has no row; asking for it must not spend a
    request or write one."""
    calls = install_routed_http(
        monkeypatch, frankfurter=frankfurter_range_payload(0.878), fallback=FALLBACK_PAYLOAD
    )

    summary = await CurrencyService(session).warm_rates(["EUR"])

    assert calls == []
    assert summary["currencies"] == []
    assert await _rows(session) == []


@pytest.mark.parametrize("stand_in", [
    CurrencyService.FALLBACK_SOURCE,
    "frankfurter" + CurrencyService.CARRIED_SUFFIX,
    CurrencyService.FALLBACK_SOURCE + CurrencyService.CARRIED_SUFFIX,
])
@pytest.mark.asyncio
async def test_a_published_quote_replaces_the_stand_in_for_its_day(monkeypatch, session, stand_in):
    """
    Found 2026-10-01: the fallback's latest CHF->EUR (1.05918) was stored under 09-28
    before the ECB published that day, then carried onto 09-30, and the real quote
    (1.0566) was skipped on every later fetch because a row already existed. A CHF 4,500
    withdrawal read as CHF 4,517.50. A stand-in now gives way to the published figure.
    """
    day = date.today() - timedelta(days=2)
    session.add(ExchangeRate(date=day, from_currency="CHF", to_currency="EUR",
                             rate=Decimal("1.0591772946"), source=stand_in))
    await session.flush()
    install_fake_http(monkeypatch, payload=frankfurter_range_payload(1.0566, on=day))

    assert await CurrencyService(session)._batch_fetch_rates("CHF", date.today(), "EUR")

    rows = [r for r in await _rows(session) if r.date == day]
    assert len(rows) == 1
    assert rows[0].rate == Decimal("1.0566") and rows[0].source == "frankfurter"


@pytest.mark.asyncio
async def test_a_rate_that_is_not_a_stand_in_is_never_rewritten(monkeypatch, session):
    """Only what announced itself as provisional gives way — a hand-loaded rate does not."""
    day = date.today() - timedelta(days=2)
    session.add(ExchangeRate(date=day, from_currency="CHF", to_currency="EUR",
                             rate=Decimal("1.06"), source="manual"))
    await session.flush()
    install_fake_http(monkeypatch, payload=frankfurter_range_payload(1.0566, on=day))

    await CurrencyService(session)._batch_fetch_rates("CHF", date.today(), "EUR")

    rows = [r for r in await _rows(session) if r.date == day]
    assert rows[0].rate == Decimal("1.06") and rows[0].source == "manual"

