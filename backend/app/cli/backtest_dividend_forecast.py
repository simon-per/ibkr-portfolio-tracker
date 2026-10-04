"""
Replay the dividend forecast against what was actually paid.

For every security with a Yahoo per-share series, and for each month-end ``as_of`` in
the stored history that has a full year of payments after it: give the forecaster only
the payments that went ex on or before ``as_of``, project the next twelve months per
share with every sizing rule in `dividend_forecast.SIZING_METHODS`, and compare the
total with what the security actually paid per share over those twelve months.

The point is to choose the sizing rule on measured error rather than on argument —
the flat median it replaced read low on seasonal funds and on every raiser, and a
replacement should prove it does better on THIS book's history.

- **One implementation.** Every method runs through `project_dividends`; this file
  only feeds it and scores it. A copy of the rules here would be scoring a copy.
- **Per share, gross, native currency.** Yahoo's `amount_per_share` is all three, so
  no exchange rate or withholding enters the comparison — those move the money, not
  the accuracy of the schedule and the size.
- **Specials are scored out.** A special dividend is excluded from the forecast by
  design, so the realized side excludes the payments `special_payments` flags over the
  full history, and lists them, so "did my holdings ever pay one?" is answered too.
- **Read-only and offline.** No Yahoo, no Flex, no writes, no `sync_runs` row.

    python -m app.cli.backtest_dividend_forecast
    python -m app.cli.backtest_dividend_forecast --per-security
"""
import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.dividend_payment import DividendPayment
from app.models.security import Security
from app.services.dividend_forecast import (
    SIZING_METHODS,
    HistPayment,
    infer_gap_days,
    project_dividends,
    special_payments,
)

HORIZON_DAYS = 365


def _month_ends(start: date, end: date) -> List[date]:
    out = []
    y, m = start.year, start.month
    while True:
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        d = nxt - timedelta(days=1)
        if d > end:
            return out
        if d >= start:
            out.append(d)
        y, m = nxt.year, nxt.month


def score_security(
    history: List[HistPayment],
    first_as_of: Optional[date] = None,
    last_as_of: Optional[date] = None,
) -> Tuple[Dict[str, Tuple[Decimal, Decimal, Decimal]], List[HistPayment], int]:
    """
    ``({method: (predicted, actual, abs_error)}, specials, points)`` for one security.

    Pure, so the scoring itself is unit-tested. ``as_of`` runs from one year after the
    first payment (no rule can repeat a year it has not seen) to a year before the
    last (the realized side needs the whole twelve months).
    """
    history = sorted(history, key=lambda p: p.on_date)
    gap = infer_gap_days([p.on_date for p in history])
    if gap is None:
        return {}, [], 0
    specials = special_payments(history, gap)
    special_ids = {id(p) for p in specials}

    start = first_as_of or (history[0].on_date + timedelta(days=HORIZON_DAYS))
    end = last_as_of or (history[-1].on_date - timedelta(days=HORIZON_DAYS))
    totals: Dict[str, List[Decimal]] = defaultdict(lambda: [Decimal(0)] * 3)
    points = 0
    for as_of in _month_ends(start, end):
        known = [p for p in history if p.on_date <= as_of]
        horizon_end = as_of + timedelta(days=HORIZON_DAYS)
        actual = sum(
            (p.per_share_eur for p in history
             if as_of < p.on_date <= horizon_end and id(p) not in special_ids
             and p.per_share_eur is not None),
            Decimal("0"),
        )
        points += 1
        for method in SIZING_METHODS:
            predicted = sum(
                (fp.net_eur for fp in project_dividends(
                    known, Decimal("1"), as_of + timedelta(days=1), horizon_end,
                    as_of=as_of, sizing=method,
                )),
                Decimal("0"),
            )
            t = totals[method]
            t[0] += predicted
            t[1] += actual
            t[2] += abs(predicted - actual)
    return {m: tuple(v) for m, v in totals.items()}, specials, points


def _pct(num: Decimal, den: Decimal) -> str:
    return f"{float(num / den * 100):+7.1f}%" if den > 0 else "    n/a"


async def run(per_security: bool, held_only: bool) -> int:
    async with AsyncSessionLocal() as db:
        securities = {s.id: s for s in (await db.execute(select(Security))).scalars()}
        rows = (await db.execute(
            select(DividendPayment).where(DividendPayment.amount_per_share.isnot(None))
        )).scalars().all()
        held: Optional[set] = None
        if held_only:
            from app.models.taxlot import TaxLot
            held = {
                tl.security_id for tl in (await db.execute(
                    select(TaxLot).where(TaxLot.close_date.is_(None))
                )).scalars()
            }

    series: Dict[int, List[HistPayment]] = defaultdict(list)
    for r in rows:
        if r.amount_per_share and r.amount_per_share > 0 and r.ex_date:
            series[r.security_id].append(
                HistPayment(on_date=r.ex_date, per_share_eur=r.amount_per_share)
            )

    overall: Dict[str, List[Decimal]] = defaultdict(lambda: [Decimal(0)] * 3)
    # Equal weight per security for the bias, so one large-amount payer (a KRW or TWD
    # per-share figure) cannot decide the verdict on its own.
    bias_by_method: Dict[str, List[float]] = defaultdict(list)
    lines = []
    all_specials = []
    scored = 0
    for sid, hist in sorted(series.items(), key=lambda kv: securities[kv[0]].symbol):
        if held is not None and sid not in held:
            continue
        result, specials, points = score_security(hist)
        sym = securities[sid].symbol
        all_specials.extend((sym, p) for p in specials)
        if not points:
            continue
        scored += 1
        cells = []
        for method in SIZING_METHODS:
            pred, act, err = result[method]
            if act > 0:
                bias_by_method[method].append(float(pred / act - 1))
            for i, v in enumerate((pred, act, err)):
                overall[method][i] += v / act if act > 0 else Decimal(0)
            cells.append(f"{_pct(pred - act, act)} /{_pct(err, act).strip():>7}")
        lines.append(f"{sym:<12} {points:>4}  " + "  ".join(cells))

    if not scored:
        print("Nothing to score: no security has a year of Yahoo per-share history "
              "with a full year after it.")
        return 1

    print(f"Replayed {scored} securities, month-end by month-end, 12 months ahead.")
    print("bias = predicted vs paid (negative reads low); error = mean absolute error.\n")
    print(f"{'method':<24} {'mean bias':>10} {'median bias':>12} {'abs error':>10}")
    for method in SIZING_METHODS:
        biases = bias_by_method[method]
        mid = median(biases) if biases else 0.0
        mean = sum(biases) / len(biases) if biases else 0.0
        err = overall[method][2] / scored
        print(f"{method:<24} {mean * 100:>+9.1f}% {mid * 100:>+11.1f}% "
              f"{float(err) * 100:>9.1f}%")

    if per_security:
        print(f"\n{'security':<12} {'pts':>4}  " + "  ".join(
            f"{m[:16]:>17}" for m in SIZING_METHODS))
        print("\n".join(lines))

    print(f"\nSpecial dividends found in the history: {len(all_specials)}")
    for sym, p in all_specials:
        print(f"  {sym:<12} ex {p.on_date}  {p.per_share_eur} per share")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--per-security", action="store_true",
                        help="also print bias / error per security and method")
    parser.add_argument("--all", action="store_true",
                        help="score every security with a series, not only those held")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.per_security, held_only=not args.all))


if __name__ == "__main__":
    sys.exit(main())
