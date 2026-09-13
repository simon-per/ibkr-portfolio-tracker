# Performance analytics — where the return came from

> Added 2026-09-13 with the Analytics tab. **This file is authoritative for
> `performance_analytics_service.py`, `/api/performance/*`, `AnalyticsTab.tsx`,
> `lib/rollingRisk.ts` and `lib/returnDecomposition.ts`.** Update it in the same change
> that moves the code it describes.

## What it answers, and from where

Three server-side questions and two client-side ones, all from tables that already exist.
**Nothing here can reach Yahoo or IBKR** — the three routes are pure database reads, and the
tab only asks the benchmark endpoint for a benchmark the Performance tab has already
selected (a cache miss there is a Yahoo request, and this tab must never be the first to
make it).

| question | where | inputs |
|---|---|---|
| Return decomposition — what the change in Total Value was made of | `GET /api/performance/decomposition` | `attribution_rows`, the timeline, money-in legs, IBKR dividend receipts, `CashService.measured_corrections` |
| Segment attribution — which sectors and countries made it | `GET /api/performance/segments` | `attribution_rows`, `securities.sector/country`, the stored ETF baskets |
| Closed positions — realized, holding period, post-sale move | `GET /api/performance/closed-positions` | `realized_rows_from_closed_lots`, SELL trades, `market_prices` |
| Rolling Sharpe / volatility / beta | `lib/rollingRisk.ts` | the value series the chart already fetched, plus the selected benchmark |
| Drawdown ledger | `lib/rollingRisk.ts` | the same |

## One loop over the lots — `PortfolioService.attribution_rows`

The per-security window arithmetic behind `/api/portfolio/attribution` was extracted into
`attribution_rows(start, end)` and **both** the attribution endpoint and the analytics
service read it. This is the load-bearing decision: the stacked bar on the Analytics tab and
the per-security bar chart on the Performance tab must agree about every gain to the cent,
and a second loop over the same lots is how such figures stop agreeing
([failure-modes.md](failure-modes.md)). If the close-date convention or the disposal window
ever moves, it moves in one place.

It returns Decimals in **both** the base currency and each security's own price currency,
with the same rules the presenter always had: a lot sold on D is not held at D's close, the
disposal window is `(start, end]`, and a security held at an endpoint that cannot be valued
there is in `unpriced` and **excluded from both sides**.

## The price / FX split — a convention, stated

```
price_effect = local-currency gain × (price currency → base rate at the window END)
fx_effect    = base-currency gain − price_effect
```

So *price* answers "what did the holdings earn in their own currency, worth today" and *FX*
is what the base currency's move against them added or took away. It is one of several
defensible conventions; this one makes a security that did not move in its own currency
contribute a price effect of **exactly zero**, which is what a chart viewer expects, and it
gives EUR holdings a non-zero FX leg under a CHF base — correct, since the franc moved.

The local leg is **`None`, never 0**, when any of its inputs is missing: a lot booked in a
currency other than the price currency with no rate on its open date, a closed lot whose
price history is in a different currency (a repair state), or no rate for the price currency
at the window end. Such a security's gain is carried whole as `unsplit_eur`, counted in
`unsplit_securities`, and named in `warnings[]`. A zero there would claim the whole gain was
FX.

## The decomposition identity

Per window, on the **rounded** figures the response carries:

```
end_total_value − start_total_value
  = net_flows + price + fx + unsplit + dividends + cash_adjustment + unexplained
```

`unexplained_eur` is computed last as the remainder, so the identity holds by construction
(the `fund_residual_eur` rule), and it is a named leg rather than something folded into
another. What lands in it, in order of size on this account: **in-kind transfers** (a lot
arrives with a cost and no cash left), **trading commissions** (the lot carries proceeds at
market while the cash received less), and the gap between a sale's real proceeds and the
market-price approximation its lot carries. When cash is not tracked at all
(`cash_source: unknown`) every sale's proceeds land here too, because they leave the total
value with nowhere to go — and the warning says so rather than letting a large negative
remainder read as a loss.

The other legs:

- **`net_flows_eur`** — `money_in_legs` inside `(start, end]`, i.e. the same splice the
  Money In line uses ([cash-contributions-benchmark.md](cash-contributions-benchmark.md)).
  A flow, not a return; the waterfall draws it grey.
- **`dividends_eur`** — `DividendService.ibkr_cash_receipts()` in the window, converted at
  the pay date. IBKR rows only: an estimate is a guess about cash another broker received.
- **`cash_adjustment_eur`** — `CashService.measured_corrections()` in the window. Each
  correction is `measured − derived-so-far`: whatever IBKR's own balance says that the three
  ledgers do not record — broker interest, account fees, FX on idle cash, **and any gap in the
  ledgers themselves**. It was first shipped as "Fees & interest" and renamed the same day,
  because on this account it read **+349 CHF** for the year: IBKR holds *more* than the
  ledgers explain (the +289 step of 2026-08-25 is most of it), so the label claimed a fee bill
  that did not exist. The card says *positive = more cash than the ledgers explain*. `None`
  when no measured row exists.
- **`gain_pct`** — Modified Dietz, `gain / (start + flows/2)`, `None` when the denominator is
  not positive (a window that starts from nothing has no base).

`start_total_value_eur` and `end_total_value_eur` come off the **timeline** at the effective
dates, not from a separate valuation, so the tab's endpoints are the chart's own points. The
year rows use one timeline over the whole span and the window `(31 Dec Y−1, 31 Dec Y]`, with
`partial` set for the current year and for the year the account began.

## Segments — an approximation, badged

A direct holding lands on `securities.sector` (normalised through `sector_taxonomy`) and
`securities.country`; both are written only by the allocation sync, so NULL reads as
**Unknown** and the warning names it. A fund's gain, start value and end value are spread by
its stored basket's *company* rows (`LookthroughService._counts_as_company`, the same
predicate the look-through uses), with proxies aliased the same way
(`_alias_proxied_baskets`). Weight the basket cannot place — cash, derivatives, nested funds,
zero-weight rows — stays visible as **Fund residual**; a fund with no basket at all lands
whole on **Funds without a basket**. Nothing is renormalised.

The approximation is that a fund's *current* basket describes its mix over the whole window.
`warnings[]` carries that sentence and the oldest basket date on every response; a caveat that
is not on the surface does not exist.

`start_weight_pct` beside `share_of_gain_pct` is the honest stand-in for a Brinson
allocation/selection split, which is **not built** — see *What is deliberately not here*.

## Closed positions

Realized P&L is **IBKR's own FIFO figure** summed over the security's SELL trades when any
exist (`realized_source: trade`), otherwise the market-price approximation over its closed
lots that the tax report also uses (`closed_lots`). Cost is the closed lots' cost basis in
both cases, so `return_pct` is against what was paid. Holding days are cost-weighted.

`post_sale_pct` compares two rows of the security's **own price history in its quote
currency** — the last close on or before the sale date against the newest close after it.
Base-currency proceeds cannot serve as the reference (they carry FX), and a quote in a
different currency is refused rather than compared. It is `None` without a later price, which
is the normal case for a fully sold security: the market-data sync prices open positions
only. The summary says how many were judged.

## Client-side: rolling risk and the drawdown ledger

`rollingMetrics` walks the flow-adjusted daily returns from `dailyReturnSeries` over a
trailing `ROLLING_WINDOW_DAYS` (252) window and reuses `portfolioKpis.ts`' own constants and
`meanAndStdDev`, so **the last rolling point equals the risk cards over the same window** —
`rollingRisk.test.ts` pins that against `sharpeRatio` and `annualizedVolatilityPct`. Beta
follows `betaAndCorrelation`'s pairing rule (flow-free days only, `MIN_PAIRED_RETURNS`), and
every metric is `null` below its sample floor. A range shorter than one window renders a
sentence asking for 2Y or ALL, not an empty chart.

`drawdownEpisodes` compounds the same returns into an index and records every fall deeper
than 5% from a running peak: peak, trough, recovery (or *still open*), the two durations, and
the benchmark's own move over the same peak-to-trough dates. Walking the **index** rather than
the value series is what makes a deposit not a recovery and a withdrawal not a fall — pinned.
Only returns after `firstValuedPoint` are walked, for the reason documented in
[frontend.md](frontend.md) (*A pre-inception day is measurable and zero*).

## What is deliberately not here

- **Brinson allocation / selection attribution against the benchmark.** It needs the
  benchmark's *per-sector* weights and *per-sector* returns over the window. The benchmarks
  are indices (`^GSPC`, `^IXIC`, …) with one stored price series each and no basket, so
  neither exists in the database and both would be new upstream data — which the constraint
  on this work ruled out. The segment table's *weight at start* next to *share of gain* is
  the portfolio-only half of that question. Recorded in STATUS.md, *Worth doing next*.
- **Post-sale performance for fully sold securities**, until something prices them. It would
  be one Yahoo request per closed security per day; not worth the budget by default.

## Tests

`tests/test_performance_analytics.py` (a fixture with a flat-in-USD holding, a mid-window
sale with IBKR's realized figure, an unpriced holding, a basket with a cash row, a 3a lot, a
deposit before the first purchase and a measured cash balance three francs off the derived
one; the identity on the rounded figures; the CHF-base FX leg; the wire shape in both
directions), the three routes in `tests/test_api_smoke.py`, `frontend/src/lib/rollingRisk.test.ts`,
and `frontend/src/components/AnalyticsTab.test.tsx` (dashes for absences, caveats on the
surface, the segment toggle, and that the benchmark is never requested unselected).
