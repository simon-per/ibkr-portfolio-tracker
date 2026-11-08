"""
One source per company for its Yahoo figures and analyst counts (review items DR1, DR2,
2026-11-08; `app/services/company_yahoo.py`).

The watchlist fetched its own `.info` copy of a held company's figures, so its P/E and
growth disagreed with Fundamentals the same morning; and the positions table read each
listing's own counts while the Company overview read the company's lowest-symbol listing,
so GOOGL's row and the Alphabet page it links to showed two ratings. Offline: `yf.Ticker`
is the stub the watchlist rating tests use.
"""

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import app.models  # noqa: F401
from app.clock import utcnow
from app.models.analyst_rating import AnalystRating
from app.models.company_financials import SecurityCik
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.routers.watchlist import get_watchlist
from app.services.company_yahoo import WATCHLIST_FIGURES
from app.services.portfolio_service import PortfolioService
from app.services.watchlist_service import WatchlistService
from tests.test_watchlist_ratings import _item, reads  # noqa: F401 - the fixture

APP = Path(__file__).resolve().parent.parent / "app"

# Deliberately unlike anything the stub's `.info` answers.
STORED = dict(
    trailing_pe=31.5,
    forward_pe=24.25,
    peg_ratio=1.375,
    revenue_growth=0.42,
    earnings_growth=0.61,
    fwd_revenue_growth=0.18,
    fwd_eps_growth=0.22,
    profit_margins=0.31,
    market_cap=987_654_321,
    target_mean_price=140.0,
    number_of_analysts=37,
    data_currency="USD",
)


async def _held(db, sid, symbol, cik=None, **counts):
    db.add(
        Security(
            id=sid,
            isin=f"US00000000{sid:02d}",
            symbol=symbol,
            description=symbol,
            currency="USD",
            conid=9000 + sid,
            asset_category="STK",
            exchange="NASDAQ",
        )
    )
    await db.flush()
    db.add(
        TaxLot(
            security_id=sid,
            open_date=date(2025, 1, 2),
            is_open=True,
            quantity=Decimal("1"),
            cost_basis=Decimal("10"),
            price_per_unit=Decimal("10"),
            currency="USD",
            cost_basis_eur=Decimal("9"),
        )
    )
    if cik is not None:
        db.add(
            SecurityCik(
                security_id=sid, cik=cik, sec_ticker=symbol, cik_checked_at=utcnow()
            )
        )
    if counts:
        db.add(AnalystRating(security_id=sid, last_updated=utcnow(), **counts))
    await db.commit()


@pytest.mark.asyncio
async def test_a_held_tickers_yahoo_columns_are_its_fundamentals_rows(db, reads):  # noqa: F811
    await _held(db, 1, "AAA")
    db.add(FundamentalMetrics(security_id=1, last_updated=utcnow(), **STORED))
    await db.commit()
    await _item(db, "AAA")

    await WatchlistService(db).sync_item("AAA", force=True)
    (row,) = [r for r in await get_watchlist(db=db) if r.yahoo_ticker == "AAA"]

    for column, field in WATCHLIST_FIGURES.items():
        assert getattr(row, column) == STORED[field], column


@pytest.mark.asyncio
async def test_every_listing_of_a_company_shows_the_overviews_counts(db):
    goog = dict(strong_buy=11, buy=43, hold=6, sell=0, strong_sell=0)
    googl = dict(strong_buy=10, buy=43, hold=5, sell=0, strong_sell=0)
    await _held(db, 1, "GOOG", cik=1652044, **goog)
    await _held(db, 2, "GOOGL", cik=1652044, **googl)

    positions = {
        p["symbol"]: p for p in await PortfolioService(db).get_positions_breakdown()
    }
    for symbol in ("GOOG", "GOOGL"):
        shown = positions[symbol]["analyst_rating"]
        assert {f: shown[f] for f in goog} == goog, symbol


def test_only_the_resolver_reads_the_counts_off_a_row():
    """A comprehension of `getattr(row, f) for f in RATING_COUNT_FIELDS` is how the five
    copies began; `company_yahoo.counts_of` is the one."""
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "company_yahoo.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.DictComp, ast.GeneratorExp, ast.ListComp)):
                text = ast.unparse(node)
                if "RATING_COUNT_FIELDS" in text and "getattr(" in text:
                    offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert offenders == []
