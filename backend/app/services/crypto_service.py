"""
The crypto book: synced from CoinStats, read back in the base currency (docs/crypto.md).

**Separate from the stock book by construction.** This module writes only the three
`crypto_*` tables and reads only those plus the shared FX rows, settings and sync history;
no stock reader reads a `crypto_*` table. `tests/test_crypto_isolation.py` pins both.

## Sync — two refuse-whole units, all HTTP before any write

1. **Snapshot** — `/portfolio/value` + every page of `/portfolio/coins`. Stored in one
   short transaction, or not at all: a failed page, or a coin list that is empty while
   CoinStats reports a positive total (the wipe guard), keeps the previous snapshot.
2. **History** — `/portfolio/chart` + `/portfolio/pl/history`, at most two *attempts* per
   Berlin day. Replaces `crypto_daily` wholesale, or is refused when it is empty or much
   shorter than what is stored. A history or FX failure is a **warning on a successful
   run**; it never costs the snapshot the run already paid for.

No write transaction is ever open during an HTTP call — the job shares minute :00 with
the stock jobs, and SQLite has one writer. Every run leaves a `sync_runs` row carrying
type, status, reason and a message only: counts, credits and warnings live on
`crypto_snapshots`, because `/api/scheduler/history` is public.

## Read — database only

A GET must not reach the network or take SQLite's write lock, and
`CurrencyService.get_exchange_rate` does both on a cache miss (which, for a daily series,
is every weekend). So amounts are projected through the same `NativeToBase` every other
reader uses, over `fx_preload.PreloadedRates` — one query up front, then pure lookups.
The sync keeps those rates warm for the crypto window.

Values convert at their own date. Cost, average buy and P&L are CoinStats' **USD**
figures converted at the snapshot's rate, which leaves out every FX move since purchase —
so whenever the base is not USD the response carries `fx_caveat`, and the view prints it
beside those figures. An amount with no rate is `None` and counted, never zero. Spam and
unpriced coins are excluded **and counted**. Weights are shares of CoinStats' total and
are never renormalised; the gap is served as `unitemised_value`.
"""
import asyncio
import logging
import math
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.models.crypto import (
    SPAM,
    UNPRICED,
    VALUED,
    CryptoDailyPoint,
    CryptoHolding,
    CryptoSnapshot,
)
from app.models.exchange_rate import ExchangeRate
from app.repositories.app_settings_repository import (
    SUPPORTED_BASE_CURRENCIES,
    AppSettingsRepository,
)
from app.repositories.sync_run_repository import SyncRunRepository, utc_iso
from app.services.base_fx import load_base_fx
from app.services.coinstats_client import (
    CREDIT_COST,
    CoinStatsAuthError,
    CoinStatsClient,
    CoinStatsError,
    CoinStatsNotSynced,
    CoinStatsQuotaError,
    is_configured,
)
from app.services.currency_service import CurrencyService
from app.services.fx_preload import FX_LOOKBACK_DAYS, PreloadedRates, preload_eur_rates
from app.services.native_amounts import NativeToBase

logger = logging.getLogger(__name__)

SYNC_TYPE = "crypto_sync"

# The crypto sync's own gates. Never `SYNC_PIPELINE`: entering that one would put the
# stock Sync buttons on cooldown, and a crypto run has no upstream in common with them.
# The manual button has a second, outer gate carrying the cooldown, so a *scheduled* run
# never starts the button's cooldown (mirrors "manual-trigger" in routers/scheduler.py).
CRYPTO_GATE = "crypto-sync"
CRYPTO_MANUAL_GATE = "crypto-sync-manual"
MANUAL_COOLDOWN_SECONDS = 600

# A snapshot run costs /portfolio/value plus one page of coins; the history pull costs the
# chart plus the P&L history. Derived from the client's documented costs, never restated.
SNAPSHOT_RUN_COST = CREDIT_COST["value"] + CREDIT_COST["coins"]
HISTORY_PULL_COST = CREDIT_COST["chart"] + CREDIT_COST["pl_history"]
# Below this balance the daily history pull is skipped, so the month's last credits go to
# the snapshot — the figures people open the view for.
HISTORY_CREDIT_FLOOR = 500
# `warnings[]` says so when the month's balance falls under this share of the plan.
CREDITS_LOW_FRACTION = 0.2
# Attempts at the history pull per Berlin day: one, plus one retry for a transient failure.
HISTORY_ATTEMPTS_PER_DAY = 2
# A replacement history shorter than this share of the stored one is refused.
HISTORY_SHRINK_REFUSE_FRACTION = 0.5
# The FX warm-up re-fetches the whole crypto window only when the cache does not already
# reach within this many days of its start: a window starting on a weekend or over Easter
# has no rate on its first day, and without the slack every run would re-ask for it.
FX_WARM_SLACK_DAYS = 7
# A snapshot older than this is flagged: eight slots a day leave at most eight hours
# between runs, so twelve means at least one scheduled run failed.
STALE_SNAPSHOT_HOURS = 12

# A total-minus-holdings gap within this share of the total is rounding, not a position:
# CoinStats' `totalValue` and its per-coin figures disagree in the last digits (measured
# 2026-09-28: a few ten-thousandths of a percent), and serving that as "Not itemised"
# would print a line of cents on every sync. Reported as 0 inside it, as itself outside.
UNITEMISED_ROUNDING_PCT = 0.05

BERLIN = ZoneInfo("Europe/Berlin")

# `CryptoSnapshot.history_status`
HISTORY_REFRESHED = "refreshed"
HISTORY_FAILED = "failed"
HISTORY_REFUSED = "refused"
HISTORY_SKIPPED_CREDITS = "skipped_credits"
_HISTORY_ATTEMPTS = (HISTORY_REFRESHED, HISTORY_FAILED, HISTORY_REFUSED)

FX_CAVEAT = (
    "Cost and P&L are CoinStats' USD figures converted at the {day} rate; FX moves "
    "since purchase are not in them."
)


# ─────────────────────────────────────────────────────────────── parsing (pure)


def _num(value: Any) -> Optional[float]:
    """A finite float, or None. Booleans are not numbers; numeric strings are."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _usd(block: Any) -> Optional[float]:
    """The USD figure of a CoinStats money block (`{USD, BTC, ETH}`), or a bare number."""
    if isinstance(block, dict):
        return _num(block.get("USD"))
    return _num(block)


def _usd_period(block: Any, period: str) -> Optional[float]:
    """`profit`, `averageBuy` and `profitPercent` are keyed by period (`allTime`,
    `hour24`, `unrealized`, …), each holding a money block."""
    if isinstance(block, dict):
        return _usd(block.get(period))
    return None


def _int(value: Any) -> Optional[int]:
    number = _num(value)
    return int(number) if number is not None else None


def _sum_known(values: Iterable[Optional[float]]) -> Optional[float]:
    """A sum in which one unknown makes the total unknown — summing only the known parts
    would understate it while looking complete."""
    total = 0.0
    for value in values:
        if value is None:
            return None
        total += value
    return total


@dataclass
class ParsedHolding:
    coin_id: str
    symbol: Optional[str]
    name: Optional[str]
    rank: Optional[int]
    is_fiat: bool
    status: str
    count: float
    price_usd: Optional[float]
    value_usd: Optional[float]
    total_cost_usd: Optional[float]
    avg_buy_usd: Optional[float]
    unrealized_pl_usd: Optional[float]
    unrealized_pl_pct: Optional[float]
    realized_pl_usd: Optional[float]
    pl_24h_usd: Optional[float]
    change_24h_pct: Optional[float]


# Why an item did not become a holding.
_CLOSED = "closed"        # quantity exactly 0: a coin no longer held
_MALFORMED = "malformed"  # no identifier, no quantity, or a negative one


def _skip_reason(item: Any) -> Optional[str]:
    """
    None when the item is a holding. **A closed position is not an error**: CoinStats lists
    every coin ever held, at a quantity of 0 once sold, so their realized P&L can be shown —
    and `/portfolio/value` already totals it. Measured 2026-09-28: 19 of 31 listed items were
    closed. Counting those as malformed would put a warning on every sync, which teaches the
    reader to skip the banner; only a genuinely unusable item is worth one.
    """
    if not isinstance(item, dict):
        return _MALFORMED
    coin = item.get("coin") if isinstance(item.get("coin"), dict) else {}
    count = _num(item.get("count"))
    if not (coin.get("identifier") or coin.get("id")) or count is None or count < 0:
        return _MALFORMED
    if count == 0:
        return _CLOSED
    return None


def _parse_coin(item: Dict[str, Any]) -> ParsedHolding:
    """One held coin; `_skip_reason(item)` is None."""
    coin = item.get("coin") if isinstance(item.get("coin"), dict) else {}
    coin_id = coin.get("identifier") or coin.get("id")
    count = _num(item.get("count"))

    price = _usd(item.get("price"))
    if coin.get("isFake"):
        status = SPAM
    elif price is None or price <= 0:
        status = UNPRICED
    else:
        status = VALUED
    profit = item.get("profit")

    return ParsedHolding(
        coin_id=str(coin_id)[:128],
        symbol=(str(coin["symbol"])[:32] if coin.get("symbol") else None),
        name=(str(coin["name"])[:128] if coin.get("name") else None),
        rank=_int(coin.get("rank")),
        is_fiat=bool(coin.get("isFiat")),
        status=status,
        count=count,
        price_usd=price if status == VALUED else None,
        value_usd=count * price if status == VALUED else None,
        total_cost_usd=_usd(item.get("totalCost")),
        avg_buy_usd=_usd_period(item.get("averageBuy"), "allTime"),
        unrealized_pl_usd=_usd_period(profit, "unrealized"),
        unrealized_pl_pct=_usd_period(item.get("profitPercent"), "unrealized"),
        realized_pl_usd=_usd_period(profit, "realized"),
        pl_24h_usd=_usd_period(profit, "hour24"),
        change_24h_pct=_num(coin.get("priceChange24h")),
    )


def _merge(first: ParsedHolding, other: ParsedHolding) -> ParsedHolding:
    """
    One coin listed twice — the same token held in two connected accounts, if CoinStats
    ever itemises it that way. Additive figures are summed (an unknown part makes the sum
    unknown); the per-unit ones are re-derived from the sums rather than averaged.
    """
    count = first.count + other.count
    value = _sum_known([first.value_usd, other.value_usd])
    cost = _sum_known([first.total_cost_usd, other.total_cost_usd])
    unrealized = _sum_known([first.unrealized_pl_usd, other.unrealized_pl_usd])
    return replace(
        first,
        count=count,
        value_usd=value,
        total_cost_usd=cost,
        avg_buy_usd=(cost / count) if cost is not None and count > 0 else None,
        unrealized_pl_usd=unrealized,
        unrealized_pl_pct=(
            unrealized / cost * 100 if unrealized is not None and cost else None
        ),
        realized_pl_usd=_sum_known([first.realized_pl_usd, other.realized_pl_usd]),
        pl_24h_usd=_sum_known([first.pl_24h_usd, other.pl_24h_usd]),
    )


def parse_coins(items: Iterable[Any]) -> Tuple[List[ParsedHolding], int, int]:
    """The holdings, one per coin; how many items were unusable; how many were closed
    positions (see `_skip_reason`)."""
    merged: Dict[str, ParsedHolding] = {}
    malformed = closed = 0
    for item in items:
        reason = _skip_reason(item)
        if reason == _MALFORMED:
            malformed += 1
            continue
        if reason == _CLOSED:
            closed += 1
            continue
        parsed = _parse_coin(item)
        existing = merged.get(parsed.coin_id)
        merged[parsed.coin_id] = parsed if existing is None else _merge(existing, parsed)
    return list(merged.values()), malformed, closed


def parse_value(body: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """`/portfolio/value` → the snapshot's USD totals, as CoinStats computed them."""
    return {
        "total_value_usd": _num(body.get("totalValue")),
        "defi_value_usd": _num(body.get("defiValue")),
        "total_cost_usd": _num(body.get("totalCost")),
        "unrealized_pl_usd": _num(body.get("unrealizedProfitLoss")),
        "unrealized_pl_pct": _num(body.get("unrealizedProfitLossPercent")),
        "realized_pl_usd": _num(body.get("realizedProfitLoss")),
        "realized_pl_pct": _num(body.get("realizedProfitLossPercent")),
        "all_time_pl_usd": _num(body.get("allTimeProfitLoss")),
        "all_time_pl_pct": _num(body.get("allTimeProfitLossPercent")),
    }


def _to_utc_date(stamp: Any) -> Optional[date]:
    """CoinStats dates arrive as epoch seconds, epoch milliseconds or ISO strings. Always
    read in UTC: `fromtimestamp` without a zone would read them in the server's."""
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        seconds = stamp / 1000 if stamp > 10_000_000_000 else stamp
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.date()
    return None


def parse_history(
    chart_rows: Iterable[Any], pl_body: Dict[str, Any]
) -> Dict[date, Tuple[Optional[float], Optional[float]]]:
    """
    `{day: (value_usd, pnl_usd)}` from the chart rows (`[timestamp, usd, btc, eth]`) and
    the P&L points (`{date, profitLoss}`). Several points on one UTC day keep the last,
    so an intraday tail cannot outvote the day's close.
    """
    values: Dict[date, Tuple[Any, float]] = {}
    for row in chart_rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        day, value = _to_utc_date(row[0]), _num(row[1])
        if day is None or value is None:
            continue
        stamp = _num(row[0]) or 0.0
        if day not in values or stamp >= values[day][0]:
            values[day] = (stamp, value)

    pnls: Dict[date, Tuple[str, float]] = {}
    for point in pl_body.get("result") or []:
        if not isinstance(point, dict):
            continue
        day, pnl = _to_utc_date(point.get("date")), _num(point.get("profitLoss"))
        if day is None or pnl is None:
            continue
        stamp = str(point.get("date"))
        if day not in pnls or stamp >= pnls[day][0]:
            pnls[day] = (stamp, pnl)

    days = sorted(set(values) | set(pnls))
    return {
        d: (values[d][1] if d in values else None, pnls[d][1] if d in pnls else None)
        for d in days
    }


# ──────────────────────────────────────────────────────────────────────── sync


async def _retry_once_if_locked(operation: Callable[[], Awaitable[Any]]) -> Any:
    """Run a write transaction, retrying once on SQLite's "database is locked". The job
    shares minute :00 with the stock jobs, which can hold the one writer's lock past the
    30 s busy timeout while a long batch commits."""
    try:
        return await operation()
    except OperationalError as e:
        if "database is locked" not in str(e):
            raise
        logger.warning("crypto sync: database locked, retrying the write once")
        await asyncio.sleep(2)
        return await operation()


def _berlin_day_start_utc(now_utc: datetime) -> datetime:
    """Midnight today in Berlin, as naive UTC — the unit the history pull is bounded in."""
    berlin_day = now_utc.replace(tzinfo=timezone.utc).astimezone(BERLIN).date()
    start = datetime.combine(berlin_day, time.min, tzinfo=BERLIN)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


class CryptoSyncService:
    """
    One crypto sync. The session and client factories are injectable so tests run the
    whole path against an in-memory database and `httpx.MockTransport`.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any] = AsyncSessionLocal,
        client_factory: Callable[[], CoinStatsClient] = CoinStatsClient,
        currency_service_factory: Callable[[AsyncSession], Any] = CurrencyService,
    ):
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._currency_service_factory = currency_service_factory

    async def sync(self) -> Optional[Dict[str, Any]]:
        """
        Run once. Returns the run's result, or None when CoinStats is not configured — in
        which case nothing is asked and nothing is recorded: the crypto view and
        `/api/crypto/status` already say `configured: false`, and eight skipped rows a
        day would only be noise in the history.
        """
        if not is_configured():
            return None

        started_at = utcnow()
        warnings: List[str] = []
        history: Optional[Dict[date, Tuple[Optional[float], Optional[float]]]] = None
        history_status: Optional[str] = None

        async with self._client_factory() as client:
            try:
                credits = await client.credits()
            except (CoinStatsAuthError, CoinStatsQuotaError) as e:
                # The paid calls would fail the same way; asking them would only fail again.
                return await self._finish(started_at, "error", e.reason, str(e))
            except CoinStatsError as e:
                # The balance is a guard, not the data: a transient failure of the free
                # check must not cost the slot its snapshot. The run goes ahead blind to
                # the balance, and says so.
                credits = {}
                warnings.append(
                    f"CoinStats' credit balance could not be read ({e.reason}); the run "
                    f"went ahead without it."
                )

            remaining = _int(credits.get("remainingCredits"))
            total = _int(credits.get("totalCredits"))
            plan = credits.get("subscription")
            if remaining is not None and remaining < SNAPSHOT_RUN_COST:
                return await self._finish(
                    started_at, "skipped", "credits_exhausted",
                    f"CoinStats has {remaining} credits left this period; a sync needs "
                    f"{SNAPSHOT_RUN_COST}. The next period's allowance resumes it.",
                )
            if remaining is not None and total and remaining < total * CREDITS_LOW_FRACTION:
                warnings.append(
                    f"CoinStats credits are low: {remaining} of {total} left this period."
                )

            # ── snapshot unit ────────────────────────────────────────────────
            try:
                value_body = await client.portfolio_value()
                coin_items = await client.portfolio_coins()
            except CoinStatsNotSynced as e:
                return await self._finish(started_at, "skipped", e.reason, str(e))
            except CoinStatsError as e:
                return await self._finish(started_at, "error", e.reason, str(e))

            summary = parse_value(value_body)
            holdings, malformed, _closed = parse_coins(coin_items)
            valued = [h for h in holdings if h.status == VALUED]
            if not valued and (summary["total_value_usd"] or 0) > 0:
                # The wipe guard: an empty list beside a positive total is a broken
                # answer, and storing it would draw every coin as sold.
                return await self._finish(
                    started_at, "error", "empty_holdings",
                    "CoinStats reported a portfolio value but no priced holdings; the "
                    "previous snapshot is kept.",
                )
            coin_pages = client.coin_pages
            if coin_pages > 1:
                warnings.append(
                    f"CoinStats listed holdings over {coin_pages} pages; each sync costs "
                    f"{CREDIT_COST['coins']} credits per page. Hiding spam tokens in "
                    f"CoinStats reduces it."
                )
            if malformed:
                warnings.append(f"{malformed} CoinStats holding(s) had no identifier or no "
                                f"usable quantity and were skipped.")

            # ── history unit (fetched now; written after the snapshot) ─────────
            if await self._history_due(started_at):
                balance = (remaining - client.credits_spent) if remaining is not None else None
                if balance is not None and balance < HISTORY_CREDIT_FLOOR + HISTORY_PULL_COST:
                    history_status = HISTORY_SKIPPED_CREDITS
                    warnings.append(
                        "CoinStats history not refreshed today: credits are below the "
                        "floor kept for the snapshot."
                    )
                else:
                    try:
                        chart = await client.portfolio_chart()
                        pl_body = await client.portfolio_pl_history()
                        history = parse_history(chart, pl_body)
                    except CoinStatsError as e:
                        history_status = HISTORY_FAILED
                        warnings.append(f"CoinStats history not refreshed ({e.reason}): {e}")
            credits_spent = client.credits_spent

        # ── writes: no HTTP in flight from here on, except the FX warm-up, which
        #    commits per currency pair and never inside a crypto transaction ──
        if history is not None:
            try:
                history_status, refusal = await _retry_once_if_locked(
                    lambda: self._write_history(history)
                )
                if refusal:
                    warnings.append(refusal)
            except Exception as e:  # a history failure never costs the snapshot
                logger.exception("crypto sync: storing the history failed")
                history_status = HISTORY_FAILED
                warnings.append(f"CoinStats history not stored: {type(e).__name__}")

        if history_status is not None and history_status != HISTORY_SKIPPED_CREDITS:
            warnings.extend(await self._warm_fx(started_at.date()))

        snapshot_fields = {
            **summary,
            "taken_at": started_at,
            "pl_24h_usd": _sum_known(h.pl_24h_usd for h in valued),
            "valued_count": len(valued),
            "spam_count": sum(1 for h in holdings if h.status == SPAM),
            "unpriced_count": sum(1 for h in holdings if h.status == UNPRICED),
            "coin_pages": coin_pages,
            "credits_remaining": remaining,
            "credits_total": total,
            "credits_plan": str(plan)[:32] if plan else None,
            "credits_spent": credits_spent,
            "warnings": warnings or None,
            "history_status": history_status,
        }
        try:
            await _retry_once_if_locked(lambda: self._write_snapshot(snapshot_fields, holdings))
        except Exception as e:
            logger.exception("crypto sync: storing the snapshot failed")
            return await self._finish(
                started_at, "error", "store_failed",
                f"The CoinStats snapshot could not be stored: {type(e).__name__}",
            )

        message = "Crypto snapshot stored"
        if history_status == HISTORY_REFRESHED:
            message += "; history refreshed"
        return await self._finish(started_at, "success", None, message, warnings)

    async def _history_due(self, now_utc: datetime) -> bool:
        """No successful pull yet today (Berlin) and fewer than the allowed attempts."""
        day_start = _berlin_day_start_utc(now_utc)
        async with self._session_factory() as db:
            rows = (await db.execute(
                select(CryptoSnapshot.history_status).where(
                    CryptoSnapshot.taken_at >= day_start,
                    CryptoSnapshot.history_status.in_(_HISTORY_ATTEMPTS),
                )
            )).scalars().all()
        if HISTORY_REFRESHED in rows:
            return False
        return len(rows) < HISTORY_ATTEMPTS_PER_DAY

    async def _write_history(
        self, points: Dict[date, Tuple[Optional[float], Optional[float]]]
    ) -> Tuple[str, Optional[str]]:
        """Replace the stored history, or refuse and say why."""
        async with self._session_factory() as db:
            stored = (await db.execute(
                select(func.count()).select_from(CryptoDailyPoint)
            )).scalar_one()
            if not points:
                return HISTORY_REFUSED, (
                    "CoinStats returned an empty history; the stored one is kept."
                )
            if stored and len(points) < stored * HISTORY_SHRINK_REFUSE_FRACTION:
                return HISTORY_REFUSED, (
                    f"CoinStats returned {len(points)} days of history against "
                    f"{stored} stored; refused rather than shrinking it."
                )
            fetched_at = utcnow()
            await db.execute(delete(CryptoDailyPoint))
            db.add_all(
                CryptoDailyPoint(date=day, value_usd=value, pnl_usd=pnl, fetched_at=fetched_at)
                for day, (value, pnl) in points.items()
            )
            await db.commit()
        return HISTORY_REFRESHED, None

    async def _write_snapshot(
        self, fields: Dict[str, Any], holdings: List[ParsedHolding]
    ) -> int:
        async with self._session_factory() as db:
            snapshot = CryptoSnapshot(**fields)
            db.add(snapshot)
            await db.flush()
            db.add_all(CryptoHolding(snapshot_id=snapshot.id, **asdict(h)) for h in holdings)
            await db.flush()
            # Only the newest snapshot keeps its holdings.
            await db.execute(
                delete(CryptoHolding).where(CryptoHolding.snapshot_id != snapshot.id)
            )
            await db.commit()
            return snapshot.id

    async def _warm_fx(self, today: date) -> List[str]:
        """
        Keep the rates the read path needs in the cache: USD->EUR (the crypto book's own
        currency — the stock side warms USD only because a security happens to be held in
        it) and EUR->each non-EUR base, over the crypto window. The whole window is
        fetched only while the cache does not reach its start; after that, a week keeps
        the recent end fresh. Each pair commits on its own session, so no write lock is
        held across the next pair's request. Never raises.
        """
        warnings: List[str] = []
        async with self._session_factory() as db:
            first_day = (await db.execute(select(func.min(CryptoDailyPoint.date)))).scalar()
            if first_day is None:
                first_day = (await db.execute(
                    select(func.min(CryptoSnapshot.taken_at))
                )).scalar()
                first_day = first_day.date() if first_day else today
        window_start = first_day - timedelta(days=FX_LOOKBACK_DAYS)

        pairs = [("USD", "EUR")] + [("EUR", b) for b in SUPPORTED_BASE_CURRENCIES if b != "EUR"]
        for from_currency, to_currency in pairs:
            try:
                async with self._session_factory() as db:
                    earliest = (await db.execute(
                        select(func.min(ExchangeRate.date)).where(
                            ExchangeRate.from_currency == from_currency,
                            ExchangeRate.to_currency == to_currency,
                        )
                    )).scalar()
                    reaches = (
                        earliest is not None
                        and earliest <= window_start + timedelta(days=FX_WARM_SLACK_DAYS)
                    )
                    days_back = 7 if reaches else max((today - window_start).days, 7)
                    summary = await self._currency_service_factory(db).warm_rates(
                        {from_currency}, target_date=today, days_back=days_back,
                        to_currency=to_currency,
                    )
                    await db.commit()
                if summary.get("frankfurter_failed"):
                    warnings.append(
                        f"FX rates {from_currency}->{to_currency} could not be refreshed; "
                        f"crypto figures may carry older rates."
                    )
            except Exception as e:
                logger.exception("crypto sync: FX warm-up failed")
                warnings.append(
                    f"FX warm-up {from_currency}->{to_currency} failed: {type(e).__name__}"
                )
        return warnings

    async def _finish(
        self,
        started_at: datetime,
        status: str,
        reason: Optional[str],
        message: str,
        warnings: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Record the run — type, status, reason and message only — and return it."""
        async with self._session_factory() as db:
            await SyncRunRepository(db).record(
                sync_type=SYNC_TYPE,
                status=status,
                message=message,
                details={"reason": reason} if reason else None,
                started_at=started_at,
            )
        log = logger.info if status == "success" else logger.warning
        log("crypto sync %s%s: %s", status, f" ({reason})" if reason else "", message)
        return {
            "type": SYNC_TYPE,
            "status": status,
            "reason": reason,
            "message": message,
            "warnings": warnings or [],
            "timestamp": utc_iso(utcnow()),
        }


# ──────────────────────────────────────────────────────────────────────── read


def _amount(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return round(number, 2) if math.isfinite(number) else None


def _per_unit(value: Optional[Decimal]) -> Optional[float]:
    """A price keeps its significant digits: a token worth 0.0000123 is not 0.00."""
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if number == 0:
        return 0.0
    digits = 8 - int(math.floor(math.log10(abs(number)))) - 1
    return round(number, max(digits, 2))


def _pct(value: Optional[float]) -> Optional[float]:
    return round(value, 2) if value is not None and math.isfinite(value) else None


class _Projector:
    """
    USD amounts on dates → the base currency, counting what could not be converted.

    `no_base_rates` is the one case `BaseFx` would paper over: with no EUR->base rate
    cached at all it hands back the EUR amount unchanged, which here would print euros
    under a CHF label. The stock readers accept that edge; the crypto view reports the
    figure as unconvertible instead.
    """

    def __init__(self, converter: NativeToBase, no_base_rates: bool = False):
        self._converter = converter
        self._no_base_rates = no_base_rates
        self.unavailable = 0

    async def __call__(self, usd: Optional[float], on_date: date) -> Optional[Decimal]:
        if usd is None:
            return None
        if self._no_base_rates:
            self.unavailable += 1
            return None
        converted = await self._converter.convert(Decimal(repr(usd)), "USD", on_date)
        if converted is None:
            self.unavailable += 1
        return converted


class CryptoService:
    """The crypto book as the `/api/crypto` routes serve it. Database only."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.currency_service = CurrencyService(db)

    async def _base_currency(self) -> str:
        return await AppSettingsRepository(self.db).get_base_currency()

    async def _latest_snapshot(self) -> Optional[CryptoSnapshot]:
        return (await self.db.execute(
            select(CryptoSnapshot)
            .order_by(CryptoSnapshot.taken_at.desc(), CryptoSnapshot.id.desc())
            .limit(1)
        )).scalar_one_or_none()

    async def _projector(self, base: str, start: date, end: date) -> _Projector:
        # BaseFx is loaded from a fortnight before the window so its carry-forward always
        # has a rate on or before the window's first day: the ECB publishes today's rate
        # mid-afternoon, and an empty window would send the loader's safety net to
        # Frankfurter from inside a GET.
        base_fx = await load_base_fx(
            self.db, self.currency_service, base, start - timedelta(days=FX_LOOKBACK_DAYS),
            backfill=False,
        )
        rates = await preload_eur_rates(self.db, {"USD"}, start, end)
        # EUR needs no EUR->base rate, and USD amounts in a USD base are never converted.
        no_base_rates = base not in ("EUR", "USD") and not base_fx.rate_cache
        return _Projector(NativeToBase(PreloadedRates(rates), base_fx), no_base_rates)

    @staticmethod
    def _fx_caveat(base: str, day: Optional[date]) -> Optional[str]:
        if base == "USD" or day is None:
            return None
        return FX_CAVEAT.format(day=day.isoformat())

    async def portfolio(self) -> Dict[str, Any]:
        """The newest snapshot: KPI totals and the holdings table, from ONE snapshot —
        holdings are read by its id, so a sync committing between this method's queries
        cannot pair one snapshot's total with another's table."""
        base = await self._base_currency()
        snapshot = await self._latest_snapshot()
        result: Dict[str, Any] = {
            "configured": is_configured(),
            "base_currency": base,
            "as_of": None,
            "total_value": None,
            "itemised_value": None,
            "unitemised_value": None,
            "defi_value": None,
            "total_cost": None,
            "unrealized_pl": None,
            "unrealized_pl_pct": None,
            "realized_pl": None,
            "realized_pl_pct": None,
            "all_time_pl": None,
            "all_time_pl_pct": None,
            "change_24h": None,
            "change_24h_pct": None,
            "fx_caveat": None,
            "fx_unavailable": 0,
            "valued_count": 0,
            "spam_count": 0,
            "unpriced_count": 0,
            "unpriced_symbols": [],
            "holdings": [],
            "color_order": [],
            "warnings": [],
        }
        if snapshot is None:
            return result

        rows = (await self.db.execute(
            select(CryptoHolding).where(CryptoHolding.snapshot_id == snapshot.id)
        )).scalars().all()
        valued = [r for r in rows if r.status == VALUED]
        unpriced = [r for r in rows if r.status == UNPRICED]

        day = snapshot.taken_at.date()
        project = await self._projector(base, day, day)

        total_usd = snapshot.total_value_usd
        itemised_usd = _sum_known(r.value_usd for r in valued) if valued else 0.0
        unitemised_usd = (
            total_usd - itemised_usd
            if total_usd is not None and itemised_usd is not None else None
        )
        if (
            unitemised_usd is not None and total_usd
            and abs(unitemised_usd) <= abs(total_usd) * UNITEMISED_ROUNDING_PCT / 100
        ):
            unitemised_usd = 0.0
        pl_24h = snapshot.pl_24h_usd
        before_24h = (itemised_usd - pl_24h) if itemised_usd is not None and pl_24h is not None else None

        warnings: List[str] = list(snapshot.warnings or [])
        if unitemised_usd is not None and unitemised_usd < -0.005 * max(abs(total_usd or 0), 1):
            warnings.append(
                "CoinStats' holdings add up to more than its portfolio total; the "
                "difference is shown, not hidden."
            )
        age_hours = (utcnow() - snapshot.taken_at).total_seconds() / 3600
        if age_hours > STALE_SNAPSHOT_HOURS:
            warnings.append(
                f"Crypto figures are {int(age_hours)} hours old — recent syncs did not "
                f"complete."
            )

        holdings = []
        for row in sorted(valued, key=lambda r: -(r.value_usd or 0)) + sorted(
            unpriced, key=lambda r: (r.symbol or r.coin_id)
        ):
            holdings.append({
                "coin_id": row.coin_id,
                "symbol": row.symbol,
                "name": row.name,
                "rank": row.rank,
                "is_fiat": row.is_fiat,
                "status": row.status,
                "quantity": row.count,
                "price": _per_unit(await project(row.price_usd, day)),
                "value": _amount(await project(row.value_usd, day)),
                "weight_pct": _pct(
                    row.value_usd / total_usd * 100
                    if row.value_usd is not None and total_usd else None
                ),
                "change_24h_pct": _pct(row.change_24h_pct),
                "avg_buy": _per_unit(await project(row.avg_buy_usd, day)),
                "total_cost": _amount(await project(row.total_cost_usd, day)),
                "unrealized_pl": _amount(await project(row.unrealized_pl_usd, day)),
                "unrealized_pl_pct": _pct(row.unrealized_pl_pct),
                "realized_pl": _amount(await project(row.realized_pl_usd, day)),
            })

        result.update({
            "as_of": utc_iso(snapshot.taken_at),
            "total_value": _amount(await project(total_usd, day)),
            "itemised_value": _amount(await project(itemised_usd, day)),
            "unitemised_value": _amount(await project(unitemised_usd, day)),
            "defi_value": _amount(await project(snapshot.defi_value_usd, day)),
            "total_cost": _amount(await project(snapshot.total_cost_usd, day)),
            "unrealized_pl": _amount(await project(snapshot.unrealized_pl_usd, day)),
            "unrealized_pl_pct": _pct(snapshot.unrealized_pl_pct),
            "realized_pl": _amount(await project(snapshot.realized_pl_usd, day)),
            "realized_pl_pct": _pct(snapshot.realized_pl_pct),
            "all_time_pl": _amount(await project(snapshot.all_time_pl_usd, day)),
            "all_time_pl_pct": _pct(snapshot.all_time_pl_pct),
            "change_24h": _amount(await project(pl_24h, day)),
            "change_24h_pct": _pct(
                pl_24h / before_24h * 100 if before_24h and before_24h > 0 else None
            ),
            "fx_caveat": self._fx_caveat(base, day),
            "valued_count": snapshot.valued_count,
            # Counted, never itemised: an airdropped scam token's "symbol" is often a
            # phishing URL, and this is the one place it would otherwise be rendered.
            "spam_count": snapshot.spam_count,
            "unpriced_count": snapshot.unpriced_count,
            "unpriced_symbols": sorted({r.symbol or r.coin_id for r in unpriced}),
            "holdings": holdings,
            # Colour identity: valued coins by CoinStats' market-cap rank, which belongs
            # to the coin and not to its size in this portfolio, so a sync that reorders
            # the holdings by value cannot repaint the chart.
            "color_order": [
                r.coin_id for r in sorted(
                    valued, key=lambda r: (r.rank is None, r.rank or 0, r.coin_id)
                )
            ],
        })
        result["fx_unavailable"] = project.unavailable
        if project.unavailable:
            warnings.append(
                f"{project.unavailable} crypto figure(s) could not be converted to {base} "
                f"— no exchange rate is cached for their date."
            )
        result["warnings"] = warnings
        return result

    async def history(self) -> Dict[str, Any]:
        """CoinStats' daily value and P&L history in the base currency, ending at the
        newest snapshot so the chart is current between daily pulls."""
        base = await self._base_currency()
        rows = (await self.db.execute(
            select(CryptoDailyPoint).order_by(CryptoDailyPoint.date)
        )).scalars().all()
        snapshot = await self._latest_snapshot()

        series: Dict[date, List[Optional[float]]] = {
            r.date: [r.value_usd, r.pnl_usd] for r in rows
        }
        if snapshot is not None and snapshot.total_value_usd is not None:
            as_of_day = snapshot.taken_at.date()
            if as_of_day in series:
                series[as_of_day][0] = snapshot.total_value_usd
            elif not series or as_of_day > max(series):
                series[as_of_day] = [snapshot.total_value_usd, None]

        result: Dict[str, Any] = {
            "configured": is_configured(),
            "base_currency": base,
            "points": [],
            "fetched_at": utc_iso(max((r.fetched_at for r in rows), default=None)),
            "fx_caveat": None,
            "fx_unavailable": 0,
            "warnings": [],
        }
        if not series:
            return result

        days = sorted(series)
        project = await self._projector(base, days[0], days[-1])
        points = []
        for day in days:
            value_usd, pnl_usd = series[day]
            points.append({
                "date": day.isoformat(),
                "value": _amount(await project(value_usd, day)),
                "pnl": _amount(await project(pnl_usd, day)),
            })
        result["points"] = points
        result["fx_caveat"] = self._fx_caveat(base, days[-1])
        result["fx_unavailable"] = project.unavailable
        if project.unavailable:
            result["warnings"].append(
                f"{project.unavailable} history point(s) could not be converted to {base} "
                f"— no exchange rate is cached for their date."
            )
        return result

    async def status(
        self, next_run: Optional[datetime], sync_in_progress: bool, retry_after_seconds: int
    ) -> Dict[str, Any]:
        """The last crypto run and the credit balance it saw. Never asks CoinStats."""
        run = await SyncRunRepository(self.db).get_latest(sync_type=SYNC_TYPE)
        snapshot = await self._latest_snapshot()
        return {
            "configured": is_configured(),
            "last_run": None if run is None else {
                "status": run.status,
                "reason": (run.details or {}).get("reason") if isinstance(run.details, dict) else None,
                "message": SyncRunRepository.to_dict(run)["message"],
                "finished_at": utc_iso(run.finished_at),
            },
            "last_snapshot_at": utc_iso(snapshot.taken_at) if snapshot else None,
            "next_run": next_run.isoformat() if next_run else None,
            "sync_in_progress": sync_in_progress,
            "manual_retry_after_seconds": retry_after_seconds,
            "credits_remaining": snapshot.credits_remaining if snapshot else None,
            "credits_total": snapshot.credits_total if snapshot else None,
            "credits_plan": snapshot.credits_plan if snapshot else None,
            "credits_spent_last_run": snapshot.credits_spent if snapshot else None,
        }
