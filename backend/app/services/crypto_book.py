"""
The crypto book's arithmetic — pure, no database, no network (docs/crypto.md).

One formula for the whole year, used by the read path, the sync (to know which prices it
needs) and the rebuild CLI:

    qty(d, coin)  = the holdings set dated on or before d; before the earliest set, the
                    earliest set (the reconstructed span), never before CRYPTO_HISTORY_START
    value(d)      = Σ qty(d, coin) × price(d, coin)
    pnl(d)        = Σ qty(d−1, coin) × (price(d, coin) − price(d−1, coin))

Yesterday's coins times today's price move: a buy, a DCA, a transfer between exchanges or
a staking reward changes a quantity and never the P&L. That is what removes the "Kraken
transfer as profit" spike CoinStats' own history drew, without rewriting any history.

**A coin without a price is left out and counted, never valued at 0.** It drops out of
that day's value, and out of the day's P&L whenever either end of its move is unpriced —
so a coin gaining or losing its price is never a gain or a loss. `DayPoint.missing`
names it, and every surface says the figure excludes it. A figure is None only when NO
coin is priced. This is the app's convention for unpriced stock holdings; until
2026-10-03 the book instead made the whole day unknown, and one 0.64-BNB position CoinGecko
could not identify blanked every total, tile and chart point of the book.

The one valuation exception is `STABLECOIN_PEGS`: the owner's decision (2026-10-02) to
value USDC at exactly 1.00 USD on a day CoinGecko has no price for it. It is flagged as a
peg wherever it is used, so it can never pass for a market price.
"""
import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

# The book is shown from here. Nothing before it is computed, fetched or drawn.
CRYPTO_HISTORY_START = date(2026, 1, 1)

# Coins valued at a fixed USD price on a day CoinGecko has no price for them — an
# explicit, narrow exception to "unknown is absent" (owner's decision, 2026-10-02). Keyed
# by CoinStats identifier, and by symbol for an identifier CoinStats spells differently.
# Add a coin here only on the owner's word.
STABLECOIN_PEGS: Dict[str, float] = {"usd-coin": 1.0}
STABLECOIN_PEG_SYMBOLS: Dict[str, float] = {"USDC": 1.0}

PRICE_MARKET = "coingecko"
PRICE_PEG = "peg"


def peg_for(coin_id: str, symbol: Optional[str]) -> Optional[float]:
    """The coin's fixed USD peg, or None for every coin not in the table."""
    if coin_id in STABLECOIN_PEGS:
        return STABLECOIN_PEGS[coin_id]
    if symbol and symbol.upper() in STABLECOIN_PEG_SYMBOLS:
        return STABLECOIN_PEG_SYMBOLS[symbol.upper()]
    return None


class HoldingsTimeline:
    """`qty(d)` over dated holdings sets: the set dated on or before `d`, else the earliest."""

    def __init__(self, sets: Mapping[date, Mapping[str, float]]):
        self._dates = sorted(sets)
        self._sets = {d: {c: float(n) for c, n in sets[d].items() if n > 0} for d in self._dates}

    def __bool__(self) -> bool:
        return bool(self._dates)

    @property
    def earliest(self) -> Optional[date]:
        return self._dates[0] if self._dates else None

    @property
    def latest(self) -> Optional[date]:
        return self._dates[-1] if self._dates else None

    def coins(self) -> Set[str]:
        return {c for s in self._sets.values() for c in s}

    def qty(self, day: date) -> Dict[str, float]:
        if not self._dates:
            return {}
        i = bisect.bisect_right(self._dates, day) - 1
        return self._sets[self._dates[max(i, 0)]]


class PriceBook:
    """
    Prices by CoinStats coin: through the CoinGecko mapping into the stored USD prices,
    then the peg table for a pegged coin with no market price. Exact dates only — a
    missing day is never filled from a neighbour.
    """

    def __init__(
        self,
        prices: Mapping[Tuple[str, date], float],
        mapping: Mapping[str, Optional[str]],
        symbols: Mapping[str, Optional[str]],
    ):
        self._prices = prices
        self._mapping = mapping
        self._symbols = symbols

    def price(self, coin_id: str, day: date) -> Tuple[Optional[float], Optional[str]]:
        gecko = self._mapping.get(coin_id)
        if gecko is not None:
            value = self._prices.get((gecko, day))
            if value is not None:
                return value, PRICE_MARKET
        peg = peg_for(coin_id, self._symbols.get(coin_id))
        if peg is not None:
            return peg, PRICE_PEG
        return None, None


@dataclass
class DayPoint:
    day: date
    value_usd: Optional[float]
    pnl_usd: Optional[float]
    # Coins held (or held the day before, for the P&L) with no price: left out of the
    # day's figures, which cover the priced coins only.
    missing: Set[str] = field(default_factory=set)
    pegged: Set[str] = field(default_factory=set)


def compute_series(
    timeline: HoldingsTimeline, book: PriceBook, start: date, end: date
) -> List[DayPoint]:
    """Value and P&L for every day from `start` to `end` inclusive. The first day's P&L
    is None: its baseline is before the series."""
    points: List[DayPoint] = []
    previous_prices: Dict[str, Optional[float]] = {}
    day = start
    while day <= end:
        held = timeline.qty(day)
        missing: Set[str] = set()
        pegged: Set[str] = set()
        prices: Dict[str, Optional[float]] = {}

        def lookup(coin: str, on: date) -> Optional[float]:
            price, source = book.price(coin, on)
            if source == PRICE_PEG:
                pegged.add(coin)
            return price

        value = 0.0
        priced_any = not held
        for coin, count in held.items():
            prices[coin] = lookup(coin, day)
            if prices[coin] is None:
                missing.add(coin)
            else:
                value += count * prices[coin]
                priced_any = True

        pnl: Optional[float] = None
        if day > start:
            before = timeline.qty(day - timedelta(days=1))
            pnl = 0.0
            moved_any = not before
            for coin, count in before.items():
                now = prices[coin] if coin in prices else lookup(coin, day)
                then = previous_prices.get(coin) if coin in previous_prices else lookup(
                    coin, day - timedelta(days=1)
                )
                if now is None or then is None:
                    # Left out, both ends: a coin gaining or losing its price is never a
                    # gain or a loss.
                    missing.add(coin)
                else:
                    pnl += count * (now - then)
                    moved_any = True
            if not moved_any:
                pnl = None
        value_out: Optional[float] = value if priced_any else None

        points.append(DayPoint(day, value_out, pnl, missing, pegged))
        previous_prices = {**prices}
        day += timedelta(days=1)
    return points


def needed_price_dates(
    timeline: HoldingsTimeline, start: date, end: date
) -> Dict[str, Set[date]]:
    """Per coin, the days a price is needed on: held that day (value) or held the day
    before (that day's P&L needs both ends of the move)."""
    needed: Dict[str, Set[date]] = {}
    day = start
    while day <= end:
        for coin in timeline.qty(day):
            needed.setdefault(coin, set()).add(day)
            if day > start:
                needed[coin].add(day - timedelta(days=1))
        if day > start:
            for coin in timeline.qty(day - timedelta(days=1)):
                needed.setdefault(coin, set()).add(day)
        day += timedelta(days=1)
    return needed


def daily_closes(points: Iterable[Tuple[int, float]], today: date) -> Dict[date, float]:
    """
    CoinGecko's `[epoch_ms, price]` points → one close per **finished** UTC day.

    A point belongs to the day that ends at or after it, so CoinGecko's daily point at
    00:00 UTC closes the day before; inside that, the latest point wins (hourly data for
    spans under 90 days). Today is never a close — `/simple/price` covers it.
    """
    latest: Dict[date, Tuple[int, float]] = {}
    for stamp, price in points:
        moment = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc) - timedelta(seconds=1)
        day = moment.date()
        if day >= today:
            continue
        if day not in latest or stamp >= latest[day][0]:
            latest[day] = (stamp, price)
    return {d: p for d, (_, p) in latest.items()}


def sum_known(values: Iterable[Optional[float]]) -> Optional[float]:
    """One unknown makes the sum unknown."""
    total = 0.0
    for value in values:
        if value is None:
            return None
        total += value
    return total
