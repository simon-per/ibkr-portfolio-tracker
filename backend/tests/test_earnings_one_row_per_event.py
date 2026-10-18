"""
The earnings lists show each company's report once (issue #69).

Two mechanisms listed a company twice:

* **A moved event.** Rows were upserted on the exact datetime, so when Yahoo replaced a
  placeholder date with the confirmed one, the old row stayed "upcoming" beside the new
  until it passed. Each fetch now replaces the security's *future* events with what Yahoo
  returned (`replace_earnings_events`); the past is only ever added to.
* **Several listings.** Rows are per security, so GOOGL, GOOG and ABEA each listed
  Alphabet's report. The lists now fold on the look-through table's notion of a company
  (`identity_service.companies_of_securities`) and the earnings day, naming every held
  listing.

And the date is served as UTC (`utc_iso`), not a naive string a browser reads as local.

Offline: `_fetch_yahoo_data` is faked; the identity cache is seeded by hand.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.clock import utcnow
import app.models  # noqa: F401  register all mappers
from app.models.earnings_event import EarningsEvent
from app.models.isin_identity import IsinIdentity
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.repositories.fundamentals_repository import FundamentalsRepository
from app.services import fundamentals_service as fs_mod
from app.services.fundamentals_service import FundamentalsService

NOW = utcnow().replace(microsecond=0)
ALPHABET_LEI = "5493006MHB84DD0ZWV18"


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    async def _sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(fs_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(fs_mod.random, "uniform", lambda *_a: 0)


def _security(db, sid, symbol, isin, exchange="NASDAQ", held=True):
    db.add(Security(id=sid, isin=isin, symbol=symbol, description=symbol, currency="USD",
                    conid=sid, asset_category="STK", exchange=exchange))
    db.add(TaxLot(security_id=sid, open_date=date(2025, 1, 2), is_open=held,
                  close_date=None if held else date(2025, 6, 2),
                  quantity=Decimal("10"), cost_basis=Decimal("1000"),
                  price_per_unit=Decimal("100"), currency="USD",
                  cost_basis_eur=Decimal("900")))


def _event(sid, when, reported=None, estimate=1.0):
    return EarningsEvent(security_id=sid, earnings_date=when, eps_estimate=estimate,
                         reported_eps=reported,
                         surprise_percent=None if reported is None else 10.0,
                         is_upcoming=reported is None and when > NOW)


async def _dates(db, sid):
    rows = (await db.execute(
        select(EarningsEvent.earnings_date).where(EarningsEvent.security_id == sid)
        .order_by(EarningsEvent.earnings_date)
    )).scalars().all()
    return list(rows)


# --- (a) a moved event --------------------------------------------------------------

def _payload(when, reported=None):
    return {'security_id': 1, 'earnings_date': when, 'eps_estimate': 1.0,
            'reported_eps': reported, 'surprise_percent': None,
            'is_upcoming': reported is None and when > NOW}


@pytest.mark.asyncio
async def test_a_moved_event_leaves_one_upcoming_row(db):
    _security(db, 1, "AAPL", "US0378331005")
    placeholder = NOW + timedelta(days=12)
    confirmed = NOW + timedelta(days=9, hours=20)
    past = NOW - timedelta(days=80)
    db.add_all([_event(1, placeholder), _event(1, past, reported=1.1)])
    await db.commit()

    # Yahoo now answers with the confirmed date and the past quarter.
    await FundamentalsRepository(db).replace_earnings_events(
        1, [_payload(confirmed), _payload(past, reported=1.1)], now=NOW,
    )
    await db.commit()

    assert await _dates(db, 1) == [past, confirmed]
    calendar = await FundamentalsService(db).get_earnings_calendar()
    assert [row["earnings_date"] for row in calendar] == [confirmed.isoformat() + "+00:00"]


@pytest.mark.asyncio
async def test_the_past_is_never_deleted(db):
    _security(db, 1, "AAPL", "US0378331005")
    old = NOW - timedelta(days=400)        # fell out of Yahoo's window long ago
    unreported = NOW - timedelta(days=2)   # passed, figures not in yet
    db.add_all([_event(1, old, reported=0.9), _event(1, unreported)])
    await db.commit()

    nxt = NOW + timedelta(days=60)
    await FundamentalsRepository(db).replace_earnings_events(1, [_payload(nxt)], now=NOW)
    await db.commit()
    assert await _dates(db, 1) == [old, unreported, nxt]


@pytest.mark.asyncio
async def test_an_empty_answer_replaces_nothing(db):
    _security(db, 1, "AAPL", "US0378331005")
    upcoming = NOW + timedelta(days=12)
    db.add(_event(1, upcoming))
    await db.commit()

    assert await FundamentalsRepository(db).replace_earnings_events(1, [], now=NOW) == 0
    await db.commit()
    assert await _dates(db, 1) == [upcoming]


class _Frame:
    """The subset of yfinance's earnings_dates DataFrame `_extract_earnings` reads."""

    empty = False

    def __init__(self, pairs):
        self._pairs = pairs

    def iterrows(self):
        return iter(self._pairs)


@pytest.mark.asyncio
async def test_the_sync_replaces_future_events_on_each_fetch(db, monkeypatch):
    """The pass itself, not only the repository: a second fetch moving the date leaves
    one future row."""
    _security(db, 1, "AAPL", "US0378331005")
    await db.commit()
    answer = [NOW + timedelta(days=12)]

    async def _fetch(_self, _ticker):
        frame = _Frame([(answer[0], {"EPS Estimate": 1.0})])
        return {"quoteType": "EQUITY", "trailingPE": 20.0}, frame, None, None, None

    monkeypatch.setattr(FundamentalsService, "_fetch_yahoo_data", _fetch)
    await FundamentalsService(db).sync_fundamentals_data(force_refresh=True)
    answer[0] = NOW + timedelta(days=9)
    await FundamentalsService(db).sync_fundamentals_data(force_refresh=True)

    assert await _dates(db, 1) == [NOW + timedelta(days=9)]


# --- (b) several listings of one company ----------------------------------------------

@pytest_asyncio.fixture
async def alphabet(db):
    """GOOGL and ABEA share an ISIN; GOOG joins on the LEI; MSFT is another company;
    AMZN is sold."""
    _security(db, 1, "GOOGL", "US02079K3059")
    _security(db, 2, "ABEA", "US02079K3059", exchange="IBIS")
    _security(db, 3, "GOOG", "US02079K1079")
    _security(db, 4, "MSFT", "US5949181045")
    _security(db, 5, "AMZN", "US0231351067", held=False)
    for isin in ("US02079K3059", "US02079K1079"):
        db.add(IsinIdentity(isin=isin, lei=ALPHABET_LEI, issuer_name="Alphabet Inc."))
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_listings_of_one_company_yield_one_calendar_row(alphabet):
    db = alphabet
    day = datetime.combine((NOW + timedelta(days=10)).date(), datetime.min.time())
    # Each listing is fetched under its own ticker; the Xetra one is stamped differently.
    db.add_all([
        _event(1, day + timedelta(hours=20, minutes=30)),
        _event(2, day + timedelta(hours=4), estimate=None),
        _event(3, day + timedelta(hours=20, minutes=30)),
        _event(4, day + timedelta(hours=20, minutes=30)),
        _event(5, day + timedelta(hours=20, minutes=30)),
    ])
    await db.commit()

    rows = await FundamentalsService(db).get_earnings_calendar()
    by_symbol = {row["symbol"]: row for row in rows}
    assert sorted(by_symbol) == ["GOOG", "MSFT"]
    # The row names every held listing, its own symbol first.
    assert by_symbol["GOOG"]["symbols"] == ["GOOG", "ABEA", "GOOGL"]
    assert by_symbol["MSFT"]["symbols"] == ["MSFT"]
    assert all(row["earnings_date"].endswith("+00:00") for row in rows)


@pytest.mark.asyncio
async def test_listings_of_one_company_yield_one_history_row(alphabet):
    db = alphabet
    when = NOW - timedelta(days=30)
    db.add_all([
        _event(1, when, reported=2.1),
        _event(2, when, reported=2.1),
        _event(3, when, reported=2.1),
        # An earlier quarter is its own row.
        _event(1, when - timedelta(days=91), reported=1.9),
    ])
    await db.commit()

    rows = await FundamentalsService(db).get_earnings_history()
    # Equal figures: the lowest symbol is shown, deterministically.
    assert [(row["symbol"], row["symbols"]) for row in rows] == [
        ("ABEA", ["ABEA", "GOOG", "GOOGL"]),
        ("GOOGL", ["GOOGL", "ABEA", "GOOG"]),
    ]
    assert rows[0]["earnings_date"] == when.isoformat() + "+00:00"


@pytest.mark.asyncio
async def test_without_the_identity_cache_only_the_shared_isin_folds(db):
    """No LEI cached: GOOGL and ABEA still fold on their one ISIN, and GOOG — another
    ISIN — stands alone rather than being guessed into the group by name."""
    _security(db, 1, "GOOGL", "US02079K3059")
    _security(db, 2, "ABEA", "US02079K3059", exchange="IBIS")
    _security(db, 3, "GOOG", "US02079K1079")
    when = NOW + timedelta(days=10)
    db.add_all([_event(1, when), _event(2, when), _event(3, when)])
    await db.commit()

    rows = await FundamentalsService(db).get_earnings_calendar()
    assert sorted(row["symbols"] for row in rows) == [["ABEA", "GOOGL"], ["GOOG"]]
