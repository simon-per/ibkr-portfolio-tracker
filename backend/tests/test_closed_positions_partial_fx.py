"""
A security whose SELL trades only partly convert to the base currency (GitHub issue #26).

`closed_positions` used to keep the convertible trades in `trade_pnl`, so the position
read `realized_source: "trade"` — the source that means authoritative — with only part
of IBKR's figure, beside a warning claiming the security had fallen back to the
closed-lot approximation. Refused whole now: IBKR's figure only when every SELL of the
security converted, otherwise the closed-lot figure, and the warning names exactly the
positions that fell back.

`PortfolioService.get_realized_totals` (the summary's realized headline) dropped an
unconvertible SELL with only a log line; it now counts it in `unpriced_disposals`.

Offline: the currency service is stubbed, no provider is reached.
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.services.performance_analytics_service import PerformanceAnalyticsService
from app.services.portfolio_service import PortfolioService

D1 = date(2026, 4, 1)
D2 = date(2026, 4, 15)
# IBKR's realized figures on the two SELLs. Their sum (40) equals the closed-lot
# estimate, 2 x (120 - 100), so a served 10 or 30 can only be a partial trade sum.
IBKR_PNL = {D1: Decimal("10"), D2: Decimal("30")}


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    s = AsyncSession(engine, expire_on_commit=False)
    s.add(Security(id=1, isin="US0000000021", symbol="TWOSELL", description="x",
                   currency="USD", conid=21, asset_category="STK", exchange="X"))
    await s.flush()
    for closed in (D1, D2):
        s.add(TaxLot(security_id=1, open_date=date(2026, 1, 5), close_date=closed,
                     is_open=False, quantity=Decimal("10"), cost_basis=Decimal("100"),
                     cost_basis_eur=Decimal("100"), price_per_unit=Decimal("10"),
                     currency="USD"))
        s.add(MarketPrice(security_id=1, date=closed, close_price=Decimal("12"),
                          currency="USD", source="test"))
        s.add(ExchangeRate(date=closed, from_currency="USD", to_currency="EUR",
                           rate=Decimal("1"), source="test"))
    for key, d in (("A", D1), ("B", D2)):
        s.add(Trade(ib_key=key, conid="21", security_id=1, symbol="TWOSELL",
                    trade_date=d, buy_sell="SELL", quantity=Decimal("-10"),
                    price=Decimal("12"), proceeds=Decimal("120"),
                    commission=Decimal("0"), currency="USD", realized_pnl=IBKR_PNL[d]))
    await s.commit()
    return engine, s


def _stub_rates(portfolio: PortfolioService, unconvertible: set):
    """Trade-date conversions fail on `unconvertible` dates; the closed-lot path reads
    the stored ExchangeRate rows, so it stays priceable either way."""
    async def fake_rate(currency, on_date):
        if on_date in unconvertible:
            raise ValueError("no rate")
        return Decimal("1")

    portfolio.currency_service.get_exchange_rate = fake_rate


@pytest.mark.asyncio
@pytest.mark.parametrize("unconvertible, source, realized, fallback_warnings", [
    # Every SELL converted: IBKR's own figure, no warning.
    (set(), "trade", 40.0, 0),
    # One of two unconvertible: refused whole, the closed-lot figure, one warning.
    ({D2}, "closed_lots", 40.0, 1),
    # Every SELL unconvertible: the closed-lot figure, as before.
    ({D1, D2}, "closed_lots", 40.0, 1),
])
async def test_a_security_is_served_from_trades_only_when_every_sell_converted(
    unconvertible, source, realized, fallback_warnings,
):
    engine, s = await _session()
    try:
        svc = PerformanceAnalyticsService(s)
        _stub_rates(svc.portfolio, unconvertible)
        out = await svc.closed_positions()
        [p] = out["positions"]
        assert p["realized_source"] == source
        assert p["realized_pnl_eur"] == pytest.approx(realized)
        assert out["summary"]["total_realized_eur"] == pytest.approx(realized)
        fell_back = [w for w in out["warnings"] if "closed-lot approximation" in w]
        assert len(fell_back) == fallback_warnings
        if fell_back:
            assert fell_back[0].startswith("1 security has a SELL trade with no FX rate")
    finally:
        await s.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("unconvertible, gain, omitted", [
    (set(), 40.0, 0),
    ({D2}, 10.0, 1),
    ({D1, D2}, 0.0, 2),
])
async def test_the_summary_counts_the_sells_its_realized_figure_left_out(
    unconvertible, gain, omitted,
):
    engine, s = await _session()
    try:
        portfolio = PortfolioService(s)
        _stub_rates(portfolio, unconvertible)
        totals = await portfolio.get_realized_totals()
        # The trade path still sums what converted; the count is what declares the
        # figure partial rather than letting it read as complete.
        assert totals["total_realized_gain_loss_eur"] == pytest.approx(gain)
        assert totals["unpriced_disposals"] == omitted
    finally:
        await s.close()
        await engine.dispose()
