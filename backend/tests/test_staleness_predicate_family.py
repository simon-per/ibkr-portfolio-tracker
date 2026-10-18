"""
The family rule for cached-upstream tables with a `/status` route: the stale count it
reports is the work its `sync_stale_*` would do.

Fundamentals and analyst ratings each had a `/status` running its own staleness query
beside the sync's: different thresholds first (fundamentals: 7 days counted, 1 day
synced), then a different scope (every row counted, only held and Yahoo-eligible
securities synced), so a sold position's row stayed "stale" forever (GitHub issue #21,
ratings repeating what fundamentals had fixed). Each table now has one `STALE_AFTER` and
one scoped `_stale` in its repository, and `/status` counts through it.

Each family member is seeded with one row per category — held and eligible and old,
sold and old, held but a `price_source` opt-out and old, held and fresh — and `/status`'s
stale count must equal what the sync then fetches. A new table with a `/status` and a
`sync_stale_*` belongs in `FAMILY`.

Offline: every fetch is faked, so Yahoo is never reached.
"""
import ast
import inspect
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.clock import utcnow
from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.analyst_rating import AnalystRating
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.security import PRICE_SOURCE_MANUAL, Security
from app.models.taxlot import TaxLot
from app.repositories import analyst_rating_repository as ar_repo
from app.repositories import fundamentals_repository as fm_repo
from app.routers import analyst_ratings as ar_router
from app.routers import fundamentals as fm_router
from app.services import analyst_rating_service as ar_service
from app.services import fundamentals_service as fm_service


@dataclass(frozen=True)
class Member:
    name: str
    stale_after: timedelta
    row: Callable[[int, object], object]          # (security_id, last_updated) -> model
    fetch_attr: tuple                              # (service class, method name) faked
    sync: Callable                                 # async (db) -> the sync_stale_* pass
    status: Callable                               # async (db) -> the /status payload
    status_key: str
    repo_cls: type
    count_method: str
    get_method: str
    modules: tuple                                 # where a threshold literal must not be


def _rating(security_id, last_updated):
    return AnalystRating(security_id=security_id, strong_buy=1, buy=0, hold=0, sell=0,
                         strong_sell=0, last_updated=last_updated)


def _metrics(security_id, last_updated):
    return FundamentalMetrics(security_id=security_id, trailing_pe=20.0,
                              quote_type="EQUITY", last_updated=last_updated)


FAMILY = [
    Member(
        name="fundamentals",
        stale_after=fm_repo.STALE_AFTER,
        row=_metrics,
        fetch_attr=(fm_service.FundamentalsService, "_fetch_yahoo_data"),
        sync=lambda db: fm_service.FundamentalsService(db).sync_stale_fundamentals(),
        status=lambda db: fm_router.get_fundamentals_status(db=db),
        status_key="stale_metrics",
        repo_cls=fm_repo.FundamentalsRepository,
        count_method="count_stale_metrics",
        get_method="get_stale_metrics",
        modules=(fm_repo, fm_service, fm_router),
    ),
    Member(
        name="analyst ratings",
        stale_after=ar_repo.STALE_AFTER,
        row=_rating,
        fetch_attr=(ar_service.AnalystRatingService, "fetch_rating_for_security"),
        sync=lambda db: ar_service.AnalystRatingService(db).sync_stale_ratings(),
        status=lambda db: ar_router.get_analyst_ratings_status(db=db),
        status_key="stale_ratings",
        repo_cls=ar_repo.AnalystRatingRepository,
        count_method="count_stale_ratings",
        get_method="get_stale_ratings",
        modules=(ar_repo, ar_service, ar_router),
    ),
]


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
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


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    async def _sleep(*_a, **_kw):
        return None

    for module in (fm_service, ar_service):
        monkeypatch.setattr(module.asyncio, "sleep", _sleep)
        monkeypatch.setattr(module.random, "uniform", lambda *_a: 0)


def _fake_fetch(member: Member, monkeypatch) -> list:
    """Record each security the sync would ask Yahoo about, by symbol."""
    seen = []
    cls, attr = member.fetch_attr
    if attr == "_fetch_yahoo_data":
        def fake(_self, ticker):
            seen.append(ticker)
            return {}, None, None, None, None
    else:
        async def fake(_self, security):
            seen.append(security.symbol)
            return {"security_id": security.id, "strong_buy": 1, "buy": 0,
                    "hold": 0, "sell": 0, "strong_sell": 0}
    monkeypatch.setattr(cls, attr, fake)
    return seen


def _security(db, security_id: int, symbol: str, *, held: bool = True,
              price_source: str | None = None) -> None:
    security = Security(id=security_id, isin=f"US000000000{security_id}", symbol=symbol,
                        description=symbol, currency="USD", conid=security_id,
                        asset_category="STK", exchange="NASDAQ")
    if price_source:
        security.price_source = price_source
    db.add(security)
    db.add(TaxLot(security_id=security_id, open_date=date(2025, 1, 2),
                  close_date=None if held else date(2026, 1, 2), is_open=held,
                  quantity=Decimal("10"), cost_basis=Decimal("1000"),
                  price_per_unit=Decimal("100"), currency="USD",
                  cost_basis_eur=Decimal("900")))


@pytest.mark.asyncio
@pytest.mark.parametrize("member", FAMILY, ids=lambda m: m.name)
async def test_status_counts_what_the_sync_would_fetch(db, monkeypatch, member):
    fetched = _fake_fetch(member, monkeypatch)
    old = utcnow() - member.stale_after - timedelta(hours=1)

    _security(db, 1, "OLD")                                         # held, eligible, old
    _security(db, 2, "SOLD", held=False)                            # sold, old
    _security(db, 3, "OPTOUT", price_source=PRICE_SOURCE_MANUAL)    # ineligible, old
    _security(db, 4, "FRESH")                                       # held, fresh
    for security_id in (1, 2, 3):
        db.add(member.row(security_id, old))
    db.add(member.row(4, utcnow()))
    await db.commit()

    status = await member.status(db)
    await member.sync(db)

    assert fetched == ["OLD"], f"{member.name}: the sync fetched {fetched}"
    assert status[member.status_key] == len(fetched), (
        f"{member.name}: /status reports {status[member.status_key]} stale, "
        f"the sync fetched {len(fetched)}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("member", FAMILY, ids=lambda m: m.name)
async def test_status_counts_through_the_repository(db, monkeypatch, member):
    """
    Not only an agreeing number: no copy at all. A `/status` running its own query with
    today's threshold and scope passes the test above and drifts the first time either
    changes.
    """
    sentinel = 987_654

    async def counted(_self, *args, **kwargs):
        assert not args and not kwargs, "a caller passes its own threshold"
        return sentinel

    monkeypatch.setattr(member.repo_cls, member.count_method, counted)
    status = await member.status(db)
    assert status[member.status_key] == sentinel


@pytest.mark.parametrize("member", FAMILY, ids=lambda m: m.name)
def test_the_threshold_lives_in_one_place(member):
    """
    The repository defaults are its `STALE_AFTER`, and no call site passes a threshold
    of its own. By AST, so a docstring quoting an old call does not trip it.
    """
    for method in (member.count_method, member.get_method):
        default = inspect.signature(
            getattr(member.repo_cls, method)
        ).parameters["older_than"].default
        assert default == member.stale_after, f"{member.name}.{method}"

    offenders = []
    for module in member.modules:
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") in
                    (member.count_method, member.get_method)
                    and (node.args or node.keywords)):
                offenders.append(f"{module.__name__}:{node.lineno}")
    assert not offenders, f"a call site passes its own staleness threshold: {offenders}"
