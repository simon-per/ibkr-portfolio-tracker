"""
Today's uninvested cash has one read for a share-of-the-book denominator:
`CashService.uninvested_cash` (GitHub #28).

Allocation read it inline (`cash_source()` + `balance_as_of(balance_events(...))`, positive
only) while Look-through divided by holdings alone, so the same money read higher on one
tab than on the other under the same "% of portfolio" header. The scan pins the family
rather than the two call sites: both tabs go through the helper, neither re-derives the
balance itself, and no other service picks up `balance_as_of` without being named here
with its reason.

A trade with no FX rate makes the balance unknown rather than short (GitHub #59): the
summary card reads the same helper, and every other reader of the raw events consults
`unconverted_trades` — so no surface serves the partial sum, and the summary counts it.
"""
import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.clock import utc_today
from app.models.cash_flow import CashFlow, DEPOSIT_WITHDRAW
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.trade import Trade
from app.schemas.portfolio import PortfolioSummary
from app.services.allocation_service import AllocationService
from app.services.cash_service import DERIVED, UNKNOWN, CashService, UninvestedCash
from app.services.currency_service import CurrencyService
from app.services.lookthrough_service import LookthroughService
from app.services.portfolio_service import PortfolioService

SERVICES_DIR = Path(__file__).resolve().parents[1] / "app" / "services"

#: The two "% of portfolio" denominators that include cash server-side.
SHARE_OF_BOOK_SERVICES = ("allocation_service.py", "lookthrough_service.py")

#: The low-level reads the helper wraps.
LOW_LEVEL = {"balance_as_of", "balance_events", "cash_source"}

#: Services allowed to call `balance_as_of` themselves, each with its reason. The summary
#: card reads `uninvested_cash` since GitHub #59, and the timeline sweeps the events itself.
BALANCE_AS_OF_ALLOWED = {
    "cash_service.py": "the helper's own home",
}

#: Reads of the derived events or their provenance that bypass `uninvested_cash`. Each
#: hands back a figure or a label that is wrong while a trade is unconverted, so a
#: service calling one must also ask `unconverted_trades` (GitHub #59).
RAW_CASH_READS = {"balance_events", "measured_corrections", "cash_source", "derived_source"}


def _called_attributes(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_both_share_of_book_services_read_cash_through_the_one_helper():
    for name in SHARE_OF_BOOK_SERVICES:
        called = _called_attributes(SERVICES_DIR / name)
        assert "uninvested_cash" in called, f"{name} does not call CashService.uninvested_cash"
        assert not called & LOW_LEVEL, (
            f"{name} calls {sorted(called & LOW_LEVEL)} itself — a second derivation of "
            f"the balance beside uninvested_cash"
        )


def test_no_other_service_derives_todays_balance_on_its_own():
    offenders = [
        path.name
        for path in sorted(SERVICES_DIR.glob("*.py"))
        if path.name not in BALANCE_AS_OF_ALLOWED
        and "balance_as_of" in _called_attributes(path)
    ]
    assert offenders == [], (
        f"{offenders} call balance_as_of: use CashService.uninvested_cash, or name the "
        f"module in BALANCE_AS_OF_ALLOWED with the reason it is not a denominator"
    )


def test_only_a_known_positive_balance_joins_the_book():
    # The server half of a pair: `inBookCash` in frontend/src/lib/portfolioCash.test.ts pins
    # the same four cases for Positions' weight denominator (GitHub #52).
    assert UninvestedCash(DERIVED, Decimal("100")).in_book == Decimal("100")
    assert UninvestedCash(DERIVED, Decimal("-250")).in_book == Decimal("0")
    assert UninvestedCash(DERIVED, Decimal("0")).in_book == Decimal("0")
    assert UninvestedCash(UNKNOWN, None).in_book == Decimal("0")
    assert UninvestedCash(UNKNOWN, None, unconverted=1).in_book == Decimal("0")


def test_every_raw_reader_of_the_cash_events_asks_whether_a_trade_was_left_out():
    offenders = []
    for path in sorted(SERVICES_DIR.glob("*.py")):
        if path.name == "cash_service.py":
            continue
        called = _called_attributes(path)
        if called & RAW_CASH_READS and "unconverted_trades" not in called:
            offenders.append(path.name)
    assert offenders == [], (
        f"{offenders} read the cash events without CashService.unconverted_trades — a "
        f"trade with no FX rate would reach them as a balance short by the whole trade"
    )


# ------------------------------------------------- an unconvertible trade (GitHub #59)

TODAY = utc_today()


@pytest.fixture
def no_foreign_rate(monkeypatch):
    """Every native->EUR lookup fails, as for a currency neither provider serves."""
    async def no_rate(self, currency, *args, **kwargs):
        raise ValueError(f"no {currency} rate")
    monkeypatch.setattr(CurrencyService, "get_exchange_rate", no_rate)


async def _book(db, trade_currency: str) -> None:
    """Holdings 1000 (a EUR stock), a 1000 deposit and a 900 buy in `trade_currency`."""
    db.add(Security(id=1, isin="DE0000000001", symbol="AAA", description="A",
                    currency="EUR", conid=1, asset_category="STK", exchange="XETRA"))
    db.add(TaxLot(security_id=1, open_date=TODAY - timedelta(days=30),
                  quantity=Decimal("10"), cost_basis=Decimal("900"),
                  price_per_unit=Decimal("90"), currency="EUR",
                  cost_basis_eur=Decimal("900"), is_open=True))
    for back in range(15):
        db.add(MarketPrice(security_id=1, date=TODAY - timedelta(days=back),
                           close_price=Decimal("100"), currency="EUR", source="test"))
    db.add(CashFlow(ib_key="D1", flow_date=TODAY - timedelta(days=40),
                    flow_type=DEPOSIT_WITHDRAW, amount=Decimal("1000"), currency="EUR",
                    amount_eur=Decimal("1000")))
    db.add(Trade(ib_key="B1", conid="1", security_id=1, symbol="AAA",
                 trade_date=TODAY - timedelta(days=30), buy_sell="BUY",
                 quantity=Decimal("10"), price=Decimal("90"), proceeds=Decimal("-900"),
                 commission=Decimal("0"), currency=trade_currency,
                 realized_pnl=Decimal("0"), asset_category="STK"))
    await db.commit()


@pytest.mark.asyncio
async def test_an_unconvertible_trade_makes_cash_unknown_and_counted_on_every_surface(
    db, no_foreign_rate
):
    await _book(db, "TWD")
    portfolio = PortfolioService(db)
    base_fx = await portfolio._load_base_fx()

    # Not 1000, the deposit with the purchase silently missing: unknown, and counted.
    cash = await CashService(db).uninvested_cash(base_fx, TODAY)
    assert cash == UninvestedCash(UNKNOWN, None, unconverted=1)

    # The count survives the summary's response_model, and nothing stands in for cash.
    summary = PortfolioSummary.model_validate(
        await portfolio.get_current_portfolio_summary()
    ).model_dump()
    assert summary["cash_source"] == UNKNOWN
    assert summary["cash_unconverted"] == 1
    assert summary["total_cash_eur"] is None
    assert summary["total_value_eur"] is None
    assert summary["total_market_value_eur"] == pytest.approx(1000)

    allocation = await AllocationService(db).get_portfolio_allocation()
    assert allocation["cash_source"] == UNKNOWN
    assert allocation["cash_eur"] is None
    assert "Cash" not in allocation["asset_type_allocation"]
    assert allocation["total_value_eur"] == pytest.approx(1000)

    report = await LookthroughService(db).get_lookthrough()
    assert report["cash_source"] == UNKNOWN
    assert report["cash_eur"] is None
    assert report["cash_pct_of_portfolio"] is None
    assert any("1 trade has no FX rate" in w for w in report["warnings"])

    # The timeline carries the unknown state, never the partial sum.
    points = await portfolio.get_portfolio_value_over_time(TODAY - timedelta(days=10), TODAY)
    assert points
    for p in points:
        assert p["cash_source"] == UNKNOWN
        assert p["cash_eur"] == 0.0
        assert p["total_value_eur"] == pytest.approx(p["market_value_eur"])


@pytest.mark.asyncio
async def test_a_book_whose_every_trade_converts_is_unchanged(db, no_foreign_rate):
    await _book(db, "EUR")
    portfolio = PortfolioService(db)
    base_fx = await portfolio._load_base_fx()

    cash = await CashService(db).uninvested_cash(base_fx, TODAY)
    assert cash == UninvestedCash(DERIVED, Decimal("100"))

    summary = PortfolioSummary.model_validate(
        await portfolio.get_current_portfolio_summary()
    ).model_dump()
    assert summary["cash_source"] == DERIVED
    assert summary["cash_unconverted"] == 0
    assert summary["total_cash_eur"] == pytest.approx(100)
    assert summary["total_value_eur"] == pytest.approx(1100)

    report = await LookthroughService(db).get_lookthrough()
    assert report["cash_eur"] == pytest.approx(100)
    assert not any("FX rate" in w for w in report["warnings"])

    points = await portfolio.get_portfolio_value_over_time(TODAY - timedelta(days=10), TODAY)
    assert points[-1]["cash_source"] == DERIVED
    assert points[-1]["cash_eur"] == pytest.approx(100)
