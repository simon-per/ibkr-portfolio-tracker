"""
A holding that cannot be valued is excluded from attribution, never counted at zero.

`get_performance_attribution` computes `value_change = end_mv - start_mv` per security,
and its `get_eur_value` helper returned `Decimal("0.0")` whenever the price or the FX
rate was missing. So a still-held position whose price went stale read as
**`-start_value`** — precisely the shape CLAUDE.md records the disposal term being added
to fix for *sales*, reached by the other route and never covered. The mirror case (an
unvaluable start) fabricates a gain of the same size.

This endpoint is the worst place in the app for it. It renders one bar per security, so
the fabricated figure is not buried in an aggregate: it is the largest bar on the chart,
labelled with the security's own name.

Two further consequences, both fixed by excluding rather than zeroing:
  * `weight_percent` divides by `total_end_mv`, which the zeroed holding is missing
    from, so every *other* security's weight is inflated.
  * `contribution_percent` divides by `total_pnl`, which the fabricated loss moved.

The distinction that makes exclusion safe: a lot held at neither endpoint never reaches
the valuation helper at all, so a fully-sold position keeps its legitimate zero and is
never confused with one that could not be priced.

Offline: prices and rates are seeded directly, no network.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

import app.models  # noqa: F401  register all mappers
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.market_price import MarketPrice
from app.services.portfolio_service import PortfolioService

START = date(2026, 3, 2)
END = date(2026, 3, 31)


async def _security(db, *, symbol, isin, conid, currency="EUR"):
    security = Security(
        isin=isin, symbol=symbol, description=f"{symbol} Inc", currency=currency,
        conid=conid, asset_category="STK", exchange="NASDAQ",
    )
    db.add(security)
    await db.flush()
    return security


async def _lot(db, security, *, quantity="10", cost="500", open_date=None, close_date=None):
    db.add(TaxLot(
        security_id=security.id, open_date=open_date or (START - timedelta(days=60)),
        quantity=Decimal(quantity), cost_basis=Decimal(cost),
        price_per_unit=Decimal("50"), currency="EUR",
        cost_basis_eur=Decimal(cost), is_open=close_date is None,
        close_date=close_date,
    ))


async def _price(db, security, on_date, close):
    db.add(MarketPrice(
        security_id=security.id, date=on_date,
        close_price=Decimal(close), currency="EUR", source="test",
    ))


@pytest.mark.asyncio
async def test_a_stale_price_is_not_reported_as_losing_the_whole_position(db):
    """The bug: PRICED holds its value, STALE reads -start_value."""
    priced = await _security(db, symbol="PRICED", isin="US0000000001", conid=1)
    stale = await _security(db, symbol="STALE", isin="US0000000002", conid=2)
    await _lot(db, priced)
    await _lot(db, stale)

    for sec in (priced, stale):
        await _price(db, sec, START, "100")
    # Only PRICED has a price at the end of the window.
    await _price(db, priced, END, "110")
    await db.commit()

    result = await PortfolioService(db).get_performance_attribution(START, END)

    symbols = {a["symbol"] for a in result["attributions"]}
    assert "STALE" not in symbols, "an unpriced holding was reported as a contributor"
    assert "PRICED" in symbols
    assert result["unpriced_holdings"] == 1

    # -1000 is what the old code produced for STALE (10 shares x 100 at the start,
    # zero at the end). Nothing in the response may carry it.
    assert all(a["pnl_contribution_eur"] > -1000 for a in result["attributions"])
    assert result["total_pnl_eur"] == pytest.approx(100.0, abs=0.01)


@pytest.mark.asyncio
async def test_a_missing_fx_rate_counts_as_unpriced_too(db):
    """
    `price is None` is not the test. A price whose currency has no FX rate also
    fails to value the holding, and reads as *priced* to any narrower check —
    the same second route `rebalance.ts` was fixed for.
    """
    usd = await _security(db, symbol="NOFX", isin="US0000000003", conid=3, currency="USD")
    eur = await _security(db, symbol="OKAY", isin="US0000000004", conid=4)
    await _lot(db, usd)
    await _lot(db, eur)
    for sec, ccy in ((usd, "USD"), (eur, "EUR")):
        for on_date in (START, END):
            db.add(MarketPrice(
                security_id=sec.id, date=on_date,
                close_price=Decimal("100"), currency=ccy, source="test",
            ))
    await db.commit()  # no USD->EUR rate seeded anywhere

    result = await PortfolioService(db).get_performance_attribution(START, END)

    assert "NOFX" not in {a["symbol"] for a in result["attributions"]}
    assert result["unpriced_holdings"] == 1


@pytest.mark.asyncio
async def test_a_sold_position_keeps_its_real_zero(db):
    """
    The mirror-image bug, and the reason exclusion is safe. A lot sold inside the
    window legitimately ends at zero; treating that as "unpriced" would drop the
    disposal the attribution exists to show.
    """
    sold = await _security(db, symbol="SOLD", isin="US0000000005", conid=5)
    await _lot(db, sold, close_date=START + timedelta(days=10))
    held = await _security(db, symbol="HELD", isin="US0000000006", conid=6)
    await _lot(db, held)

    for sec in (sold, held):
        await _price(db, sec, START, "100")
        await _price(db, sec, START + timedelta(days=10), "100")
    await _price(db, held, END, "100")
    await db.commit()

    result = await PortfolioService(db).get_performance_attribution(START, END)

    assert result["unpriced_holdings"] == 0, "a sold position was mistaken for an unpriced one"
    assert "SOLD" in {a["symbol"] for a in result["attributions"]}


@pytest.mark.asyncio
async def test_a_fully_priced_book_reports_zero_and_changes_nothing(db):
    """The guard must be inert on good data."""
    a = await _security(db, symbol="AAA", isin="US0000000007", conid=7)
    b = await _security(db, symbol="BBB", isin="US0000000008", conid=8)
    await _lot(db, a)
    await _lot(db, b)
    for sec in (a, b):
        await _price(db, sec, START, "100")
        await _price(db, sec, END, "120")
    await db.commit()

    result = await PortfolioService(db).get_performance_attribution(START, END)

    assert result["unpriced_holdings"] == 0
    assert len(result["attributions"]) == 2
    assert result["total_pnl_eur"] == pytest.approx(400.0, abs=0.01)
    # Weights must still sum to the whole book when nothing is excluded.
    assert sum(x["weight_percent"] for x in result["attributions"]) == pytest.approx(100.0, abs=0.05)
