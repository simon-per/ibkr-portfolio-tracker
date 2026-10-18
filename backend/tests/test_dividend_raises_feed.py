"""
The raises feed — `raises[]` on `/api/dividends/forecast-history` (GitHub issue #75).

Each company's raise or cut, dated the day the forecast first carried it: the old and
new level per share in its own currency, the change, and the annual income it added in
the base currency. It is the breakdown's `raises` part per security per day instead of
summed, so the questions worth pinning are the breakdown's own: a purchase, a split, a
fund's distribution and a first forecast are not raises — and the entries add up to the
"+X from raises" beside them, because they are one computation, not two.
"""
import ast
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.security import Security
from app.repositories.dividend_forecast_snapshot_repository import (
    DividendForecastSnapshotRepository,
)
from app.schemas.portfolio import DividendRaise
from app.services.base_fx import BaseFx
from app.services.dividend_service import DividendService
from tests.test_dividend_forecast_snapshot import (
    DAY_1, DAY_2, DAY_3, THREE_RATES, _base_currency, _line, _make_session, _three_days,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"
EUR = BaseFx("EUR", {})


@pytest.mark.asyncio
async def test_a_raise_is_the_breakdowns_own_part_for_that_company(monkeypatch):
    """
    `_three_days`: #1 is bought at an unchanged level, #2 raises from 10 to 12.5 USD a
    share (its level, 9 then 10 EUR, while the dollar falls from 0.9 to 0.8), #3 is
    forecast for the first time. Only #2 raised.
    """
    engine, session = await _make_session()
    try:
        await _three_days(session)
        _base_currency(monkeypatch, THREE_RATES)

        history = await DividendService(session).get_forecast_history()

        assert history["raises"] == [{
            "date": "2026-09-02",
            "security_id": 2,
            "symbol": "S2",
            "currency": "USD",
            "level_from": 10.0,                 # 9 EUR at 0.9
            "level_to": 12.5,                   # 10 EUR at 0.8
            "change_pct": 25.0,
            "annual_change_eur": 20.25,         # 10 shares x 2.50 USD x 0.9 x 0.90 CHF
        }]
        # One computation: the entries are the breakdown's `raises`, split by company.
        assert sum(r["annual_change_eur"] for r in history["raises"]) == (
            history["points"][-1]["breakdown"]["raises"]
        )
        assert set(history["raises"][0]) == set(DividendRaise.model_fields)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_funds_distribution_is_never_in_the_feed(monkeypatch):
    """The same move on a fund (`is_known_etf_isin`) is timing, as in the breakdown."""
    from app.etf_mappings import ETF_ALLOCATIONS

    engine, session = await _make_session()
    try:
        await _three_days(session)
        (await session.get(Security, 2)).isin = ETF_ALLOCATIONS["VT"]["isins"][0]
        await session.commit()
        _base_currency(monkeypatch, THREE_RATES)

        history = await DividendService(session).get_forecast_history()

        assert history["raises"] == []
        assert history["points"][-1]["breakdown"]["raises"] == 0.0
    finally:
        await session.close()
        await engine.dispose()


async def _store(session, day, lines):
    total = sum((ln["next_12m_eur"] for ln in lines), Decimal("0"))
    await DividendForecastSnapshotRepository(session).upsert(
        {"snapshot_date": day, "next_12m_eur": total, "ttm_net_eur": Decimal("1")},
        lines=lines,
    )


@pytest.mark.asyncio
async def test_newest_first_with_cuts_beside_raises(monkeypatch):
    engine, session = await _make_session()
    try:
        session.add(Security(id=2, isin="US0000000002", symbol="BBB", description="Beta",
                             currency="USD", conid=102, asset_category="STK",
                             exchange="NYSE"))
        await session.flush()
        await _store(session, DAY_1, [
            _line(1, "10", "40", level="4"), _line(2, "10", "90", "0.9", "USD", "9"),
        ])
        await _store(session, DAY_2, [
            _line(1, "10", "40", level="4.4"), _line(2, "10", "90", "0.9", "USD", "9"),
        ])
        await _store(session, DAY_3, [
            _line(1, "10", "40", level="4.4"), _line(2, "10", "90", "0.9", "USD", "8.1"),
        ])
        _base_currency(monkeypatch, EUR)

        history = await DividendService(session).get_forecast_history()

        assert [(r["date"], r["symbol"], r["level_from"], r["level_to"], r["change_pct"],
                 r["annual_change_eur"]) for r in history["raises"]] == [
            ("2026-09-03", "BBB", 10.0, 9.0, -10.0, -9.0),
            ("2026-09-02", "AAA", 4.0, 4.4, 10.0, 4.0),
        ]
        assert round(sum(r["annual_change_eur"] for r in history["raises"]), 2) == (
            history["points"][-1]["breakdown"]["raises"]
        )
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_split_is_not_a_cut_in_the_feed(monkeypatch):
    """Four for one: four times the shares at a quarter of the level, the same money."""
    engine, session = await _make_session()
    try:
        await _store(session, DAY_1, [_line(1, "10", "40", level="4")])
        await _store(session, DAY_2, [_line(1, "40", "40", level="1")])
        _base_currency(monkeypatch, EUR)

        history = await DividendService(session).get_forecast_history()

        assert history["raises"] == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_without_a_level_on_both_days_nothing_is_in_the_feed(monkeypatch):
    engine, session = await _make_session()
    try:
        await _store(session, DAY_1, [_line(1, "10", "40")])
        await _store(session, DAY_2, [_line(1, "10", "44", level="4.4")])
        _base_currency(monkeypatch, EUR)

        assert (await DividendService(session).get_forecast_history())["raises"] == []
    finally:
        await session.close()
        await engine.dispose()


# ---- The family: one implementation of the level arithmetic ---------------------


def _divides_a_level(tree: ast.AST) -> bool:
    """A division with a stored level (`x.level_eur` or `x["level_eur"]`) on one side."""
    def is_level(node):
        return (isinstance(node, ast.Attribute) and node.attr == "level_eur") or (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant) and node.slice.value == "level_eur"
        )
    return any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        and (is_level(node.left) or is_level(node.right))
        for node in ast.walk(tree)
    )


def test_only_the_attribution_turns_a_stored_level_into_a_raise():
    """
    A raise is measured in one place, `dividend_forecast_attribution.attribute_holding`:
    the feed and the breakdown both read it through `attribute_by_holding`. A second
    reading of `level_eur` per share would be a second raise arithmetic free to drift
    from the "+X from raises" it is listed beside — the question here is whether a copy
    exists at all, not whether two copies agree today.
    """
    found = sorted(
        path.relative_to(APP_DIR.parent).as_posix()
        for path in APP_DIR.rglob("*.py")
        if _divides_a_level(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert found == ["app/services/dividend_forecast_attribution.py"]


def test_the_server_classifies_no_raise_by_a_band_of_its_own():
    """
    "Raised", "cut" and "flat" are decided once, on the client (`RAISE_BAND_PCT` in
    `frontend/src/lib/dividendGrowth.ts`), for the count, the calendar's tag and the
    feed alike. The server serves signed percentages; a band constant here would be a
    second definition of a raise (`dividendGrowth.test.ts` pins the client's side).
    """
    named = sorted(
        f"{path.name}:{node.lineno}"
        for path in APP_DIR.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        and "RAISE" in node.id.upper() and "BAND" in node.id.upper()
    )
    assert named == []


def test_a_level_that_moved_by_rounding_alone_is_no_raise():
    """A stored level re-divided by its own day's rate is a few parts in 10^8 off."""
    from app.services.dividend_forecast_attribution import Holding, attribute_holding

    fx0, fx1 = Decimal("0.02860000"), Decimal("0.02873000")
    a = Holding(Decimal("100"), Decimal("80.08"), fx0, (Decimal("28") * fx0).quantize(Decimal("1e-8")))
    b = Holding(Decimal("100"), Decimal("80.44"), fx1, (Decimal("28") * fx1).quantize(Decimal("1e-8")) + Decimal("1e-8"))
    change = attribute_holding(a, b, Decimal("1"), Decimal("1"))
    assert change.parts["raises"] == 0
    assert sum(change.parts.values()) == b.next_12m_eur - a.next_12m_eur

    raised = Holding(Decimal("100"), Decimal("83.00"), fx1, (Decimal("29") * fx1).quantize(Decimal("1e-8")))
    assert attribute_holding(a, raised, Decimal("1"), Decimal("1")).parts["raises"] > 0
