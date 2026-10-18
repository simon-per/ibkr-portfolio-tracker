"""
`sync_stale_fundamentals` has to be able to fetch a security for the first time.

It pre-filtered on `get_stale_metrics(days_old=1)` and bailed when that came back empty.
That query selects metrics rows which already **exist**, so a security with no row was
invisible to it — and whenever every existing row happened to be fresh, this path reported
"No stale fundamentals to update" and a newly-bought security never acquired fundamentals
through it at all. The union that would have caught it (`s.id not in existing_ids or
s.id in stale_ids`) sat one call *below* the guard that stopped it being reached.

The reason this survived is worth pinning too: `sync_stale_ratings` was fixed for exactly
this shape, and both its docstring and CLAUDE.md's duplicated-logic table cite *this*
method as the sibling that "already unions the two sets". Both were describing the inner
function while the entry point pre-filtered — a correct fix justified by a false reading
of the code it was copying. `tests/test_analyst_rating_bootstrap.py` is the mirror of this
file, and its own opening paragraph repeats the claim.

Offline: `_fetch_yahoo_data` is faked, so yfinance is never reached.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.clock import utcnow
import app.models  # noqa: F401  register all mappers
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import Security
from app.models.taxlot import TaxLot
from app.services import fundamentals_service as fs_mod
from app.services.fundamentals_service import FundamentalsService


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    async def _sleep(*_a, **_kw):
        return None

    monkeypatch.setattr(fs_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(fs_mod.random, "uniform", lambda *_a: 0)


def add_security(session, security_id: int, symbol: str, held: bool = True) -> Security:
    """A security with one lot: open while held, closed once sold."""
    security = Security(
        id=security_id, isin=f"US000000000{security_id}", symbol=symbol,
        description=symbol, currency="USD", conid=security_id,
        asset_category="STK", exchange="NASDAQ",
    )
    session.add(security)
    session.add(TaxLot(
        security_id=security_id, open_date=date(2025, 1, 2),
        close_date=None if held else date(2026, 1, 2), is_open=held,
        quantity=Decimal("10"), cost_basis=Decimal("1000"),
        price_per_unit=Decimal("100"), currency="USD", cost_basis_eur=Decimal("900"),
    ))
    return security


def add_metrics(session, security_id: int, age_days: float = 0, age_hours: float = 0
                ) -> FundamentalMetrics:
    metrics = FundamentalMetrics(
        security_id=security_id, trailing_pe=20.0, quote_type="EQUITY",
        last_updated=utcnow() - timedelta(days=age_days, hours=age_hours),
    )
    session.add(metrics)
    return metrics


@pytest.fixture
def fetched(monkeypatch):
    """Record which securities were fetched, without touching yfinance."""
    seen = []

    def _fake_fetch(_self, ticker):
        seen.append(ticker)
        return {}, None, None, None, None

    monkeypatch.setattr(FundamentalsService, "_fetch_yahoo_data", _fake_fetch)
    return seen


@pytest.mark.asyncio
async def test_a_security_with_no_metrics_row_is_picked_up(db, fetched):
    """The bug: the stale query cannot see a security that has no row to be stale."""
    add_security(db, 1, "NEW")
    await db.commit()

    result = await FundamentalsService(db).sync_stale_fundamentals()

    assert fetched, "a never-synced security was never fetched"
    assert result["message"] != "No stale fundamentals to update"


@pytest.mark.asyncio
async def test_a_fresh_table_still_bootstraps_a_new_security(db, fetched):
    """
    The precise failure, and the one production was one fundamentals run away from: every
    existing row is fresh, so the old pre-filter returned empty and bailed — while a
    newly-bought security sat there with no row at all.
    """
    add_security(db, 1, "OLD")
    add_security(db, 2, "NEW")
    add_metrics(db, 1, age_days=0)          # fresh, so `get_stale_metrics` finds nothing
    await db.commit()

    await FundamentalsService(db).sync_stale_fundamentals()

    assert fetched == ["NEW"], f"expected only the unsynced security, got {fetched}"


@pytest.mark.asyncio
async def test_a_stale_row_is_still_refreshed(db, fetched):
    """The direction that must not regress — dropping the guard must not stop refreshes."""
    add_security(db, 1, "OLD")
    add_metrics(db, 1, age_days=5)
    await db.commit()

    await FundamentalsService(db).sync_stale_fundamentals()

    assert fetched == ["OLD"]


@pytest.mark.asyncio
async def test_an_all_fresh_portfolio_still_reports_nothing_to_do(db, fetched):
    """
    And the other direction: with every security synced and fresh, this must not start
    spending ~5 Yahoo requests per security on every call. The delegate's own early
    return covers it, which is why the guard was removable rather than replaceable.
    """
    add_security(db, 1, "AAA")
    add_metrics(db, 1, age_days=0)
    await db.commit()

    result = await FundamentalsService(db).sync_stale_fundamentals()

    assert fetched == []
    assert result["securities_processed"] == 0
    assert result["message"] == "All fundamentals are up to date"


# ---------------------------------------------------------------------------
# One definition of "stale". There used to be three that disagreed: the repository
# defaulted to 7 days, `sync_fundamentals_data` passed `days_old=1`, and
# `/api/fundamentals/status` ran its own hardcoded 7-day query — so the status endpoint
# could report `stale_metrics: 0` beside a sync that would refresh nearly every row.
# The `/sync-stale` docstring claimed 7 days as well.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_counts_exactly_what_a_sync_would_refresh(db, fetched):
    """
    The anti-drift assertion: the number `/status` reports and the work the sync does
    have to come from one threshold. A row three days old is stale under the real
    (20-hour) rule and was invisible to the old 7-day count.
    """
    from app.repositories.fundamentals_repository import FundamentalsRepository

    add_security(db, 1, "AAA")
    add_security(db, 2, "BBB")
    add_security(db, 3, "SOLD", held=False)
    add_metrics(db, 1, age_days=3)      # stale under 20 hours, fresh under the old 7 days
    add_metrics(db, 2, age_days=0)      # genuinely fresh
    add_metrics(db, 3, age_days=30)     # stale, but sold: no sync refreshes it, so not counted
    await db.commit()

    counted = await FundamentalsRepository(db).count_stale_metrics()
    await FundamentalsService(db).sync_stale_fundamentals()

    assert counted == 1, "the status count disagrees with the sync's own threshold"
    assert fetched == ["AAA"], f"the sync refreshed {fetched}, not what /status counted"


@pytest.mark.asyncio
async def test_status_counts_the_holdings_a_sync_walks(db, fetched):
    """
    A held `price_source` opt-out (the pillar 3a funds) is something no sync will fetch,
    so `/status` must not report it as a holding without data — it did, through `held()`
    alone, which kept the tab's amber line and Update button up for good and made Update
    start a pass that could not clear them (GitHub issue #18).
    """
    from app.models.security import PRICE_SOURCE_MANUAL
    from app.routers.fundamentals import get_fundamentals_status
    from app.schemas.portfolio import FundamentalsStatus

    add_security(db, 1, "AAA")
    add_security(db, 2, "PILLAR").price_source = PRICE_SOURCE_MANUAL
    add_security(db, 3, "NEW")
    add_metrics(db, 1, age_days=0)
    add_metrics(db, 2, age_days=30)     # an opt-out's old row is not "data" either
    await db.commit()

    status = await get_fundamentals_status(db)
    await FundamentalsService(db).sync_fundamentals_data()

    # The handler's own keys, against the model it is filtered through, both ways.
    assert set(status) == set(FundamentalsStatus.model_fields)
    assert (status["total_securities"], status["securities_with_data"],
            status["securities_without_data"]) == (2, 1, 1)
    assert fetched == ["NEW"], f"the sync fetched {fetched}, not what /status reported missing"


# The threshold-in-one-place guard (repository defaults, no call site passing its own)
# is the family form in `tests/test_staleness_predicate_family.py`, with ratings.


# ---------------------------------------------------------------------------
# The daily pass (`FUNDAMENTALS_REFRESH_HOUR`, Tuesday to Saturday): stale has to mean
# "written before this morning's pass", and only what is held is asked about.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_written_by_yesterdays_pass_is_stale_at_todays(db, fetched):
    """
    Under a one-day rule a row written at 05:02 is 23 h 58 m old at the next 05:00 and
    was skipped, so each security refreshed only every other day.
    """
    add_security(db, 1, "YDAY")
    add_security(db, 2, "TODAY")
    add_metrics(db, 1, age_hours=23.9)
    add_metrics(db, 2, age_hours=19)    # a manual Refresh the same day spends nothing
    await db.commit()

    await FundamentalsService(db).sync_stale_fundamentals()

    assert fetched == ["YDAY"]


@pytest.mark.asyncio
async def test_a_sold_position_is_neither_fetched_nor_listed(db, fetched):
    """
    Held only: the daily pass would otherwise spend ~6 Yahoo requests a day on every
    position ever sold, and the tab would list rows nothing refreshes any more.
    """
    add_security(db, 1, "HELD")
    add_security(db, 2, "SOLD", held=False)
    add_metrics(db, 2, age_days=30)
    await db.commit()

    await FundamentalsService(db).sync_fundamentals_data(force_refresh=True)
    listed = await FundamentalsService(db).get_fundamentals_for_portfolio()

    assert fetched == ["HELD"]
    assert [row["symbol"] for row in listed] == ["HELD"]
    # Its stored row stays: buying it back shows the old figures until the next pass.
    from app.repositories.fundamentals_repository import FundamentalsRepository
    assert await FundamentalsRepository(db).get_metrics_by_security_id(2) is not None
