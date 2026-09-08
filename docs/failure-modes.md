# The dominant failure mode — every duplication that drifted, and the lenses

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## The dominant failure mode: two implementations of one job, drifting

More bugs here have come from duplicated logic diverging than from any other cause, and the
divergences are quiet — both copies keep working, they just stop agreeing, and the number nobody
recomputes by hand is the one that is wrong. The known instances:

| what | how it diverged |
|---|---|
| `ttm_growth_from_quarterly` | the watchlist copy had a 5–7-quarter tier the fundamentals copy lacked, and they measured different things (`shape[1]` vs a row's non-NaN length) |
| `realized_rows_from_closed_lots` | the portfolio totals and the tax report disagreed until it was shared |
| `_calculate_daily_value` / `_calculate_timeline_swept` | must stay numerically identical; pinned by `tests/test_timeline_equivalence.py` |
| `_to_eur` | the tax copy was fixed to return `None` on FX failure; the **dividend copy kept storing the unconverted foreign amount**, on the ingest path, for another day |
| `_get_yahoo_ticker` | three services delegated to `MarketDataService`; **allocation had its own**, with no suffix table, no ARCA/BATS, and `None` for every non-US listing |
| PEG fallbacks | the watchlist tried forward-EPS growth before the 5-year CAGR; **fundamentals had no forward-EPS tier at all**. Extracted to `peg_ratio.py` — which then carried its own defect: its decimal-vs-percent *inference* read any growth `>= 1` as already-a-percent, but `stockTrend` is a fraction that exceeds 1 above 100% growth, so 222% became 2.2245% and a PEG of 0.25 became **24.56**. The forward-EPS tier now passes `is_fraction=True`; the long-term tier still infers, because there the convention really is unknown |
| `_safe_float` | the watchlist rounded to 4dp, fundamentals did not — the same P/E read differently on two screens |
| `sync_stale_*` | **both** took only "stale" in the end. Analyst ratings was fixed first, citing fundamentals as the sibling that already unioned "missing" with "stale" — but fundamentals' union sat one call *below* a pre-filter that bailed on an empty stale list, so a security with no row could never bootstrap there either. A correct fix justified by a false reading of the code it copied; when citing a sibling as right, read its **entry point** |
| the market-data securities loop | the scheduled job gained a Yahoo rate-limit breaker on 2026-08-04; **`POST /api/market-data/sync` kept its own copy without one**, so the *public* path went on asking after a 429. Extracted to `MarketDataService.sync_securities` |
| the KPI card | sixteen hand-written copies across three files, each with its own idea of what an absent value looks like (`—` in one file, `N/A` in another) — so making the values responsive was sixteen mechanical edits. Extracted to `ui/KpiCard.tsx` |
| `yield_on_cost_pct` | the Dividends-tab column divided **trailing** income by cost while the Performance card divided the **forward** projection by it. One name, two quantities, two screens — and the column's version broke whenever a position changed size, understating nine of fifteen rows |
| "stale" fundamentals | three definitions: the repository defaulted to **7** days, the sync passed **1**, and `/api/fundamentals/status` ran its own hardcoded 7-day query — so the status endpoint could report `stale_metrics: 0` beside a sync about to refresh every row. One `STALE_AFTER_DAYS`, and `/status` now counts through the repository |
| the dividend reader's two rules | `ActivityService._dividends` adopted the income test and not the era splice, so the ledger listed the same dividend from both sources and overstated income 72%. **Partial** alignment is the nastiest variant: its own docstring cites the readers, so it reads as deliberate rather than forgotten |
| the Yahoo rate-limit breaker | `market_data_service` latched on a 429 and abandoned the pass; the **five other** services importing `yfinance` — fundamentals, ratings, watchlist, allocation, dividends — plus the benchmark warm-up all caught, logged and asked again seconds later. Extracted to `yahoo_rate_limit.is_rate_limit`, with an AST test over every module that imports `yfinance` |
| `_to_eur`'s third site | after the tax copy and then the dividend copy were both fixed to return `None` on FX failure, `compute_dividend_income` **still had the original `gross_eur = gross_amount  # fallback: store unconverted`** — a few dozen lines below the helper it never called. Fixing a helper is not fixing the file; grep the *pattern*, not the function |
| the three allocation charts | one function buckets each holding three times, and only asset type used `or 'Unknown'`. Sector and geography used `if security.sector:` / `if security.country:` and **dropped** the holding, so those two summed to under 100% while the UI printed every slice as "% of portfolio". Not one module copied into another — three adjacent call sites of the same helper, one of which got the rule |
| "is this holding a fund?" | the same three call sites again, a rule later. Sector and geography asked a live look-through-table lookup needing no sync, while the asset-type chart read `securities.asset_type`, which **only** `POST /api/allocation/sync` writes and nothing schedules. `sync_helper` never writes it, so an IBKR-ingested fund keeps the `"Stock"` column default indefinitely and was drawn as a Stock in one chart while being distributed across eleven sectors as an ETF a few pixels below. Two sources for one predicate, one of them needing a manual step the other doesn't; the table wins now |
| "is this holding a fund?", **the third time** | the same three call sites, a *third* rule later — and this one had drifted from a different module entirely: they asked the table by **ticker** while `lookthrough_service` asked it by **ISIN**, so one app answered one question two ways. Found 2026-08-17 by asking which other code publishes the same predicate, not by reading either file. Nothing was wrong on this account, because every held fund's symbol happened to be unique — the failure needs a colliding ticker, and then it either hands the UCITS `SMH` the US fund's split or spreads a plain stock across eleven sectors. All three now share one `allocation_for_fund_isin`, resolved once per holding, and `test_fundness_predicate.py` fails any service that asks by ticker. **Three visits to three call sites is the lesson**: adjacent call sites of one helper are where this codebase's rules go to diverge, and each visit fixed the rule it came for and left the next one |
| the 12-month deployment average | `ContributionsStrip` renders the server's `avg_deployed_per_month_eur`; `MonthlyDeploymentCard`, on the same tab a few hundred pixels below, recomputed it as `monthly.slice(-12)` divided by its own length. `monthly` omits months with no activity, so that takes the last twelve *rows* — which can span more than twelve months — and divides by a count smaller than the period covered. Both errors push it up. **Two numbers under one name on one screen** is the cheapest instance of this failure to find and the easiest to leave: neither is obviously wrong on its own |
| which months exist | the same two components, one rule later — and this one is an *omission* rather than a disagreement. `_contribution_inputs` publishes one event list; the value chart's `money_in_running` steps every leg on it, while `get_contributions`' monthly aggregator keyed its output on months with **tax-lot** activity. A month carrying a deposit and no purchase therefore had no row: absent from a chart of contributions, and `Σ monthly != windows['all']` with nothing comparing them. Two readers of one list that agree about every value and disagree about the *index* — which no assertion over the values can see, and which is why the fix shipped with a sum identity rather than a spot check |
| `isUnpriced` | "can the backend value this position?" was inline in `rebalance.ts` and `currencyExposure.ts` — identical, correct, each carrying its own copy of the reasoning — and `winRate` was about to make it three. Caught **while writing the third copy**, which is the only cheap moment to catch one. Note what makes it more than tidiness: the two existing copies had already needed correcting *together* on 2026-08-05, when the one-clause `market_price === null` form turned out to miss the FX case — so the drift had already happened once, in lockstep, by luck. Extracted to `positionValuation.ts`, with a family test that strips comments (the rule is *documented* in `api.ts`, deliberately) and fails naming any `lib/` module that tests the columns itself |
| `formatMarketCap` | the same T/B/M formatter existed **byte-identically** in `FundamentalsTab.tsx` and `watchlistColumns.tsx` — so one defect had to be fixed twice, and its sub-million branch carried the worst instance of the locale bug in the app: a bare `toLocaleString()` renders a market cap of 850,000 as **"850.000"** under a German runtime, reading as eight hundred fifty *thousandths*. Extracted to `lib/utils.ts` beside `formatCount`, both pinned to `en-US` like every other formatter in that file. The call sites that had grown their own formatting inline were exactly the ones that were not pinned |
| the native→base two-step | `trades` and `corporate_actions` store money in the trade's own currency with no `_eur` column, so they need native→EUR at the row's date *and then* EUR→base — and a single `BaseFx.convert()` is wrong twice over. Three copies: `ActivityService._to_base`, `PortfolioService._realized_from_trades`, and the cash balance was about to be the fourth. Two had already diverged in a way that mattered — the activity one memoized the rate, the realized one issued a query per call. Extracted to `native_amounts.NativeToBase`. Caught **while writing the fourth copy**, which is the only cheap moment |
| the forecast formula | `ForecastTab.tsx` wrote the same `PV(1+r)^t + PMT·((1+r)^t−1)/r` out **four times** — the horizon table, the scenario cards, the sampled chart series and that series' hand-copied final point — each with its own NaN guard, and two of the four had already diverged: the table's "Total Contributions" was `PMT × months` while the chart band of the **same name** was `market value + PMT × months`. Neither was money in, and the chart's included every gain the book had ever made — which is what the owner noticed. Extracted to `lib/forecast.ts` on 2026-09-08; the family test is that a table horizon equals the series point at the same month |

**The lens that finds them**, and which found the last four: walk the AST for function names defined in
more than one module, ignore trivial bodies, and read each cluster. Router-to-service pairs and
per-entity repository CRUD are noise; a *service* helper appearing twice is not.

**That lens missed the market-data loop, and the reason generalises.** The two copies shared no
function *name* — `sync_market_data` on the scheduler, `_sync_market_data_locked` in the router — so a
name-keyed AST walk cannot see them, and "router-to-service pairs are noise" actively argues for
skipping it. What gave it away was behavioural: a route and a job that both loop every security and
both call Yahoo must agree about *when to stop*. So also ask which paths reach the same **upstream**,
not only which share a name.

**The last two instances were invisible to both lenses, and suggest a third.** Neither shared a
function name, and neither reached an upstream: `yield_on_cost_pct` was one *name* computed two ways in
two files, and `ActivityService._dividends` was a reader that applied one of another reader's two
rules. What would have found both is asking, of every figure and every table, **which other code reads
the same rows or publishes the same name — and does it apply the same rules?** Both were found by
reading a screen and disbelieving a number, which is the lens of last resort. Note the tell in the
second: its docstring said it matched "the same test the two dividend readers use", singular. A comment
claiming alignment with one rule is evidence worth checking for the others.

**A correct copy is still a copy, and that is the sharpest form of the rule.** The ledger's inline
era-splice comparison agreed with the helper exactly — for two days, until the helper learned to drop
the boundary duplicate and the ledger did not. Its inline `_net_eur` and `_is_income` equivalents
agreed to the digit too, and were replaced for the same reason rather than because they were wrong.
So the test to write is **"is there a copy at all"**, not "do the copies agree": agreement is what a
copy looks like right up to the moment it stops being one.
`tests/test_era_splice_boundary.py` enforces both — no service may read dividend rows without
reaching `_splice_by_era`, and none may decide the net-vs-gross fallback locally.

**When you find one, extract rather than sync the copies** — that is what `ttm_growth.py`,
`peg_ratio.py` and `safe_numbers.py` are — and write the test against the **family** ("every service
resolving a ticker agrees with the price path") rather than the instance, so the next service to roll
its own is caught the same way.

**The lesson is not "never duplicate", though.** `lib/dividendGrowth.ts` deliberately reimplements the
server's year-over-year arithmetic — it cannot be extracted, since it lives across the language
boundary — and it has **not** drifted: adjacency, the zero-base refusal, the 1-decimal rounding and
the `yoy_vs_partial` gating all still match `DividendService._pct` and the annual-row loop exactly.
The difference is that both ends *write the rules down*: the client's docstring names the two it
copies, and the server's comment says why adjacency matters. A duplicate survives when the reasoning
travels with it; the eight above all lost their reasoning on one side. (Its one divergence is
invisible: Python rounds halves to even and JS rounds them up, so a growth landing exactly on a
half-tenth differs by 0.1 pp — and the two are never on screen together, because the client value
*replaces* the server's when the Forecast toggle is off.)

---
