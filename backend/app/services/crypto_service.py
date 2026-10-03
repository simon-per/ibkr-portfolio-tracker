"""
The crypto book: holdings from CoinStats, prices from CoinGecko, read back in the base
currency (docs/crypto.md).

**Separate from the stock book by construction.** This module writes only the `crypto_*`
tables and reads only those plus the shared FX rows, settings and sync history; no stock
reader reads a `crypto_*` table. `tests/test_crypto_isolation.py` pins both.

## Sync — all HTTP before any write

1. **Snapshot** — `/portfolio/value` + every page of `/portfolio/coins`. Stored in one
   short transaction, or not at all: a failed page, or a coin list that is empty while
   CoinStats reports a positive total (the wipe guard), keeps the previous snapshot. The
   same transaction replaces today's `crypto_daily_holdings` set.
2. **Prices** — CoinGecko: the id mapping when a coin is new, `/simple/price` for today,
   and the daily closes a held coin is still missing. Without `COINGECKO_API_KEY` this is
   skipped with a warning; a CoinGecko failure abandons the pass with a warning. Neither
   ever costs the snapshot.

The value and P&L history are **computed**, not fetched (`crypto_book.py`): CoinStats'
`/portfolio/chart` and `/portfolio/pl/history` are no longer asked, because CoinStats'
history counted transfers from an untracked exchange as profit.

No write transaction is ever open during an HTTP call — the job shares minute :00 with
the stock jobs, and SQLite has one writer. Every run leaves a `sync_runs` row carrying
type, status, reason and a message only: counts, credits and warnings live on
`crypto_snapshots`, because `/api/scheduler/history` is public.

## Read — database only

A GET must not reach the network or take SQLite's write lock, so amounts are projected
through the same `NativeToBase` every other reader uses, over `fx_preload.PreloadedRates`
— one query up front, then pure lookups. Every figure converts at its own date. An amount
with no price or no rate is `None` and counted, never zero.
"""
import asyncio
import logging
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.models.crypto import (
    HOLDINGS_FROM_SNAPSHOT,
    PRICE_DAILY,
    PRICE_SPOT,
    SPAM,
    UNPRICED,
    VALUED,
    CryptoCoinId,
    CryptoCoinPrice,
    CryptoDailyHolding,
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
from app.services.coingecko_client import (
    HISTORY_LIMIT_DAYS,
    CoinGeckoClient,
    CoinGeckoError,
)
from app.services.coingecko_client import is_configured as prices_configured
from app.services.coinstats_client import (
    CREDIT_COST,
    CoinStatsAuthError,
    CoinStatsClient,
    CoinStatsError,
    CoinStatsNotSynced,
    CoinStatsQuotaError,
    is_configured,
)
from app.services.crypto_book import (
    CRYPTO_HISTORY_START,
    PRICE_PEG,
    DayPoint,
    HoldingsTimeline,
    PriceBook,
    compute_series,
    daily_closes,
    needed_price_dates,
    peg_for,
)
from app.services.currency_service import CurrencyService
from app.services.fx_preload import FX_LOOKBACK_DAYS, PreloadedRates, preload_eur_rates
from app.services.native_amounts import NativeToBase

logger = logging.getLogger(__name__)

SYNC_TYPE = "crypto_sync"
# The one-off rebuild CLI's run type (app/cli/crypto_rebuild_holdings.py).
REBUILD_SYNC_TYPE = "crypto_rebuild"
# Every crypto run type: the public `/api/scheduler/history` leaves all of them out.
PUBLIC_EXCLUDED_SYNC_TYPES = (SYNC_TYPE, REBUILD_SYNC_TYPE)

# The crypto sync's own gates. Never `SYNC_PIPELINE`: entering that one would put the
# stock Sync buttons on cooldown, and a crypto run has no upstream in common with them.
# The manual button has a second, outer gate carrying the cooldown, so a *scheduled* run
# never starts the button's cooldown (mirrors "manual-trigger" in routers/scheduler.py).
CRYPTO_GATE = "crypto-sync"
CRYPTO_MANUAL_GATE = "crypto-sync-manual"
MANUAL_COOLDOWN_SECONDS = 600

# A snapshot run costs /portfolio/value plus one page of coins. Derived from the client's
# documented costs, never restated.
SNAPSHOT_RUN_COST = CREDIT_COST["value"] + CREDIT_COST["coins"]
# `warnings[]` says so when the month's balance falls under this share of the plan.
CREDITS_LOW_FRACTION = 0.2
# The FX warm-up re-fetches the whole crypto window only when the cache does not already
# reach within this many days of its start: a window starting on a weekend or over Easter
# has no rate on its first day, and without the slack every run would re-ask for it.
FX_WARM_SLACK_DAYS = 7
# A snapshot older than this is flagged: eight slots a day leave at most eight hours
# between runs, so twelve means at least one scheduled run failed.
STALE_SNAPSHOT_HOURS = 12

# CoinGecko re-asks, bounded (CLAUDE.md, *Bound every retry*):
# an unmatched coin is looked up in `/coins/list` again after this many days;
MAPPING_RECHECK_DAYS = 7
# a coin whose daily closes are still incomplete is asked again after this many hours —
# a day CoinGecko cannot fill (before a listing) would otherwise be asked at every slot.
CLOSES_RECHECK_HOURS = 6

BERLIN = ZoneInfo("Europe/Berlin")

COINGECKO_NOT_CONFIGURED = (
    "CoinGecko is not configured (COINGECKO_API_KEY): prices were not refreshed, and "
    "every day without a stored price reads as unknown."
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


def daily_set(holdings: Iterable[ParsedHolding]) -> Dict[str, Tuple[Optional[str], float]]:
    """The holdings that go into `crypto_daily_holdings`: coins CoinStats values that are
    not exchange cash. Spam, unpriced coins and fiat never enter the book's history."""
    return {
        h.coin_id: (h.symbol, h.count)
        for h in holdings
        if h.status == VALUED and not h.is_fiat and h.count > 0
    }


def _day_start_ts(day: date) -> int:
    return int(datetime.combine(day, time.min, tzinfo=timezone.utc).timestamp())


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
        logger.warning("crypto: database locked, retrying the write once")
        await asyncio.sleep(2)
        return await operation()


def _berlin_day_start_utc(now_utc: datetime) -> datetime:
    """Midnight today in Berlin, as naive UTC — the unit the FX warm-up is bounded in."""
    berlin_day = now_utc.replace(tzinfo=timezone.utc).astimezone(BERLIN).date()
    start = datetime.combine(berlin_day, time.min, tzinfo=BERLIN)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class PriceFetch:
    """What one CoinGecko pass brought back, held in memory until the writes."""

    mapping_rows: List[Dict[str, Any]] = field(default_factory=list)
    # (coingecko_id, day, price_usd, source)
    prices: List[Tuple[str, date, float, str]] = field(default_factory=list)
    # coingecko ids whose closes were asked this pass: stamped `closes_checked_at`.
    closes_checked: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)
    calls: int = 0


def resolve_coingecko_id(
    coin_id: str, symbol: Optional[str], coins: List[Dict[str, str]]
) -> Tuple[Optional[str], Optional[str]]:
    """`(coingecko_id, method)`: an exact id match, else a **unique** symbol match, else
    `(None, None)`. A symbol shared by several CoinGecko coins is not guessed."""
    ids = {c["id"] for c in coins}
    if coin_id in ids:
        return coin_id, "id"
    matches = symbol_candidates(symbol, coins)
    if len(matches) == 1:
        return matches[0], "symbol"
    return None, None


# How a CoinStats coin and a CoinGecko coin are proven to be the same one when the id does
# not say so (owner's BNB, 2026-10-03: CoinStats' id is not CoinGecko's `binancecoin`, and
# dozens of CoinGecko tokens use the symbol "BNB", so neither rule matched and one 0.64-BNB
# position left the whole book without a total). CoinStats prices every coin it lists, so
# the candidate trading at CoinStats' price is the coin — when exactly one does.
PRICE_MATCH_TOLERANCE = 0.10
# Every NEW mapping, by id or symbol, is also checked against CoinStats' price: a
# coincidental id or symbol would otherwise value the position as some other token.
# Wider than the match tolerance, because two quotes of one coin taken minutes apart from
# two aggregators drift, and a thin token drifts more.
PRICE_SANITY_TOLERANCE = 0.25
# The most candidates one ambiguous symbol may cost in the single price call.
MAX_SYMBOL_CANDIDATES = 50


def symbol_candidates(symbol: Optional[str], coins: List[Dict[str, str]]) -> List[str]:
    """Every CoinGecko id listed under ``symbol``, case-insensitively."""
    if not symbol:
        return []
    return [c["id"] for c in coins if c["symbol"].lower() == symbol.lower()]


def prices_agree(reference: float, other: float, tolerance: float) -> bool:
    """True when ``other`` is within ``tolerance`` (a fraction) of ``reference``."""
    return reference > 0 and abs(other - reference) <= tolerance * reference


def pick_by_price(
    reference: Optional[float], candidate_prices: Dict[str, float]
) -> Optional[str]:
    """The one candidate trading within `PRICE_MATCH_TOLERANCE` of CoinStats' price, or
    None when none does or several do — two tokens at one price are not told apart by
    guessing."""
    if reference is None:
        return None
    close = [gid for gid, p in candidate_prices.items()
             if prices_agree(reference, p, PRICE_MATCH_TOLERANCE)]
    return close[0] if len(close) == 1 else None


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
        gecko_factory: Callable[[], CoinGeckoClient] = CoinGeckoClient,
    ):
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._currency_service_factory = currency_service_factory
        self._gecko_factory = gecko_factory

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
        today = started_at.date()
        warnings: List[str] = []

        async with self._client_factory() as client:
            try:
                credits = await client.credits()
            except (CoinStatsAuthError, CoinStatsQuotaError) as e:
                # The paid calls would fail the same way; asking them would only fail again.
                return await self._finish(started_at, "error", e.reason, str(e))
            except CoinStatsError as e:
                # The balance is a guard, not the data: a transient failure of the free
                # check must not cost the slot its snapshot.
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

            try:
                value_body = await client.portfolio_value()
                coin_items = await client.portfolio_coins()
            except CoinStatsNotSynced as e:
                return await self._finish(started_at, "skipped", e.reason, str(e))
            except CoinStatsError as e:
                return await self._finish(started_at, "error", e.reason, str(e))
            credits_spent = client.credits_spent
            coin_pages = client.coin_pages

        summary = parse_value(value_body)
        holdings, malformed, _closed = parse_coins(coin_items)
        valued = [h for h in holdings if h.status == VALUED]
        if not valued and (summary["total_value_usd"] or 0) > 0:
            # The wipe guard: an empty list beside a positive total is a broken answer,
            # and storing it would draw every coin as sold.
            return await self._finish(
                started_at, "error", "empty_holdings",
                "CoinStats reported a portfolio value but no priced holdings; the "
                "previous snapshot is kept.",
            )
        if coin_pages > 1:
            warnings.append(
                f"CoinStats listed holdings over {coin_pages} pages; each sync costs "
                f"{CREDIT_COST['coins']} credits per page. Hiding spam tokens in "
                f"CoinStats reduces it."
            )
        if malformed:
            warnings.append(f"{malformed} CoinStats holding(s) had no identifier or no "
                            f"usable quantity and were skipped.")

        today_set = daily_set(holdings)

        # ── prices: CoinGecko, still before any write ──
        fetched = await self.fetch_prices(
            today, today_set,
            reference_prices={h.coin_id: h.price_usd for h in valued if h.price_usd},
        )
        warnings.extend(fetched.warnings)

        # ── writes: no HTTP in flight from here on, except the FX warm-up, which
        #    commits per currency pair and never inside a crypto transaction ──
        try:
            await _retry_once_if_locked(lambda: self.write_prices(fetched))
        except Exception as e:  # a price failure never costs the snapshot
            logger.exception("crypto sync: storing the prices failed")
            warnings.append(f"CoinGecko prices not stored: {type(e).__name__}")

        if await self._first_snapshot_today(started_at):
            warnings.extend(await self._warm_fx(today))

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
            "history_status": None,
        }
        try:
            await _retry_once_if_locked(
                lambda: self._write_snapshot(snapshot_fields, holdings, today, today_set)
            )
        except Exception as e:
            logger.exception("crypto sync: storing the snapshot failed")
            return await self._finish(
                started_at, "error", "store_failed",
                f"The CoinStats snapshot could not be stored: {type(e).__name__}",
            )

        message = "Crypto snapshot stored"
        if fetched.prices:
            message += "; prices refreshed"
        return await self._finish(started_at, "success", None, message, warnings)

    # ---------------------------------------------------------------- prices

    async def fetch_prices(
        self,
        today: date,
        today_set: Optional[Dict[str, Tuple[Optional[str], float]]] = None,
        extra_sets: Optional[Dict[date, Dict[str, Tuple[Optional[str], float]]]] = None,
        reference_prices: Optional[Dict[str, float]] = None,
    ) -> PriceFetch:
        """
        One CoinGecko pass: database reads, then HTTP, then nothing written — the caller
        writes `write_prices(result)` once every request is done.

        `today_set` replaces today's stored holdings (the sync has not written it yet);
        `extra_sets` adds dated sets the rebuild CLI is about to write, so their coins'
        closes are fetched in the same pass. `reference_prices` is CoinStats' own USD price
        per coin from the snapshot, which proves a new id mapping (see `pick_by_price`);
        without it an ambiguous symbol stays unmatched and no mapping is price-checked.
        """
        result = PriceFetch()
        if not prices_configured():
            result.warnings.append(COINGECKO_NOT_CONFIGURED)
            return result

        now = utcnow()
        async with self._session_factory() as db:
            sets = await _load_sets(db)
            mapping_rows = {
                r.coinstats_id: r
                for r in (await db.execute(select(CryptoCoinId))).scalars().all()
            }
            stored_closes = {
                (gid, d) for gid, d in (await db.execute(
                    select(CryptoCoinPrice.coingecko_id, CryptoCoinPrice.date).where(
                        CryptoCoinPrice.source == PRICE_DAILY,
                        CryptoCoinPrice.date >= CRYPTO_HISTORY_START,
                    )
                )).all()
            }
        symbols: Dict[str, Optional[str]] = {}
        quantities: Dict[date, Dict[str, float]] = {}
        for day, coins in sets.items():
            quantities[day] = {c: n for c, (_, n) in coins.items()}
            symbols.update({c: s for c, (s, _) in coins.items() if s})
        for day, coins in (extra_sets or {}).items():
            quantities[day] = {c: n for c, (_, n) in coins.items()}
            symbols.update({c: s for c, (s, _) in coins.items() if s})
        if today_set is not None:
            quantities[today] = {c: n for c, (_, n) in today_set.items()}
            symbols.update({c: s for c, (s, _) in today_set.items() if s})
        timeline = HoldingsTimeline(quantities)
        if not timeline:
            return result

        needed = needed_price_dates(timeline, CRYPTO_HISTORY_START, today)
        mapping: Dict[str, Optional[str]] = {
            c: r.coingecko_id for c, r in mapping_rows.items()
        }
        reach = today - timedelta(days=HISTORY_LIMIT_DAYS - 1)
        out_of_reach: Set[str] = set()

        gecko = self._gecko_factory()
        async with gecko:
            try:
                # 1. Which CoinGecko coin each CoinStats id is — asked only for a coin
                #    never seen, or unmatched and not re-asked for a week.
                to_map = [
                    c for c in sorted(needed)
                    if c not in mapping_rows or (
                        mapping_rows[c].coingecko_id is None
                        and now - mapping_rows[c].checked_at
                        > timedelta(days=MAPPING_RECHECK_DAYS)
                    )
                ]
                quoted: Dict[str, float] = {}
                asked: Set[str] = set()
                if to_map:
                    listing = await gecko.coins_list()
                    refs = reference_prices or {}
                    resolved: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
                    ambiguous: Dict[str, List[str]] = {}
                    for coin in to_map:
                        gid, method = resolve_coingecko_id(coin, symbols.get(coin), listing)
                        resolved[coin] = (gid, method)
                        if gid is None and refs.get(coin):
                            cands = symbol_candidates(symbols.get(coin), listing)
                            if 1 < len(cands) <= MAX_SYMBOL_CANDIDATES:
                                ambiguous[coin] = cands
                    # One price call settles both questions: which ambiguous candidate
                    # trades at CoinStats' price, and whether each new match does.
                    to_price = {g for g, _ in resolved.values() if g}
                    to_price |= {g for cands in ambiguous.values() for g in cands}
                    price_error: Optional[CoinGeckoError] = None
                    if to_price and refs:
                        # The same call is today's spot price for the coins already
                        # mapped, so a first sync still costs one price call, not two.
                        to_price |= {
                            mapping[c] for c in timeline.qty(today) if mapping.get(c)
                        }
                        try:
                            quoted = await gecko.simple_price(to_price)
                            asked = set(to_price)
                        except CoinGeckoError as e:
                            # The matches found so far are kept; an ambiguous coin is
                            # simply not recorded, so the next sync asks it again.
                            price_error = e
                    for coin in to_map:
                        gid, method = resolved[coin]
                        ref = refs.get(coin)
                        if price_error is not None and coin in ambiguous:
                            continue
                        if gid is None and coin in ambiguous:
                            gid = pick_by_price(
                                ref, {g: quoted[g] for g in ambiguous[coin] if g in quoted}
                            )
                            method = "symbol+price" if gid else None
                        elif gid and ref and gid in quoted and not prices_agree(
                            ref, quoted[gid], PRICE_SANITY_TOLERANCE
                        ):
                            result.warnings.append(
                                f"CoinGecko's '{gid}' trades at {quoted[gid]:.6g} USD but "
                                f"CoinStats prices {symbols.get(coin) or coin} at "
                                f"{ref:.6g}: not the same coin, so it is left unmatched."
                            )
                            gid, method = None, None
                        mapping[coin] = gid
                        result.mapping_rows.append({
                            "coinstats_id": coin, "coingecko_id": gid,
                            "symbol": symbols.get(coin), "method": method, "checked_at": now,
                        })
                    if price_error is not None:
                        raise price_error

                # 2. Today's price, one call for every coin held today — less those the
                #    mapping check above has already quoted.
                held_today = {mapping[c] for c in timeline.qty(today) if mapping.get(c)}
                spot = {g: quoted[g] for g in held_today if g in quoted}
                if held_today - asked:
                    spot.update(await gecko.simple_price(held_today - asked))
                result.prices.extend((gid, today, p, PRICE_SPOT) for gid, p in spot.items())

                # 3. The finished days' closes a held coin is still missing.
                by_gecko: Dict[str, Set[date]] = {}
                for coin, days in needed.items():
                    gid = mapping.get(coin)
                    if gid:
                        by_gecko.setdefault(gid, set()).update(d for d in days if d < today)
                checked = {
                    r.coingecko_id: r.closes_checked_at
                    for r in mapping_rows.values() if r.coingecko_id
                }
                for gid in sorted(by_gecko):
                    missing = {d for d in by_gecko[gid] if (gid, d) not in stored_closes}
                    if any(d < reach for d in missing):
                        out_of_reach.add(gid)
                    missing = {d for d in missing if d >= reach}
                    if not missing:
                        continue
                    last = checked.get(gid)
                    if last is not None and now - last < timedelta(hours=CLOSES_RECHECK_HOURS):
                        continue
                    points = await gecko.market_chart_range(
                        gid, _day_start_ts(min(missing)), int(
                            now.replace(tzinfo=timezone.utc).timestamp()
                        ),
                    )
                    closes = daily_closes(points, today)
                    result.prices.extend(
                        (gid, d, p, PRICE_DAILY) for d, p in closes.items()
                        if CRYPTO_HISTORY_START <= d
                    )
                    result.closes_checked.add(gid)
            except CoinGeckoError as e:
                result.warnings.append(
                    f"CoinGecko prices not fully refreshed ({e.reason}): {e}. What was "
                    f"fetched is kept; the next slot asks again."
                )
            result.calls = gecko.calls

        unmapped = sorted(
            symbols.get(c) or c for c in needed
            if not mapping.get(c) and peg_for(c, symbols.get(c)) is None
        )
        if unmapped:
            result.warnings.append(
                f"No CoinGecko coin found for {', '.join(unmapped)}: their value is "
                f"unknown on every day they are held."
            )
        if out_of_reach:
            result.warnings.append(
                f"{len(out_of_reach)} coin(s) need prices older than CoinGecko's "
                f"{HISTORY_LIMIT_DAYS}-day Demo history; those days stay unknown."
            )
        return result

    async def write_prices(self, fetched: PriceFetch) -> None:
        """The mapping and the prices in one transaction. A price row replaces the same
        coin's row for the same day (a close replaces yesterday's last spot)."""
        if not (fetched.mapping_rows or fetched.prices or fetched.closes_checked):
            return
        now = utcnow()
        async with self._session_factory() as db:
            for row in fetched.mapping_rows:
                await db.merge(CryptoCoinId(**row))
            await db.flush()
            for gid in fetched.closes_checked:
                await db.execute(
                    CryptoCoinId.__table__.update()
                    .where(CryptoCoinId.coingecko_id == gid)
                    .values(closes_checked_at=now)
                )
            by_coin: Dict[str, Dict[date, Tuple[float, str]]] = {}
            for gid, day, price, source in fetched.prices:
                by_coin.setdefault(gid, {})[day] = (price, source)
            for gid, days in by_coin.items():
                ordered = sorted(days)
                for i in range(0, len(ordered), 500):
                    await db.execute(delete(CryptoCoinPrice).where(
                        CryptoCoinPrice.coingecko_id == gid,
                        CryptoCoinPrice.date.in_(ordered[i:i + 500]),
                    ))
                db.add_all(
                    CryptoCoinPrice(coingecko_id=gid, date=d, price_usd=p, source=s,
                                    fetched_at=now)
                    for d, (p, s) in days.items()
                )
            await db.commit()

    async def refresh_prices(
        self,
        today: date,
        extra_sets: Optional[Dict[date, Dict[str, Tuple[Optional[str], float]]]] = None,
    ) -> PriceFetch:
        """Fetch, then write — the rebuild CLI's price fill, the same code as the sync's."""
        fetched = await self.fetch_prices(today, None, extra_sets)
        await _retry_once_if_locked(lambda: self.write_prices(fetched))
        return fetched

    # ---------------------------------------------------------------- writes

    async def _first_snapshot_today(self, now_utc: datetime) -> bool:
        """True while no snapshot has been stored yet today (Berlin): the FX warm-up is a
        once-a-day job, not an every-slot one."""
        async with self._session_factory() as db:
            count = (await db.execute(
                select(func.count()).select_from(CryptoSnapshot).where(
                    CryptoSnapshot.taken_at >= _berlin_day_start_utc(now_utc)
                )
            )).scalar_one()
        return count == 0

    async def _write_snapshot(
        self,
        fields: Dict[str, Any],
        holdings: List[ParsedHolding],
        today: date,
        today_set: Dict[str, Tuple[Optional[str], float]],
    ) -> int:
        async with self._session_factory() as db:
            snapshot = CryptoSnapshot(**fields)
            db.add(snapshot)
            await db.flush()
            db.add_all(CryptoHolding(snapshot_id=snapshot.id, **asdict(h)) for h in holdings)
            await db.flush()
            # Only the newest snapshot keeps its full holdings rows; the per-day
            # quantities the book is computed from live in crypto_daily_holdings.
            await db.execute(
                delete(CryptoHolding).where(CryptoHolding.snapshot_id != snapshot.id)
            )
            await db.execute(delete(CryptoDailyHolding).where(CryptoDailyHolding.date == today))
            db.add_all(
                CryptoDailyHolding(date=today, coin_id=coin, symbol=symbol, count=count,
                                   source=HOLDINGS_FROM_SNAPSHOT)
                for coin, (symbol, count) in today_set.items()
            )
            await db.commit()
            return snapshot.id

    async def _warm_fx(self, today: date) -> List[str]:
        """
        Keep the rates the read path needs in the cache, through the existing FX path:
        USD->EUR (the crypto book's own currency — the stock side warms USD only because a
        security happens to be held in it) and EUR->each non-EUR base, from
        `CRYPTO_HISTORY_START`. The whole window is fetched only while the cache does not
        reach its start; after that, a week keeps the recent end fresh. Each pair commits
        on its own session, so no write lock is held across the next pair's request.
        Never raises.
        """
        warnings: List[str] = []
        window_start = CRYPTO_HISTORY_START - timedelta(days=FX_LOOKBACK_DAYS)
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


async def _load_sets(db: AsyncSession) -> Dict[date, Dict[str, Tuple[Optional[str], float]]]:
    """Every stored daily holdings set, `{day: {coin: (symbol, count)}}`."""
    sets: Dict[date, Dict[str, Tuple[Optional[str], float]]] = {}
    for row in (await db.execute(select(CryptoDailyHolding))).scalars().all():
        sets.setdefault(row.date, {})[row.coin_id] = (row.symbol, row.count)
    return sets


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


@dataclass
class _Book:
    """Everything the two read endpoints compute from, loaded in one go."""

    timeline: HoldingsTimeline
    prices: PriceBook
    symbols: Dict[str, Optional[str]]
    first_snapshot_date: Optional[date]
    prices_fetched_at: Optional[datetime]


def _peg_note(pegged: Dict[str, int], symbols: Dict[str, Optional[str]]) -> Optional[str]:
    if not pegged:
        return None
    parts = ", ".join(
        f"{symbols.get(c) or c} on {n} day{'s' if n != 1 else ''}"
        for c, n in sorted(pegged.items())
    )
    return (
        f"Valued at a fixed 1.00 USD peg where CoinGecko has no price — {parts}. A "
        f"deliberate exception, not a market price."
    )


def _missing_note(missing: Dict[str, int], symbols: Dict[str, Optional[str]]) -> Optional[str]:
    if not missing:
        return None
    names = ", ".join(sorted(symbols.get(c) or c for c in missing))
    days = max(missing.values())
    return (
        f"No price for {names} on up to {days} day(s): left out of those days' value and "
        f"P&L, which cover the priced coins only — never counted as zero."
    )


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
        # has a rate on or before the window's first day, and never backfills from inside
        # a GET.
        base_fx = await load_base_fx(
            self.db, self.currency_service, base, start - timedelta(days=FX_LOOKBACK_DAYS),
            backfill=False,
        )
        rates = await preload_eur_rates(self.db, {"USD"}, start, end)
        # EUR needs no EUR->base rate, and USD amounts in a USD base are never converted.
        no_base_rates = base not in ("EUR", "USD") and not base_fx.rate_cache
        return _Projector(NativeToBase(PreloadedRates(rates), base_fx), no_base_rates)

    async def _book(self) -> _Book:
        sets = await _load_sets(self.db)
        symbols: Dict[str, Optional[str]] = {}
        for coins in sets.values():
            symbols.update({c: s for c, (s, _) in coins.items() if s})
        mapping = {
            r.coinstats_id: r.coingecko_id
            for r in (await self.db.execute(select(CryptoCoinId))).scalars().all()
        }
        rows = (await self.db.execute(
            select(CryptoCoinPrice).where(CryptoCoinPrice.date >= CRYPTO_HISTORY_START)
        )).scalars().all()
        first_snapshot = (await self.db.execute(
            select(func.min(CryptoDailyHolding.date)).where(
                CryptoDailyHolding.source == HOLDINGS_FROM_SNAPSHOT
            )
        )).scalar()
        return _Book(
            timeline=HoldingsTimeline(
                {d: {c: n for c, (_, n) in coins.items()} for d, coins in sets.items()}
            ),
            prices=PriceBook({(r.coingecko_id, r.date): r.price_usd for r in rows},
                             mapping, symbols),
            symbols=symbols,
            first_snapshot_date=first_snapshot,
            prices_fetched_at=max((r.fetched_at for r in rows), default=None),
        )

    @staticmethod
    def _series(book: _Book, end: date) -> List[DayPoint]:
        if not book.timeline or end < CRYPTO_HISTORY_START:
            return []
        return compute_series(book.timeline, book.prices, CRYPTO_HISTORY_START, end)

    @staticmethod
    def _notes(series: List[DayPoint], symbols) -> Tuple[Optional[str], Optional[str]]:
        pegged: Dict[str, int] = {}
        missing: Dict[str, int] = {}
        for point in series:
            for coin in point.pegged:
                pegged[coin] = pegged.get(coin, 0) + 1
            for coin in point.missing:
                missing[coin] = missing.get(coin, 0) + 1
        return _peg_note(pegged, symbols), _missing_note(missing, symbols)

    async def portfolio(self) -> Dict[str, Any]:
        """
        The newest snapshot's coins at CoinGecko's price on the snapshot's UTC day: the
        KPI totals and the holdings table, from ONE snapshot. The P&L since
        `CRYPTO_HISTORY_START` and today's change come from the same series `history()`
        serves, so a tile and the chart's last point cannot disagree.
        """
        base = await self._base_currency()
        snapshot = await self._latest_snapshot()
        result: Dict[str, Any] = {
            "configured": is_configured(),
            "prices_configured": prices_configured(),
            "base_currency": base,
            "as_of": None,
            "prices_as_of": None,
            "total_value": None,
            "defi_value": None,
            "cash_value": None,
            "change_today": None,
            "change_today_pct": None,
            "pnl_since_start": None,
            "start_date": CRYPTO_HISTORY_START.isoformat(),
            "basket_date": None,
            "first_snapshot_date": None,
            "peg_note": None,
            "fx_unavailable": 0,
            "valued_count": 0,
            "spam_count": 0,
            "unpriced_count": 0,
            "unpriced_symbols": [],
            "no_price_symbols": [],
            "holdings": [],
            "color_order": [],
            "warnings": [],
        }
        if snapshot is None:
            return result

        rows = (await self.db.execute(
            select(CryptoHolding).where(CryptoHolding.snapshot_id == snapshot.id)
        )).scalars().all()
        coins = [r for r in rows if r.status == VALUED and not r.is_fiat]
        fiat = [r for r in rows if r.status == VALUED and r.is_fiat]
        unpriced = [r for r in rows if r.status == UNPRICED]

        day = snapshot.taken_at.date()
        book = await self._book()
        series = self._series(book, day)
        by_day = {p.day: p for p in series}
        project = await self._projector(base, CRYPTO_HISTORY_START, day)

        priced: List[Tuple[CryptoHolding, Optional[float], Optional[str], Optional[float]]] = []
        for row in coins:
            price, source = book.prices.price(row.coin_id, day)
            if price is None:
                # The snapshot's own coins may not be in the book yet (an older row);
                # its symbol still resolves a peg.
                peg = peg_for(row.coin_id, row.symbol)
                price, source = (peg, PRICE_PEG) if peg is not None else (None, None)
            before, _ = book.prices.price(row.coin_id, day - timedelta(days=1))
            change = (price / before - 1) * 100 if price is not None and before else None
            priced.append((row, price, source, change))

        # The priced coins' total: a coin without a price is left out and named, never
        # valued at 0 — and never allowed to blank the rest (docs/crypto.md). None only
        # when there are coins and not one of them is priced.
        valued_now = [r.count * p for r, p, _, _ in priced if p is not None]
        total_usd: Optional[float] = (
            sum(valued_now) if valued_now or not priced else None
        )
        warnings: List[str] = list(snapshot.warnings or [])
        no_price = sorted(r.symbol or r.coin_id for r, p, _, _ in priced if p is None)
        if not result["prices_configured"]:
            warnings.append(COINGECKO_NOT_CONFIGURED)
        if no_price:
            warnings.append(
                f"No CoinGecko price for {', '.join(no_price)} on {day.isoformat()}: left "
                f"out of the total, which covers the priced coins only."
            )
        age_hours = (utcnow() - snapshot.taken_at).total_seconds() / 3600
        if age_hours > STALE_SNAPSHOT_HOURS:
            warnings.append(
                f"Crypto figures are {int(age_hours)} hours old — recent syncs did not "
                f"complete."
            )

        holdings = []
        ordered = sorted(priced, key=lambda t: (t[1] is None, -(t[0].count * (t[1] or 0))))
        for row, price, source, change in ordered:
            value_usd = row.count * price if price is not None else None
            holdings.append({
                "coin_id": row.coin_id,
                "symbol": row.symbol,
                "name": row.name,
                "rank": row.rank,
                "status": VALUED if price is not None else "no_price",
                "quantity": row.count,
                "price": _per_unit(await project(price, day)),
                "price_source": source,
                "value": _amount(await project(value_usd, day)),
                "weight_pct": _pct(
                    value_usd / total_usd * 100 if value_usd is not None and total_usd else None
                ),
                "change_today_pct": _pct(change),
            })
        for row in sorted(unpriced, key=lambda r: (r.symbol or r.coin_id)):
            holdings.append({
                "coin_id": row.coin_id, "symbol": row.symbol, "name": row.name,
                "rank": row.rank, "status": UNPRICED, "quantity": row.count, "price": None,
                "price_source": None, "value": None, "weight_pct": None,
                "change_today_pct": None,
            })

        # Today's change: yesterday's coins times the move since yesterday's close.
        today_point = by_day.get(day)
        yesterday_point = by_day.get(day - timedelta(days=1))
        change_today = today_point.pnl_usd if today_point else None
        base_value = yesterday_point.value_usd if yesterday_point else None

        # P&L since the start: the sum of each day's P&L, each converted at its own date
        # — the same sum the chart's ALL range ends on. One unknown day makes it unknown.
        pnl_total: Optional[Decimal] = Decimal(0) if len(series) > 1 else None
        for point in series[1:]:
            converted = await project(point.pnl_usd, point.day)
            if converted is None:
                pnl_total = None
                break
            pnl_total += converted
        peg_note, missing_note = self._notes(series, book.symbols)
        if missing_note:
            warnings.append(missing_note)

        cash_usd = _sum_known(r.value_usd for r in fiat) if fiat else None
        result.update({
            "as_of": utc_iso(snapshot.taken_at),
            "prices_as_of": utc_iso(book.prices_fetched_at),
            "total_value": _amount(await project(total_usd, day)),
            "defi_value": _amount(await project(snapshot.defi_value_usd, day)),
            "cash_value": _amount(await project(cash_usd, day)),
            "change_today": _amount(await project(change_today, day)),
            "change_today_pct": _pct(
                change_today / base_value * 100
                if change_today is not None and base_value else None
            ),
            "pnl_since_start": _amount(pnl_total),
            "basket_date": book.timeline.earliest.isoformat() if book.timeline else None,
            "first_snapshot_date": (
                book.first_snapshot_date.isoformat() if book.first_snapshot_date else None
            ),
            "peg_note": peg_note,
            "valued_count": len(coins),
            # Counted, never itemised: an airdropped scam token's "symbol" is often a
            # phishing URL, and this is the one place it would otherwise be rendered.
            "spam_count": snapshot.spam_count,
            "unpriced_count": snapshot.unpriced_count,
            "unpriced_symbols": sorted({r.symbol or r.coin_id for r in unpriced}),
            "no_price_symbols": no_price,
            "holdings": holdings,
            # Colour identity: CoinStats' market-cap rank, which belongs to the coin and
            # not to its size in this portfolio.
            "color_order": [
                r.coin_id for r in sorted(
                    coins, key=lambda r: (r.rank is None, r.rank or 0, r.coin_id)
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
        """
        The book's daily value and P&L from `CRYPTO_HISTORY_START`, computed from the daily
        holdings and CoinGecko's prices, each point converted at its own date. Points
        before the first snapshot-sourced holdings day carry `reconstructed: true`.
        """
        base = await self._base_currency()
        book = await self._book()
        today = utcnow().date()
        last_price_day = (await self.db.execute(
            select(func.max(CryptoCoinPrice.date))
        )).scalar()
        candidates = [d for d in (book.timeline.latest, last_price_day) if d is not None]
        end = min(max(candidates), today) if candidates else None
        series = self._series(book, end) if end else []

        result: Dict[str, Any] = {
            "configured": is_configured(),
            "prices_configured": prices_configured(),
            "base_currency": base,
            "points": [],
            "start_date": CRYPTO_HISTORY_START.isoformat(),
            "basket_date": book.timeline.earliest.isoformat() if book.timeline else None,
            "first_snapshot_date": (
                book.first_snapshot_date.isoformat() if book.first_snapshot_date else None
            ),
            "fetched_at": utc_iso(book.prices_fetched_at),
            "peg_note": None,
            "fx_unavailable": 0,
            "warnings": [],
        }
        if not series:
            return result

        project = await self._projector(base, series[0].day, series[-1].day)
        first = book.first_snapshot_date
        points = []
        for point in series:
            points.append({
                "date": point.day.isoformat(),
                "value": _amount(await project(point.value_usd, point.day)),
                "pnl": _amount(await project(point.pnl_usd, point.day)),
                "reconstructed": first is None or point.day < first,
                "excluded": sorted(book.symbols.get(c) or c for c in point.missing),
            })
        result["points"] = points
        result["peg_note"], missing_note = self._notes(series, book.symbols)
        if missing_note:
            result["warnings"].append(missing_note)
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
            "prices_configured": prices_configured(),
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
