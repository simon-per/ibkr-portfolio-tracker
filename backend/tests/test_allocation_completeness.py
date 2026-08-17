"""
All three allocation charts must account for every priced holding.

`get_portfolio_allocation` publishes three breakdowns off one position list, and
until 2026-08-05 they disagreed about what to do with a holding whose category is
unknown. Asset type used `security.asset_type or 'Unknown'`, so it always bucketed
the position. Sector and geography used `if security.sector:` / `if
security.country:` and silently dropped it — so those two summed to less than 100%
while `AllocationTab` printed every slice as "% of portfolio", and the treemap,
which sizes by area and therefore renormalises, still drew a full rectangle. The
picture looked complete and only the printed numbers were short.

The trigger is routine, not theoretical: `sync_helper` never writes `sector` or
`country`, so a security created by an IBKR ingest has both NULL, while
`asset_type` carries a `"Stock"` column default. A newly bought holding therefore
appeared in the asset-type chart and in neither of the others. Only
`sync_allocation_data` fills those columns, and nothing schedules it — it needs
Yahoo — so the gap persisted until someone ran it by hand.

The tests are written against the **family** rather than the instance: every
breakdown the endpoint returns must sum to 100%, so a fourth chart added later is
held to the same rule without anyone remembering to add a case.

Offline: no Yahoo, no network. Prices are seeded directly.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.market_price import MarketPrice
from app.services.allocation_service import AllocationService

BREAKDOWNS = ("asset_type_allocation", "sector_allocation", "geographic_allocation")


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


async def _hold(db, *, symbol, isin, conid, sector, country, asset_type="Stock",
                quantity="10", price="100"):
    """One fully priced open position, with the category columns under test."""
    security = Security(
        isin=isin, symbol=symbol, description=f"{symbol} Inc", currency="EUR",
        conid=conid, asset_category="STK", exchange="NASDAQ",
        sector=sector, country=country, asset_type=asset_type,
    )
    db.add(security)
    await db.flush()

    db.add(TaxLot(
        security_id=security.id, open_date=date.today() - timedelta(days=200),
        quantity=Decimal(quantity), cost_basis=Decimal("500"),
        price_per_unit=Decimal("50"), currency="EUR",
        cost_basis_eur=Decimal("500"), is_open=True,
    ))
    # A price for today and yesterday, so the forward-fill lookback resolves
    # regardless of which one the service asks for.
    for offset in (0, 1):
        db.add(MarketPrice(
            security_id=security.id, date=date.today() - timedelta(days=offset),
            close_price=Decimal(price), currency="EUR", source="test",
        ))
    await db.commit()
    return security


def _sums(allocation):
    return {k: sum(v["percentage"] for v in allocation[k].values()) for k in BREAKDOWNS}


@pytest.mark.asyncio
async def test_a_holding_with_no_sector_is_still_counted(db):
    """The bug: the position vanished from sector and geography, not from asset type."""
    await _hold(db, symbol="KNOWN", isin="US0000000001", conid=1,
                sector="Technology", country="United States")
    await _hold(db, symbol="FRESH", isin="US0000000002", conid=2,
                sector=None, country=None)

    allocation = await AllocationService(db).get_portfolio_allocation()

    assert "FRESH" in {p["symbol"]
                       for cat in allocation["sector_allocation"].values()
                       for p in cat["positions"]}, "the holding is missing from the sector chart"
    assert "FRESH" in {p["symbol"]
                       for cat in allocation["geographic_allocation"].values()
                       for p in cat["positions"]}, "the holding is missing from the geography chart"


@pytest.mark.asyncio
async def test_every_breakdown_sums_to_the_whole_portfolio(db):
    """
    The family rule, and the one that fails loudest on a regression.

    Two equal holdings, one with no category on record. Before the fix the sector
    and geography charts summed to 50% while asset type summed to 100 — and the
    frontend labels each slice "% of portfolio".
    """
    await _hold(db, symbol="KNOWN", isin="US0000000001", conid=1,
                sector="Technology", country="United States")
    await _hold(db, symbol="FRESH", isin="US0000000002", conid=2,
                sector=None, country=None)

    sums = _sums(await AllocationService(db).get_portfolio_allocation())

    for name, total in sums.items():
        assert total == pytest.approx(100.0, abs=0.05), f"{name} sums to {total}, not 100"


@pytest.mark.asyncio
async def test_the_unknown_bucket_is_named_rather_than_folded_into_a_real_category(db):
    """
    Making the chart sum to 100 by attributing the holding to some *existing*
    category would be the worse bug — a confident wrong answer instead of a gap.
    It must land in its own labelled bucket, which `AllocationTab` already has a
    colour for.
    """
    await _hold(db, symbol="KNOWN", isin="US0000000001", conid=1,
                sector="Technology", country="United States")
    await _hold(db, symbol="FRESH", isin="US0000000002", conid=2,
                sector=None, country=None)

    allocation = await AllocationService(db).get_portfolio_allocation()

    assert "Unknown" in allocation["sector_allocation"]
    assert "Unknown" in allocation["geographic_allocation"]
    assert {p["symbol"] for p in allocation["sector_allocation"]["Unknown"]["positions"]} == {"FRESH"}
    assert {p["symbol"] for p in allocation["sector_allocation"]["Technology"]["positions"]} == {"KNOWN"}


@pytest.mark.asyncio
async def test_a_fully_categorised_portfolio_grows_no_unknown_bucket(db):
    """The other direction: the fix must not invent an empty slice on good data."""
    await _hold(db, symbol="AAA", isin="US0000000001", conid=1,
                sector="Technology", country="United States")
    await _hold(db, symbol="BBB", isin="US0000000002", conid=2,
                sector="Healthcare", country="Switzerland")

    allocation = await AllocationService(db).get_portfolio_allocation()

    assert "Unknown" not in allocation["sector_allocation"]
    assert "Unknown" not in allocation["geographic_allocation"]
    for name, total in _sums(allocation).items():
        assert total == pytest.approx(100.0, abs=0.05), f"{name} sums to {total}"


@pytest.mark.asyncio
async def test_an_unpriced_holding_does_not_break_the_percentages(db):
    """
    A holding the backend cannot value must not stop the remaining charts adding up:
    adding a NaN or dividing by a zero total is the failure this guards.

    **Note what summing to 100 does NOT prove**, which took until 2026-08-17 to say out
    loud: the three breakdowns sum to exactly 100 whether the unvaluable holding is
    excluded or included at a 0% weight, so this assertion is blind to the holding being
    silently absent from a chart labelling every slice "% of portfolio". Here, summing to
    100 is what being wrong looks like — see the test below, which is the missing half.
    """
    await _hold(db, symbol="PRICED", isin="US0000000001", conid=1,
                sector="Technology", country="United States")
    unpriced = Security(
        isin="US0000000002", symbol="DARK", description="No price on record",
        currency="EUR", conid=2, asset_category="STK", exchange="NASDAQ",
        sector=None, country=None, asset_type="Stock",
    )
    db.add(unpriced)
    await db.flush()
    db.add(TaxLot(
        security_id=unpriced.id, open_date=date.today() - timedelta(days=200),
        quantity=Decimal("10"), cost_basis=Decimal("500"),
        price_per_unit=Decimal("50"), currency="EUR",
        cost_basis_eur=Decimal("500"), is_open=True,
    ))
    await db.commit()

    allocation = await AllocationService(db).get_portfolio_allocation()

    for name, total in _sums(allocation).items():
        assert total == pytest.approx(100.0, abs=0.05), f"{name} sums to {total}"


@pytest.mark.asyncio
async def test_an_unpriced_holding_is_named_rather_than_silently_absent(db):
    """
    The missing half of the test above.

    Every slice on this tab is labelled "% of portfolio", and the percentages are shares of
    the value that could be *priced* — so an unvaluable holding does not render as a visible
    zero, it renders as an **absence** from a picture that still adds to 100. That is the
    same failure the look-through reports as `unvaluable_positions` and the summary card as
    `unpriced_holdings`, reaching the one surface in that family with no signal at all.

    Live rather than theoretical: three newly bought ETFs were unpriced for hours on
    2026-08-07, and for that window these three charts quietly described a smaller
    portfolio than the one on the card above them.
    """
    await _hold(db, symbol="PRICED", isin="US0000000001", conid=1,
                sector="Technology", country="United States")
    unpriced = Security(
        isin="US0000000002", symbol="DARK", description="No price on record",
        currency="EUR", conid=2, asset_category="STK", exchange="NASDAQ",
        sector="Healthcare", country="Germany", asset_type="Stock",
    )
    db.add(unpriced)
    await db.flush()
    db.add(TaxLot(
        security_id=unpriced.id, open_date=date.today() - timedelta(days=200),
        quantity=Decimal("10"), cost_basis=Decimal("500"),
        price_per_unit=Decimal("50"), currency="EUR",
        cost_basis_eur=Decimal("500"), is_open=True,
    ))
    await db.commit()

    allocation = await AllocationService(db).get_portfolio_allocation()

    assert allocation["unpriced_holdings"] == 1
    assert allocation["unpriced_symbols"] == ["DARK"]
    # Excluded from both sides rather than carried at 0.00, matching the look-through and
    # the forward yield: a zero-weight row in a chart legend is noise, and the count is
    # what actually informs.
    everywhere = [
        p["symbol"]
        for breakdown in BREAKDOWNS
        for cat in allocation[breakdown].values()
        for p in cat["positions"]
    ]
    assert "DARK" not in everywhere
    # ...and its sector must not have been invented into existence by a zero-weight row.
    assert "Healthcare" not in allocation["sector_allocation"]


@pytest.mark.asyncio
async def test_a_portfolio_with_nothing_priced_still_reports_what_it_dropped(db):
    """The early-return path needs the field too, or the one case where the charts are
    entirely empty is the one that explains nothing."""
    unpriced = Security(
        isin="US0000000003", symbol="DARK", description="No price on record",
        currency="EUR", conid=3, asset_category="STK", exchange="NASDAQ",
        sector=None, country=None, asset_type="Stock",
    )
    db.add(unpriced)
    await db.flush()
    db.add(TaxLot(
        security_id=unpriced.id, open_date=date.today() - timedelta(days=200),
        quantity=Decimal("10"), cost_basis=Decimal("500"),
        price_per_unit=Decimal("50"), currency="EUR",
        cost_basis_eur=Decimal("500"), is_open=True,
    ))
    await db.commit()

    allocation = await AllocationService(db).get_portfolio_allocation()

    assert allocation["total_market_value_eur"] == 0.0
    assert allocation["unpriced_holdings"] == 1
    assert allocation["unpriced_symbols"] == ["DARK"]


@pytest.mark.asyncio
async def test_a_fully_priced_portfolio_reports_no_omission(db):
    """The other side, so the field cannot be a hardcoded count."""
    await _hold(db, symbol="PRICED", isin="US0000000001", conid=1,
                sector="Technology", country="United States")

    allocation = await AllocationService(db).get_portfolio_allocation()

    assert allocation["unpriced_holdings"] == 0
    assert allocation["unpriced_symbols"] == []


@pytest.mark.asyncio
async def test_a_fund_is_a_fund_in_all_three_charts(db):
    """
    The three charts must not answer "is this a fund?" by different rules.

    All three now read one `allocation_for_fund_isin` lookup — a live table lookup that
    needs no sync, keyed on ISIN because a ticker is not an identity (see
    `test_fundness_predicate.py`). The asset-type chart read `securities.asset_type`,
    which is written *only* by `POST /api/allocation/sync`; `sync_helper` never writes it
    and nothing schedules that route, so an IBKR-ingested fund keeps the column default
    "Stock" indefinitely.

    The result was one holding described two ways on one tab: a Stock in the asset-type
    chart, and simultaneously an ETF spread across eleven sectors a few pixels below.
    Reachable the moment any mapped fund is bought — VT, GRID and QTUM were mapped on
    2026-08-06 for exactly that reason, ahead of the statement that creates them.
    """
    # asset_type left at the ingest default on purpose: this is what IBKR gives us.
    await _hold(db, symbol="VT", isin="US9220427424", conid=1,
                sector=None, country=None, asset_type="Stock")

    allocation = await AllocationService(db).get_portfolio_allocation()

    buckets = {
        name: cat for name, cat in allocation["asset_type_allocation"].items()
        if any(p["symbol"] == "VT" for p in cat["positions"])
    }
    assert list(buckets) == ["ETF"], (
        f"VT is bucketed as {list(buckets)} while the sector and geography charts "
        "distribute it as a fund"
    )
    # And the look-through it is being given at the same time, so the two really are
    # describing one holding.
    assert any(p["symbol"] == "VT"
               for cat in allocation["sector_allocation"].values()
               for p in cat["positions"])


@pytest.mark.asyncio
async def test_an_unmapped_holding_still_reads_its_own_column(db):
    """
    The mirror direction: the table wins only where it has an entry. A security the
    look-through does not know must keep whatever `asset_type` it carries, or a manual
    allocation sync's work would be discarded.
    """
    await _hold(db, symbol="NOTAFUND", isin="US0000000009", conid=9,
                sector="Technology", country="United States", asset_type="Bond")

    allocation = await AllocationService(db).get_portfolio_allocation()

    assert any(p["symbol"] == "NOTAFUND"
               for p in allocation["asset_type_allocation"]["Bond"]["positions"])
