"""
One close-date convention everywhere: a lot sold on D is not held at D's close.

Under the old include-on-close rule a same-day rotation double-counted that day
(the sold lot still 'active on its close date' plus the replacement lot), and a
position sold on 31 December appeared in BOTH the year's Steuerwert snapshot and
its realized gains.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.security_repository import held_at, lot_held_at
from app.services.dividend_service import shares_held_at
from app.services.portfolio_service import BaseFx, PortfolioService

D = date(2026, 3, 10)


def _sec(sid, sym):
    return Security(id=sid, isin=f"US000000000{sid}", symbol=sym, description=sym,
                    currency="EUR", conid=100 + sid, asset_category="STK", exchange="XETRA")


def _lot(sid, open_date, qty, cost, close_date=None):
    return TaxLot(
        security_id=sid, open_date=open_date, close_date=close_date,
        is_open=close_date is None, quantity=Decimal(qty),
        cost_basis=Decimal(cost), cost_basis_eur=Decimal(cost),
        price_per_unit=Decimal(cost) / Decimal(qty), currency="EUR",
    )


def test_a_same_day_rotation_counts_once_on_the_rotation_day():
    svc = PortfolioService.__new__(PortfolioService)  # sync helper needs no DB
    sec_a, sec_b = _sec(1, "OLD"), _sec(2, "NEW")
    lots = [
        (_lot(1, date(2025, 6, 1), "10", "100", close_date=D), sec_a),
        (_lot(2, D, "10", "110"), sec_b),
    ]
    price_cache = {1: {D: Decimal("11")}, 2: {D: Decimal("11")}}
    value = svc._calculate_daily_value(
        D, lots, price_cache, {}, price_currency_cache={1: "EUR", 2: "EUR"},
        base_fx=BaseFx("EUR", {}),
    )
    # Only the replacement position exists at D's close — 110, not 220.
    assert value["market_value_eur"] == pytest.approx(110.0)
    assert value["cost_basis_eur"] == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_a_31_dec_sale_is_realized_gains_not_steuerwert():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        year_end = date(2025, 12, 31)
        session.add(_sec(1, "AAA"))
        session.add(_lot(1, date(2025, 2, 3), "10", "100", close_date=year_end))
        session.add(MarketPrice(security_id=1, date=year_end, close_price=Decimal("12"),
                                currency="EUR", source="test"))
        await session.flush()

        svc = PortfolioService(session)
        base_fx = BaseFx("EUR", {})
        snapshot = await svc.holdings_snapshot_as_of(base_fx, year_end)
        realized = await svc.realized_rows_from_closed_lots(
            base_fx, start=date(2025, 1, 1), end=year_end
        )
        assert snapshot == []            # not held at year-end
        assert len(realized) == 1        # the sale lands in realized gains
        assert float(realized[0]["proceeds"]) == pytest.approx(120.0)
    finally:
        await session.close()
        await engine.dispose()


# ── The one definition: security_repository.lot_held_at / held_at (GitHub issue #43) ──

_EDGES = [  # (open_date, close_date, held at D's close?)
    (D - timedelta(days=1), None, True),
    (D, None, True),                                     # bought on D: held at D's close
    (D + timedelta(days=1), None, False),
    (D - timedelta(days=5), D - timedelta(days=1), False),
    (D - timedelta(days=5), D, False),                   # sold on D: not held at D's close
    (D - timedelta(days=5), D + timedelta(days=1), True),
    (D, D, False),                                       # bought and sold on D
]


@pytest.mark.parametrize("open_date,close_date,expected", _EDGES)
def test_lot_held_at_over_the_day_before_the_day_and_the_day_after(open_date, close_date, expected):
    lot = _lot(1, open_date, "1", "1", close_date=close_date)
    assert lot_held_at(lot, D) is expected


@pytest.mark.asyncio
async def test_the_sql_criterion_and_the_python_helper_agree():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        session.add(_sec(1, "AAA"))
        lots = [_lot(1, o, "1", "1", close_date=c) for o, c, _ in _EDGES]
        session.add_all(lots)
        await session.flush()
        for d in (D - timedelta(days=1), D, D + timedelta(days=1)):
            in_sql = set((await session.execute(
                select(TaxLot.id).where(held_at(d))
            )).scalars())
            assert in_sql == {lot.id for lot in lots if lot_held_at(lot, d)}, d
    finally:
        await session.close()
        await engine.dispose()


def test_ex_date_shares_count_a_buy_on_the_ex_date_and_drop_a_sale_on_it():
    """
    The ex-date rule is kept as `open_date <= ex_date < close_date` (owner, 2026-10-18;
    docs/dividends.md *Shares held on the ex-date*). Strictly the entitlement belongs to
    the holder at the previous close — the buyer on the ex-date gets nothing and the
    seller on it keeps the dividend — so this pins the choice: a change must be deliberate.
    """
    bought_on_ex = _lot(1, D, "10", "100")
    sold_on_ex = _lot(1, date(2025, 6, 1), "7", "70", close_date=D)
    held_through = _lot(1, date(2025, 6, 1), "3", "30")
    lots = [bought_on_ex, sold_on_ex, held_through]
    assert shares_held_at(lots, D) == Decimal("13")                        # 10 + 3, not the 7
    assert shares_held_at(lots, D - timedelta(days=1)) == Decimal("10")    # 7 + 3
