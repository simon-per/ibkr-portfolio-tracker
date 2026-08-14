"""
Guards on the basket parsers and the replace-or-refuse gate.

**The failure this file exists for is a successful import, not a failed one.** A parser that
accepts junk replaces a real 1,338-row basket atomically, and every look-through figure then
shrinks with nothing reporting it — whereas a refusal is loud and leaves the previous basket in
place. So most of what follows feeds the parsers deliberately broken input and asserts they say
no.

Every poison shape here was observed rather than imagined:

- the retired iShares holdings URL answers **HTTP 200 + `Content-Type: text/csv` +
  `Content-Disposition: attachment` with an HTML body**, so status and content type are not
  evidence;
- the BlackRock payload is index-aligned parallel arrays *and* carries unrelated short arrays
  beside them, so both a genuine mismatch and a false alarm have to be pinned;
- Xtrackers ships weights as fractions of 1 and a German-locale variant uses a decimal comma;
- Vanguard pages 500 rows at a time, and a missing page leaves weights that still sum
  plausibly.

Offline: the parsers touch no network and no database.
"""
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401
from app.models.etf_basket import EtfHolding
from app.repositories.etf_basket_repository import (
    BasketReplaceRefused,
    EtfBasketRepository,
)
from app.services.etf_basket_parsers import (
    BasketParseError,
    counts_as_invested,
    parse_dws,
    parse_ishares,
    parse_vanguard_us,
)

XNAS = "IE00BMFKG444"
SXR8 = "IE00B5BMR087"
VT = "US9220427424"
TODAY = date.today()

DWS_HEADER = (
    "ShareClass ISIN;Constituent ISIN;Constituent Name;Constituent Country;"
    "Constituent Currency ISO Code;Constituent Weighting;Constituent Rating;"
    "Constituent Main Exchange Name;Constituent Industry Classification Name"
)


def dws_csv(rows, fund_isin=XNAS, header=DWS_HEADER) -> bytes:
    """A DWS export. `rows` are (constituent_isin, name, weight_as_written, sector)."""
    lines = [header]
    for isin, name, weight, sector in rows:
        lines.append(
            f"{fund_isin};{isin};{name};United States;USD;{weight};Aa1;"
            f"NASDAQ National Market System;{sector}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def ishares_json(rows, as_of="13/Aug/2026", extra_lengths=True, break_alignment=False) -> bytes:
    """
    A BlackRock product-data payload. `rows` are (isin, name, percent, asset_class).

    `extra_lengths` adds the unrelated four-element array the real response carries, which a
    naive "every array must be the same length" check would reject.
    """
    isins = [r[0] for r in rows]
    names = [r[1] for r in rows]
    weights = [r[2] for r in rows]
    classes = [r[3] for r in rows]
    if break_alignment:
        isins = isins[:-1]
    points = {
        "isin": {"formattedValue": isins},
        "issueName": {"formattedValue": names},
        "holdingPercent": {"formattedValue": weights},
        "assetClass": {"formattedValue": classes},
        "ticker": {"formattedValue": [n[:4] for n in names]},
        "sectorName": {"formattedValue": ["Technology"] * len(rows)},
        "countryOfRisk": {"formattedValue": ["United States"] * len(rows)},
        "asOfDate": {"formattedValue": as_of},
    }
    if extra_lengths:
        points["dateList"] = {"formattedValue": ["a", "b", "c", "d"]}
    return json.dumps({
        "componentsByNameMap": {
            "holdings": {"containersByNameMap": {"all": {"dataPointsByNameMap": points}}}
        }
    }).encode("utf-8")


def vanguard_page(entities, size, has_next, as_of="2026-06-30T00:00:00-04:00") -> bytes:
    return json.dumps({
        "size": size,
        "asOfDate": as_of,
        "next": {"href": "http://x/next"} if has_next else {},
        "fund": {"entity": [
            {"ticker": t, "isin": i, "longName": n, "percentWeight": w}
            for t, i, n, w in entities
        ]},
    }).encode("utf-8")


# ------------------------------------------------------------------------ the lying body

@pytest.mark.parametrize("body", [
    b"<!DOCTYPE html><html><head><title>Products</title></head><body>...</body></html>",
    b"<html>\n<body>Sign in</body></html>",
    b"",
    b"   \n  ",
])
def test_a_web_page_served_as_a_holdings_file_is_refused(body):
    """
    The documented trap: 200 + `text/csv` + `Content-Disposition: attachment`, HTML body. The
    check is on the bytes, because no header on that response was truthful.
    """
    with pytest.raises(BasketParseError):
        parse_dws(body, XNAS, TODAY)
    with pytest.raises(BasketParseError):
        parse_ishares(body, SXR8)
    with pytest.raises(BasketParseError):
        parse_vanguard_us([body], VT)


# ------------------------------------------------------------------------------- DWS

def test_dws_scales_fractions_into_percent():
    """
    Weights arrive as fractions of 1.0. A fraction reaching `weight_pct` would make the fund
    contribute a hundredth of its value and read as 99% cash — a plausible figure.
    """
    basket = parse_dws(dws_csv([
        ("US5949181045", "MICROSOFT", "0.0571624952", "Technology"),
        ("US67066G1040", "NVIDIA CORP", "0.0483200485", "Technology"),
        ("US0231351067", "AMAZON COM INC", "0.8945174563", "Consumer Discretionary"),
    ]), XNAS, TODAY)

    assert basket.rows[0].weight_pct == Decimal("5.71624952")
    assert basket.total_weight_pct == Decimal("100.00000000")
    assert basket.rows[0].isin == "US5949181045"
    assert basket.rows[0].sector == "Technology"
    assert basket.rows[0].country == "United States"


def test_dws_reads_a_decimal_comma():
    """The German-locale variant of the same export."""
    basket = parse_dws(dws_csv([
        ("US5949181045", "MICROSOFT", "0,60", "Technology"),
        ("US67066G1040", "NVIDIA CORP", "0,40", "Technology"),
    ]), XNAS, TODAY)
    assert basket.total_weight_pct == Decimal("100.00")


def test_dws_has_no_asset_class_or_as_of_and_says_so():
    """
    Both absences are real and both are declared rather than papered over: no asset-class
    column means the equity filter cannot run, and no published date means the file's own date
    stands in — which errs toward looking fresher than the data is.
    """
    basket = parse_dws(dws_csv([
        ("US5949181045", "MICROSOFT", "0.5", "Technology"),
        ("US67066G1040", "NVIDIA CORP", "0.5", "unknown"),
    ]), XNAS, TODAY)
    assert basket.asset_class_available is False
    assert basket.as_of_is_issuer_stated is False
    assert basket.as_of_date == TODAY
    # Every row counts toward the invested weight, since the filter could not be applied.
    assert basket.equity_weight_pct == Decimal("100.0")
    # 'unknown' is what the export writes when unclassified; storing it would assert a sector.
    assert basket.rows[1].sector is None


def test_dws_refuses_a_basket_belonging_to_another_fund():
    """
    The URL is keyed by ISIN, so a redirect is the plausible failure. The export echoes the
    share-class ISIN on every row, which makes it free to check.
    """
    body = dws_csv([("US5949181045", "MICROSOFT", "0.5", "Technology"),
                    ("US67066G1040", "NVIDIA", "0.5", "Technology")], fund_isin=SXR8)
    with pytest.raises(BasketParseError, match="served a different fund"):
        parse_dws(body, XNAS, TODAY)


def test_dws_refuses_a_missing_column_rather_than_guessing():
    truncated = DWS_HEADER.replace(";Constituent Weighting", "")
    with pytest.raises(BasketParseError, match="absent"):
        parse_dws(dws_csv([("US5949181045", "MICROSOFT", "0.5", "Technology")],
                          header=truncated), XNAS, TODAY)


def test_dws_keeps_a_row_with_no_usable_isin_without_inventing_one():
    """
    Some rows carry a CUSIP, a blank or a dash. Storing any of those in the ISIN column would
    eventually fold two unrelated companies together, so the identifier is dropped and the row
    survives under its name — counted against `identifier_coverage_pct`.
    """
    basket = parse_dws(dws_csv([
        ("US5949181045", "MICROSOFT", "0.5", "Technology"),
        ("-", "SOMETHING UNIDENTIFIED", "0.3", "Technology"),
        ("67066G104", "CUSIP ONLY", "0.2", "Technology"),
    ]), XNAS, TODAY)
    assert [r.isin for r in basket.rows] == ["US5949181045", None, None]
    assert basket.identifier_coverage_pct == Decimal("50")


# --------------------------------------------------------------------------- iShares

def test_ishares_zips_parallel_arrays_and_reads_the_issuers_as_of():
    basket = parse_ishares(ishares_json([
        ("US67066G1040", "NVIDIA CORP", "8.12", "Equity"),
        ("US0378331005", "APPLE INC", "6.50", "Equity"),
        ("", "EUR CASH", "0.30", "Cash"),
    ]), SXR8)

    assert basket.as_of_date == date(2026, 8, 13)
    assert basket.as_of_is_issuer_stated is True
    assert basket.asset_class_available is True
    assert basket.total_weight_pct == Decimal("14.92")
    # The cash row is excluded from the invested weight but still stored.
    assert basket.equity_weight_pct == Decimal("14.62")
    assert len(basket.rows) == 3
    assert basket.rows[2].isin is None


def test_ishares_refuses_misaligned_arrays():
    """
    The worst corruption available to this feature: one short array and every subsequent ISIN
    is attached to another company's weight, silently and plausibly.
    """
    with pytest.raises(BasketParseError, match="disagree in length"):
        parse_ishares(ishares_json([
            ("US67066G1040", "NVIDIA CORP", "8.12", "Equity"),
            ("US0378331005", "APPLE INC", "6.50", "Equity"),
            ("US5949181045", "MICROSOFT", "6.00", "Equity"),
        ], break_alignment=True), SXR8)


def test_ishares_tolerates_the_unrelated_short_arrays_the_real_response_carries():
    """
    The live payload has a four-element array beside a 1,338-element one, so "every array must
    match" would refuse every valid response. Only the fields actually read are compared.
    """
    basket = parse_ishares(ishares_json([
        ("US67066G1040", "NVIDIA CORP", "8.12", "Equity"),
        ("US0378331005", "APPLE INC", "6.50", "Equity"),
    ], extra_lengths=True), SXR8)
    assert len(basket.rows) == 2


def test_ishares_refuses_a_payload_with_no_as_of():
    """
    This issuer does publish a date, so its absence means the payload is not what we think it
    is — and standing the fetch date in would report a basket of unknown vintage as today's.
    """
    with pytest.raises(BasketParseError, match="asOfDate"):
        parse_ishares(ishares_json([
            ("US67066G1040", "NVIDIA CORP", "8.12", "Equity"),
            ("US0378331005", "APPLE INC", "6.50", "Equity"),
        ], as_of=""), SXR8)


def test_ishares_refuses_a_payload_with_no_holdings_container():
    with pytest.raises(BasketParseError, match="no holdings container"):
        parse_ishares(json.dumps({"componentsByNameMap": {}}).encode(), SXR8)


# -------------------------------------------------------------------------- Vanguard

def test_vanguard_concatenates_pages_in_order():
    basket = parse_vanguard_us([
        vanguard_page([("NVDA", "US67066G1040", "NVIDIA Corp.", "3.99"),
                       ("AAPL", "US0378331005", "Apple Inc.", "3.50")], size=3, has_next=True),
        vanguard_page([("MSFT", "US5949181045", "Microsoft Corp.", "3.20")],
                      size=3, has_next=False),
    ], VT)
    assert [r.ticker for r in basket.rows] == ["NVDA", "AAPL", "MSFT"]
    assert basket.as_of_date == date(2026, 6, 30)
    assert basket.asset_class_available is False


def test_vanguard_refuses_when_a_page_is_missing():
    """
    A dropped page makes the fund look like it holds only its largest names, and the weights
    still sum plausibly — so the declared count is checked rather than trusted.
    """
    with pytest.raises(BasketParseError, match="page is missing"):
        parse_vanguard_us([
            vanguard_page([("NVDA", "US67066G1040", "NVIDIA Corp.", "3.99"),
                           ("AAPL", "US0378331005", "Apple Inc.", "3.50")],
                          size=10_032, has_next=True),
        ], VT)


# -------------------------------------------------------------------- weight sanity

def test_a_negative_weight_on_a_security_refuses_the_whole_file():
    """
    A short equity line. Kept, it would *subtract* a company's exposure and could cancel a real
    direct holding — so it is refused, never clamped.
    """
    with pytest.raises(BasketParseError, match="negative weight on a security"):
        parse_ishares(ishares_json([
            ("US67066G1040", "NVIDIA CORP", "8.12", "Equity"),
            ("US0378331005", "SOMETHING SHORTED", "-2.00", "Equity"),
        ]), SXR8)


def test_negative_cash_lines_are_accepted_because_real_funds_have_them():
    """
    EMIM's live basket ends with five negative currency rows — THB -0.01, TWD -0.01, CNH -0.01,
    HKD -0.02, KRW -0.10 — which are ordinary overdrawn balances. Refusing all 4,042 rows over
    -0.10% of cash would trade the whole fund's look-through for nothing, so the refusal is
    scoped to *invested* rows. This is the real shape, trimmed.
    """
    basket = parse_ishares(ishares_json([
        ("US67066G1040", "NVIDIA CORP", "8.12", "Equity"),
        ("US0378331005", "APPLE INC", "6.50", "Equity"),
        ("", "KRW CASH", "-0.10", "Cash"),
        ("", "HKD CASH", "-0.02", "Cash"),
    ]), SXR8)
    assert len(basket.rows) == 4
    # The negatives reduce the total but never the invested weight.
    assert basket.total_weight_pct == Decimal("14.50")
    assert basket.equity_weight_pct == Decimal("14.62")


def test_a_negative_row_is_still_refused_when_the_issuer_states_no_asset_class():
    """
    Xtrackers publishes no asset class, so cash and equity are indistinguishable — and the only
    available reading of a negative row is the conservative one.
    """
    with pytest.raises(BasketParseError, match="negative weight on a security"):
        parse_dws(dws_csv([
            ("US5949181045", "MICROSOFT", "0.60", "Technology"),
            ("US67066G1040", "NVIDIA CORP", "-0.01", "Technology"),
        ]), XNAS, TODAY)


def test_a_nameless_row_falls_back_to_its_identifier():
    """
    XNAS's live export carries IE00BYQNZ507 with an empty name at 0.008% of the fund. Refusing
    a 106-row basket over a blank label would be absurd — the ISIN is what folds the row, and
    the displayed name comes from the resolved identity when there is one.
    """
    basket = parse_dws(dws_csv([
        ("US5949181045", "MICROSOFT", "0.9", "Technology"),
        ("IE00BYQNZ507", "", "0.1", "unknown"),
    ]), XNAS, TODAY)
    assert basket.rows[1].name == "IE00BYQNZ507"
    assert basket.rows[1].isin == "IE00BYQNZ507"


def test_a_row_with_neither_a_name_nor_an_identifier_refuses_the_file():
    """Nameless and unidentifiable is not a holding that can be attributed, or even shown."""
    with pytest.raises(BasketParseError, match="neither a name nor any identifier"):
        parse_dws(dws_csv([
            ("US5949181045", "MICROSOFT", "0.9", "Technology"),
            ("", "", "0.1", "unknown"),
        ]), XNAS, TODAY)


def test_a_weight_above_a_hundred_refuses_the_whole_file():
    with pytest.raises(BasketParseError, match="more than the whole fund"):
        parse_ishares(ishares_json([
            ("US67066G1040", "NVIDIA CORP", "812", "Equity"),
            ("US0378331005", "APPLE INC", "6.50", "Equity"),
        ]), SXR8)


def test_a_total_beyond_the_tolerance_refuses_the_whole_file():
    with pytest.raises(BasketParseError, match="beyond the"):
        parse_ishares(ishares_json([
            ("US67066G1040", "NVIDIA CORP", "60", "Equity"),
            ("US0378331005", "APPLE INC", "60", "Equity"),
        ]), SXR8)


def test_a_hundred_point_zero_one_total_is_accepted_because_real_files_do_that():
    """IWDA measures 100.01. Refusing rounding would refuse the real world."""
    basket = parse_ishares(ishares_json([
        ("US67066G1040", "NVIDIA CORP", "50.00", "Equity"),
        ("US0378331005", "APPLE INC", "50.01", "Equity"),
    ]), SXR8)
    assert basket.total_weight_pct == Decimal("100.01")


def test_a_single_row_is_a_parse_failure_not_a_fund():
    with pytest.raises(BasketParseError, match="truncated"):
        parse_dws(dws_csv([("US5949181045", "MICROSOFT", "1.0", "Technology")]), XNAS, TODAY)


def test_a_repeated_isin_is_kept_twice_rather_than_deduplicated():
    """
    Keying on the source file's row index rather than the ISIN is what lets the read path sum a
    genuinely repeated constituent. `import_prices.py`'s last-row-wins rule for a repeated date
    would drop weight here.
    """
    basket = parse_dws(dws_csv([
        ("US67066G1040", "NVIDIA CORP", "0.4", "Technology"),
        ("US67066G1040", "NVIDIA CORP", "0.6", "Technology"),
    ]), XNAS, TODAY)
    assert len(basket.rows) == 2
    assert basket.total_weight_pct == Decimal("100.0")


def test_the_invested_predicate_is_shared_and_whitelists():
    """
    The parser computes `equity_weight_pct` with the same function the read path filters on, so
    a stored figure and a value derived from the same rows cannot disagree.
    """
    assert counts_as_invested("Equity", True) is True
    assert counts_as_invested("Cash", True) is False
    assert counts_as_invested("Futures", True) is False
    assert counts_as_invested("Money Market", True) is False
    # A fund holding is invested — the read path then classifies it as nested rather than a
    # company, so the two together still sum to the invested weight.
    assert counts_as_invested("Investment Fund", True) is True
    # A label nobody anticipated is NOT admitted, which is the point of a whitelist.
    assert counts_as_invested("Rights", True) is False
    # With no asset-class column the filter cannot run, so an unclassified row counts...
    assert counts_as_invested(None, False) is True
    # ...but a class the ADAPTER derived still decides. Xtrackers publishes no class column and
    # yet marks its cash and futures lines by identifier, so `parse_dws` states them; before
    # this, XNAS's basket produced company rows called "US DOLLAR".
    assert counts_as_invested("Cash", False) is False
    assert counts_as_invested("Futures", False) is False


# ------------------------------------------------------------- replace-or-refuse gate

@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        yield db
    finally:
        await db.close()
        await engine.dispose()


def _full_basket(rows: int, as_of: date):
    return parse_dws(dws_csv([
        (f"US{i:010d}", f"HOLDING {i}", f"{1 / rows:.10f}", "Technology")
        for i in range(rows)
    ]), XNAS, as_of)


@pytest.mark.asyncio
async def test_a_first_import_stores_the_whole_basket(session):
    repo = EtfBasketRepository(session)
    result = await repo.replace_basket(_full_basket(20, TODAY))
    assert result.stored_rows == 20
    assert result.previous_rows == 0
    stored = (await session.execute(select(EtfHolding))).scalars().all()
    assert len(stored) == 20
    assert {r.fund_isin for r in stored} == {XNAS}


@pytest.mark.asyncio
async def test_a_replacement_is_wholesale_not_a_merge(session):
    repo = EtfBasketRepository(session)
    await repo.replace_basket(_full_basket(20, TODAY))
    await repo.replace_basket(_full_basket(15, TODAY + timedelta(days=1)))
    stored = (await session.execute(select(EtfHolding))).scalars().all()
    assert len(stored) == 15, "the old rows must be gone, not merged with the new ones"


@pytest.mark.asyncio
async def test_a_collapse_in_row_count_is_refused_and_the_old_basket_survives(session):
    """
    The quiet failure this gate exists for. `validate()` catches an HTML body or a one-row
    file, but a genuinely partial parse yields hundreds of plausible rows — and replacing a real
    basket with those makes every look-through figure shrink with nothing reporting it.
    """
    repo = EtfBasketRepository(session)
    await repo.replace_basket(_full_basket(20, TODAY))
    with pytest.raises(BasketReplaceRefused, match="floor"):
        await repo.replace_basket(_full_basket(5, TODAY + timedelta(days=1)))

    stored = (await session.execute(select(EtfHolding))).scalars().all()
    assert len(stored) == 20, "the previous basket must be untouched"
    basket = (await repo.get_baskets([XNAS]))[XNAS]
    assert basket.stored_rows == 20
    assert basket.as_of_date == TODAY


@pytest.mark.asyncio
async def test_a_basket_moving_backwards_in_time_is_refused(session):
    """Re-importing an older saved file would regress the basket while looking like a refresh."""
    repo = EtfBasketRepository(session)
    await repo.replace_basket(_full_basket(20, TODAY))
    with pytest.raises(BasketReplaceRefused, match="backwards"):
        await repo.replace_basket(_full_basket(20, TODAY - timedelta(days=5)))


@pytest.mark.asyncio
async def test_the_same_as_of_re_imports_cleanly_so_a_rerun_is_idempotent(session):
    repo = EtfBasketRepository(session)
    await repo.replace_basket(_full_basket(20, TODAY))
    result = await repo.replace_basket(_full_basket(20, TODAY))
    assert result.stored_rows == 20
    stored = (await session.execute(select(EtfHolding))).scalars().all()
    assert len(stored) == 20


@pytest.mark.asyncio
async def test_force_overrides_both_refusals(session):
    """An index reconstitution can legitimately halve a niche fund's row count."""
    repo = EtfBasketRepository(session)
    await repo.replace_basket(_full_basket(20, TODAY))
    result = await repo.replace_basket(
        _full_basket(4, TODAY - timedelta(days=5)), force=True
    )
    assert result.stored_rows == 4
    stored = (await session.execute(select(EtfHolding))).scalars().all()
    assert len(stored) == 4


def test_dws_cash_and_futures_rows_are_read_off_the_issuers_own_identifiers():
    """
    Xtrackers has no asset-class column but marks non-securities unmistakably: the live XNAS
    basket carries `_CURRENCYUSD` ("US DOLLAR"), `_CURRENCYEUR` ("EURO CURRENCY") and
    `___ADI34XYM5` ("NASDAQ 100 E-MINI SEP26"). Without reading that convention those became
    company rows, and "US DOLLAR" is not a company.
    """
    basket = parse_dws(dws_csv([
        ("US5949181045", "MICROSOFT", "0.90", "Technology"),
        ("_CURRENCYUSD", "US DOLLAR", "0.06", "unknown"),
        ("_CURRENCYEUR", "EURO CURRENCY", "0.01", "unknown"),
        ("___ADI34XYM5", "NASDAQ 100 E-MINI SEP26", "0.03", "unknown"),
    ]), XNAS, TODAY)

    assert [r.asset_class for r in basket.rows] == [None, "Cash", "Cash", "Futures"]
    # The securities weight excludes them; the rows are still stored, so the residual is real.
    assert basket.total_weight_pct == Decimal("100.00")
    assert basket.equity_weight_pct == Decimal("90.00")
    # Only the negatives are derived — a real holding is left unclassified rather than asserted
    # to be equity, which would be an inference about instruments nobody looked at.
    assert basket.rows[0].asset_class is None
