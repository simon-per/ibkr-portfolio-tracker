"""
The decomposition, the value timeline and the contributions, byte for byte against a golden
built before GitHub #73 made them read each cash ledger once.

#73 was a query-only change: the decomposition hands its own `_contribution_inputs` to the
timeline and both share one `CashService`, which reads the flow dates, the first measured
day and the dividend receipts off its memoized rows instead of querying for each. Nothing
it touched may move a figure, and a before/after comparison cannot run across two versions
of the code, so the responses on a fixture carrying every branch those reads feed were
recorded from the code as it stood (`tests/fixtures/decomposition_golden.json`) and are
compared here as canonical JSON.

The fixture is `test_read_path_query_cost`'s decomposition book (CHF base, a USD holding, a
sale, an unpriced holding, a measured balance, a 3a lot, the mixed-currency and gap shapes)
plus what the money-in splice and the cash line branch on: a statement coverage date the
ledger's first IBKR row clamps forward, an older pillar 3a deposit that must not disable
that clamp, an in-kind TRANSFER_IN, a cash transfer, a withdrawal, two deposits on one day
in two currencies, a USD trade and a dividend in an earlier year. Three books: CHF base,
EUR base, and CHF with a trade no rate converts (the cash-unknown branch).

A change that is *meant* to move one of these figures regenerates the golden with
``UPDATE_DECOMPOSITION_GOLDEN=1`` and says why in its commit; this test then fails until
the file is committed beside it.
"""
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from app.accounts import IBKR, PILLAR3A
from app.models.app_settings import AppSetting
from app.models.cash_flow import DEPOSIT_WITHDRAW, TRANSFER, TRANSFER_IN, CashFlow
from app.models.dividend_payment import DividendPayment
from app.models.trade import Trade
from app.repositories.app_settings_repository import CASH_FLOWS_COVERED_FROM_KEY
from app.services.performance_analytics_service import PerformanceAnalyticsService
from app.services.portfolio_service import PortfolioService
from tests.test_performance_analytics import END, MID, START
from tests.test_read_path_query_cost import _decomposition_session

GOLDEN = Path(__file__).parent / "fixtures" / "decomposition_golden.json"

DECOMPOSITION_WINDOWS = [
    (START, END),
    (date(2025, 12, 31), END),
    (date(2025, 6, 15), date(2026, 2, 20)),
]
# Short spans over what moves the cash and Money In lines: the USD buy (and the
# unconvertible one), MID's deposits, sale and dividend up to the first measured balance,
# the last days, and the transfer across the price and rate gap. The decomposition above
# walks the whole span's timeline itself.
TIMELINE_WINDOWS = [
    (date(2026, 2, 26), date(2026, 3, 6)),
    (date(2026, 4, 13), date(2026, 4, 24)),
    (date(2026, 5, 27), END),
    (date(2025, 12, 24), date(2026, 1, 20)),
]


class _NoNetwork:
    """A rate the fixture lacks must not reach Frankfurter: the read refuses instead."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError("no network in this test")


async def _book(base_currency: str, unconvertible_trade: bool):
    engine, s = await _decomposition_session()
    if base_currency != "CHF":
        await s.execute(
            update(AppSetting).where(AppSetting.key == "base_currency")
            .values(value=base_currency)
        )
    s.add(AppSetting(key=CASH_FLOWS_COVERED_FROM_KEY, value="2026-01-01"))
    s.add_all([
        # Older than every IBKR row: the clamp must read IBKR's ledger alone.
        CashFlow(ib_key="P1", flow_date=date(2025, 5, 20), flow_type=DEPOSIT_WITHDRAW,
                 account=PILLAR3A, amount=Decimal("700"), amount_eur=Decimal("744.31"),
                 currency="CHF"),
        # In kind: no cash, but the first IBKR row, so coverage clamps forward to it.
        CashFlow(ib_key="TI1", flow_date=date(2026, 1, 15), flow_type=TRANSFER_IN,
                 account=IBKR, amount=Decimal("0"), amount_eur=Decimal("0"), currency="EUR"),
        CashFlow(ib_key="TR1", flow_date=date(2026, 4, 20), flow_type=TRANSFER,
                 account=IBKR, amount=Decimal("-20"), amount_eur=Decimal("-20"),
                 currency="EUR"),
        # A second deposit on the in-window deposit's day, in another currency.
        CashFlow(ib_key="C2", flow_date=MID, flow_type=DEPOSIT_WITHDRAW, account=IBKR,
                 amount=Decimal("200"), amount_eur=Decimal("180.37"), currency="USD"),
        CashFlow(ib_key="W1", flow_date=date(2026, 5, 4), flow_type=DEPOSIT_WITHDRAW,
                 account=IBKR, amount=Decimal("-150"), amount_eur=Decimal("-150"),
                 currency="EUR"),
        Trade(ib_key="B2", conid="102", security_id=2, symbol="USD",
              trade_date=date(2026, 3, 4), buy_sell="BUY", quantity=Decimal("3"),
              price=Decimal("100.13"), proceeds=Decimal("-300.39"),
              commission=Decimal("-1.07"), currency="USD"),
        DividendPayment(security_id=7, ex_date=date(2025, 9, 15), pay_date=date(2025, 9, 15),
                        currency="EUR", shares_held=Decimal("4"),
                        gross_amount_eur=Decimal("5.13"), withholding_tax_eur=Decimal("0.77"),
                        net_amount_eur=Decimal("4.36"), source="ibkr"),
    ])
    if unconvertible_trade:
        s.add(Trade(ib_key="X1", conid="101", security_id=1, symbol="HELD",
                    trade_date=date(2026, 3, 5), buy_sell="BUY", quantity=Decimal("1"),
                    price=Decimal("90"), proceeds=Decimal("-90"), commission=Decimal("0"),
                    currency="XXX"))
    await s.commit()
    return engine, s


def _iso(window):
    return f"{window[0].isoformat()}..{window[1].isoformat()}"


async def _responses(s) -> dict:
    """Each call on fresh services, as each request would be served."""
    out = {"decomposition": {}, "timeline": {}}
    for window in DECOMPOSITION_WINDOWS:
        out["decomposition"][_iso(window)] = \
            await PerformanceAnalyticsService(s).decomposition(*window)
    for window in TIMELINE_WINDOWS:
        out["timeline"][_iso(window)] = \
            await PortfolioService(s).get_portfolio_value_over_time(*window)
    out["contributions"] = await PortfolioService(s).get_contributions(as_of=END)
    return out


async def _snapshot(monkeypatch) -> dict:
    from app.services import currency_service
    monkeypatch.setattr(currency_service.httpx, "AsyncClient", _NoNetwork)
    assert currency_service.httpx is httpx
    books = {}
    for name, base, unconvertible in (
        ("chf", "CHF", False), ("eur", "EUR", False), ("chf_unconverted", "CHF", True),
    ):
        engine, s = await _book(base, unconvertible)
        try:
            books[name] = await _responses(s)
        finally:
            await s.close()
            await engine.dispose()
    # Canonical JSON: what the wire would carry, keys sorted, floats at full repr.
    return json.loads(json.dumps(books, sort_keys=True))


@pytest.mark.asyncio
async def test_the_responses_match_the_golden_recorded_before_the_shared_reads(monkeypatch):
    actual = await _snapshot(monkeypatch)
    if os.environ.get("UPDATE_DECOMPOSITION_GOLDEN"):
        GOLDEN.write_text(json.dumps(actual, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        pytest.skip("golden rewritten; commit it with the change that moved the figures")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for book in expected:
        for surface in expected[book]:
            assert actual[book][surface] == expected[book][surface], (book, surface)
    assert actual == expected


@pytest.mark.asyncio
async def test_the_fixture_reaches_the_branches_it_is_for():
    """A golden over a book that misses a branch proves nothing about that branch."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    chf, unconverted = golden["chf"], golden["chf_unconverted"]
    contributions = chf["contributions"]
    # The coverage date clamped forward to the first IBKR row, past the older 3a deposit.
    assert contributions["coverage_from"] == "2026-01-15"
    assert contributions["transfer_in_date"] == "2026-01-15"
    assert contributions["deposits_from"] == "2025-05-20"
    window = chf["decomposition"][_iso((START, END))]
    assert window["cash_source"] == "mixed"
    assert window["window"]["cash_adjustment_eur"] not in (None, 0)
    assert [y["year"] for y in window["years"]] == [2025, 2026]
    assert unconverted["decomposition"][_iso((START, END))]["cash_source"] == "unknown"
    points = chf["timeline"][_iso((date(2026, 4, 13), date(2026, 4, 24)))]
    assert {p["cash_source"] for p in points} == {"derived", "mixed"}
    assert {p["cash_source"] for p in unconverted["timeline"][_iso(TIMELINE_WINDOWS[0])]} \
        == {"unknown"}
    assert golden["eur"]["decomposition"][_iso((START, END))]["base_currency"] == "EUR"
