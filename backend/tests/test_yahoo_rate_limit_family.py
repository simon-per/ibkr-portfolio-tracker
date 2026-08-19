"""
Every service that loops securities against Yahoo must abandon the pass on a 429.

Rule 1 in CLAUDE.md is that a rate limit means stop immediately, because continuing is
what turns a short block into a long one. `MarketDataService` was fixed on 2026-08-04
after its "rate-limit detection that aborts the run" turned out to abort only the ticker
*variations* for the security in hand, while the caller moved on to the next of forty.

Three services still had that older shape: `FundamentalsService` (~5 Yahoo endpoints per
security, the most expensive of the four), `AnalystRatingService` and `WatchlistService`
all caught the error, logged it, and continued through the whole list.

**These tests are written against the family, not the instances.** The last one walks the
source for any service that loops and calls Yahoo and asserts it consults the shared
predicate — so a fifth service added later is caught without anyone remembering to
extend this file. That is the lesson CLAUDE.md draws from the same bug appearing twice.

Offline: no network. Every fetch is replaced by a raiser.
"""

import ast
import inspect
import pathlib

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  register all mappers
from app.models.security import Security
from app.models.watchlist_item import WatchlistItem
import app.services.analyst_rating_service as ratings_module
from app.services.analyst_rating_service import AnalystRatingService
from app.services.fundamentals_service import FundamentalsService
from app.services.watchlist_service import WatchlistService
from app.services.yahoo_rate_limit import is_rate_limit


async def _no_sleep(_seconds):
    """The per-security courtesy pacing is not what these tests measure."""
    return None


class RateLimited(Exception):
    """What yfinance raises through when Yahoo refuses us for volume."""

    def __init__(self):
        super().__init__("429 Client Error: Too Many Requests for url: https://query2.finance.yahoo.com")


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


@pytest.fixture(autouse=True)
def _no_delays(monkeypatch):
    """
    These loops sleep 1-4s per security and the pacing is not what is under test.

    Zeroing `random.uniform` per service module rather than patching `asyncio.sleep`
    globally: the global patch also intercepts SQLAlchemy's own awaits and fails the
    session with `MissingGreenlet`, which looks like a bug in the code under test.
    """
    for module in (
        "app.services.fundamentals_service",
        "app.services.analyst_rating_service",
        "app.services.watchlist_service",
        "app.services.allocation_service",
        "app.services.dividend_service",
    ):
        monkeypatch.setattr(f"{module}.random.uniform", lambda *_a, **_kw: 0)


async def _seed_securities(db, n=6):
    for i in range(n):
        db.add(Security(
            isin=f"US000000000{i}", symbol=f"SEC{i}", description=f"Security {i}",
            currency="USD", conid=1000 + i, asset_category="STK", exchange="NASDAQ",
        ))
    await db.commit()


# ── The predicate itself ────────────────────────────────────────────────────

def test_the_predicate_recognises_what_yahoo_actually_sends():
    assert is_rate_limit(RateLimited())
    assert is_rate_limit("429 Too Many Requests")
    assert is_rate_limit("Rate limit exceeded")
    assert is_rate_limit("Your request was blocked")


def test_the_predicate_stays_narrow_on_purpose():
    """
    A JSON-decode failure and a 404 are equally what a *bad ticker* looks like, and
    `_try_fetch_yahoo` uses this same verdict to decide whether to keep trying ticker
    variations. Matching them would abort auto-discovery on every security Yahoo does
    not know — a real cost paid on good data, to catch a case the 429 marker already
    covers whenever Yahoo says so plainly.
    """
    assert not is_rate_limit("Expecting value: line 1 column 1 (char 0)")
    assert not is_rate_limit("404 Not Found")
    assert not is_rate_limit("No data found for this date range")
    assert not is_rate_limit(None)
    assert not is_rate_limit("")


# ── Each loop stops ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fundamentals_abandons_the_pass(db, monkeypatch):
    await _seed_securities(db)
    service = FundamentalsService(db)

    calls = []

    async def _raise(ticker):
        calls.append(ticker)
        raise RateLimited()

    monkeypatch.setattr(service, "_fetch_yahoo_data", _raise)
    monkeypatch.setattr(service, "_get_yahoo_ticker", lambda sec, ms=None: _ticker(sec))

    result = await service.sync_fundamentals_data(force_refresh=True)

    assert len(calls) == 1, f"kept fetching after a 429: {calls}"
    assert result["rate_limited"] is True
    assert result["warnings"], "a pass that stopped early must say so"


@pytest.mark.asyncio
async def test_analyst_ratings_abandons_the_pass(db, monkeypatch):
    """
    Driven from the yfinance boundary, not from the method under test.

    The previous version of this test monkeypatched `fetch_rating_for_security` itself
    with a function that raises — and that method is precisely the one that could not
    raise, because it wrapped the whole yfinance call in `except Exception: return
    None`. So the 429 never reached the loop, `is_rate_limit(e)` there was unreachable,
    and after a limit the pass went on asking Yahoo about the remaining securities two
    to four seconds apart. The test replaced the broken part with a working one and then
    measured the replacement: it passed for months against the bug it was written for,
    which is the third time that has happened in this repository.

    Patching `yf.Ticker` keeps the real error path in the picture — the raise has to
    travel out through `fetch_rating_for_security` for this to go green.
    """
    await _seed_securities(db)
    service = AnalystRatingService(db)

    calls = []

    class _Exploding:
        def __init__(self, ticker):
            calls.append(ticker)

        @property
        def recommendations(self):
            raise RateLimited()

    monkeypatch.setattr(ratings_module.yf, "Ticker", _Exploding)
    # The 1-3s per-security courtesy sleep would otherwise make this test slow for no
    # coverage; the pacing itself is not what is under test here.
    monkeypatch.setattr(ratings_module.asyncio, "sleep", _no_sleep)

    result = await service.sync_ratings_for_securities()

    assert len(calls) == 1, f"kept fetching after a 429: {calls}"
    assert result["rate_limited"] is True
    assert result["warnings"], "a pass that stopped early must say so"


@pytest.mark.asyncio
async def test_an_ordinary_ratings_error_still_only_skips_one_security(db, monkeypatch):
    """
    The mirror image, and the reason the fix re-raises selectively rather than removing
    the handler. A ticker Yahoo has never heard of must cost that security and no more.
    """
    await _seed_securities(db, n=4)
    service = AnalystRatingService(db)

    calls = []

    class _Unknown:
        def __init__(self, ticker):
            calls.append(ticker)

        @property
        def recommendations(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(ratings_module.yf, "Ticker", _Unknown)
    monkeypatch.setattr(ratings_module.asyncio, "sleep", _no_sleep)

    result = await service.sync_ratings_for_securities()

    assert len(calls) == 4, "an unknown ticker must not abandon the pass"
    assert result["rate_limited"] is False
    assert not result.get("warnings")


@pytest.mark.asyncio
async def test_the_watchlist_abandons_the_pass(db, monkeypatch):
    for i in range(5):
        db.add(WatchlistItem(yahoo_ticker=f"TCK{i}"))
    await db.commit()

    service = WatchlistService(db)
    calls = []

    async def _sync_item(ticker, force=False):
        calls.append(ticker)
        # sync_item catches its own fetch failure and returns the message rather than
        # raising, which is exactly why the shared predicate accepts a plain string.
        return {"error": "429 Client Error: Too Many Requests"}

    monkeypatch.setattr(service, "sync_item", _sync_item)

    result = await service.sync_all(force=True)

    assert len(calls) == 1, f"kept fetching after a 429: {calls}"
    assert result["rate_limited"] is True
    assert result["warnings"]


# ── An ordinary failure must NOT stop the pass ──────────────────────────────

@pytest.mark.asyncio
async def test_an_ordinary_error_still_only_skips_one_security(db, monkeypatch):
    """
    The mirror-image bug. One security Yahoo has never heard of must cost that security
    and no more — breaking on every error would let a single delisted ticker suppress
    the whole portfolio's fundamentals indefinitely.
    """
    await _seed_securities(db, n=4)
    service = FundamentalsService(db)

    calls = []

    async def _raise(ticker):
        calls.append(ticker)
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(service, "_fetch_yahoo_data", _raise)
    monkeypatch.setattr(service, "_get_yahoo_ticker", lambda sec, ms=None: _ticker(sec))

    result = await service.sync_fundamentals_data(force_refresh=True)

    assert len(calls) == 4, "an unknown ticker must not abandon the pass"
    assert result["rate_limited"] is False
    assert "warnings" not in result


async def _ticker(security):
    return security.symbol


# ── The family rule ─────────────────────────────────────────────────────────

def test_every_yahoo_looping_service_consults_the_shared_predicate():
    """
    The check that survives a fifth service being added.

    Any service module that both imports yfinance and loops is in this family, and must
    reach the one definition of "rate limited" rather than rolling its own keyword list
    — which is how `market_data_service`'s copy sat alone for months.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in sorted(list((root / "services").glob("*.py"))):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        # An AST import, not the word "yfinance" — three pure helper modules
        # (peg_ratio, safe_numbers, ttm_growth) only mention it in prose, and matching
        # those would make this assertion noise that gets silenced rather than read.
        imports_yahoo = any(
            (isinstance(n, ast.Import) and any(a.name.split(".")[0] == "yfinance" for a in n.names))
            or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "yfinance")
            for n in ast.walk(tree)
        )
        if not imports_yahoo:
            continue
        if "is_rate_limit" not in source:
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} call Yahoo but never consult yahoo_rate_limit.is_rate_limit. "
        "A loop that keeps asking after a 429 is what turns a short block into a long "
        "one — see rule 1 in CLAUDE.md."
    )


def test_nobody_has_reintroduced_a_private_keyword_list():
    """
    Extract, don't sync: a second copy of the marker list is the failure this module was
    created to end, and it would drift silently because both copies keep working.
    """
    services = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    offenders = []
    for path in sorted(services.glob("*.py")):
        if path.name == "yahoo_rate_limit.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            # A literal list/tuple of rate-limit markers anywhere but the shared module
            if isinstance(node, (ast.List, ast.Tuple)):
                values = [e.value for e in node.elts
                          if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if any("too many requests" in v.lower() for v in values):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"a private rate-limit marker list reappeared at {offenders}; "
        "import is_rate_limit instead"
    )


def test_the_shared_predicate_is_the_one_market_data_uses():
    """`MarketDataService` is the reference implementation and must not drift back."""
    from app.services import market_data_service

    source = inspect.getsource(market_data_service.MarketDataService._try_fetch_yahoo)
    assert "is_rate_limit" in source


@pytest.mark.asyncio
async def test_two_failures_in_one_pass_do_not_crash_it(db, monkeypatch):
    """
    A pre-existing crash this file's fixtures exposed, separate from the rate limit.

    The handler calls `db.rollback()`, which expires **every** object in the session —
    so the next iteration's `security.symbol` became a lazy refresh, and in async
    SQLAlchemy an expired-attribute load outside an await raises `MissingGreenlet`. That
    read sat *outside* the try, so the second failure of a pass did not cost one
    security: it propagated out of the sync entirely.

    One security Yahoo has no data for is completely ordinary, so this was reachable on
    any pass with two such securities. Each iteration now reloads through an awaited
    `db.get`.
    """
    await _seed_securities(db, n=5)
    service = FundamentalsService(db)

    seen = []

    async def _raise(ticker):
        seen.append(ticker)
        raise ValueError("404 Not Found")

    monkeypatch.setattr(service, "_fetch_yahoo_data", _raise)
    monkeypatch.setattr(service, "_get_yahoo_ticker", lambda sec, ms=None: _ticker(sec))

    result = await service.sync_fundamentals_data(force_refresh=True)

    assert len(seen) == 5, "the pass stopped early on an ordinary error"
    assert result["errors"] == 5


def test_no_yahoo_call_is_wrapped_in_a_handler_that_swallows_a_rate_limit():
    """
    The check the module's other family test could not make, and the reason it could
    not.

    `test_every_yahoo_looping_service_consults_the_shared_predicate` asks whether the
    string `is_rate_limit` appears in a module — which is true of **dead code**.
    `analyst_rating_service` satisfied it for months while its breaker was unreachable:
    `fetch_rating_for_security` wrapped the whole yfinance call in
    `except Exception: logger.error(...); return None`, so the 429 became a `None` the
    caller read as "this security has no ratings", and the loop's `is_rate_limit(e)` one
    level up — which looked entirely correct — never saw anything to test.

    So this asks a structural question: when a `try` block *itself* contains the Yahoo
    call, a broad handler on it must mention `is_rate_limit`. Re-raising, returning a
    message the caller inspects (the watchlist does this), or latching are all fine.
    Discarding silently is not, because the caller then cannot tell "no data for this
    one" from "stop asking".

    Deliberately narrow on three axes: only handlers catching `Exception`/
    `BaseException` (a `except KeyError` around a field lookup is not this family's
    problem); only when the guarded block reaches yfinance directly (an earlier draft
    flagged 22 fine-grained `.info` extraction handlers and would have been silenced
    rather than read); and only when the handler *discards* what it caught.

    That third axis is the rule rather than an exemption. `allocation_service` catches
    broadly and returns `{'success': False, 'error': str(e)}`, and its caller runs
    `is_rate_limit(result.get('error'))` — the message reaches the predicate, just by
    value instead of by exception. `watchlist_service` does the same. What must not
    happen is the text going nowhere, because then no caller can tell the two cases
    apart no matter how carefully it is written.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders = []

    def reaches_yahoo(nodes) -> bool:
        for node in nodes:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                    if sub.value.id in {"yf", "yfinance"}:
                        return True
        return False

    def is_broad(handler: ast.ExceptHandler) -> bool:
        t = handler.type
        if t is None:
            return True
        names = [t] if isinstance(t, ast.Name) else (list(t.elts) if isinstance(t, ast.Tuple) else [])
        return any(isinstance(n, ast.Name) and n.id in {"Exception", "BaseException"} for n in names)

    def _discards_the_exception(handler: ast.ExceptHandler) -> bool:
        """
        True when nothing downstream can learn what was caught.

        Three ways to be fine: consult `is_rate_limit` here; re-raise (bare or the bound
        name); or hand the caller the exception text so it can consult the predicate
        itself, which is what allocation and the watchlist do.
        """
        for node in ast.walk(handler):
            if isinstance(node, ast.Name) and node.id == "is_rate_limit":
                return False
            if isinstance(node, ast.Raise):
                return False
        if handler.name:
            for node in ast.walk(handler):
                if isinstance(node, ast.Return) and node.value is not None:
                    for sub in ast.walk(node.value):
                        if isinstance(sub, ast.Name) and sub.id == handler.name:
                            return False
        return True

    for path in sorted((root / "services").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try) or not reaches_yahoo(node.body):
                continue
            for handler in node.handlers:
                if not is_broad(handler):
                    continue
                if not _discards_the_exception(handler):
                    continue
                offenders.append(f"{path.name}:{handler.lineno}")

    assert not offenders, (
        "these handlers wrap a yfinance call, catch everything, and never ask whether "
        f"it was a rate limit: {sorted(set(offenders))}. A swallowed 429 makes the "
        "caller's breaker unreachable — exactly how analyst_rating_service went on "
        "asking after a limit while appearing to be covered."
    )
