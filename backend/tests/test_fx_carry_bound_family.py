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
alone, whichever path asks first. The structural half fails a second definition of the
bound. Offline: both provider fetches are stubbed to no-ops.
"""
import ast
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

import app.models  # noqa: F401
from app.models.exchange_rate import ExchangeRate
from app.services.currency_service import CurrencyService
from app.services.fx_policy import CARRIED_SUFFIX, FX_MAX_CARRY_DAYS
from app.services.portfolio_service import PortfolioService

APP_DIR = Path(__file__).resolve().parents[1] / "app"
OWNER = "services/fx_policy.py"

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
