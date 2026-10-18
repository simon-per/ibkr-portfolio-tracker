"""
Every service that resolves a Yahoo ticker must resolve it the same way.

`MarketDataService._get_yahoo_ticker` is the canonical one: `ticker_mappings`
first, then `EXCHANGE_SUFFIXES`, with TSE disambiguated by currency (Tokyo `.T`
versus Toronto `.TO`) — the logic the SBI repair turned on. Fundamentals,
dividends and analyst ratings all delegate to it.

`AllocationService` did not. It had its own: mappings, then the bare symbol for
NASDAQ/NYSE/AMEX, then **None**. So it ignored the suffix table, omitted ARCA and
BATS, and could not disambiguate TSE.

The consequence was quiet and large, because a mapping row is only auto-saved when
a *variation* succeeds — i.e. when the primary suffix-derived ticker failed. A
XETRA holding priced fine as `AMZ.DE` therefore has no mapping row, so allocation
returned None and that security got **no sector and no country**, permanently,
while counting toward "missing allocation data".

These pin agreement across the family rather than one implementation, so a future
service that rolls its own is caught the same way.

Offline: resolution is pure lookup — no yfinance, no network.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.security import Security
from app.models.ticker_mapping import TickerMapping
from app.services.allocation_service import AllocationService
from app.services.analyst_rating_service import AnalystRatingService
from app.services.dividend_service import DividendService
from app.services.fundamentals_service import FundamentalsService
from app.services.market_data_service import MarketDataService


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


def security(symbol, exchange, currency="USD"):
    return Security(
        isin=f"XX{symbol}{exchange}"[:12], symbol=symbol, description=symbol,
        currency=currency, conid=1, asset_category="STK", exchange=exchange,
    )


# The cases that separate the two implementations. Each is a real shape from this
# account's holdings or from EXCHANGE_SUFFIXES.
CASES = [
    ("AMZN", "NASDAQ", "USD", "AMZN"),      # US: both agreed already
    ("SOXQ", "ARCA", "USD", "SOXQ"),        # ARCA was missing from the US list
    ("SPY", "BATS", "USD", "SPY"),          # BATS too
    ("AMZ", "XETRA", "EUR", "AMZ.DE"),      # suffix table — was None
    ("IWDA", "LSEETF", "GBP", "IWDA.L"),    # was None
    ("ASML", "AEB", "EUR", "ASML.AS"),      # was None
    ("2330", "TWSE", "TWD", "2330.TW"),     # was None
    ("005930", "KRX", "KRW", "005930.KS"),  # was None
    ("SBI", "TSE", "CAD", "SBI.TO"),        # Toronto, the SBI shape — was None
    ("7203", "TSE", "JPY", "7203.T"),       # Tokyo, same exchange code — was None
]


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,exchange,currency,expected", CASES)
async def test_allocation_resolves_the_same_ticker_as_the_price_path(
    db, symbol, exchange, currency, expected
):
    sec = security(symbol, exchange, currency)

    canonical = await MarketDataService(db)._get_yahoo_ticker(sec)
    allocation = await AllocationService(db)._get_yahoo_ticker(sec)

    assert canonical == expected
    assert allocation == canonical, (
        f"{symbol}@{exchange}: allocation resolves {allocation!r}, prices resolve "
        f"{canonical!r} — the same security would be looked up as two instruments"
    )


@pytest.mark.asyncio
async def test_every_service_agrees_with_the_price_path(db):
    """
    The family invariant. A service that rolls its own resolver is caught here even
    if it never touches allocation.
    """
    sec = security("AMZ", "XETRA", "EUR")
    canonical = await MarketDataService(db)._get_yahoo_ticker(sec)

    resolvers = {
        "allocation": AllocationService(db),
        "fundamentals": FundamentalsService(db),
        "dividends": DividendService(db),
        "analyst_ratings": AnalystRatingService(db),
    }
    resolved = {}
    for name, service in resolvers.items():
        resolved[name] = await service._get_yahoo_ticker(sec)

    disagreeing = {n: t for n, t in resolved.items() if t != canonical}
    assert not disagreeing, (
        f"these resolve a XETRA security differently from the price path "
        f"(canonical {canonical!r}): {disagreeing}"
    )


@pytest.mark.asyncio
async def test_a_manual_mapping_still_wins_everywhere(db):
    """Tier 1 must outrank the suffix logic in every service, as it does in the
    canonical one — that precedence is what makes `manage_mappings set` effective."""
    db.add(TickerMapping(
        ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI.TO",
        is_active=True, source="manual",
    ))
    await db.commit()

    sec = security("SBI", "TSE", "CAD")
    assert await AllocationService(db)._get_yahoo_ticker(sec) == "SBI.TO"
    assert await MarketDataService(db)._get_yahoo_ticker(sec) == "SBI.TO"


@pytest.mark.asyncio
async def test_a_security_with_no_exchange_still_resolves(db):
    """The old implementation returned None here; the canonical one uses the bare
    symbol, which is right for a US listing reported without an exchange."""
    sec = security("AMZN", None, "USD")
    assert await AllocationService(db)._get_yahoo_ticker(sec) == "AMZN"


async def _mapped_suffixed_and_bare(db):
    """A mapped, an inactively mapped, suffixed and exchange-less securities, persisted."""
    db.add_all([
        TickerMapping(ibkr_symbol="SBI", ibkr_exchange="TSE", yahoo_ticker="SBI.TO",
                      is_active=True, source="manual"),
        TickerMapping(ibkr_symbol="XNAS", ibkr_exchange="IBIS2", yahoo_ticker="XNAS.F",
                      is_active=False, source="manual"),
        # Same symbol, another exchange: the mapping is keyed by both.
        TickerMapping(ibkr_symbol="AMZ", ibkr_exchange="FWB", yahoo_ticker="AMZ.F",
                      is_active=True, source="manual"),
    ])
    secs = [security("SBI", "TSE", "CAD"), security("XNAS", "IBIS2", "EUR")]
    secs += [security(s, e, c) for s, e, c, _ in CASES]
    secs.append(security("AMZN", None, "USD"))
    for i, sec in enumerate(secs, start=1):
        sec.conid, sec.isin = i, f"XX{i:010d}"
    db.add_all(secs)
    await db.commit()
    return secs


@pytest.mark.asyncio
async def test_the_bulk_resolver_agrees_with_the_single_one(db):
    """`yahoo_tickers_for` (mappings read once) must answer what `_get_yahoo_ticker` (one
    SELECT per security) answers: they share `yahoo_ticker_for`, and this pins that neither
    grows its own lookup."""
    secs = await _mapped_suffixed_and_bare(db)
    market = MarketDataService(db)
    single = [await market._get_yahoo_ticker(s) for s in secs]
    assert await market.yahoo_tickers_for(secs) == single
    assert single[:2] == ["SBI.TO", "XNAS.DE"]  # the active mapping wins; an inactive is ignored


@pytest.mark.asyncio
async def test_the_watchlist_index_agrees_with_the_price_path(db):
    """The watchlist keys a row to a security by its Yahoo ticker (`security_ids_by_ticker`):
    every eligible security must be keyed by the ticker the price path resolves for it."""
    from app.services.watchlist_service import WatchlistService

    secs = await _mapped_suffixed_and_bare(db)
    market = MarketDataService(db)
    index = await WatchlistService(db).security_ids_by_ticker()
    for sec in secs:
        ticker = (await market._get_yahoo_ticker(sec)).upper()
        assert ticker in index, f"{sec.symbol}@{sec.exchange}: {ticker!r} is not in the index"
