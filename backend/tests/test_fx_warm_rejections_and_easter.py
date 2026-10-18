"""
Two edges of the batched FX warm-up (`CurrencyService.warm_rates`, issue #40).

A code Frankfurter rejects costs only its own pair. Every ECB pair rides one request, and
a 4xx for it used to send every pair to the latest-only fallback. Now a 4xx for a batch is
retried once per code, a code rejected alone is left out of every request until the UTC
day ends, and only its pairs go to the fallback. A 5xx or 429 is still an outage for the
whole batch (`test_fx_incremental_warm.test_an_outage_still_falls_through_to_the_fallback`).
Frankfurter's real answer for an unknown `to=` is not verified here (no network); the fake
answers 404, which is what the deleted `_fetch_from_api` assumed.

Good Friday and Easter Monday are TARGET closing days. Unmodelled, every slot from Good
Friday 16:00 UTC to Tuesday 16:00 UTC asked once for a day that would never come (18 of
the 26 stock slots over Easter 2027); the reply re-carried Thursday, so this was cost only.
"""
from datetime import date, datetime, timedelta

import pytest

from app.services import currency_service as cs
from app.services.currency_service import CurrencyService, easter_sunday, is_target_closing_day
from tests.test_fx_incremental_warm import (  # noqa: F401  (fixtures)
    BASE_PAIRS, ECB, FALLBACK, FRIDAY, FakeResponse, at, engine, frankfurter, rows, seed, session,
)

GOOD_FRIDAY, EASTER_MONDAY = date(2027, 3, 26), date(2027, 3, 29)
THURSDAY = date(2027, 3, 25)
MONDAY = FRIDAY + timedelta(days=3)


def _install(monkeypatch, closed=(), unknown=(), status=404):
    """A fake Frankfurter that publishes every weekday not in ``closed`` and answers
    ``status`` to any request naming a code in ``unknown``."""
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, **kw):
            calls.append((url, dict(params or {})))
            if url == CurrencyService.FALLBACK_API_URL:
                return FakeResponse(FALLBACK)
            quotes = params["to"].split(",")
            if any(q in unknown for q in quotes):
                return FakeResponse({"message": "not found"}, status)
            start, end = (date.fromisoformat(d) for d in url.rsplit("/", 1)[1].split(".."))
            rates, day = {}, start
            while day <= end:
                if day.weekday() < 5 and day not in closed:
                    rates[day.isoformat()] = {q: float(ECB[q]) for q in quotes if q in ECB}
                day += timedelta(days=1)
            return FakeResponse({"base": "EUR", "rates": rates})

    monkeypatch.setattr(cs.httpx, "AsyncClient", lambda *a, **k: FakeClient())
    return calls


def _drifted(monkeypatch, code="XXA"):
    """Pretend the provider no longer carries a code SUPPORTED_CURRENCIES still lists."""
    monkeypatch.setattr(CurrencyService, "SUPPORTED_CURRENCIES",
                        CurrencyService.SUPPORTED_CURRENCIES | {code})


def _asked(call):
    return call[1]["to"].split(",")


# --- a rejected code -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_unknown_code_does_not_send_every_pair_to_the_fallback(monkeypatch, session):
    today = date.today()
    at(datetime.combine(today, datetime.min.time()) + timedelta(hours=18), monkeypatch)
    _drifted(monkeypatch)
    calls = _install(monkeypatch, unknown={"XXA"})

    summary = await CurrencyService(session).warm_rates(
        ["USD", "JPY", "XXA"], target_date=today, extra_pairs=BASE_PAIRS,
    )

    assert summary["frankfurter_failed"] == ["XXA"]
    assert summary["frankfurter"] == 4                  # USD, JPY, EUR->CHF, EUR->USD
    # The batch, then each of CHF, JPY, USD, XXA once on its own.
    assert summary["frankfurter_requests"] == 5
    assert [_asked(c) for c in frankfurter(calls)[1:]] == [["CHF"], ["JPY"], ["USD"], ["XXA"]]
    assert {(r.from_currency, r.to_currency) for r in await rows(session)
            if r.source == "frankfurter"} == {
        ("USD", "EUR"), ("JPY", "EUR"), ("EUR", "CHF"), ("EUR", "USD"),
    }


@pytest.mark.asyncio
async def test_a_rejected_code_is_not_asked_again_that_day(monkeypatch, session):
    """The bound: once rejected alone, a code is left out of every later request until
    the UTC day ends, so each slot after the first costs 0 or 1 request, not N+1."""
    _drifted(monkeypatch)
    calls = _install(monkeypatch, unknown={"XXA"})
    service = CurrencyService(session)

    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    await service.warm_rates(["USD", "XXA"], target_date=FRIDAY, extra_pairs=BASE_PAIRS)
    first = len(frankfurter(calls))
    assert first == 1 + 3                               # batch + CHF, USD, XXA alone

    at(datetime(2026, 10, 16, 22, 0), monkeypatch)      # a later slot, same UTC day
    again = await service.warm_rates(["USD", "XXA"], target_date=FRIDAY,
                                     extra_pairs=BASE_PAIRS)
    assert len(frankfurter(calls)) == first             # everything else is current
    assert again["frankfurter_requests"] == 0
    assert again["frankfurter_failed"] == ["XXA"]       # still reported, and to the fallback

    await service._batch_fetch_rates("XXA", FRIDAY, "EUR")  # the on-demand path, too
    assert len(frankfurter(calls)) == first

    # Monday's publication: one request, and XXA is not in it.
    at(datetime(2026, 10, 19, 18, 0), monkeypatch)
    await service.warm_rates(["USD", "XXA"], target_date=MONDAY, extra_pairs=BASE_PAIRS)
    assert len(frankfurter(calls)) == first + 1 + 3     # a new UTC day asks XXA once more
    assert "XXA" in _asked(frankfurter(calls)[first])


@pytest.mark.asyncio
async def test_a_rejected_extra_pair_is_named_like_one(monkeypatch, session):
    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    _drifted(monkeypatch)
    _install(monkeypatch, unknown={"XXA"})

    summary = await CurrencyService(session).warm_rates(
        ["USD"], target_date=FRIDAY, extra_pairs=[("EUR", "CHF"), ("EUR", "XXA")],
    )

    assert summary["frankfurter_failed"] == ["EUR->XXA"]
    assert summary["frankfurter"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_a_throttle_or_outage_is_not_retried_per_code(monkeypatch, session, status):
    """A 429 is a 4xx about the asking, not about a code: one request, nothing
    remembered, so the next slot asks for everything again."""
    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    calls = _install(monkeypatch, unknown={"USD"}, status=status)

    summary = await CurrencyService(session).warm_rates(
        ["USD", "JPY"], target_date=FRIDAY, extra_pairs=BASE_PAIRS,
    )

    assert len(frankfurter(calls)) == 1
    assert summary["frankfurter_failed"] == ["JPY", "USD", "EUR->CHF", "EUR->USD"]
    assert cs._frankfurter_rejected == {}


@pytest.mark.asyncio
async def test_an_outage_midway_stops_the_per_code_retries(monkeypatch, session):
    """Bounded the other way: once the provider stops answering, the codes not yet
    asked are not asked one by one into it."""
    at(datetime(2026, 10, 16, 18, 0), monkeypatch)
    _drifted(monkeypatch)
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, **kw):
            calls.append((url, dict(params or {})))
            if url == CurrencyService.FALLBACK_API_URL:
                return FakeResponse(FALLBACK)
            return FakeResponse({}, 404 if len(calls) == 1 else 503)

    monkeypatch.setattr(cs.httpx, "AsyncClient", lambda *a, **k: FakeClient())

    summary = await CurrencyService(session).warm_rates(
        ["USD", "JPY", "XXA"], target_date=FRIDAY,
    )

    assert summary["frankfurter_requests"] == 2         # the batch, then JPY alone: 503
    assert cs._frankfurter_rejected == {}


# --- Easter ----------------------------------------------------------------------------


@pytest.mark.parametrize("year, expected", [
    (2000, date(2000, 4, 23)), (2019, date(2019, 4, 21)), (2024, date(2024, 3, 31)),
    (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28)),
    (2028, date(2028, 4, 16)), (2038, date(2038, 4, 25)),   # the latest possible
    (2285, date(2285, 3, 22)),                              # the earliest possible
])
def test_easter_sunday(year, expected):
    assert easter_sunday(year) == expected


def test_good_friday_and_easter_monday_are_target_closing_days():
    assert is_target_closing_day(date(2026, 4, 3)) and is_target_closing_day(date(2026, 4, 6))
    assert is_target_closing_day(GOOD_FRIDAY) and is_target_closing_day(EASTER_MONDAY)
    assert not is_target_closing_day(THURSDAY)
    assert not is_target_closing_day(EASTER_MONDAY + timedelta(days=1))
    assert is_target_closing_day(date(2026, 12, 25))


@pytest.mark.parametrize("now, expected", [
    (datetime(2027, 3, 26, 18, 0), THURSDAY),           # Good Friday evening
    (datetime(2027, 3, 29, 18, 0), THURSDAY),           # Easter Monday evening
    (datetime(2027, 3, 30, 10, 0), THURSDAY),           # Tuesday, not yet out
    (datetime(2027, 3, 30, 18, 0), date(2027, 3, 30)),  # Tuesday, out
])
def test_the_last_ecb_publication_day_over_easter(monkeypatch, now, expected):
    at(now, monkeypatch)
    assert CurrencyService(None)._last_ecb_publication_day(now.date()) == expected


# The stock scheduler's slots in UTC (Berlin is UTC+1 until 2027-03-28, then UTC+2):
# Fri 00 08 11 13 15 18 20 22, Sat 00 08 18, Sun 00 18, Mon 00 08 11 13 15 18 20 22,
# Tue 00 08 11 13 15 (the ECB publishes Tuesday's rates ~16:00 UTC, after which a
# request is legitimately due).
def _easter_slots():
    def berlin(day, hours, offset):
        return [datetime(day.year, day.month, day.day, h) - timedelta(hours=offset) for h in hours]
    fri, sat, sun = GOOD_FRIDAY, GOOD_FRIDAY + timedelta(1), GOOD_FRIDAY + timedelta(2)
    mon, tue = EASTER_MONDAY, EASTER_MONDAY + timedelta(1)
    week = (0, 8, 11, 13, 15, 18, 20, 22)
    return (berlin(fri, week, 1) + berlin(sat, (0, 8, 18), 1) + berlin(sun, (0,), 1)
            + berlin(sun, (18,), 2) + berlin(mon, week, 2) + berlin(tue, (0, 8, 11, 13, 15), 2))


@pytest.mark.asyncio
async def test_easter_costs_no_request_once_thursday_is_held(monkeypatch, session):
    calls = _install(monkeypatch, closed={GOOD_FRIDAY, EASTER_MONDAY})
    seed(session, THURSDAY)
    await session.commit()

    summaries = []
    for moment in _easter_slots():
        at(moment, monkeypatch)
        summaries.append(await CurrencyService(session).warm_rates(
            ["USD", "JPY"], target_date=moment.date(), days_back=7, extra_pairs=BASE_PAIRS,
        ))

    assert not any("frankfurter_failed" in s for s in summaries)
    assert len(_easter_slots()) == 26
    assert len(frankfurter(calls)) == 0
