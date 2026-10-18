"""
Ask CoinStats each question the crypto sync depends on, once, and print the *shape* of
every answer — never a value.

    python -m app.cli.coinstats_probe

Why it exists: the crypto tables were designed from CoinStats' documentation, and a few
things the documentation does not settle decide how the numbers must be read — whether
`totalValue` covers DeFi positions and flagged spam, how many coin pages a real portfolio
needs, what resolution the value chart has, and whether the P&L history's points are
running totals or per-day amounts. Measured beats assumed; this measures.

What it prints is structure: key names, types, counts, date spans, ratios. **No amount,
no symbol, no address** — this repository is public and its docs carry no figures, so
nothing here should be pasteable into a file that could be committed. Credit balances are
printed; they describe the API plan, not the portfolio.

Every figure is read with the sync's own readers (`coinstats_parse`), so what it
measures is what the sync would see.

Read-only: it writes nothing and records no `sync_runs` row (like `fetch_etf_baskets`,
the other read-only CLI). Cost: about 53 credits (value 10, coins 8 per page, chart 10,
P&L history 25).
"""
import asyncio
import statistics
from datetime import datetime
from typing import List

from app.services.coinstats_client import (
    COINS_PAGE_LIMIT,
    CoinStatsClient,
    CoinStatsError,
    is_configured,
)
from app.services.coinstats_parse import num, shape, to_utc_datetime, usd


def _spacing(dates: List[datetime]) -> str:
    if len(dates) < 2:
        return "n/a"
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(dates, dates[1:])]
    return f"median {statistics.median(gaps):.1f} h, max {max(gaps):.1f} h"


async def probe() -> int:
    if not is_configured():
        print("CoinStats is not configured: set COIN_STATS_API_KEY and "
              "COIN_STATS_SHARE_TOKEN in backend/.env.")
        return 1

    async with CoinStatsClient() as client:
        try:
            before = await client.credits()
            print(f"credits: plan={before.get('subscription')!r} "
                  f"remaining={before.get('remainingCredits')} of {before.get('totalCredits')}")

            value = await client.portfolio_value()
            print(f"\n/portfolio/value shape: {shape(value)}")
            total = num(value.get("totalValue"))
            defi = num(value.get("defiValue"))
            print(f"  defiValue present: {value.get('defiValue') is not None}; "
                  f"a number: {defi is not None}; nonzero: {bool(defi)}")

            coins = await client.portfolio_coins()
            pages = client.coin_pages
            print(f"\n/portfolio/coins: {len(coins)} holdings over {pages} page(s) of "
                  f"{COINS_PAGE_LIMIT}")
            if coins:
                print(f"  item shape: {shape(coins[0])}")
            fake = sum(1 for c in coins if (c.get("coin") or {}).get("isFake"))
            fiat = sum(1 for c in coins if (c.get("coin") or {}).get("isFiat"))
            no_price = sum(1 for c in coins if not usd(c.get("price")))
            print(f"  isFake: {fake}  isFiat: {fiat}  without a USD price: {no_price}")

            def worth(items):
                return sum((num(c.get("count")) or 0) * (usd(c.get("price")) or 0) for c in items)

            real = [c for c in coins if not (c.get("coin") or {}).get("isFake")]
            if total:
                print(f"  sum(count*price) / totalValue, all coins:   {worth(coins) / total:.4f}")
                print(f"  sum(count*price) / totalValue, spam removed: {worth(real) / total:.4f}")
                if defi is not None:
                    print(f"  (sum spam-free + defiValue) / totalValue:   "
                          f"{(worth(real) + defi) / total:.4f}")

            chart = await client.portfolio_chart()
            rows = [r for r in chart if isinstance(r, list) and r]
            dates = sorted(d for d in (to_utc_datetime(r[0]) for r in rows) if d)
            print(f"\n/portfolio/chart?type=all: {len(chart)} rows; row length "
                  f"{sorted({len(r) for r in rows})}; timestamp type "
                  f"{type(rows[0][0]).__name__ if rows else 'n/a'}")
            if dates:
                print(f"  span {dates[0].date()} -> {dates[-1].date()}; spacing {_spacing(dates)}")

            pl = await client.portfolio_pl_history()
            points = [p for p in pl.get("result", []) if isinstance(p, dict)]
            print(f"\n/portfolio/pl/history: {len(points)} points; "
                  f"meta={ {k: v for k, v in (pl.get('meta') or {}).items()} }")
            if points:
                print(f"  point shape: {shape(points[0])}")
                pl_dates = sorted(d for d in (to_utc_datetime(p.get('date')) for p in points) if d)
                if pl_dates:
                    print(f"  span {pl_dates[0].date()} -> {pl_dates[-1].date()}; "
                          f"spacing {_spacing(pl_dates)}")
                values = [v for v in (num(p.get("profitLoss")) for p in points)
                          if v is not None]
                if len(values) > 2:
                    steps = [abs(b - a) for a, b in zip(values, values[1:])]
                    level = statistics.mean(abs(v) for v in values) or 1.0
                    # A running total moves by a small fraction of its level each day; a
                    # per-day amount moves by about its own size.
                    print(f"  mean |step| / mean |value|: {statistics.mean(steps) / level:.3f} "
                          f"(<<1 suggests running totals, ~1 per-period amounts)")

            after = await client.credits()
            print(f"\ncredits spent by this probe (documented costs): {client.credits_spent}; "
                  f"by the balance: "
                  f"{(before.get('remainingCredits') or 0) - (after.get('remainingCredits') or 0)}")
        except CoinStatsError as e:
            print(f"\nCoinStats refused ({e.reason}): {e}")
            return 2
    return 0


def main() -> int:
    return asyncio.run(probe())


if __name__ == "__main__":
    raise SystemExit(main())
