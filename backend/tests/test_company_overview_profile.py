"""
The Company tab overview's `profile` (docs/company-financials.md, *The overview*).

One source per company for Yahoo's figures — a held company's listing rows, any other's
chunk row, never a mix — the price line from the market closes (not the figures' split
basis), and an upside only when the target and the close are in one currency and current.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.clock import utc_today, utcnow
from app.models.analyst_rating import AnalystRating
from app.models.company_financials import (
    CompanyPrice,
    CompanySplit,
    CompanyYahooProfile,
    SecCompany,
)
from app.models.earnings_event import EarningsEvent
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.market_price import MarketPrice
from app.schemas.company_financials import CompanyFinancialsProfile
from app.services.company_financials_service import (
    CompanyFinancialsService,
    price_line,
    price_series,
)
from tests.test_company_financials_contract import _fields, _seed

NVDA, APPLE = 1045810, 320193
COUNTS = dict(strong_buy=5, buy=10, hold=3, sell=1, strong_sell=0)


def _weekly(db, cik, closes, currency="USD"):
    """`closes` ending two days ago, a week apart, oldest first."""
    end = utc_today() - timedelta(days=2)
    for i, close in enumerate(reversed(closes)):
        db.add(
            CompanyPrice(
                cik=cik, date=end - timedelta(weeks=i), close=close, currency=currency
            )
        )


# ------------------------------------------------------------------ the price line


def test_the_price_line_reads_the_newest_close_and_its_history():
    today = date(2026, 11, 6)
    closes = [(today - timedelta(weeks=w), 100.0 + (60 - w)) for w in range(60, -1, -1)]
    line = price_line(closes)
    assert line["price"] == 160.0 and line["price_date"] == "2026-11-06"
    # The newest close on or before a month back: five weeks, as weekly closes fall.
    assert line["return_1m"] == pytest.approx(160 / 155 - 1)
    assert line["return_1y"] == pytest.approx(160 / 108 - 1)  # 52 weeks back
    assert line["return_5y"] is None, "no close five years back"
    year = [c for d, c in closes if d > today - timedelta(days=365)]
    assert (line["week52_low"], line["week52_high"]) == (min(year), 160.0)
    assert set(line) == {
        "price",
        "price_date",
        "return_1m",
        "return_1y",
        "return_5y",
        "return_10y",
        "week52_low",
        "week52_high",
    }
    assert all(v is None for v in price_line([]).values())


def test_the_price_series_follows_the_range_from_the_newest_close():
    today = date(2026, 11, 6)
    closes = [
        (today - timedelta(weeks=w), 50.0 + (600 - w) / 10) for w in range(600, -1, -1)
    ]
    five = price_series(closes, "5y")
    assert five["points"][0]["period_end"] > "2021-11-06"
    assert five["points"][-1] == {
        "period_end": "2026-11-06",
        "label": "2026-11-06",
        "value": 110.0,
        "yoy": pytest.approx(110.0 / 104.8 - 1),
    }
    assert len(price_series(closes, "max")["points"]) == 601
    assert price_series([], "10y")["points"] == []


# ------------------------------------------------------------------ one source per company


@pytest.mark.asyncio
async def test_a_held_company_reads_its_listings_rows_and_never_the_chunks(db):
    await _seed(db)
    nvda = await db.get(SecCompany, NVDA)
    nvda.sic_description, nvda.fiscal_year_end = (
        "Semiconductors & Related Devices",
        "0126",
    )
    db.add(
        FundamentalMetrics(
            security_id=1,
            forward_pe=30.0,
            peg_ratio=1.5,
            fwd_eps_growth=0.4,
            target_low_price=80.0,
            target_mean_price=110.0,
            target_high_price=150.0,
            number_of_analysts=50,
            data_currency="USD",
            business_summary="Chips.",
            last_updated=utcnow(),
        )
    )
    db.add(AnalystRating(security_id=1, last_updated=utcnow(), **COUNTS))
    db.add(
        EarningsEvent(
            security_id=1, earnings_date=utcnow() + timedelta(days=10), eps_estimate=0.9
        )
    )
    db.add(
        EarningsEvent(
            security_id=1,
            earnings_date=utcnow() + timedelta(days=100),
            eps_estimate=1.1,
        )
    )
    # A chunk row for the same company must never be read: one company, one source.
    db.add(
        CompanyYahooProfile(
            cik=NVDA, forward_pe=99.0, fetched_at=utcnow(), business_summary="Wrong"
        )
    )
    _weekly(db, NVDA, [90.0, 95.0])
    db.add(
        MarketPrice(
            security_id=1,
            date=utc_today() - timedelta(days=1),
            close_price=Decimal("100"),
            currency="USD",
            source="test",
        )
    )
    await db.commit()

    profile = (await CompanyFinancialsService(db).get_financials(nvda))["profile"]

    assert set(profile) == _fields(CompanyFinancialsProfile)
    assert profile["industry"] == "Semiconductors & Related Devices"
    assert profile["fiscal_year_end"] == "0126"
    assert (profile["forward_pe"], profile["description"]) == (30.0, "Chips.")
    assert profile["rating"] == {**COUNTS, "consensus": "Buy"} and profile["analysts"] == 50
    assert profile["next_eps_estimate"] == 0.9
    assert profile["price"] == 100.0, "the held listing's newest daily close"
    assert profile["target_upside"] == pytest.approx(0.1)
    assert profile["yahoo_as_of"] is not None


@pytest.mark.asyncio
async def test_a_company_nobody_holds_reads_the_chunks_row(db):
    await _seed(db)
    db.add(
        CompanyYahooProfile(
            cik=APPLE,
            forward_pe=28.0,
            target_mean_price=250.0,
            data_currency="USD",
            business_summary="Phones.",
            fetched_at=utcnow(),
            next_earnings_date=utcnow() + timedelta(days=5),
            next_eps_estimate=2.1,
            **COUNTS,
        )
    )
    _weekly(db, APPLE, [190.0, 200.0])
    await db.commit()

    profile = (
        await CompanyFinancialsService(db).get_financials(
            await db.get(SecCompany, APPLE)
        )
    )["profile"]

    assert (profile["forward_pe"], profile["description"]) == (28.0, "Phones.")
    assert profile["rating"] == {**COUNTS, "consensus": "Buy"}
    assert profile["price"] == 200.0
    assert profile["target_upside"] == pytest.approx(0.25)
    assert profile["next_eps_estimate"] == 2.1


@pytest.mark.asyncio
async def test_a_company_the_chunk_has_not_reached_has_no_yahoo_figures(db):
    await _seed(db)
    _weekly(db, APPLE, [190.0, 200.0])
    await db.commit()

    profile = (
        await CompanyFinancialsService(db).get_financials(
            await db.get(SecCompany, APPLE)
        )
    )["profile"]

    for key in (
        "forward_pe",
        "rating",
        "analysts",
        "target_mean",
        "target_upside",
        "description",
        "yahoo_as_of",
    ):
        assert profile[key] is None, key
    assert profile["price"] == 200.0, "the price line needs no Yahoo figure"


@pytest.mark.asyncio
@pytest.mark.parametrize("target_currency, stale", [("EUR", False), ("USD", True)])
async def test_no_upside_across_currencies_or_from_an_old_close(
    db, target_currency, stale
):
    await _seed(db)
    db.add(
        CompanyYahooProfile(
            cik=APPLE,
            target_mean_price=250.0,
            data_currency=target_currency,
            fetched_at=utcnow(),
        )
    )
    end = utc_today() - timedelta(days=30 if stale else 2)
    db.add(CompanyPrice(cik=APPLE, date=end, close=200.0, currency="USD"))
    await db.commit()

    profile = (
        await CompanyFinancialsService(db).get_financials(
            await db.get(SecCompany, APPLE)
        )
    )["profile"]

    assert profile["target_mean"] == 250.0
    assert profile["target_upside"] is None


@pytest.mark.asyncio
async def test_the_price_is_the_market_close_while_the_multiples_take_the_split_basis(
    db,
):
    """A 10:1 split after the newest filing scales the closes for the P/E, never the price."""
    await _seed(db)
    _weekly(db, APPLE, [190.0, 20.0])
    db.add(CompanySplit(cik=APPLE, date=utc_today() - timedelta(days=5), ratio=10.0))
    await db.commit()

    body = await CompanyFinancialsService(db).get_financials(
        await db.get(SecCompany, APPLE), "ttm", "max"
    )

    assert body["profile"]["price"] == 20.0
    assert body["series"]["price"]["points"][-1]["value"] == 20.0
