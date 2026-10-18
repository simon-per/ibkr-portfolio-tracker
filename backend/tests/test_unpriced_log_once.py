"""
A valuation walk logs each unpriced security once, not once per day (GitHub #64).

Both walks logged a WARNING per unpriced security per day, and the price lookup a second
one: 21 unpriced days were 42 lines, and a 5-year read with one long-unpriced holding wrote
thousands into a container log whose rotation then pushed out the sync evidence. Now one
line per security per walk, carrying the day count and the first and last day; the
response's `unpriced_holdings` is still the per-point figure.
"""
import logging
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.portfolio_service import BaseFx, PortfolioService, UnpricedDayLog

LOGGER = "app.services.portfolio_service"
START = date(2026, 3, 2)
END = date(2026, 4, 10)


def _sec(sid, sym, currency):
    return SimpleNamespace(id=sid, symbol=sym, currency=currency)


def _lot(sec, qty="10", cost="100"):
    lot = SimpleNamespace(
        open_date=date(2026, 1, 5), close_date=None,
        quantity=Decimal(qty), cost_basis_eur=Decimal(cost),
    )
    return lot, sec


def _weekdays(first, last):
    d, out = first, []
    while d <= last:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture
def book():
    """
    Three holdings: STALE's prices stop on 6 March, so past the 14-day carry it is
    unpriced from Monday 23 March to the end (and it is split across two lots); NOFX is
    priced but its currency has no rate at all; FINE is valued throughout.
    """
    stale, nofx, fine = _sec(1, "STALE", "EUR"), _sec(2, "NOFX", "USD"), _sec(3, "FINE", "EUR")
    lots = [_lot(stale), _lot(stale, "5", "50"), _lot(nofx), _lot(fine)]
    every_day = _weekdays(date(2026, 1, 5), END)
    price_cache = {
        1: {d: Decimal("11") for d in every_day if d <= date(2026, 3, 6)},
        2: {d: Decimal("30") for d in every_day},
        3: {d: Decimal("12") for d in every_day},
    }
    currency_map = {1: "EUR", 2: "USD", 3: "EUR"}
    return lots, price_cache, {}, currency_map


STALE_DAYS = _weekdays(date(2026, 3, 23), END)
NOFX_DAYS = _weekdays(START, END)


def _lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno >= logging.WARNING]


def _assert_one_line_each(lines):
    assert len(lines) == 2, lines
    stale, nofx = sorted(lines)  # security 1 before security 2
    assert "security 1 (STALE)" in stale
    assert f"on {len(STALE_DAYS)} day(s), {STALE_DAYS[0]}..{STALE_DAYS[-1]}" in stale
    assert "no market price" in stale
    assert "security 2 (NOFX)" in nofx
    assert f"on {len(NOFX_DAYS)} day(s), {NOFX_DAYS[0]}..{NOFX_DAYS[-1]}" in nofx
    assert "no exchange rate for USD" in nofx


def test_the_timeline_logs_one_line_per_unpriced_security(book, caplog):
    lots, price_cache, fx_cache, currency_map = book
    svc = PortfolioService.__new__(PortfolioService)  # sync methods need no DB
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        timeline = svc._calculate_timeline_swept(
            START, END, lots, price_cache, fx_cache, currency_map, BaseFx("EUR", {}),
        )
    _assert_one_line_each(_lines(caplog))
    # The response still says it per point: the log line is a summary, not the signal.
    assert sum(p["unpriced_holdings"] for p in timeline) == len(STALE_DAYS) + len(NOFX_DAYS)


def test_point_valuations_sharing_a_log_count_days_not_lots(book, caplog):
    """
    `_calculate_daily_value` walks lots, and STALE has two: a counter would report twice
    its days. XIRR values two dates per request through one shared log.
    """
    lots, price_cache, fx_cache, currency_map = book
    svc = PortfolioService.__new__(PortfolioService)
    log = UnpricedDayLog()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for d in _weekdays(START, END):
            svc._calculate_daily_value(
                d, lots, price_cache, fx_cache, price_currency_cache=currency_map,
                unpriced_log=log,
            )
        assert _lines(caplog) == []  # nothing until the caller flushes
        assert log.flush("XIRR") == 2
    _assert_one_line_each(_lines(caplog))


def test_a_point_valuation_without_a_shared_log_flushes_its_own(book, caplog):
    lots, price_cache, fx_cache, currency_map = book
    svc = PortfolioService.__new__(PortfolioService)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        value = svc._calculate_daily_value(
            END, lots, price_cache, fx_cache, price_currency_cache=currency_map,
        )
    lines = _lines(caplog)
    assert value["unpriced_holdings"] == 2
    assert len(lines) == 2 and all("on 1 day(s)" in line for line in lines)
