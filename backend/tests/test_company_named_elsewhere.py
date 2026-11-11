"""
Company pages for the companies the app names outside the index (review item R5,
2026-11-08; owner's approval 2026-11-07 for the EDGAR and Yahoo requests).

37 of the look-through's top 100 rows linked nowhere: the tab carried the held filers and
the Nasdaq-100 only. The pass now adds the look-through's heaviest companies with a US ISIN
and the watchlist's US tickers on top of the index's capped hundred; and a line QQQM
publishes with a CINS (Linde, Seagate) takes its ISIN from the identity store, or no
look-through row of the company could find its page. Offline: the pass's fake SEC.
"""

from decimal import Decimal

import pytest

from app.clock import utcnow
from app.models.company_financials import SecCompany, SecTicker
from app.models.etf_basket import EtfHolding
from app.models.isin_identity import IsinIdentity
from app.models.watchlist_item import WatchlistItem
from app.services import company_financials_sync
from app.services.lookthrough_service import LookthroughService
from tests.test_company_financials_sync import (
    ALPHABET_CIK,
    APPLE_CIK,
    FACTS_URL,
    FUND,
    NASDAQ_100,
    NVDA_CIK,
    TICKERS_URL,
    UA,
    Edgar,
    _qqqm,
    _sync,
)
from app.config import settings

OTHER_FUND = "IE00B5BMR087"  # any fund's stored basket


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "edgar_user_agent", UA, raising=False)
    monkeypatch.setattr(company_financials_sync, "UNIVERSE_FUNDS", (FUND,))


def _view(*rows):
    async def get_lookthrough(self, limit=50):
        return {"companies": [{"name": name, "isins": isins} for name, isins in rows]}

    return get_lookthrough


async def _named(db):
    for line_no, (isin, ticker) in enumerate(
        [
            ("US0378331005", "AAPL"),
            ("US0378331005", "AAPL"),
            ("CH0012032048", "ROG"),  # Roche: Rogers Corp's ticker on the SEC's list
        ]
    ):
        db.add(
            EtfHolding(
                fund_isin=OTHER_FUND,
                line_no=line_no,
                constituent_isin=isin,
                constituent_ticker=ticker,
                constituent_name=ticker,
                weight_pct=Decimal("1"),
                asset_class="Equity",
            )
        )
    for ticker in ("GOOGL", "NESN.SW", "^GSPC"):
        db.add(WatchlistItem(yahoo_ticker=ticker, company_name=ticker))
    await db.commit()


@pytest.mark.asyncio
async def test_the_named_companies_come_on_top_of_the_capped_index(
    db, configured, monkeypatch
):
    monkeypatch.setattr(company_financials_sync, "UNIVERSE_SIZE", 1)
    monkeypatch.setattr(
        LookthroughService,
        "get_lookthrough",
        _view(("APPLE INC", ["US0378331005"]), ("ROCHE", ["CH0012032048"])),
    )
    await _qqqm(db, NASDAQ_100)
    await _named(db)

    named = await company_financials_sync.CompanyFinancialsSync(db).named_elsewhere()
    assert [(c.ticker, c.isin, c.capped) for c in named] == [
        ("AAPL", "US0378331005", False),
        ("GOOGL", None, False),
    ]

    edgar = Edgar()
    summary = await _sync(db, edgar).refresh()
    asked = {u for u in edgar.facts_urls() if u != TICKERS_URL}
    # The index's one (NVDA, its heaviest), then Apple and Alphabet beyond the cap.
    assert asked == {
        FACTS_URL.format(cik=c) for c in (NVDA_CIK, APPLE_CIK, ALPHABET_CIK)
    }
    assert summary["universe"] == 3
    apple = await db.get(SecCompany, APPLE_CIK)
    assert apple.universe_seen_at is not None and apple.isin == "US0378331005"


@pytest.mark.asyncio
async def test_a_company_named_twice_is_looked_up_once(db, configured, monkeypatch):
    """Apple on the look-through and the watchlist: one new `sec_tickers` row, not two.
    Without autoflush, as `AsyncSessionLocal` runs it: the test session's own autoflush
    hid the second INSERT that failed the production run."""
    db.sync_session.autoflush = False
    monkeypatch.setattr(
        LookthroughService, "get_lookthrough", _view(("APPLE INC", ["US0378331005"]))
    )
    await _qqqm(db, NASDAQ_100)
    await _named(db)
    db.add(WatchlistItem(yahoo_ticker="AAPL", company_name="Apple"))
    await db.commit()

    summary = await _sync(db, Edgar()).refresh(force=True)
    assert summary["status"] == "success"
    assert await db.get(SecTicker, "AAPL") is not None


@pytest.mark.asyncio
async def test_a_line_published_with_a_cins_takes_its_isin_from_the_identity_store(
    db, configured, monkeypatch
):
    monkeypatch.setattr(LookthroughService, "get_lookthrough", _view())
    db.add(
        EtfHolding(
            fund_isin=FUND,
            line_no=0,
            constituent_isin=None,
            constituent_identifier="G5494J103",
            constituent_share_class_figi="BBG00GVR8YQ9",
            constituent_ticker="LIN",
            constituent_name="Linde PLC",
            weight_pct=Decimal("1.2"),
            asset_class="Equity",
        )
    )
    db.add(
        IsinIdentity(
            isin="IE000S9YS762",
            share_class_figi="BBG00GVR8YQ9",
            figi_checked_at=utcnow(),
        )
    )
    await db.commit()

    constituents, _ = await company_financials_sync.CompanyFinancialsSync(db).universe()
    assert [(c.ticker, c.isin) for c in constituents] == [("LIN", "IE000S9YS762")]
