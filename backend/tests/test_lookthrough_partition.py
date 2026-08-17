"""
Guards on `LookthroughService.get_lookthrough` — the company-level exposure view.

**The partition is the assertion that matters.** Five buckets must account for the whole
portfolio to the cent:

    direct_equity + looked_through_equity + fund_residual + nested_fund + uncovered_fund
      == total_market_value

One equality catches a dropped holding, a double-counted constituent, a renormalisation and
a mis-scaled weight alike, which no amount of spot-checking individual figures would. It is
paired with a test that fails when the response grows a new money field nobody classified,
so a bucket added later cannot slip past it.

The fixture mirrors this account's real shape in miniature: one company reachable three ways
directly (GOOGL + ABEA share an ISIN, GOOG needs the LEI), a fund whose basket also holds
that company, a fund with no basket at all, and the synthetic fund that must never be looked
through. Everything is EUR so no FX rate can quietly zero a value — the unvaluable case is
constructed explicitly instead.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401 - register every mapper
from app.models.app_settings import AppSetting
from app.models.etf_basket import EtfBasket, EtfHolding
from app.models.isin_identity import IsinIdentity
from app.models.market_price import MarketPrice
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.etf_sources import FUND_SOURCES
from app.schemas.lookthrough import (
    LookthroughCompanyRow,
    LookthroughFundCoverage,
    LookthroughResponse,
)
from app.services.lookthrough_service import LookthroughService

TODAY = date.today()

ALPHABET_LEI = "5493006MHB84DD0ZWV18"          # verified: both share classes return it
ALPHABET_A_ISIN = "US02079K3059"               # GOOGL and ABEA
ALPHABET_C_ISIN = "US02079K1079"               # GOOG
NVIDIA_ISIN = "US67066G1040"
NVIDIA_LEI = "LEI_NVIDIA_FIXTURE01"            # a fixture value, not a claim about NVIDIA

XNAS_ISIN = "IE00BMFKG444"
XAIX_ISIN = "IE00BGV5VN51"
VWCE_ISIN = "IE00BK5BQT80"
VT_ISIN = "US9220427424"                       # VWCE's declared basket proxy
VOO_ISIN = "US9229083632"                      # DBPG's declared basket proxy; not held
DBPG_ISIN = "LU0411078552"

# Every top-level money field, classified. The partition test sums the first group; the
# guard below fails if the response gains a field in neither, so a new bucket forces a
# decision instead of silently escaping the identity.
BUCKET_FIELDS = (
    "direct_equity_eur",
    "looked_through_equity_eur",
    "fund_residual_eur",
    "nested_fund_eur",
    "uncovered_fund_eur",
)
NON_BUCKET_EUR_FIELDS = (
    "total_market_value_eur",   # the sum the buckets must equal
    "shown_value_eur",          # a slice of the company rows, not of the portfolio
    "other_companies_eur",      # the rest of that slice
)


@pytest_asyncio.fixture
async def session():
    """A fresh in-memory schema per test, disposed afterwards.

    A fixture rather than a helper returning a session: an undisposed engine leaves its
    connection to the garbage collector and SQLAlchemy warns on every one, which buries
    the suite's real output.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = AsyncSession(engine, expire_on_commit=False)
    db.add(AppSetting(key="base_currency", value="EUR"))
    await db.flush()
    try:
        yield db
    finally:
        await db.close()
        await engine.dispose()


def _security(sid: int, symbol: str, isin: str, exchange: str) -> Security:
    return Security(
        id=sid, isin=isin, symbol=symbol, description=f"{symbol} Description",
        currency="EUR", conid=1000 + sid, asset_category="STK", exchange=exchange,
    )


def _lot(sid: int, quantity: str, cost: str) -> TaxLot:
    return TaxLot(
        security_id=sid, open_date=TODAY - timedelta(days=90),
        quantity=Decimal(quantity), cost_basis=Decimal(cost),
        price_per_unit=Decimal(cost) / Decimal(quantity), currency="EUR",
        cost_basis_eur=Decimal(cost), is_open=True,
    )


def _price(sid: int, close: str) -> MarketPrice:
    return MarketPrice(
        security_id=sid, date=TODAY, close_price=Decimal(close),
        currency="EUR", source="test",
    )


def _basket(
    fund_isin: str,
    equity_weight: str,
    *,
    as_of: Optional[date] = None,
    asset_class_available: bool = True,
    issuer_stated: bool = True,
) -> EtfBasket:
    return EtfBasket(
        fund_isin=fund_isin, as_of_date=as_of or TODAY,
        as_of_is_issuer_stated=issuer_stated,
        source="test", adapter="test", source_rows=3, stored_rows=3, skipped_rows=0,
        total_weight_pct=Decimal("100"), equity_weight_pct=Decimal(equity_weight),
        identifier_coverage_pct=Decimal("100"),
        asset_class_available=asset_class_available,
    )


def _holding(
    fund_isin: str, line_no: int, isin: Optional[str], name: str, weight: str,
    asset_class: Optional[str] = "Equity", sector: Optional[str] = None,
) -> EtfHolding:
    return EtfHolding(
        fund_isin=fund_isin, line_no=line_no, constituent_isin=isin,
        constituent_name=name, weight_pct=Decimal(weight), asset_class=asset_class,
        sector=sector,
    )


def _identity(isin: str, lei: Optional[str] = None, name: Optional[str] = None,
              scfigi: Optional[str] = None) -> IsinIdentity:
    return IsinIdentity(
        isin=isin, lei=lei, issuer_name=name, share_class_figi=scfigi,
        lei_checked_at=None, figi_checked_at=None,
    )


async def _standard_book(session: AsyncSession) -> None:
    """
    Total 5,200:
      Alphabet held three ways     1,700  (GOOGL 1,000 + ABEA 200 + GOOG 500)
      XNAS fund                    2,000  (basket: 60% NVDA, 30% Alphabet-A, 10% cash)
      VWCE fund                    1,000  (no basket at all — and none for the fund it
                                           proxies either, which is what keeps it the
                                           uncovered case the tests below rely on)
      DBPG fund                      500  (synthetic, excluded)
    """
    session.add_all([
        _security(1, "GOOGL", ALPHABET_A_ISIN, "NASDAQ"),
        _security(2, "ABEA", ALPHABET_A_ISIN, "IBIS"),
        _security(3, "GOOG", ALPHABET_C_ISIN, "NASDAQ"),
        _security(4, "XNAS", XNAS_ISIN, "IBIS2"),
        _security(5, "VWCE", VWCE_ISIN, "IBIS2"),
        _security(6, "DBPG", DBPG_ISIN, "IBIS2"),
    ])
    session.add_all([
        _lot(1, "10", "800"), _lot(2, "2", "150"), _lot(3, "5", "400"),
        _lot(4, "20", "1800"), _lot(5, "10", "900"), _lot(6, "5", "450"),
    ])
    session.add_all([_price(sid, "100") for sid in (1, 2, 3, 4, 5, 6)])
    session.add_all([
        _basket(XNAS_ISIN, "90"),
        _holding(XNAS_ISIN, 1, NVIDIA_ISIN, "NVIDIA CORP", "60"),
        _holding(XNAS_ISIN, 2, ALPHABET_A_ISIN, "ALPHABET INC CLASS A", "30"),
        _holding(XNAS_ISIN, 3, None, "EUR CASH", "10", asset_class="Cash"),
    ])
    session.add_all([
        _identity(ALPHABET_A_ISIN, ALPHABET_LEI, "ALPHABET INC."),
        _identity(ALPHABET_C_ISIN, ALPHABET_LEI, "ALPHABET INC."),
        _identity(NVIDIA_ISIN, NVIDIA_LEI, "NVIDIA CORPORATION"),
    ])
    await session.flush()


async def _holding_row(session: AsyncSession, fund_isin: str, line_no: int) -> EtfHolding:
    """`EtfHolding`'s primary key is a surrogate `id`; `UNIQUE(fund_isin, line_no)` is the
    business key, so fetch on that rather than on a composite `session.get()`."""
    return (await session.execute(
        select(EtfHolding).where(
            EtfHolding.fund_isin == fund_isin, EtfHolding.line_no == line_no
        )
    )).scalars().one()


def _row(report: dict, name_fragment: str) -> dict:
    matches = [r for r in report["companies"] if name_fragment.lower() in r["name"].lower()]
    assert matches, f"no company row matching {name_fragment!r} in {[r['name'] for r in report['companies']]}"
    return matches[0]


def _fund(report: dict, symbol: str) -> dict:
    return next(f for f in report["funds"] if f["symbol"] == symbol)


def _bucket_sum(report: dict) -> Decimal:
    return sum((Decimal(str(report[f])) for f in BUCKET_FIELDS), Decimal("0"))


# ------------------------------------------------------------------------- the partition

@pytest.mark.asyncio
async def test_the_partition_closes_to_the_cent(session):
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()

    assert Decimal(str(report["total_market_value_eur"])) == Decimal("5200.00")
    assert _bucket_sum(report) == Decimal(str(report["total_market_value_eur"])), (
        f"buckets {[(f, report[f]) for f in BUCKET_FIELDS]} do not account for the "
        f"portfolio total {report['total_market_value_eur']}"
    )
    assert report["direct_equity_eur"] == 1700.0
    assert report["looked_through_equity_eur"] == 1800.0   # 2000 * 90%
    assert report["fund_residual_eur"] == 200.0            # 2000 * 10% cash
    assert report["nested_fund_eur"] == 0.0
    assert report["uncovered_fund_eur"] == 1500.0          # VWCE 1000 + DBPG 500


@pytest.mark.asyncio
async def test_every_money_field_is_classified_as_a_bucket_or_not(session):
    """
    The family guard. A new top-level `*_eur` field must be added to one of the two tuples
    above, which forces whoever adds it to decide whether the partition should include it —
    rather than the identity quietly ceasing to cover the whole response.
    """
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()

    money_fields = {k for k in report if k.endswith("_eur")}
    classified = set(BUCKET_FIELDS) | set(NON_BUCKET_EUR_FIELDS)
    assert money_fields - classified == set(), (
        f"unclassified money field(s) {sorted(money_fields - classified)}: decide whether "
        f"they belong in the partition and add them to BUCKET_FIELDS or "
        f"NON_BUCKET_EUR_FIELDS"
    )
    assert classified - money_fields == set(), (
        f"{sorted(classified - money_fields)} no longer exist on the response"
    )


@pytest.mark.asyncio
async def test_the_company_rows_sum_to_the_two_equity_buckets(session):
    """
    A company row can only be fed by a direct holding or by a looked-through fund, so the
    rows must sum to exactly those two buckets — the check that no constituent leaked into
    or out of the table.
    """
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough(limit=200)

    rows_total = sum((Decimal(str(r["value_eur"])) for r in report["companies"]), Decimal("0"))
    expected = (
        Decimal(str(report["direct_equity_eur"]))
        + Decimal(str(report["looked_through_equity_eur"]))
    )
    assert rows_total == expected


# --------------------------------------------------------------------------- the folding

@pytest.mark.asyncio
async def test_a_company_held_three_ways_and_inside_a_fund_is_one_row(session):
    """
    The motivating case. GOOGL and ABEA share an ISIN; GOOG is a different ISIN folded by the
    LEI; and XNAS holds Alphabet too. All four contributions are one row, reported split so
    the direct and indirect halves stay legible.
    """
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()

    alphabet = _row(report, "Alphabet")
    assert alphabet["company_key"] == ALPHABET_LEI
    assert alphabet["key_type"] == "lei"
    assert alphabet["value_eur"] == 2300.0          # 1700 direct + 600 via XNAS
    assert alphabet["direct_value_eur"] == 1700.0
    assert alphabet["via_funds_value_eur"] == 600.0
    assert sorted(alphabet["listings"]) == ["ABEA@IBIS", "GOOG@NASDAQ", "GOOGL@NASDAQ"]
    assert sorted(alphabet["isins"]) == [ALPHABET_C_ISIN, ALPHABET_A_ISIN]
    assert [f["symbol"] for f in alphabet["via_funds"]] == ["XNAS"]
    assert alphabet["via_funds"][0]["value_eur"] == 600.0

    # And it outranks the fund-only holding, which is the point of folding at all.
    assert report["companies"][0]["company_key"] == ALPHABET_LEI
    assert _row(report, "NVIDIA")["value_eur"] == 1200.0
    assert _row(report, "NVIDIA")["direct_value_eur"] == 0.0


@pytest.mark.asyncio
async def test_a_repeated_constituent_isin_is_summed_rather_than_last_wins(session):
    """
    `import_prices.py` resolves a repeated date by letting the last row win. Copying that
    idiom here would silently drop weight, so the rows are keyed on the source file's line
    number and summed.
    """
    session.add_all([
        _security(4, "XNAS", XNAS_ISIN, "IBIS2"), _lot(4, "10", "900"), _price(4, "100")
    ])
    session.add_all([
        _basket(XNAS_ISIN, "100"),
        _holding(XNAS_ISIN, 1, NVIDIA_ISIN, "NVIDIA CORP", "40"),
        _holding(XNAS_ISIN, 2, NVIDIA_ISIN, "NVIDIA CORP", "60"),
    ])
    session.add(_identity(NVIDIA_ISIN, NVIDIA_LEI, "NVIDIA CORPORATION"))
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    assert _row(report, "NVIDIA")["value_eur"] == 1000.0
    assert _bucket_sum(report) == Decimal("1000.00")


# ---------------------------------------------------------------------- the stated gaps

@pytest.mark.asyncio
async def test_a_fund_with_no_basket_is_named_and_never_silently_dropped(session):
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()

    vwce = _fund(report, "VWCE")
    assert vwce["status"] == "no_basket"
    assert "by hand" in vwce["reason"]
    assert vwce["market_value_eur"] == 1000.0
    assert vwce["basket_as_of"] is None
    assert any("VWCE" in w for w in report["warnings"])


@pytest.mark.asyncio
async def test_the_synthetic_fund_is_not_decomposed_until_its_proxy_has_a_basket(session):
    """
    DBPG was `excluded` outright until 2026-08-17 and is now proxied to VOO. With no VOO
    basket stored it must still contribute no companies — a declared proxy is not data.
    """
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()

    dbpg = _fund(report, "DBPG")
    assert dbpg["status"] == "no_basket"
    # And none of its published collateral names reach the company table.
    assert not [r for r in report["companies"] if "collateral" in r["name"].lower()]


@pytest.mark.asyncio
async def test_the_synthetic_fund_decomposes_through_its_proxy_and_declares_the_leverage(
    session,
):
    """
    The owner's instruction, and the half of it the proxy cannot fix.

    VOO's basket answers "the disclosed names are collateral, not the index". It does nothing
    about the 2x leverage: the fund is decomposed at its market value, because scaling one
    bucket would break the partition. So the understatement has to be *said*, and a company
    row showing half the real exposure is a plausible figure — the dangerous kind.
    """
    await _standard_book(session)
    session.add_all([
        _basket(VOO_ISIN, "100"),
        _holding(VOO_ISIN, 1, NVIDIA_ISIN, "NVIDIA Corp", "100"),
    ])
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    dbpg = _fund(report, "DBPG")

    assert dbpg["status"] == "looked_through"
    assert dbpg["proxy_for_symbol"] == "VOO"
    assert [w for w in report["warnings"] if "DBPG" in w and "collateral" in w.lower()]
    # Keyed on the phrase unique to the leverage line: the proxy line quotes the declared
    # reason, which mentions the leverage too, so "2x" alone matches both.
    leverage = [
        w for w in report["warnings"] if "DBPG" in w and "sum to the portfolio" in w
    ]
    assert leverage, report["warnings"]
    assert "2x leveraged" in leverage[0]

    # Stated, never scaled — the identity still has to close exactly.
    buckets = sum(Decimal(str(report[f])) for f in BUCKET_FIELDS)
    assert buckets == Decimal(str(report["total_market_value_eur"]))


@pytest.mark.asyncio
async def test_a_synthetic_funds_own_basket_loses_to_its_proxy(session):
    """
    The collateral disclosure is not a rougher answer, it is a different set of companies —
    so importing one must not silently un-proxy the fund. This is the protection the outright
    exclusion used to provide, kept after the exclusion was lifted.
    """
    await _standard_book(session)
    session.add_all([
        _basket(VOO_ISIN, "100"),
        _holding(VOO_ISIN, 1, NVIDIA_ISIN, "NVIDIA Corp", "100"),
        # What DBPG itself publishes: an S&P 500 product disclosing Altria at 5.7%.
        _basket(DBPG_ISIN, "100"),
        _holding(DBPG_ISIN, 1, ALPHABET_C_ISIN, "Altria-ish collateral name", "100"),
    ])
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    dbpg = _fund(report, "DBPG")

    assert dbpg["proxy_for_symbol"] == "VOO"
    assert dbpg["constituents"] == 1
    assert not [r for r in report["companies"] if "collateral" in r["name"].lower()]


@pytest.mark.asyncio
async def test_coverage_is_the_share_of_the_portfolio_actually_attributed(session):
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()

    # (1700 direct + 1800 looked through) / 5200
    assert report["coverage_pct"] == pytest.approx(67.31, abs=0.01)
    assert any("not known" in w for w in report["warnings"])


@pytest.mark.asyncio
async def test_percentages_are_of_the_whole_portfolio_not_of_the_covered_subset(session):
    """
    Renormalising onto covered value would make the visible rows sum to 100 and inflate
    every one of them by the undecomposed share — converting a stated gap into a confident
    lie. Alphabet is 2300/5200, never 2300/3500.
    """
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()

    assert _row(report, "Alphabet")["pct_of_portfolio"] == pytest.approx(44.23, abs=0.01)
    assert _row(report, "Alphabet")["pct_of_portfolio"] != pytest.approx(65.71, abs=0.01)


@pytest.mark.asyncio
async def test_an_unvaluable_holding_is_excluded_from_both_sides_and_named(session):
    """
    An unpriced *fund* is the sharpest silent failure here: it renders not as a visible zero
    but as the absence of its companies from rows that still say "% of portfolio".
    """
    await _standard_book(session)
    # A seventh holding with no price at all.
    session.add_all([
        _security(7, "NOPRICE", "US0000000007", "NASDAQ"), _lot(7, "10", "1000"),
    ])
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    assert report["unvaluable_positions"] == 1
    assert report["unvaluable_symbols"] == ["NOPRICE"]
    # Excluded from the total, so the partition still closes over what could be valued.
    assert Decimal(str(report["total_market_value_eur"])) == Decimal("5200.00")
    assert _bucket_sum(report) == Decimal("5200.00")
    assert not [r for r in report["companies"] if "NOPRICE" in r["name"]]
    assert any("could not be valued" in w for w in report["warnings"])


# ----------------------------------------------------------------------- basket handling

@pytest.mark.asyncio
async def test_a_non_equity_row_lands_in_the_residual_and_not_in_a_company(session):
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()

    assert report["fund_residual_eur"] == 200.0
    assert _fund(report, "XNAS")["residual_eur"] == 200.0
    assert not [r for r in report["companies"] if "CASH" in r["name"].upper()]


@pytest.mark.asyncio
async def test_a_basket_below_the_coverage_floor_is_treated_as_absent(session):
    """
    A residual is right for EMIM's real 98.04. At 3% it would say "this fund is 97% cash",
    which is a plausible figure and therefore the dangerous kind — so the basket is refused
    and the fund reported as uncovered with its measured coverage.
    """
    session.add_all([
        _security(4, "XNAS", XNAS_ISIN, "IBIS2"), _lot(4, "10", "900"), _price(4, "100")
    ])
    session.add_all([
        _basket(XNAS_ISIN, "3"),
        _holding(XNAS_ISIN, 1, NVIDIA_ISIN, "NVIDIA CORP", "3"),
    ])
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    xnas = _fund(report, "XNAS")
    assert xnas["status"] == "implausible"
    assert "3.00%" in xnas["reason"]
    assert report["uncovered_fund_eur"] == 1000.0
    assert report["looked_through_equity_eur"] == 0.0
    assert report["companies"] == []


@pytest.mark.asyncio
async def test_a_constituent_that_is_itself_a_fund_gets_its_own_bucket(session):
    """
    Live risk rather than theory: iShares baskets carry the BlackRock ICS liquidity fund as a
    line item, which would otherwise render as a top-50 'company'.
    """
    session.add_all([
        _security(4, "XNAS", XNAS_ISIN, "IBIS2"), _lot(4, "10", "900"), _price(4, "100")
    ])
    session.add_all([
        _basket(XNAS_ISIN, "100"),
        _holding(XNAS_ISIN, 1, NVIDIA_ISIN, "NVIDIA CORP", "70"),
        # A fund we know about, appearing inside another fund's basket.
        _holding(XNAS_ISIN, 2, XAIX_ISIN, "XTRACKERS AI & BIG DATA", "20"),
        # And one we do not, recognisable only by its asset class.
        _holding(XNAS_ISIN, 3, "IE00BXXXXXXX", "SOME LIQUIDITY FUND", "10",
                 asset_class="Investment Fund"),
    ])
    session.add(_identity(NVIDIA_ISIN, NVIDIA_LEI, "NVIDIA CORPORATION"))
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    assert report["nested_fund_eur"] == 300.0        # 20% + 10% of 1000
    assert report["looked_through_equity_eur"] == 700.0
    assert _bucket_sum(report) == Decimal("1000.00")
    assert [r["name"] for r in report["companies"]] == ["NVIDIA CORPORATION"]


@pytest.mark.asyncio
async def test_a_basket_with_no_asset_class_column_counts_every_row_and_says_so(session):
    """
    Xtrackers publishes no asset class, so the equity whitelist cannot be applied. Every row
    counts, the residual becomes 100 − Σ(all rows), and the response declares that the filter
    did not run — never inferring "equity" from "it has an ISIN", which a bond also has.
    """
    session.add_all([
        _security(4, "XAIX", XAIX_ISIN, "IBIS2"), _lot(4, "10", "900"), _price(4, "100")
    ])
    session.add_all([
        _basket(XAIX_ISIN, "95", asset_class_available=False),
        _holding(XAIX_ISIN, 1, NVIDIA_ISIN, "NVIDIA CORP", "95", asset_class=None),
    ])
    session.add(_identity(NVIDIA_ISIN, NVIDIA_LEI, "NVIDIA CORPORATION"))
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    xaix = _fund(report, "XAIX")
    assert xaix["asset_class_available"] is False
    assert report["looked_through_equity_eur"] == 950.0
    assert report["fund_residual_eur"] == 50.0
    assert _bucket_sum(report) == Decimal("1000.00")


@pytest.mark.asyncio
async def test_a_stale_basket_is_served_but_warned_about(session):
    session.add_all([
        _security(4, "XNAS", XNAS_ISIN, "IBIS2"), _lot(4, "10", "900"), _price(4, "100")
    ])
    session.add_all([
        _basket(XNAS_ISIN, "100", as_of=TODAY - timedelta(days=120)),
        _holding(XNAS_ISIN, 1, NVIDIA_ISIN, "NVIDIA CORP", "100"),
    ])
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    xnas = _fund(report, "XNAS")
    assert xnas["status"] == "looked_through", "a stale basket is still the best available"
    assert xnas["stale"] is True
    assert any("days old" in w for w in report["warnings"])
    assert report["oldest_basket_as_of"] == (TODAY - timedelta(days=120)).isoformat()


@pytest.mark.asyncio
async def test_a_basket_whose_issuer_publishes_no_date_says_so(session):
    session.add_all([
        _security(4, "XNAS", XNAS_ISIN, "IBIS2"), _lot(4, "10", "900"), _price(4, "100")
    ])
    session.add_all([
        _basket(XNAS_ISIN, "100", issuer_stated=False),
        _holding(XNAS_ISIN, 1, NVIDIA_ISIN, "NVIDIA CORP", "100"),
    ])
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    assert any("no as-of date" in w for w in report["warnings"])


# ------------------------------------------------------------------------- presentation

@pytest.mark.asyncio
async def test_truncation_carries_the_tail_as_a_value_and_never_renormalises(session):
    """
    Without a valued tail the visible percentages sum to well under 100 under a
    '% of portfolio' header, which reads as the portfolio being smaller than it is.
    """
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough(limit=1)

    assert report["companies_shown"] == 1
    assert report["company_count_total"] == 2
    assert report["other_companies_count"] == 1
    assert report["shown_value_eur"] == 2300.0
    assert report["other_companies_eur"] == 1200.0
    # The shown row's percentage is unchanged by truncation.
    assert report["companies"][0]["pct_of_portfolio"] == pytest.approx(44.23, abs=0.01)
    # And the partition is unaffected by how many rows were returned.
    assert _bucket_sum(report) == Decimal("5200.00")


@pytest.mark.asyncio
async def test_the_limit_is_clamped_rather_than_trusted(session):
    await _standard_book(session)
    service = LookthroughService(session)
    assert (await service.get_lookthrough(limit=0))["companies_shown"] >= 1
    assert (await service.get_lookthrough(limit=10_000))["companies_shown"] == 2


@pytest.mark.asyncio
async def test_identity_coverage_reports_value_not_only_counts(session):
    """
    14,000 unresolved ISINs worth 0.3% is fine and 3 worth 8% is not; a bare count reads as
    either at random.
    """
    await _standard_book(session)
    # Drop NVIDIA's identity so its 1,200 sits in the unresolved tail.
    nvidia = await session.get(IsinIdentity, NVIDIA_ISIN)
    await session.delete(nvidia)
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    identity = report["identity"]
    assert identity["unresolved_isins"] == 1
    assert identity["unresolved_value_eur"] == 1200.0
    assert identity["resolved_by_lei"] == 1
    assert identity["resolved_by_isin"] == 1


@pytest.mark.asyncio
async def test_an_empty_portfolio_is_not_a_failure(session):
    """
    An empty book is a legitimate state and must not claim to be an outage — the same
    distinction `PortfolioSummaryCards` draws between no-data-yet and the-backend-failed.
    """
    report = await LookthroughService(session).get_lookthrough()
    assert report["companies"] == []
    assert report["total_market_value_eur"] == 0.0
    assert report["coverage_pct"] == 0.0
    assert report["warnings"] == []


@pytest.mark.asyncio
async def test_the_service_and_the_response_model_agree_in_both_directions(session):
    """
    A `response_model` is a filter, so a key the service computes and the model does not
    declare is dropped from the wire in silence — and a key the model declares and the
    service never sets takes its default, which for a money field is a confident 0.00.
    `/api/dividends/summary` shipped exactly the first of those.
    """
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()

    model_fields = set(LookthroughResponse.model_fields)
    service_keys = set(report)
    assert service_keys - model_fields == set(), (
        f"the service returns {sorted(service_keys - model_fields)}, which the response "
        f"model would drop from the wire silently"
    )
    assert model_fields - service_keys == set(), (
        f"the model declares {sorted(model_fields - service_keys)}, which the service never "
        f"sets — each would silently take its default"
    )
    # And the whole thing must actually validate.
    LookthroughResponse.model_validate(report)


@pytest.mark.asyncio
async def test_the_company_row_and_its_model_agree_in_both_directions(session):
    """
    The same check one level down, and it was missing until a per-row field needed it.

    The test above compares only the response's TOP-level keys, so a field added to a company row
    and not to `LookthroughCompanyRow` is dropped from the wire by the same silent filter — with
    every existing assertion still green, because nothing else inspects a row's key set.
    `test_api_contract_drift.py` does not close it either: it checks that the TypeScript interface
    is a subset of the Pydantic model, which is the opposite direction.

    Nested models are exactly where this is easiest to miss, which is why it is pinned rather
    than trusted.
    """
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()
    assert report["companies"], "fixture must produce at least one company row"

    model_fields = set(LookthroughCompanyRow.model_fields)
    for row in report["companies"]:
        row_keys = set(row)
        assert row_keys - model_fields == set(), (
            f"the service puts {sorted(row_keys - model_fields)} on a company row, which the "
            f"response model would drop from the wire silently"
        )
        assert model_fields - row_keys == set(), (
            f"the model declares {sorted(model_fields - row_keys)} on a company row, which the "
            f"service never sets — each would silently take its default"
        )
        LookthroughCompanyRow.model_validate(row)


@pytest.mark.asyncio
async def test_the_fund_row_and_its_model_agree_in_both_directions(session):
    """
    And one level down again, on the other nested model — same filter, same silence.

    Written when the fund row gained two fields at once: a key the service sets and the model
    does not declare never reaches the screen, which for `proxy_for_symbol` would mean a fund
    decomposed from another fund's basket rendering as an ordinary one.
    """
    await _standard_book(session)
    report = await LookthroughService(session).get_lookthrough()
    assert report["funds"], "fixture must produce at least one fund row"

    model_fields = set(LookthroughFundCoverage.model_fields)
    for row in report["funds"]:
        row_keys = set(row)
        assert row_keys - model_fields == set(), (
            f"the service puts {sorted(row_keys - model_fields)} on a fund row, which the "
            f"response model would drop from the wire silently"
        )
        assert model_fields - row_keys == set(), (
            f"the model declares {sorted(model_fields - row_keys)} on a fund row, which the "
            f"service never sets — each would silently take its default"
        )
        LookthroughFundCoverage.model_validate(row)


# --------------------------------------------------------------- borrowed baskets
#
# VWCE publishes no machine-readable holdings file, so `etf_sources` declares VT's basket as
# its stand-in. The fixture's `_standard_book` deliberately stores no VT basket, which is what
# keeps VWCE the "no basket at all" case every other test in this file relies on — these add
# one explicitly.


def _vt_basket_and_rows():
    """VT's basket plus two rows, one of which is Alphabet — so the fold is exercised too."""
    return [
        _basket(VT_ISIN, "100"),
        _holding(VT_ISIN, 1, ALPHABET_A_ISIN, "Alphabet Inc Class A", "40"),
        _holding(VT_ISIN, 2, NVIDIA_ISIN, "NVIDIA Corp", "60"),
    ]


@pytest.mark.asyncio
async def test_a_fund_with_no_basket_of_its_own_borrows_the_one_it_proxies(session):
    await _standard_book(session)
    session.add_all(_vt_basket_and_rows())
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    vwce = next(f for f in report["funds"] if f["fund_isin"] == VWCE_ISIN)

    assert vwce["status"] == "looked_through"
    assert vwce["proxy_for_symbol"] == "VT"
    assert vwce["constituents"] == 2
    # Its value is attributed, not left in the uncovered bucket.
    assert report["uncovered_fund_eur"] < 1000.0


@pytest.mark.asyncio
async def test_a_borrowed_basket_still_closes_the_partition(session):
    """The whole point of aliasing rather than copying: nothing downstream has a proxy branch."""
    await _standard_book(session)
    session.add_all(_vt_basket_and_rows())
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    buckets = sum(Decimal(str(report[f])) for f in BUCKET_FIELDS)
    assert buckets == Decimal(str(report["total_market_value_eur"]))


@pytest.mark.asyncio
async def test_a_fund_that_publishes_its_own_basket_ignores_the_proxy(session):
    """
    A real import must win, in either order and with no code change — otherwise a declared
    proxy would quietly outrank the file it was only ever meant to stand in for.
    """
    await _standard_book(session)
    session.add_all(_vt_basket_and_rows())
    session.add_all([
        _basket(VWCE_ISIN, "100"),
        _holding(VWCE_ISIN, 1, NVIDIA_ISIN, "NVIDIA Corp", "100"),
    ])
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    vwce = next(f for f in report["funds"] if f["fund_isin"] == VWCE_ISIN)

    assert vwce["proxy_for_symbol"] is None
    assert vwce["constituents"] == 1


@pytest.mark.asyncio
async def test_a_borrowed_basket_says_so_in_a_warning_and_gives_the_reason(session):
    """
    The badge alone is not enough: `warnings[]` is the surface rendered outside every
    collapsible, and an approximation the reader cannot see is indistinguishable from a
    measurement.
    """
    await _standard_book(session)
    session.add_all(_vt_basket_and_rows())
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    borrowed = [w for w in report["warnings"] if "VWCE" in w and "VT" in w]
    assert borrowed, report["warnings"]
    # The declared reasoning travels with it rather than being re-worded here.
    assert FUND_SOURCES[VWCE_ISIN].basket_proxy_reason in borrowed[0]


@pytest.mark.asyncio
async def test_rows_published_at_zero_weight_are_counted_rather_than_inferred(session):
    """
    A broad fund's residual is its rounded-away tail, not cash. VT publishes 8,007 of 10,032
    rows at 0.00%, and without this count the 8.14% shortfall reads as an uninvested balance —
    the plausible-looking answer, which is the dangerous kind.
    """
    await _standard_book(session)
    session.add_all([
        # 91.00 rather than 100: VT's real file sums to 91.86 for exactly this reason, and it
        # must stay clear of MIN_BASKET_COVERAGE_PCT or the fund reads as implausible instead.
        _basket(VT_ISIN, "91"),
        _holding(VT_ISIN, 1, NVIDIA_ISIN, "NVIDIA Corp", "91"),
        _holding(VT_ISIN, 2, ALPHABET_A_ISIN, "Alphabet Inc Class A", "0"),
        _holding(VT_ISIN, 3, ALPHABET_C_ISIN, "Alphabet Inc Class C", "0"),
    ])
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    vwce = next(f for f in report["funds"] if f["fund_isin"] == VWCE_ISIN)
    assert vwce["unweighted_constituents"] == 2

    xnas = next(f for f in report["funds"] if f["fund_isin"] == XNAS_ISIN)
    assert xnas["unweighted_constituents"] == 0


@pytest.mark.parametrize("direct_price,uncovered_price,fund_price,weight", [
    # Sub-cent prices, so several buckets carry a fraction that rounds the same way and the
    # errors accumulate. Each of these fails if the residual is published as its own rounded
    # sum instead of as the remainder — verified by mutation.
    ("10.004", "30.004", "50.008", "50"),
    ("7.006", "3.006", "9.004", "33.333333"),
    ("1.004", "1.004", "1.008", "66.666667"),
    ("12.344", "56.784", "90.124", "25"),
    ("0.504", "0.504", "0.504", "10"),
    ("999.994", "888.884", "777.774", "99.999999"),
])
@pytest.mark.asyncio
async def test_the_published_buckets_close_whatever_the_rounding(
    session, direct_price, uncovered_price, fund_price, weight
):
    """
    The partition must close in the numbers a CALLER RECEIVES, not only in the Decimals behind
    them — and the values matter, which is why they are engineered rather than tidy.

    Five independently rounded buckets do not sum to the rounded total: on the real book they
    came to 70,842.40 against a total of 70,842.39. A partition that misses by a cent still
    misses, both for the assertion above and for anyone adding the columns up on screen. So the
    residual is published as the rounded remainder.

    The first version of this test used round prices and **passed against the bug**, which is
    the failure mode this codebase keeps meeting: a test whose fixture cannot reach the
    condition it names. Every row below carries a sub-cent fraction so at least four buckets
    round in the same direction.
    """
    session.add_all([
        _security(1, "DIRECT", ALPHABET_A_ISIN, "NASDAQ"),
        _security(2, "XNAS", XNAS_ISIN, "IBIS2"),
        _security(3, "VWCE", VWCE_ISIN, "IBIS2"),
        _lot(1, "1", "1"), _lot(2, "1", "1"), _lot(3, "1", "1"),
        _price(1, direct_price), _price(2, fund_price), _price(3, uncovered_price),
        _basket(XNAS_ISIN, weight),
        _holding(XNAS_ISIN, 1, NVIDIA_ISIN, "NVIDIA CORP", weight),
        _holding(XNAS_ISIN, 2, None, "CASH", str(Decimal("100") - Decimal(weight)),
                 asset_class="Cash"),
    ])
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    assert _bucket_sum(report) == Decimal(str(report["total_market_value_eur"])), (
        f"published buckets {[(f, report[f]) for f in BUCKET_FIELDS]} do not sum to the "
        f"published total {report['total_market_value_eur']}"
    )
    # The company rows still account for the two equity buckets, to within the cent the
    # residual absorbs.
    rows_total = sum((Decimal(str(r["value_eur"])) for r in report["companies"]), Decimal("0"))
    assert abs(rows_total - (
        Decimal(str(report["direct_equity_eur"]))
        + Decimal(str(report["looked_through_equity_eur"]))
    )) <= Decimal("0.02")


# ---------------------------------------------------------------- sector, per company

@pytest.mark.asyncio
async def test_a_company_takes_the_sector_of_its_largest_classified_contributor(session):
    """
    A folded company can be classified more than once and the sources genuinely differ — a fund
    may carry a stale classification, or none. The vote is weighted by value so the largest
    contributor decides, which is the rule `display_name()` already uses to pick the row's name.
    They must agree: a row showing one issuer's name beside another's sector is incoherent.

    Here Alphabet is 1,700 direct (Yahoo: Communication Services) against 600 through XNAS, whose
    basket deliberately disagrees. The direct side is larger, so it wins — and both spellings
    normalise to the same canonical name anyway, which is the point of the taxonomy module.
    """
    await _standard_book(session)
    # Overwrite XNAS's Alphabet row with a *different* classification than Yahoo's.
    holding = await _holding_row(session, XNAS_ISIN, 2)
    holding.sector = "Information Technology"
    for sec_id in (1, 2, 3):
        (await session.get(Security, sec_id)).sector = "Communication Services"
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    assert _row(report, "ALPHABET")["sector"] == "Communications"


@pytest.mark.asyncio
async def test_a_fund_only_company_takes_its_sector_from_the_basket(session):
    """NVIDIA is held through XNAS and nowhere directly, so `securities.sector` cannot answer."""
    await _standard_book(session)
    holding = await _holding_row(session, XNAS_ISIN, 1)
    holding.sector = "Information Technology"
    await session.flush()

    report = await LookthroughService(session).get_lookthrough()
    assert _row(report, "NVIDIA")["sector"] == "Technology"


@pytest.mark.asyncio
async def test_a_company_nothing_classifies_is_unknown_rather_than_absent(session):
    """
    Vanguard publishes no sector on any of its rows and `securities.sector` is NULL until someone
    runs the allocation sync by hand, so this is the common case, not the edge. Dropping the row
    would shrink the whole and make every other company's share read high — the failure
    `allocation_service` fixed for its own charts in 2026-08-05.
    """
    await _standard_book(session)   # no sector set anywhere
    report = await LookthroughService(session).get_lookthrough()

    assert report["companies"], "companies must still be present"
    assert all(r["sector"] == "Unknown" for r in report["companies"])
    rows_total = sum((Decimal(str(r["value_eur"])) for r in report["companies"]), Decimal("0"))
    assert rows_total == (
        Decimal(str(report["direct_equity_eur"]))
        + Decimal(str(report["looked_through_equity_eur"]))
    )


@pytest.mark.asyncio
async def test_an_exactly_tied_sector_vote_does_not_depend_on_arrival_order(session):
    """
    `company_identity`'s own 25x shuffle compares keys, names and membership — it cannot see a
    field the service adds afterwards, so the determinism the pure module guarantees has to be
    re-checked where the vote actually happens.

    A `max()` over a dict returns the FIRST key holding the maximum, and dict order is insertion
    order, which follows member order. That is stable within one process, so "run it five times
    and compare" passes against the bug — verified by mutation. What discriminates is **swapping
    which security carries which sector**: the two members keep their arrival order, so an
    order-dependent implementation flips its answer while a name-ordered one does not.
    """
    await _standard_book(session)
    for sec_id in (1, 2):                      # equal weights: neither can win on value
        lot = (await session.execute(
            select(TaxLot).where(TaxLot.security_id == sec_id)
        )).scalars().first()
        lot.quantity = Decimal("5")
    (await session.get(Security, 3)).sector = None   # GOOG abstains, or it breaks the tie

    answers = []
    for first, second in (("Financials", "Technology"), ("Technology", "Financials")):
        (await session.get(Security, 1)).sector = first
        (await session.get(Security, 2)).sector = second
        await session.flush()
        report = await LookthroughService(session).get_lookthrough()
        row = _row(report, "ALPHABET")
        # 500 + 500 tied, plus 500 from the abstaining GOOG listing.
        assert row["direct_value_eur"] == 1500.0
        answers.append(row["sector"])

    assert answers[0] == answers[1], (
        f"the tie resolved to {answers[0]!r} then {answers[1]!r} — it is being decided by the "
        f"order the members arrived in, not by the sector name"
    )
