"""
A spun-off line is not a held position before the action that created it.

IBKR reports the received shares against the *parent's* tax lots: the child inherits the
parent's `open_date` (the holding period carries over) and a slice of the parent's cost
basis. So the lot claims ownership from a date when the instrument had no listing, and the
valuation counted it as a held security it could not price — for 7½ months on this
account, after MBGL was spun out of SPGI on 2026-06-30 against lots dated 2025-11-06.
`unpriced_holdings` was 1 across that whole stretch, and the client's `isMeasurable`
correctly refused every one of those days: six months of the monthly-returns table blank,
November 2025 measured over three days, and a "YTD" figure covering six weeks.

The guards matter as much as the rule. Flooring the wrong security deletes a real holding
from its own history, so every ambiguous case must resolve to *not* flooring — which only
leaves the existing warning in place.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.corporate_action import CorporateAction
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services.portfolio_service import BaseFx, PortfolioService
from app.clock import utc_today
from tests.memory_db import memory_engine

# The parent is bought well before the window; the spinoff lands inside it.
LOT_OPEN = date(2026, 1, 5)
SPINOFF = date(2026, 3, 18)
START = date(2026, 3, 2)
END = date(2026, 3, 31)

PARENT, CHILD = 1, 2


async def _session(
    *,
    action_type: str = "SPINOFF",
    action_quantity: str | None = "10",
    action_security_id: int | None = CHILD,
    child_extra_lot: bool = False,
    record_action: bool = True,
):
    """
    Parent priced throughout; child priced only from the spinoff date, which is what a
    newly listed instrument looks like. Cost is split 950/50, mirroring IBKR reallocating
    part of the parent's basis to the child at the *parent's* open date.
    """
    engine = await memory_engine()
    session = AsyncSession(engine, expire_on_commit=False)

    for sid, sym in ((PARENT, "PAR"), (CHILD, "CHI")):
        session.add(Security(
            id=sid, isin=f"US{sid:010d}1", symbol=sym, description=sym, currency="EUR",
            conid=200 + sid, asset_category="STK", exchange="NYSE",
        ))

    def _lot(sid, cost, qty="10", open_date=LOT_OPEN):
        return TaxLot(
            security_id=sid, open_date=open_date, quantity=Decimal(qty),
            cost_basis=Decimal(cost), cost_basis_eur=Decimal(cost),
            price_per_unit=Decimal(cost) / Decimal(qty), currency="EUR", is_open=True,
        )

    session.add_all([_lot(PARENT, "950"), _lot(CHILD, "50")])
    if child_extra_lot:
        # Shares of the child bought outright, long before the distribution.
        session.add(_lot(CHILD, "120", qty="4", open_date=date(2026, 1, 20)))

    # Priced through today as well, so the summary walk (which always values `today`) is
    # exercised by the same fixture rather than reporting a stale feed.
    d = LOT_OPEN
    last = max(END, utc_today())
    while d <= last:
        session.add(MarketPrice(security_id=PARENT, date=d, close_price=Decimal("100"),
                                currency="EUR", source="test"))
        if d >= SPINOFF:
            session.add(MarketPrice(security_id=CHILD, date=d, close_price=Decimal("5"),
                                    currency="EUR", source="test"))
        d += timedelta(days=1)

    if record_action:
        session.add(CorporateAction(
            ib_key="ca-1", conid="202", security_id=action_security_id, symbol="CHI",
            action_date=SPINOFF, action_type=action_type,
            quantity=None if action_quantity is None else Decimal(action_quantity),
            value=Decimal("0"), proceeds=Decimal("0"), currency="EUR",
            description="PAR SPINOFF 1 FOR 1 (CHI)",
        ))

    await session.flush()
    await session.commit()
    return engine, session


async def _timeline(**kwargs):
    engine, session = await _session(**kwargs)
    try:
        return await PortfolioService(session).get_portfolio_value_over_time(START, END)
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_spun_off_line_is_not_an_unpriced_holding_before_it_existed():
    by_date = {r["date"]: r for r in await _timeline()}

    before = by_date["2026-03-17"]
    assert before["unpriced_holdings"] == 0
    # Only the parent is valued, and its close still carried the business being spun off —
    # counting the child beside it would double-count, which is why this is the arithmetic
    # rather than a workaround for the missing prices.
    assert before["market_value_eur"] == pytest.approx(1000.0)
    # The cost stays where the lot puts it: the parent's basis was reduced by exactly what
    # the child received, so the pair sums to the original outlay on the original date.
    assert before["cost_basis_eur"] == pytest.approx(1000.0)

    after = by_date["2026-03-18"]
    assert after["unpriced_holdings"] == 0
    assert after["market_value_eur"] == pytest.approx(1050.0)  # 10x100 + 10x5
    assert after["cost_basis_eur"] == pytest.approx(1000.0)    # unchanged by the action


@pytest.mark.asyncio
async def test_every_day_of_the_window_is_measurable():
    """
    The reported symptom. One unvaluable holding makes the client drop the day entirely, so
    a 0.2%-of-book stub blanked six months of monthly returns; nothing here may be partial.
    """
    timeline = await _timeline()
    assert timeline
    assert all(r["unpriced_holdings"] == 0 for r in timeline)


@pytest.mark.asyncio
async def test_without_a_recorded_action_the_warning_still_fires():
    """
    The floor is driven by recorded fact, and no record means no licence to drop a holding
    quietly: a security we simply failed to backfill must keep shouting. That is the same
    shape on the wire, and telling them apart is exactly what the corporate action is for.
    """
    by_date = {r["date"]: r for r in await _timeline(record_action=False)}
    assert by_date["2026-03-17"]["unpriced_holdings"] == 1
    assert by_date["2026-03-17"]["market_value_eur"] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_a_parent_side_row_with_no_quantity_change_does_not_floor_the_parent():
    """
    The catastrophic direction. A spinoff's parent-side row carries a basis adjustment and
    no quantity change; flooring on it would drop the parent out of every valuation before
    the action — here, the entire 1000 of market value.
    """
    by_date = {r["date"]: r for r in await _timeline(
        action_security_id=PARENT, action_quantity="0",
    )}
    assert by_date["2026-03-17"]["market_value_eur"] == pytest.approx(1000.0)
    # The child is still unexplained, so it is still reported — not silently dropped.
    assert by_date["2026-03-17"]["unpriced_holdings"] == 1


@pytest.mark.asyncio
async def test_a_forward_split_does_not_floor_a_long_held_position():
    """
    A forward split also adds shares, but to a security that was already held. This is why
    POSITION_CREATING_ACTIONS is far narrower than SPLIT_LIKE_ACTIONS.
    """
    by_date = {r["date"]: r for r in await _timeline(
        action_security_id=PARENT, action_type="FORWARDSPLIT", action_quantity="10",
    )}
    assert by_date["2026-03-17"]["market_value_eur"] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_a_spinoff_into_a_security_already_held_does_not_floor_it():
    """
    A distribution of something already owned must not floor the shares held before it. The
    action accounts for 10 of the 14 held at that date, so the position did not begin there.
    """
    by_date = {r["date"]: r for r in await _timeline(child_extra_lot=True)}
    # 10 parent shares at 100 plus the 4 outright child shares, which have no price yet —
    # so the day stays honest about being partial rather than losing them silently.
    assert by_date["2026-03-17"]["unpriced_holdings"] == 1
    assert by_date["2026-03-17"]["market_value_eur"] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_an_action_with_no_resolved_security_is_ignored():
    by_date = {r["date"]: r for r in await _timeline(action_security_id=None)}
    assert by_date["2026-03-17"]["unpriced_holdings"] == 1


@pytest.mark.asyncio
async def test_the_year_end_snapshot_omits_a_line_that_did_not_exist_yet():
    """
    Steuerwert is the 31 December value, and a partial one is badged as partial — so before
    this, a year-end before the spinoff served a wealth-tax base flagged incomplete over a
    holding that did not exist. The child belongs in neither the total nor the skip list.
    """
    engine, session = await _session()
    try:
        svc = PortfolioService(session)
        base_fx = BaseFx("EUR", {})

        before = await svc.holdings_snapshot_as_of(base_fx, date(2026, 3, 17))
        assert [row["symbol"] for row in before] == ["PAR"]
        assert svc.last_snapshot_skipped == []

        after = await svc.holdings_snapshot_as_of(base_fx, SPINOFF)
        assert sorted(row["symbol"] for row in after) == ["CHI", "PAR"]
        assert svc.last_snapshot_skipped == []
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_summary_and_the_last_timeline_point_still_agree():
    """
    `/api/portfolio/summary` and the chart read the same completeness off two different
    walks; the floor has to reach both or the headline and the last point disagree.
    """
    engine, session = await _session()
    try:
        svc = PortfolioService(session)
        timeline = await svc.get_portfolio_value_over_time(START, END)
        summary = await svc.get_current_portfolio_summary()
        assert summary["unpriced_holdings"] == timeline[-1]["unpriced_holdings"] == 0
    finally:
        await session.close()
        await engine.dispose()
