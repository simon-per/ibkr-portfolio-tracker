"""
One FX carry bound, measured from the published rate, honoured by both FX paths (issue #20).

- `CurrencyService.get_exchange_rate` (activity, cash, tax, dividends, sync) carries a rate
  forward on a miss and **persists** the copy under the requested date.
- `fx_preload` (timeline, benchmark, crypto) fills forward over a preloaded window.

They carried 30 and 14 days, and the first writes rows the second reads: a date 20 days
past the newest rate was unconvertible on the value chart until a ledger read persisted
a copy, and convertible after, so `unpriced_holdings` depended on which screen was read
first. And because a copy counted as a rate, daily carries chained past any bound, and a
reader filling from a copy accepted a rate up to twice the bound old.

The behavioural half pins the verdict for a date as a function of the *published* rows
alone, whichever path asks first — and that the cache-only readers (dividend accruals and
forecast, market caps) reach the same verdict (issue #56: the dividend service read the
unbounded `_get_most_recent_rate` and converted with a rate of any age). The structural
half fails a second definition of the bound and any caller of the unbounded reader
outside `fx_policy` and `currency_service`. Offline: both provider fetches are stubbed.
"""
import ast
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

import app.models  # noqa: F401
from app.clock import utc_today
from app.models.exchange_rate import ExchangeRate
from app.models.security import Security
from app.services.currency_service import CurrencyService
from app.services.dividend_service import DividendService
from app.services.fx_policy import CARRIED_SUFFIX, FX_MAX_CARRY_DAYS
from app.services.market_cap import market_cap_to_base
from app.services.portfolio_service import PortfolioService

APP_DIR = Path(__file__).resolve().parents[1] / "app"
OWNER = "services/fx_policy.py"
# The only modules that may ask the unbounded reader; both apply the bound to its answer.
UNBOUNDED_READERS = {OWNER, "services/currency_service.py"}

LAST_RATE = date(2026, 3, 2)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    async def _nothing(*_a, **_kw):
        return None
    monkeypatch.setattr(CurrencyService, "_batch_fetch_rates", _nothing)
    monkeypatch.setattr(CurrencyService, "_fetch_fallback_rate", _nothing)


async def _publish(db, day: date, rate: str = "0.9", source: str = "frankfurter"):
    db.add(ExchangeRate(date=day, from_currency="USD", to_currency="EUR",
                        rate=Decimal(rate), source=source))
    await db.commit()


async def _timeline_rate(db, day: date) -> Decimal | None:
    svc = PortfolioService(db)
    cache = await svc._preload_exchange_rates(set(), day, day, price_currency_cache={1: "USD"})
    return svc._get_exchange_rate_with_fallback("USD", day, cache)


async def _ledger_rate(db, day: date) -> Decimal | None:
    try:
        return await CurrencyService(db).get_exchange_rate("USD", day, "EUR")
    except ValueError:
        return None


# --- behaviour ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_first", [False, True], ids=["timeline-first", "ledger-first"])
@pytest.mark.parametrize("age", [FX_MAX_CARRY_DAYS - 1, FX_MAX_CARRY_DAYS + 1])
async def test_one_date_has_one_fx_verdict_regardless_of_read_order(db, age, ledger_first):
    await _publish(db, LAST_RATE)
    target = LAST_RATE + timedelta(days=age)

    if ledger_first:
        reads = [await _ledger_rate(db, target), await _timeline_rate(db, target)]
    else:
        reads = [await _timeline_rate(db, target), await _ledger_rate(db, target),
                 await _timeline_rate(db, target)]

    expected = Decimal("0.9") if age <= FX_MAX_CARRY_DAYS else None
    assert reads == [expected] * len(reads)


@pytest.mark.asyncio
async def test_daily_carries_do_not_chain_past_the_bound(db):
    """Each day's copy used to become the next day's anchor, restarting the age."""
    await _publish(db, LAST_RATE)
    for age in range(1, FX_MAX_CARRY_DAYS + 4):
        rate = await _ledger_rate(db, LAST_RATE + timedelta(days=age))
        assert (rate is not None) == (age <= FX_MAX_CARRY_DAYS), age


@pytest.mark.asyncio
async def test_the_preload_does_not_fill_from_a_carried_copy(db):
    """A copy persisted at the edge of the bound must not let the timeline reach another
    bound beyond it while the ledger refuses the same date."""
    await _publish(db, LAST_RATE)
    edge = LAST_RATE + timedelta(days=FX_MAX_CARRY_DAYS)
    assert await _ledger_rate(db, edge) == Decimal("0.9")   # persists a copy at the edge

    later = edge + timedelta(days=3)
    assert await _timeline_rate(db, later) is None
    assert await _ledger_rate(db, later) is None


@pytest.mark.asyncio
async def test_a_stored_copy_is_re_derived_from_its_published_rate(db):
    """A copy stored before the rule (a 30-day carry, a chain) is refused and removed once
    its anchor is past the bound; inside it, it follows the anchor's current value."""
    await _publish(db, LAST_RATE, "0.9")
    stale = LAST_RATE + timedelta(days=FX_MAX_CARRY_DAYS + 6)
    fresh = LAST_RATE + timedelta(days=2)
    await _publish(db, stale, "0.9", source=f"frankfurter{CARRIED_SUFFIX}")
    await _publish(db, fresh, "0.95", source=f"er-api-latest{CARRIED_SUFFIX}")

    assert await _ledger_rate(db, stale) is None
    assert await _timeline_rate(db, stale) is None
    assert await _ledger_rate(db, fresh) == Decimal("0.9")
    assert await _timeline_rate(db, fresh) == Decimal("0.9")

    rows = {r.date: r for r in (await db.execute(select(ExchangeRate))).scalars()}
    assert stale not in rows
    assert (rows[fresh].rate, rows[fresh].source) == (Decimal("0.9"), f"frankfurter{CARRIED_SUFFIX}")


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [FX_MAX_CARRY_DAYS, FX_MAX_CARRY_DAYS + 1, 90])
async def test_the_cache_only_readers_reach_the_ledgers_verdict(db, age):
    """Issue #56's repro: one USD rate `age` days old. Where `get_exchange_rate` refuses,
    the forecast's rate is absent, the accrual is skipped and counted, and the market cap
    is None — never converted at the stale rate."""
    today = utc_today()
    await _publish(db, today - timedelta(days=age))
    fresh = age <= FX_MAX_CARRY_DAYS

    ledger = await _ledger_rate(db, today)
    assert ledger == (Decimal("0.9") if fresh else None)

    svc = DividendService(db)
    assert (await svc._latest_fx_to_eur({"USD"}, today)).get("USD") == ledger

    to_base = await market_cap_to_base(db)
    assert await to_base(1_000_000, "USD") == (900_000 if fresh else None)

    db.add(Security(id=1, isin="US0378331005", symbol="AAPL", description="Apple",
                    currency="USD", conid=100, asset_category="STK", exchange="NASDAQ"))
    await db.flush()
    result = await svc.sync_dividend_accruals([{
        "conid": 100, "ex_date": today + timedelta(days=5),
        "pay_date": today + timedelta(days=20), "currency": "USD",
        "quantity": Decimal("10"), "gross_amount": Decimal("10"), "tax": Decimal("1.5"),
        "net_amount": Decimal("8.5"),
    }], {"100": 1})
    assert result["dividend_accruals"] == (1 if fresh else 0)
    assert result["accruals_skipped"] == (0 if fresh else 1)
    assert bool(result["warnings"]) is not fresh


# --- structure ----------------------------------------------------------------------

# A name that reads as an FX staleness bound, or the carried-copy tag.
_BOUND_NAME = re.compile(r"^(FX_\w*(LOOKBACK|CARRY)\w*|CARRY_FORWARD\w*|\w*CARRIED_SUFFIX)$")
# A function that converts or loads FX rates.
_FX_FUNCTION = re.compile(r"(^|_)fx(_|$)|exchange_rate|eur_rate|carry", re.IGNORECASE)
_BOUND_PARAM = re.compile(r"lookback|max_age", re.IGNORECASE)


def _modules():
    for path in sorted(APP_DIR.rglob("*.py")):
        yield path.relative_to(APP_DIR).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def test_the_fx_bound_is_defined_once():
    """An alias of the owner's value (`CARRIED_SUFFIX = CARRIED_SUFFIX`) is not a copy; a
    literal is."""
    copies = []
    for rel, tree in _modules():
        if rel == OWNER:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and _BOUND_NAME.match(t.id):
                        copies.append(f"{rel}:{node.lineno} {t.id}")
    assert copies == [], f"FX bound defined outside {OWNER}: {copies}"


def test_no_module_asks_the_unbounded_reader():
    """`_get_most_recent_rate` answers with a rate of ANY age. A cache-only reader asks
    `fx_policy.newest_published_rate`, which applies the bound (issue #56)."""
    callers = []
    for rel, tree in _modules():
        if rel in UNBOUNDED_READERS:
            continue
        for node in ast.walk(tree):
            name = (node.attr if isinstance(node, ast.Attribute)
                    else node.id if isinstance(node, ast.Name)
                    else None)
            if isinstance(node, ast.ImportFrom):
                name = next((a.name for a in node.names if a.name == "_get_most_recent_rate"),
                            None)
            if name == "_get_most_recent_rate":
                callers.append(f"{rel}:{node.lineno}")
    assert callers == [], (
        f"The unbounded FX reader asked outside {sorted(UNBOUNDED_READERS)}: {callers}. "
        f"Use fx_policy.newest_published_rate."
    )


def test_no_fx_function_restates_the_bound_as_a_literal_default():
    literals = []
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _FX_FUNCTION.search(node.name):
                continue
            positional = node.args.posonlyargs + node.args.args
            pairs = list(zip(positional[len(positional) - len(node.args.defaults):],
                             node.args.defaults))
            pairs += [(a, d) for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults) if d]
            for arg, default in pairs:
                if (_BOUND_PARAM.search(arg.arg) and isinstance(default, ast.Constant)
                        and isinstance(default.value, int)):
                    literals.append(f"{rel}:{node.lineno} {node.name}({arg.arg}={default.value})")
    assert literals == [], (
        f"FX lookback defaults restated as literals: {literals}. "
        f"Default to fx_policy.FX_MAX_CARRY_DAYS."
    )
