"""
Rebuild the crypto book's daily holdings backwards from CoinStats' transaction list.

    python -m app.cli.crypto_rebuild_holdings --probe      # shape only, ~4 credits, no write
    python -m app.cli.crypto_rebuild_holdings --dry-run    # the whole rebuild, nothing written
    python -m app.cli.crypto_rebuild_holdings              # write it, then fill the prices

Why it exists (docs/crypto.md): the book's history is `qty(d) × price(d)`, and the syncs
only know the quantities from the day they start writing `crypto_daily_holdings`. The
days before that come from here: starting from the newest snapshot's holdings and undoing,
day by day, every transaction dated after it,

    qty(d) = holdings now − Σ signed transaction legs dated after d (up to the snapshot)

down to the **reference date** (2026-08-23, the first day after the Kraken coins arrived
on Binance). Every day before the reference uses the reference basket — the reconstructed
span the read path already handles.

**Refuses whole, writes whole.** Nothing is written unless every page parsed, no coin it
would write goes negative on any day (a negative means a transaction is missing), and the
trusted window — the reference day and the two after it — holds the same basket within
`--tolerance-pct`. The rows go in one transaction, replacing any earlier rebuild; then the
CoinGecko closes for every coin ever held are filled through the same code the sync uses.

**The transaction list's exact shape is not confirmed yet.** `--probe` prints the first
page's structure — key names, types, counts of items and the *signs* of the counts — and
never an address, a hash, a note or an amount, so the leg semantics (`--legs`, `--fees`)
can be chosen from evidence before any write. The dry run prints the reference basket and
how the reconstruction compares with each day a sync already stored: **run it over ssh and
never paste its output into a committed file** (this repository is public).

Records one `sync_runs` row (`crypto_rebuild`, type/status/message only — left out of the
public history like `crypto_sync`) for a real run. Run it off-slot: it shares SQLite with
the scheduled jobs.
"""
import argparse
import asyncio
import logging
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import delete, select

from app.clock import utcnow
from app.database import AsyncSessionLocal
from app.models.crypto import (
    HOLDINGS_FROM_SNAPSHOT,
    HOLDINGS_FROM_TRANSACTIONS,
    VALUED,
    CryptoDailyHolding,
    CryptoHolding,
    CryptoSnapshot,
)
from app.repositories.sync_run_repository import SyncRunRepository
from app.services.coinstats_client import (
    CREDIT_COST,
    MAX_TRANSACTION_PAGES,
    TRANSACTIONS_PAGE_LIMIT,
    CoinStatsClient,
    CoinStatsError,
    is_configured,
)
from app.services.crypto_service import (
    REBUILD_SYNC_TYPE,
    CryptoSyncService,
    _retry_once_if_locked,
)

logger = logging.getLogger(__name__)

REFERENCE_DATE = date(2026, 8, 23)
TRUSTED_WINDOW_DAYS = 3
TOLERANCE_PCT = 0.5
# A quantity this close to zero is rounding in CoinStats' counts, not a position.
EPSILON = 1e-9
# Below this a negative quantity is a refusal: a missing transaction, not float noise.
NEGATIVE_TOLERANCE = 1e-6
# Fee dust the transaction list does not carry, accepted per symbol up to this many coins
# (owner's decision, 2026-10-03): Binance takes its trading fees in BNB, and the shortfall
# that leaves when the walk undoes them is a few euros, not a missing transaction. Within
# the allowance the coin is written as 0 on those days and the run warns; beyond it, the
# refusal stands. Add a symbol here only on the owner's word.
DUST_ALLOWANCE_BY_SYMBOL: Dict[str, float] = {"BNB": 0.05}

# A transfer whose type names an outflow, when CoinStats sends its count unsigned.
OUT_TYPES = {"sent", "send", "withdraw", "withdrawal", "sell", "out", "outgoing", "fee"}


class RebuildRefused(Exception):
    """Reported, never partially applied."""


@dataclass(frozen=True)
class Leg:
    at: datetime          # naive UTC
    coin_id: str
    symbol: Optional[str]
    count: float          # signed: positive arrives, negative leaves
    fiat: bool = False


# ───────────────────────────────────────────────────────────── parsing (pure)


def _num(value: Any) -> Optional[float]:
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


def to_utc_datetime(stamp: Any) -> Optional[datetime]:
    """Epoch seconds, epoch milliseconds or ISO → naive UTC."""
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)):
        seconds = stamp / 1000 if stamp > 10_000_000_000 else stamp
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def page_items(body: Any) -> List[Any]:
    """The items of one page, or a refusal naming the shape (keys only)."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("result", "data", "transactions"):
            if isinstance(body.get(key), list):
                return body[key]
        raise RebuildRefused(
            f"unrecognised /portfolio/transactions page: an object with keys "
            f"{sorted(body)[:20]} and no item list"
        )
    raise RebuildRefused(
        f"unrecognised /portfolio/transactions page: a {type(body).__name__}"
    )


def _coin_of(block: Any) -> Tuple[Optional[str], Optional[str], bool]:
    if not isinstance(block, dict):
        return None, None, False
    identifier = block.get("identifier") or block.get("coinId") or block.get("id")
    symbol = block.get("symbol")
    fiat = bool(block.get("isFiat")) or str(identifier or "").startswith("FiatCoin")
    return (str(identifier) if identifier else None,
            str(symbol)[:32] if symbol else None, fiat)


def _signed(count: float, kind: Any) -> float:
    if count > 0 and str(kind or "").strip().lower() in OUT_TYPES:
        return -count
    return count


def legs_of(item: Any, legs_mode: str = "transfers", fees: str = "ignore") -> List[Leg]:
    """
    One transaction's per-coin quantity changes.

    `transfers` reads every `transfers[].items[]` leg (a swap is two legs), falling back to
    `coinData` when the item has no transfers; `coindata` reads `coinData.count` only.
    A count CoinStats sends unsigned on an outflow type is negated. `--fees subtract`
    also takes `fee.count` of `fee.coin` out. Anything unusable is a refusal, never a skip.
    """
    if not isinstance(item, dict):
        raise RebuildRefused("a transaction item is not an object")
    at = to_utc_datetime(item.get("date"))
    if at is None:
        raise RebuildRefused(
            f"a transaction has no usable date (keys {sorted(item)[:20]})"
        )
    kind = item.get("transactionType") or item.get("type")
    legs: List[Leg] = []

    transfers = item.get("transfers")
    if legs_mode == "transfers" and isinstance(transfers, list) and transfers:
        for transfer in transfers:
            if not isinstance(transfer, dict) or not isinstance(transfer.get("items"), list):
                raise RebuildRefused("a transfer without an `items` list")
            for leg in transfer["items"]:
                if not isinstance(leg, dict):
                    raise RebuildRefused("a transfer item is not an object")
                coin_id, symbol, fiat = _coin_of(leg.get("coin") or leg.get("coinData"))
                count = _num(leg.get("count"))
                if coin_id is None or count is None:
                    raise RebuildRefused(
                        f"a transfer item without a coin identifier or a count "
                        f"(keys {sorted(leg)[:20]})"
                    )
                legs.append(Leg(at, coin_id, symbol,
                                _signed(count, transfer.get("transferType") or kind), fiat))
    else:
        data = item.get("coinData")
        if data is not None:
            coin_id, symbol, fiat = _coin_of(data)
            count = _num(data.get("count")) if isinstance(data, dict) else None
            if coin_id is None or count is None:
                raise RebuildRefused(
                    f"a `coinData` without an identifier or a count "
                    f"(keys {sorted(data)[:20] if isinstance(data, dict) else type(data).__name__})"
                )
            legs.append(Leg(at, coin_id, symbol, _signed(count, kind), fiat))

    if fees == "subtract" and isinstance(item.get("fee"), dict):
        fee = item["fee"]
        coin_id, symbol, fiat = _coin_of(fee.get("coin"))
        count = _num(fee.get("count"))
        if coin_id and count:
            legs.append(Leg(at, coin_id, symbol, -abs(count), fiat))
    return legs


def rebuild(
    anchor: Dict[str, float],
    anchor_at: datetime,
    legs: Iterable[Leg],
    start: date,
    end: date,
) -> Dict[date, Dict[str, float]]:
    """
    End-of-day quantities for every day `start..end`:
    `qty(d) = anchor − Σ legs dated after d and at or before anchor_at`.
    A leg after the anchor is already outside it and is ignored.
    """
    counted = [leg for leg in legs if leg.at <= anchor_at]
    out: Dict[date, Dict[str, float]] = {}
    day = start
    while day <= end:
        qty = dict(anchor)
        for leg in counted:
            if leg.at.date() > day:
                qty[leg.coin_id] = qty.get(leg.coin_id, 0.0) - leg.count
        out[day] = qty
        day += timedelta(days=1)
    return out


def check_rebuild(
    days: Dict[date, Dict[str, float]],
    eligible: Set[str],
    symbols: Dict[str, Optional[str]],
    reference: date,
    window_days: int = TRUSTED_WINDOW_DAYS,
    tolerance_pct: float = TOLERANCE_PCT,
) -> List[str]:
    """The two refusals: a negative quantity of a coin that would be written, beyond its
    `DUST_ALLOWANCE_BY_SYMBOL`, and a trusted window whose baskets disagree. Returns a
    warning per coin accepted within its allowance — those days write it as 0."""
    name = lambda c: symbols.get(c) or c  # noqa: E731
    lowest: Dict[str, Tuple[float, date, date]] = {}  # coin -> (lowest, first day, last day)
    for d in sorted(days):
        for c, n in days[d].items():
            if c in eligible and n < -NEGATIVE_TOLERANCE:
                low, first, _ = lowest.get(c, (0.0, d, d))
                lowest[c] = (min(low, n), first, d)
    refused: List[str] = []
    accepted: List[str] = []
    for c in sorted(lowest, key=name):
        low, first, last = lowest[c]
        allowance = DUST_ALLOWANCE_BY_SYMBOL.get((symbols.get(c) or "").upper(), 0.0)
        line = f"{name(c)} down to {low:.8g} ({first.isoformat()} .. {last.isoformat()})"
        if -low <= allowance:
            accepted.append(f"{line}, within the {allowance:g} allowance")
        else:
            refused.append(line)
    if refused:
        raise RebuildRefused(
            "a quantity goes negative — a transaction is missing or a leg is read with the "
            "wrong sign (try --probe, --legs, --fees): " + "; ".join(refused[:20])
        )
    window = [reference + timedelta(days=i) for i in range(window_days)]
    window = [d for d in window if d in days]
    diffs = []
    for coin in sorted(eligible, key=name):
        values = [max(days[d].get(coin, 0.0), 0.0) for d in window]
        top = max(values) if values else 0.0
        if top > EPSILON and (top - min(values)) > top * tolerance_pct / 100:
            diffs.append(
                f"{name(coin)}: " + ", ".join(
                    f"{v:.8g} on {d.isoformat()}" for d, v in zip(window, values)
                )
            )
    if diffs:
        raise RebuildRefused(
            f"the trusted window ({window[0].isoformat()} .. {window[-1].isoformat()}) does "
            f"not hold one basket within {tolerance_pct}%:\n  " + "\n  ".join(diffs)
        )
    return [f"fee dust written as 0: {line}" for line in accepted]


# ──────────────────────────────────────────────────────────────────── probe


def _sign(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "none"
    return "+" if number > 0 else "-" if number < 0 else "0"


def _shape(value: Any, depth: int = 0) -> str:
    """Keys and types, recursively for dicts; never a value."""
    if isinstance(value, dict):
        if depth >= 2:
            return "{...}"
        return "{" + ", ".join(
            f"{k}: {_shape(v, depth + 1)}" for k, v in sorted(value.items())
        ) + "}"
    if isinstance(value, list):
        return f"list[{len(value)}]" + (
            f" of {_shape(value[0], depth + 1)}" if value and depth < 2 else ""
        )
    return type(value).__name__


def describe_page(body: Any) -> List[str]:
    """What `--probe` prints: structure and signs only — no address, hash, note or amount."""
    lines = [f"page: {type(body).__name__}"
             + (f" with keys {sorted(body)}" if isinstance(body, dict) else "")]
    if isinstance(body, dict) and isinstance(body.get("meta"), dict):
        lines.append(f"meta: {_shape(body['meta'])}")
        lines.append("meta values (paging only): " + ", ".join(
            f"{k}={v}" for k, v in sorted(body["meta"].items())
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ))
    try:
        items = page_items(body)
    except RebuildRefused as e:
        return lines + [f"REFUSED: {e}"]
    lines.append(f"items on this page: {len(items)} (limit {TRANSACTIONS_PAGE_LIMIT})")
    keys: Counter = Counter()
    patterns: Counter = Counter()
    for item in items:
        if not isinstance(item, dict):
            patterns["<not an object>"] += 1
            continue
        keys.update(sorted(item))
        data = item.get("coinData") if isinstance(item.get("coinData"), dict) else {}
        transfers = item.get("transfers") if isinstance(item.get("transfers"), list) else []
        legs = [
            (str(t.get("transferType")), _sign(i.get("count")))
            for t in transfers if isinstance(t, dict)
            for i in (t.get("items") or []) if isinstance(i, dict)
        ]
        main = (data.get("identifier") if data else None)
        main_sum = sum(
            _num(i.get("count")) or 0.0
            for t in transfers if isinstance(t, dict)
            for i in (t.get("items") or []) if isinstance(i, dict)
            and _coin_of(i.get("coin"))[0] == main
        )
        agrees = (main is not None and _num(data.get("count")) is not None
                  and math.isclose(main_sum, _num(data.get("count")), rel_tol=1e-9,
                                   abs_tol=1e-12))
        patterns[(
            f"type={item.get('transactionType')!s}",
            f"coinData.count={_sign(data.get('count')) if data else 'absent'}",
            f"legs={sorted(legs)}",
            f"sum(legs of coinData coin)==coinData.count: {agrees}",
            f"fee={'yes' if isinstance(item.get('fee'), dict) else 'no'}",
        )] += 1
    lines.append(f"item keys (how many items carry each): {dict(sorted(keys.items()))}")
    if items and isinstance(items[0], dict):
        safe = {k: v for k, v in items[0].items()
                if k not in ("hash", "note", "notes", "address", "from", "to")}
        lines.append(f"first item shape (hash/note/address keys left out): {_shape(safe)}")
    lines.append("patterns (type, sign of coinData.count, transfer types and signs, "
                 "whether the legs add up to coinData.count, fee):")
    lines.extend(f"  {n} x {p}" for p, n in patterns.most_common())
    return lines


# ───────────────────────────────────────────────────────────────────── run


async def _fetch_pages(client: CoinStatsClient) -> List[Any]:
    items: List[Any] = []
    for page in range(1, MAX_TRANSACTION_PAGES + 1):
        page_list = page_items(await client.portfolio_transactions_page(page))
        items.extend(page_list)
        if len(page_list) < TRANSACTIONS_PAGE_LIMIT:
            return items
    raise RebuildRefused(
        f"more than {MAX_TRANSACTION_PAGES} pages of transactions — refusing rather than "
        f"rebuilding from a truncated list"
    )


async def _record(status: str, message: str, started_at: datetime, reason=None) -> None:
    async with AsyncSessionLocal() as db:
        await SyncRunRepository(db).record(
            sync_type=REBUILD_SYNC_TYPE, status=status, message=message,
            details={"reason": reason} if reason else None, started_at=started_at,
        )


async def probe() -> int:
    if not is_configured():
        print("CoinStats is not configured.", file=sys.stderr)
        return 1
    async with CoinStatsClient() as client:
        try:
            body = await client.portfolio_transactions_page(1)
        except CoinStatsError as e:
            print(f"CoinStats refused ({e.reason}): {e}", file=sys.stderr)
            return 2
        print("\n".join(describe_page(body)))
        print(f"credits spent: {client.credits_spent} "
              f"({CREDIT_COST['transactions']} per page)")
    return 0


async def run(
    dry_run: bool, reference: date, tolerance_pct: float, legs_mode: str, fees: str,
    exclude: Set[str],
) -> int:
    started_at = utcnow()
    if not is_configured():
        print("CoinStats is not configured.", file=sys.stderr)
        return 1
    try:
        async with AsyncSessionLocal() as db:
            snapshot = (await db.execute(
                select(CryptoSnapshot).order_by(CryptoSnapshot.taken_at.desc(),
                                                CryptoSnapshot.id.desc()).limit(1)
            )).scalar_one_or_none()
            if snapshot is None:
                raise RebuildRefused("no crypto snapshot yet — let one crypto sync run first")
            rows = (await db.execute(
                select(CryptoHolding).where(CryptoHolding.snapshot_id == snapshot.id)
            )).scalars().all()
            stored = (await db.execute(
                select(CryptoDailyHolding).where(
                    CryptoDailyHolding.source == HOLDINGS_FROM_SNAPSHOT
                )
            )).scalars().all()
        snapshot_days: Dict[date, Dict[str, float]] = {}
        for row in stored:
            snapshot_days.setdefault(row.date, {})[row.coin_id] = row.count
        if not snapshot_days:
            raise RebuildRefused(
                "no snapshot-sourced daily holdings yet — let one crypto sync run after "
                "the deploy first"
            )
        first_snapshot_day = min(snapshot_days)
        last_day = first_snapshot_day - timedelta(days=1)
        if last_day < reference:
            raise RebuildRefused(
                f"nothing to rebuild: the first synced day ({first_snapshot_day}) is not "
                f"after the reference date ({reference})"
            )
        anchor_day = snapshot.taken_at.date()

        anchor = {r.coin_id: r.count for r in rows}
        symbols: Dict[str, Optional[str]] = {r.coin_id: r.symbol for r in rows}
        excluded = {r.coin_id for r in rows if r.status != VALUED or r.is_fiat} | exclude

        async with CoinStatsClient() as client:
            items = await _fetch_pages(client)
            credits = client.credits_spent
        legs: List[Leg] = []
        empty = 0
        for item in items:
            found = legs_of(item, legs_mode, fees)
            if not found:
                empty += 1
            legs.extend(found)
        for leg in legs:
            symbols.setdefault(leg.coin_id, leg.symbol)
            if leg.fiat:
                excluded.add(leg.coin_id)

        days = rebuild(anchor, snapshot.taken_at, legs, reference, anchor_day)
        eligible = {c for qty in days.values() for c in qty} - excluded
        dust = check_rebuild(days, eligible, symbols, reference, tolerance_pct=tolerance_pct)

        name = lambda c: symbols.get(c) or c  # noqa: E731
        print(f"transactions: {len(items)} over {credits // CREDIT_COST['transactions']} "
              f"page(s), {len(legs)} legs, {empty} without a coin leg; {credits} credits")
        print(f"anchor: the snapshot of {snapshot.taken_at.isoformat()} UTC")
        for line in dust:
            print(f"WARNING: {line}")
        print(f"writing {reference} .. {last_day} "
              f"({(last_day - reference).days + 1} days); synced days start {first_snapshot_day}")
        print("reference basket (console only — never paste into a committed file):")
        for coin in sorted(eligible, key=name):
            if days[reference].get(coin, 0.0) > EPSILON:
                print(f"  {name(coin)}: {days[reference][coin]:.8g}")
        print("reconstruction against the days a sync stored (should agree):")
        for day in sorted(d for d in snapshot_days if reference <= d <= anchor_day):
            off = [
                name(c) for c in sorted(eligible | set(snapshot_days[day]), key=name)
                if not math.isclose(max(days[day].get(c, 0.0), 0.0),
                                    snapshot_days[day].get(c, 0.0),
                                    rel_tol=tolerance_pct / 100, abs_tol=EPSILON)
            ]
            print(f"  {day}: " + ("agrees" if not off else f"differs for {', '.join(off)}"))

        sets: Dict[date, Dict[str, Tuple[Optional[str], float]]] = {}
        day = reference
        while day <= last_day:
            sets[day] = {
                c: (symbols.get(c), n) for c, n in days[day].items()
                if c in eligible and n > EPSILON
            }
            day += timedelta(days=1)

        if dry_run:
            print("DRY RUN - nothing written.")
            return 0

        async def write() -> None:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(CryptoDailyHolding).where(
                    CryptoDailyHolding.source == HOLDINGS_FROM_TRANSACTIONS
                ))
                await db.execute(delete(CryptoDailyHolding).where(
                    CryptoDailyHolding.date >= reference, CryptoDailyHolding.date <= last_day
                ))
                db.add_all(
                    CryptoDailyHolding(date=d, coin_id=c, symbol=s, count=n,
                                       source=HOLDINGS_FROM_TRANSACTIONS)
                    for d, coins in sets.items() for c, (s, n) in coins.items()
                )
                await db.commit()

        await _retry_once_if_locked(write)
        print(f"Wrote {sum(len(s) for s in sets.values())} holdings rows.")
    except (RebuildRefused, CoinStatsError) as e:
        print(f"REFUSED - nothing was written: {e}", file=sys.stderr)
        if not dry_run:
            await _record("error", f"Crypto holdings rebuild refused: {e}"[:500], started_at,
                          getattr(e, "reason", "refused"))
        return 1

    fetched = await CryptoSyncService().refresh_prices(utcnow().date())
    print(f"CoinGecko: {len(fetched.prices)} price rows over {fetched.calls} call(s).")
    for warning in fetched.warnings:
        print(f"WARNING: {warning}")
    await _record(
        "success",
        f"Crypto holdings rebuilt from {reference.isoformat()} to {last_day.isoformat()}",
        started_at,
    )
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", action="store_true",
                        help="Print the first page's structure and stop (no write)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Rebuild and check everything, write nothing")
    parser.add_argument("--reference", type=date.fromisoformat, default=REFERENCE_DATE,
                        help=f"The trusted reference day (default {REFERENCE_DATE})")
    parser.add_argument("--tolerance-pct", type=float, default=TOLERANCE_PCT,
                        help="How far the trusted window's baskets may differ")
    parser.add_argument("--legs", choices=("transfers", "coindata"), default="transfers",
                        help="Read quantities from transfers[].items[] or coinData.count")
    parser.add_argument("--fees", choices=("ignore", "subtract"), default="ignore",
                        help="Whether fee.count is taken out separately")
    parser.add_argument("--exclude", action="append", default=[], metavar="COIN_ID",
                        help="A CoinStats coin id never to write (repeatable)")
    args = parser.parse_args()
    if args.probe:
        return asyncio.run(probe())
    return asyncio.run(run(args.dry_run, args.reference, args.tolerance_pct, args.legs,
                           args.fees, set(args.exclude)))


if __name__ == "__main__":
    raise SystemExit(main())
