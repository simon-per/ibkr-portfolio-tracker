"""
`ticker_isin`: a Yahoo ticker's ISIN from the stored fund baskets, for a watchlist name we
have never held (docs/logos.md). The issuers each write a ticker their own way, and a bare
code can mean different companies in different countries; these are the shapes seen in the
production baskets on 2026-10-18.
"""
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register all mappers
from app.database import Base
from app.models.etf_basket import EtfHolding
from app.services.ticker_isin import isins_by_yahoo_ticker, normalise, split_yahoo_ticker


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


ROWS = [
    # (fund, ticker as the issuer wrote it, ISIN, weight)
    ("US9220427424", "BRK.B", "US0846707026", "0.72"),
    ("IE00B4L5Y983", "BRK B", "US0846707026", "0.81"),
    ("IE00B0M63177", "700", "KYG875721634", "2.80"),
    ("US9220427424", "7003", "JP3891600003", "0.00"),
    ("US9220427424", "NOVO B", "DK0062498333", "0.13"),
    ("IE00B0M63177", "2330", "TW0002330008", "15.90"),
    ("IE00BKM4GZ66", "2330", "SA000A0LE310", "0.01"),
    ("US9220427424", "SAP", "DE0007164600", "0.16"),
    ("US9220427424", "SAP", "CA8029121057", "0.01"),
    ("IE00BMC38736", "TSM", "US8740391003", "10.07"),
    ("US9220427424", "300750", "CNE100003662", "0.02"),
]


async def _seed(db):
    for line, (fund, ticker, isin, weight) in enumerate(ROWS, start=1):
        db.add(EtfHolding(fund_isin=fund, line_no=line, constituent_ticker=ticker,
                          constituent_isin=isin, constituent_name=ticker,
                          weight_pct=Decimal(weight)))
    await db.commit()


def test_normalise_drops_separators_and_a_codes_leading_zeros():
    assert normalise("BRK.B") == normalise("BRK B") == normalise("brk-b") == "BRKB"
    assert normalise("0700") == normalise("700") == "700"
    assert normalise("000") == "000"


def test_the_suffix_narrows_the_country_and_an_unknown_one_resolves_nothing():
    assert split_yahoo_ticker("NVDA") == ("NVDA", ("US",))
    assert split_yahoo_ticker("2330.TW") == ("2330", ("TW",))
    assert split_yahoo_ticker("NOVO-B.CO") == ("NOVOB", ("DK",))
    assert split_yahoo_ticker("ABC.XX")[1] is None


@pytest.mark.asyncio
async def test_each_issuers_spelling_resolves_to_the_right_company(db):
    await _seed(db)
    found = await isins_by_yahoo_ticker(db, [
        "BRK-B", "0700.HK", "NOVO-B.CO", "2330.TW", "SAP.DE", "TSM", "300750.SZ",
    ])
    assert found == {
        "BRK-B": "US0846707026",
        "0700.HK": "KYG875721634",        # "700", not Japan's "7003"
        "NOVO-B.CO": "DK0062498333",
        "2330.TW": "TW0002330008",        # not the Saudi line one EM basket lists as "2330"
        "SAP.DE": "DE0007164600",         # not the Canadian "SAP"
        "TSM": "US8740391003",            # an ADR's ISIN is a US one
        "300750.SZ": "CNE100003662",
    }


@pytest.mark.asyncio
async def test_no_match_and_an_unknown_suffix_give_nothing(db):
    await _seed(db)
    # "2330" bare is a US listing: no US ISIN carries it, so nothing — never the Taiwanese.
    assert await isins_by_yahoo_ticker(db, ["2330", "ZZZZ", "SAP.XX"]) == {}
