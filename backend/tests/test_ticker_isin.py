"""
`ticker_isin`: a Yahoo ticker's ISIN from the stored fund baskets, for a watchlist name we
have never held (docs/logos.md). The issuers each write a ticker their own way, and a bare
code can mean different companies in different countries; these are the shapes seen in the
production baskets on 2026-10-18.
"""
from decimal import Decimal

import pytest

import app.models  # noqa: F401  register all mappers
from app.models.etf_basket import EtfHolding
from app.services.ticker_isin import isins_by_yahoo_ticker, normalise, split_yahoo_ticker


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


async def _seed(db, rows=ROWS):
    for line, (fund, ticker, isin, weight) in enumerate(rows, start=1):
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


# A CN ISIN lists in Hong Kong (an H-share) and on the mainland (an A-share); stripped of their
# zeros the two markets' codes collide, so the code's length as written decides (issue #27).
CLP = "HK0002007356"        # CLP Holdings, 0002.HK
VANKE_A = "CNE0000000T2"    # China Vanke A, 000002.SZ
CCB_H = "CNE1000002H1"      # China Construction Bank H, 0939.HK


@pytest.mark.parametrize("rows, expected", [
    # Both lines stored, the A-share heavier: CLP's own line, never Vanke's.
    ([("IE00B4L5Y983", "2", CLP, "0.03"), ("IE00B4L5Y983", "000002", VANKE_A, "0.05")], CLP),
    # Only the A-share stored (an EM basket): nothing, not Vanke.
    ([("IE00B4L5Y983", "000002", VANKE_A, "0.05")], None),
])
@pytest.mark.asyncio
async def test_a_hong_kong_code_never_resolves_to_a_shenzhen_a_share(db, rows, expected):
    await _seed(db, rows)
    assert (await isins_by_yahoo_ticker(db, ["0002.HK"])).get("0002.HK") == expected


@pytest.mark.asyncio
async def test_an_a_share_never_resolves_to_a_hong_kong_line(db):
    await _seed(db, [
        ("IE00B4L5Y983", "2", CLP, "0.30"),
        ("IE00B4L5Y983", "000002", VANKE_A, "0.05"),
        ("IE00B4L5Y983", "939", CCB_H, "0.90"),
    ])
    found = await isins_by_yahoo_ticker(db, ["000002.SZ", "000939.SZ", "0939.HK", "939.HK"])
    assert found == {
        "000002.SZ": VANKE_A,      # not CLP's heavier "2"
        "0939.HK": CCB_H,          # an H-share's CN ISIN still resolves in Hong Kong
        "939.HK": CCB_H,
    }                              # 000939.SZ: only the H-share's "939" is stored, so nothing
