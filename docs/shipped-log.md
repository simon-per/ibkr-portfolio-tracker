# Shipped log

> STATUS.md's former *Shipped* / *Ran on production* sections, moved here verbatim on
> 2026-09-08 so STATUS.md can be the snapshot its own rules describe. Newest first, as they
> stood in STATUS.md. **Append-only**: a new *Shipped* entry is written here, not in STATUS.md,
> and STATUS.md's *Watch after the next deploy* carries only what is still unverified. Each
> entry records what shipped, why, and what was verified on production — the durable rules it
> established live in `docs/<subsystem>.md`.

## Picking this up cold — the 2026-08-08 handoff

Check `/health`'s commit against `git rev-parse origin/main` before assuming a symptom is unfixed.

Four threads are genuinely open. In descending order of what they cost:

1. **The 18:00 Berlin IBKR slot is proven, and this thread is closing** → *Watching*, first entry.
   Nine ET days measured on 2026-08-17: 12:00 ET is **7 of 9**, and the one miss was recovered by
   the 00:00 Berlin slot the same ET day. The old "mid-session is fatal" reading was confounded —
   13:00 and 20:00 Berlin also ran *after* an earlier slot had spent the day's generation.
2. **The `full_sync` decoupling is deployed but still unobserved** → *Watching*, `full_sync` entry.
   It only shows on a run where IBKR refuses, which the guard now makes rarer.
3. **The Flex window is whatever the portal is set to, and nothing here may assert a number**
   → *Watching*, Flex period entry, and CLAUDE.md's *The Flex Query* for the arithmetic. Measured
   30 calendar days on 2026-08-24, which is weeks of margin; this file said 3 for a month while
   the alarm derived from it cried wolf. Read `data_to - data_from` off a successful sync.
4. **Two credentials are knowingly unrotated, both by owner decision** → *Needs a human*. Neither is
   a task, neither may be re-litigated, and both transcript exposures of `API_ADMIN_TOKEN` were
   likewise accepted. **There is no open security item.**

The durable half of these findings is in **CLAUDE.md**, not here: the once-per-day rule, the guard
that now enforces it, the `whenGenerated`-is-Eastern rule and why 18:00 Berlin was chosen are all
under *Sync schedule* / *The Flex Query*. This file carries only what is perishable about them.

## Shipped 2026-09-12 (late) — SendRequest is one HTTP request, whatever ibflex would do

The sync/ops hunt's late correction, and the most serious finding of the day. It had listed
"no path can issue two SendRequests for one slot" as verified against the application's code,
then read the installed `ibflex 0.15` source and retracted it: `client.request_statement`
delegates to `client.submit_request`, which catches `requests.exceptions.Timeout` and **re-sends
the same GET up to three times** with 5 s, 10 s and 15 s ceilings. On the request step a re-send
is a new statement generation — what `Code=1025` counts — and a *read* timeout is not "never
reached IBKR" but IBKR taking more than five seconds to answer a request it already accepted.
Our outer handler then retried `RequestException` (`Timeout` is one) on exactly the opposite
assumption, so one scheduled slot could issue up to twelve SendRequests in ~19 minutes while
`_download_statement`'s docstring said "once" and `test_flex_retry_policy.py` pinned only our own
loop. The line-62 comment had even named the inner 3-try loop. The re-send is a bare `print()`,
invisible under any logger.

Shipped: `send_flex_request` issues **one GET** (`_SEND_REQUEST_TIMEOUT = (10, 60)`, the same
params and `user-agent: Java` as ibflex, ibflex's own `parse_stmt_response`, so callers see the
same exception types); `fetch_flex_data` fails fast on a `ReadTimeout` with the `1001` reasoning
while `ConnectionError`/`ConnectTimeout` keep their retry; the poll step still uses
`client.submit_request`, where a repeat retrieves the same reference. Tests: the "SendRequest is
one HTTP request" block in `test_flex_retry_policy.py`, including an AST guard that nothing in
`ibkr_service.py` calls `request_statement`. Rule 2 in CLAUDE.md and `docs/flex-and-sync.md`
carry it. **Verified offline only**; the 18:00 Berlin `full_sync` is the first live SendRequest
through the new path — STATUS.md *Watch after the next deploy* says what to look for.

## Shipped 2026-09-12 — the bug sweep: 24 defects from three parallel hunts

Asked as "let's look for bugs or things that look wrong or not work properly and try to fix
it." Baselines first (backend 1448/1448, frontend 555/555, tsc clean, production healthy on
`a3cdee2`, every sum identity the public API exposes holding), then three read-only Explore
hunts in parallel — backend valuation, frontend, sync/scheduler/ops — with every finding
verified by reading the code and, where the API allowed, against production before it entered
the plan. 31 verified; 24 shipped in small commits with a test each; the other seven are in
STATUS.md (*Needs a human*: the rollback; *Known rough edges*: the accepted FX drifts, the
realized-P&L source switch, the 3a liquidation; *Worth doing next*: stamping a "no rating"
attempt) or dropped (recording a sync run for an unparseable Flex XML, which would have
contradicted the finpension CLI's own pinned rule that a file never read is not a database
event). Owner decisions taken in the session: fix everything in the six groups; **FX drifts
under 0.3% are accepted**; the rollback fix waits for a session with the owner present.

**Backend, in commit order.**

- **Every Yahoo step reports an abandoned pass** (`5513f05`). `sync_dividend_data` and
  `sync_benchmark_prices` both latched a 429 and broke correctly, then returned dicts with no
  `rate_limited` and no `warnings`, and neither `div_result` nor `bench_result` was passed to
  `_collect_warnings` — a pass Yahoo killed at 5 of 40 was `status: success`, indistinguishable
  from a complete one, and `securities_processed` counted everything not skipped. Both carry
  the flag and the standard warning now; the warm-up reports `benchmarks_total`;
  `sync_dividends()` hoists its two children's warnings, which is also how
  `compute_dividend_income`'s FX-skipped line first reached a reader.
- **`POST /api/dividends/sync` has the 300 s cooldown** every other bulk Yahoo route got in
  July (`a482b87`): the module flag fenced only overlapping runs, so a poller ran full passes
  back to back.
- **No Alpha Vantage fallback when Yahoo rate-limited us** (`a667bb0`): a 429 came back as the
  same empty list as an unknown ticker, spending a free-tier call and rewriting `source`.
- **The scheduled CINS/SEDOL identity pass is bounded** (`91f5b0a`): it does not cache a miss,
  which its docstring accepted "only as a manual CLI step"; the evening refresh had called it
  unbounded since 2026-09-08.
- **The finpension importer** (`6d9d2f5`): price rows insert with `ON CONFLICT DO NOTHING` —
  the Yahoo-pinned fund's provisional re-fetch rewrites a statement row's `source`, and the next
  upload's bare `db.add` on that date was an `IntegrityError` about two uploads away; and the
  shrink guard compares the file's row count with what the previous run **parsed** (read back
  from its `sync_runs` row), not with what it stored, since an FX-skipped row is parsed and never
  stored. `SYNC_TYPE` moved into the service.
- **The tax router's year ceiling is resolved per request** (`9415da1`): it was
  `date.today().year` at import, so 1 January answered 422 for the new year until a restart.
- **Credentials compare as bytes** (`0b6dd14`): Starlette decodes headers latin-1, and
  `compare_digest` raises on a non-ASCII `str`, so a malformed `X-API-Key` was a 500.
- **A forecast basis is `net` only from IBKR rows** (`3376533`, follow-up `a85aee5`): a
  yfinance estimate with shares held has `net == gross`, so its per-share figure is gross, and
  it was stamped `net`. Verified live before the fix: SK Hynix `forecast_basis: net` with zero
  IBKR payouts. The first cut dropped the estimate's EUR-derived per-share figure altogether,
  which the full suite caught (`test_dividend_estimate_purge`: a CAD payer with no rate in the
  FX dict sized nothing); the follow-up keeps the figure and moves only the label.
- **The timeline's measured-era `cash_source` is `CashService.cash_source()`'s verdict**
  (`6a8df32`): a literal `ibkr` sat on 14 tail points while the summary said `mixed`, and the
  chart — which reads the last point — dropped its caveat.
- **The contributions clamp reads the IBKR ledger only** (`ef9cbe6`): `coverage_from` is a Flex
  claim, and comparing it with any account's earliest row let a 3a row older than the claim
  disable the clamp silently. An AST test pins the `account=IBKR` keyword.

**Frontend** (one general-purpose agent on the twelve items, its diff reviewed and one defect
fixed before committing; the gates: tsc clean, 46 files / 607 tests from 555, ESLint one error
*fewer*, build fine).

- **Drawdowns and the header's period change refuse a zero-valued start.** The backend emits a
  point for every weekday in the window, including the days before the first lot, and those are
  *measurable* zeros — so `maxDrawdownPct`/`drawdownDetail` seeded from `series[0]` stayed at
  zero and printed "Never below its opening value" over a real fall (the second route to the
  08-17 bug), while Dashboard's memo published `(+0.00%)` for two undefined percentages.
  `firstValuedPoint` seeds the walk; `periodChange` is the memo extracted into `portfolioKpis.ts`
  with both percentages `number | null`. The review found the agent's version applied the
  inception pair's own return to a seed that already contained it (a −2% phantom opening dip for
  a lot bought at 100 that closed at 99, invisible to its fixture where the first close equalled
  cost); `returnsAfter` drops returns dated at or before the seed. Verified live before the fix:
  105 zero-value measurable points before 2024-05-28.
- **The value chart's tooltip drops a missing benchmark point** instead of formatting `null` as
  `0.00` (`tooltipValue`); sync timestamps go through `formatShortDateTime` (`en-US`), the last
  three unpinned locale calls.
- **The Look-through tab renders `unvaluable_positions`** as an always-visible alert above its
  KPIs, naming the symbols; it was rendered nowhere while the backend schema docstring said it
  was. The itemised staleness notes stay in the collapsed card (owner decision, 09-08).
- **The monthly-returns heatmap badges a range-truncated period.** On 1Y, last year's "YTD" was
  Sep 12–Dec 31 with no dagger: `partial` came only from the unpriced-day trim.
  `computeModifiedDietzReturn` takes `PeriodBounds` and reports `shortenedBy {unpriced, range}`
  so the footnote can name the cause; compared on the *range's* start rather than the window's
  first point, or every YTD whose 1 January fell on a weekend would badge January.
- **One rating scale for both tables** (`lib/analystRating.ts`): the watchlist `localeCompare`d
  `strong_sell` above `strong_buy`, and PositionsList scored the display spelling in a switch of
  its own. A family test sorts one fixture through both real tables. Behaviour change to know
  about: PositionsList's descending Analyst sort now leads with Strong Buy and puts unrated last
  (it led with unrated and Strong Sell).
- **EPS figures lose their hardcoded `$`**; activity rows without an `ib_key` key on
  kind-date-symbol-index rather than colliding; the allocation drill-down no longer closes itself
  on Dashboard's 60-second re-render (`mergeAllocation` memoised); `localStorage` sits behind one
  guarded `readStored`/`writeStored` at eight sites (a source scan pins it); benchmark colours
  are assigned by the benchmark's position in the full `/benchmarks` list rather than in the
  selection, so deselecting one no longer recolours the rest.

Verified offline: the full backend suite green at the new count (1467 from 1448) and the
frontend gates above, each fix with a failing-first test.

**Verified on production, 11:22 Berlin, commit `e6e7698`** (pushed 11:11, deployed by the 11:20
cron): `/health` healthy with write auth and the persistent job store; the timeline's tail points
read `cash_source: mixed`, the same word as the summary; SK Hynix `forecast_basis: gross_estimate`
and every forecast row with a basis reads `gross_estimate` (the 21 rows that had read `net` all
drew their per-share history from yfinance — MRVL and MU included, whose IBKR rows the
"second record" rule drops), so `forward_yield.basis` is `gross_estimate` and its
`gross_estimate_eur` equals the total; a `DELETE` with a non-ASCII `X-API-Key` answers 401; the
current-year tax report answers 200. The forward-yield figure moved 322.80 → 320.92 EUR across the
deploy, −0.6% — pre-ownership estimate rows now enter the size median for securities that used to
prefer `net`, plus the FX refresh between reads; recorded in `docs/dividends.md`. Still unverified:
the dividends cooldown's 429 (needs the admin key), `benchmarks_total` on the next
`market_data_only` run (13:00 Berlin), and the finpension upsert on the next monthly upload.

## Shipped 2026-09-08 (late) — the audit batch: self-refreshing baskets, no benchmark cache, build-before-down, current deps

Asked as "look for issues and let's brainstorm", then "push it then and let's call it a day for
the benchmark, and for the look-through, can we somehow get the data automatically per API call or
similar + cache?". Four changes, one push.

- **The look-through refreshes its own baskets** (`app/services/etf_basket_refresh.py`, last
  step of the 18:00 `full_sync`). The fetchers and the parser dispatch moved out of the two CLIs
  into `app/services/etf_basket_fetch.py` so the job and the CLIs share one implementation; the
  detector `find_stale_etf_baskets` now formats `stale_basket_verdicts`, the same list the refresh
  fetches from; every CLI refusal survives (fetch error, parse failure, row-count collapse,
  backwards as-of → previous basket kept, one warning per fund, job status untouched); and identity
  follows in two bounded pieces (CINS/SEDOL after a replacement, 25 ISINs an evening otherwise).
  Deliberately **not** on the read endpoint: a public GET reaching seven third-party sites is a DoS
  vector aimed at somebody else, and it would bump the shared cooldown clock. 13 tests in
  `test_etf_basket_refresh.py`, 4 in `test_scheduler_jobs.py`.
- **`benchmark_timeline_cache` is retired** — model, four call sites, the inception path's
  read/write, and migration `u4d1f8a5b9c0` to drop the table. The inception series is walked on
  request like the window series. The anchor test that asserted "window mode never writes the
  cache" went with it.
- **`deploy.sh` builds before it takes anything down.** `npm run build -- --outDir dist.next`,
  `docker compose build`, then `down`, swap `dist`, `up`, remove `dist.old`. The old order left the
  site offline for the whole `--no-cache` build on every push. Takes effect from the *next* push,
  because the deploy that ships a `deploy.sh` change runs the old copy.
- **Backend pins moved from January 2024 to current**: fastapi 0.141.1 (starlette 1.6.0), uvicorn
  0.52.4, python-multipart 0.0.32, requests 2.34.2, httpx 0.28.1, pydantic 2.13.5, sqlalchemy
  2.0.52, alembic 1.19.2, aiosqlite 0.22.1, pytest 9.1.1, pytest-asyncio 1.4.0. OSV had 14
  advisories on the old starlette alone, including the multipart DoS, on a publicly proxied API.
  One test needed changing (the route-table walk); everything else passed unchanged.

**What the audit found and did not ship** is recorded under *Worth doing next* items 7–9 and the
HSTS bullet under *Needs a human*.

## Shipped 2026-09-08 — the Forecast tab's Money In starts at what was paid in

Asked as "the starting point shows the total portfolio value as the Total Contributions, but
gains are already baked in — should it be revised?". Yes, and three more defects sat beside it in
`ForecastTab.tsx`: the *table's* "Total Contributions" was a different quantity from the chart
band's (monthly contribution × months, no seed at all), the three table columns did not partition,
and the seed was `total_market_value_eur` — holdings only — so the Current button disagreed with
the hero card's Total Value whenever cash was held. The formula was also written out four times.

**What changed.** `lib/forecast.ts` is the one copy: `projectForecast`, `forecastSeries`,
`forecastBaseline`. The baseline is `windows['all'].money_in_eur` from
`/api/portfolio/contributions` — the query Dashboard already holds, so no new request — the seed
is Total Value where cash is tracked, and `Portfolio Value = Money In + Investment Gains` holds
on every row and point on the rounded figures. The grey band and the table column are both
**Money In**; the year-0 gap is the gain already made, and a sentence under the chart says so. A
baseline that failed to load is refused — no grey band, `—` in both columns, a notice — rather
than drawn as zero. "Start from 0" zeroes both sides. The Current button now reports today's
value whichever start is selected; it used to read "Current (CHF 0)" with 0 selected. The tab had
**no tests**; it has 22 (`lib/forecast.test.ts`, `ForecastTab.test.tsx`). Frontend suite
532 → 554; backend untouched and not re-run.

**What to check on prod after the deploy.** Open the Forecast tab: the legend reads *Money In* /
*Portfolio Value*; the grey band's first point equals the ContributionsStrip's all-time money-in
figure on the Performance tab; the Current button equals the hero card's Total Value; each table
row sums. **DONE on `1cc84b5`, 2026-09-08 19:03 Berlin**, by a throwaway Playwright run against
the live URL (the 09-06 recipe under *Shipped 2026-09-06 (late)*) at 1280 and 390: legend *Money
In* / *Portfolio Value*, the year-0 tooltip reading CHF 55,588 against CHF 73,305 — the API's
all-time money in and Total Value to the franc — the 1 Year row summing (91,839 = 24,251 +
67,588), Current button equal to the hero card, zero horizontal overflow. One trap for the next
such check: a screenshot taken straight after load catches the Recharts draw animation halfway,
and the bands appear to stop at year 5 of 10. Wait ~3 s before judging their extent, and hover
`.recharts-wrapper` rather than `.recharts-surface`, which also matches the legend's icon SVGs.

**Not done, on purpose.** The nominal/12 monthly rate and end-of-period annuity are unchanged.
No `unpriced_holdings` notice on the seed — the Performance tab's cards already carry one. The
inline Y-axis tick code was not swapped for `lib/niceTicks.ts`, whose header already says it was
extracted from this tab; that is a one-file follow-up.

**Also shipped the same evening — the Look-through notes are collapsed and last.** Asked as
"I do not want the error/warning to take up half of the page, bring the irrelevant things down
to a simple dropdown". Live, the block held ten sentences, seven of them the same staleness
line for different funds. The `role="alert"` block under the KPI row is gone; the notes render
inside the *Fund coverage* card, which is now collapsed by default via `CollapsibleCardHeader`
(the same control Monthly Returns and Performance Attribution use) with a header line that
counts what is inside ("9 funds held — 7 decomposed, 1 via another fund's basket, 1 with no
basket — 10 notes on this view"). The Coverage KPI's footnote lists every condition present
(`no basket · borrowed basket · ageing`) instead of only the most severe, so the qualifier above
the fold is complete. Tests updated: the alert assertion became "qualifier on the card, notes
reachable from the collapsed header", plus a document-order check that the fund card is last.
**Check on prod:** the Look-through tab opens straight into the KPI row, the composition bar and
the company table; the last card reads *Fund coverage* with the count line and a chevron, and
opening it shows the notes then the fund table.

## Shipped 2026-09-07 — the benchmark is anchored to the selected range

Asked for as "make the benchmark line start at the same point as my portfolio line whenever I
change the time range". The request came with the difficulty already named — a naive rebase
(shift or scale the line) is wrong the moment a deposit lands inside the window — and that is
exactly the shape shipped against: a **seeded walk** through the same `_walk` the absolute
series uses, seeded with the portfolio's Total Value on the anchor day and fed only the
contributions after it. CLAUDE.md, *The benchmark is anchored to the window*, has the rules.

**What changed on the wire.** `GET /api/portfolio/benchmark` takes `anchor=inception|window`
(default `inception`, unchanged behaviour for any caller not passing it). The response carries
`anchor`, `anchor_date`, `anchor_value_eur`, `anchor_unpriced_holdings`, and window-mode
points carry `external_flow_eur` (the contributions applied that day; `null` in inception
mode, meaning *not reported*). The chart always asks for `window`, renders a sentence under
the toggles naming the anchor day and what the line means, and folds an unpriced anchor into
its existing notice.

**Beta.** Unchanged on flow-free days — pinned by
`test_flow_free_daily_ratios_are_identical_to_the_absolute_series`. It also stops counting
deposit-only days as benchmark returns: `betaAndCorrelation` skips a day whose benchmark point
carries a non-zero *explicit* flow, and only the explicit field, never the cost-line inference
that measured the exchange rate in August.

**Verified locally on a fresh production snapshot**, not only in jsdom: Playwright at 1440 and
390 read the first point of the Total Value path and the S&P 500 path straight off the SVG on
3M, 1Y and ALL — `dy = 0.00px` all six times — and the API cross-check put the two first
values within 0.005 of each other on every range. ALL ends 0.0011% from the inception series
(the seed is the first lot at its first-day close, the old first leg was its cost). The check
cost **one Yahoo request** (`^GSPC`, the provisional refresh of today's bar), inside the two
granted for it. The suite went 1413 → 1427 backend and 524 → 532 frontend.

**What to check on prod after the deploy.** Open the chart on 3M with a benchmark selected:
the two lines must start at one point and the sentence *Benchmarks start at your portfolio's
value on …* must be there. Switch to ALL — it should look as it did before.

**Follow-up done 2026-09-08:** `benchmark_timeline_cache` was retired outright (it served only
`anchor=inception`, which nothing requests). The benchmark baseline's FX wobble (*Worth doing
next*, item 6) is inherited by the rebased `cost_basis_eur` and still not drawn.

## Shipped 2026-09-06 (late) — money in per month, which the chart had never drawn

Four commits, `35230b0..2a4817b`. Deployed **18:53 UTC**, health 200.

`monthly[]` on `/api/portfolio/contributions` carried `deployed_eur` and `net_eur` and no
money in at all, so `MonthlyDeploymentCard` could only draw the gross series. That series
counts a rotation twice **by design** — CLAUDE.md defines the gap as capital churn and
forbids netting it — and the August 2026 Ireland→US ETF switch is what made drawing it
alone untenable.

Measured on a production snapshot, in CHF:

| month | money in | deployed | what the card used to say |
|---|---|---|---|
| 2026-08 | **7,211** | 30,617 | a bar 4x any real contribution |
| 2026-09 | **0** | 3,639 | "Sep 26: CHF 3,639 deployed" — for a month nothing was paid into |

**What changed.** `money_in_eur` per month, summed from the same `money_in_legs` the strip's
windows and the value chart's daily line already use — the third reader of one splice, never
a second implementation of it. The card leads with it, drops `deployed_eur` and `net_eur` to
the tooltip, and names the largest rotation month in prose under the chart. Card title is now
**Money In per Month**, which two e2e scripts match on by name.

**Amended 2026-09-07**, so read the above as of its own date: deployed shipped here as a
*second bar* and as the strip's `/suffix`, and came out a day later at the owner's request —
it is the identical number in 20 of 29 months, and they read both surfaces for a contribution
rate. It survives in both tooltips and the strip's hover `title` as the one independent check
on money in. The note was reworded to stand without a bar beside it ("These bars count new
money only. Aug 26 is where that matters most: CHF 30,617 went into new positions that month
but only CHF 7,211 of it was new money…"). `showDeployed` is now the legacy-backend fallback
alone.

Live on `12fd7ad`, 18:21 UTC, and **verified in a browser rather than only by the suite** — the
tell that the second series is gone is the Y axis topping at **8.0k instead of ~32k**, which no
jsdom test can see because Recharts renders nothing inside a zero-size `ResponsiveContainer`.
Zero horizontal overflow at 1280 and 390. The strip reads `All time CHF 2,034 · 12M 2,769 ·
6M 2,716 · 3M 4,382` with no suffix, and the 3M window is now legible as what it is: +115%
against the all-time average.

**The second bug, found while building the first.** The series was keyed on months with
tax-lot activity, so a month carrying a deposit and no purchase had **no row** — the
contribution absent from a chart of contributions, and `Σ monthly != windows['all']` with
nothing comparing them. Latent for as long as lots came first; live since the 3a deposits
landed 2026-08-25 against purchases on 09-01. Both monthly loops now clamp at `as_of` too.

**Verified against a production snapshot** (copied down, run locally, deleted): the identity
`Σ monthly[].money_in_eur == windows['all'].money_in_eur == 55,587.74` holds across all
three readers, including the value chart's last daily point. Window averages reproduce what
the screenshots showed (all 2,036/3,070 · 12M 2,769/5,122 · 6M 2,776/7,439 · 3M 4,382/13,569),
which is what confirms the snapshot was current.

**What is deliberately unchanged.** `deployed_eur` is still gross. The strip's `/deployed`
suffix still shows it. Nothing was netted, renormalised or redefined — the owner was asked
and chose to keep it, because the gap *is* the measurement.

1413 backend + 524 frontend tests, and **the deployed page was screenshotted at 1280 and
390** — which is what found the one defect the suites structurally could not. The legend
listed the two series in the *opposite* order to the bars: declared money-in-first, Recharts
derived "Deployed, Money in" from the children while the leftmost bar of each pair was money
in, so the legend put the secondary series first. It is rendered explicitly now
(`Legend content=`, since `payload` is not in this version's props). **Recharts does not
render inside jsdom's zero-size container**, so every chart-internal property — series order,
legend, axis, tile fit — is invisible to the component tests by construction; the only way to
see one is a browser against a built page.

The check itself is worth reusing and is not `e2e/mobile.mjs` (which needs a local stack): a
throwaway Playwright script against the **live URL**, importing `playwright` by absolute path
out of `e2e/node_modules` so nothing lands in the repo, with `page.route(...).abort()` on
`/api/dividends/summary` and `/api/portfolio/benchmark` — the two GETs that can reach Yahoo
on a cache miss, and neither of which feeds this card. Zero horizontal overflow at both
widths.

## Shipped 2026-09-06 — a second account, deployed and verified

Eight commits, `a8b70fa..a6fa53a`. Deployed **17:00 UTC**, imported **17:05**. 1405 backend
+ 517 frontend tests.

**Verified on production, in this order** — the order is the point, because step 2 is the
one that would have caught a wrong migration before any data moved:

1. `/health` reports `a6fa53a`, `scheduler_jobstore_persistent: true`, `write_auth_enabled: true`.
2. **Nothing changed.** Before importing, every figure was byte-identical to the pre-deploy
   snapshot: cost basis, market value, cash, `cash_source`, positions, money in, deployed,
   Steuerwert, dividend gross, realized gain. The migration is a proven no-op on data.
3. Import: 5 rows, 2 securities, 3 deposits, 2 lots, 68 price rows, `rows_restated 0`.
4. Mapping: `CH0117044948 → 0P0000S0OD.SW`, verified at **0.05%** against the published NAV
   and flipped to `price_source=yahoo`.
5. **An IBKR statement cannot reach a 3a lot** — 750 open lots partition exactly into 748
   IBKR + 2 pillar 3a, and the set `reconcile_taxlots` deletes from contains none of the
   3a ones. Checked read-only inside the container rather than by running a sync, which
   would have spent a Flex generation to learn nothing the partition does not already say.

**The numbers, for reconciliation later:** money in 53,829.74 → **55,587.74 CHF** (+1,758.00,
exactly the deposits); market value 70,828.48 → 72,568.47; cash 24.47 → 44.49 (+20.02, the
closing balance to the cent); `cash_source` `ibkr` → **`mixed`**; positions 32 → 34;
`unpriced_holdings` 0. Tax 2026 unchanged in all three sections — Steuerwert still 70,828.48
over 32 rows, no Swisscanto row, no `CH` bucket in DA-1 — with a new Pillar 3a block
reporting **1,758.00 deductible**. Look-through partition closes to the cent at 72,568.47,
coverage 97.13%, `CH1529078078` decomposing through EMIM.

**Two things found during the deploy that were not in the plan:**

- **The cash partition was live, not latent.** STATUS said `cash_balances` was empty; it is
  not — `cash_source` read `ibkr` before the import. So `_apply_measured` would have started
  subtracting the 3a balance on the first measured day rather than at some future date. The
  partition shipped in the same batch, so it never bit.
- **A carried NAV hid the staleness it bridges** (`a6fa53a`). The importer carries the last
  NAV forward 45 days, which puts `max(market_prices.date)` in the *future*, so
  `find_stale_priced_securities` could never have asked for a newer export. Fixed to read the
  newest *observed* row, at a 40-day threshold chosen to speak before the holding drops out
  on day 59.

**Still worth watching:** the first market-data sync after this (the 20:00 Berlin slot) is
the first to run with `price_source` gating in place — it should skip `CH1529078078` and
fetch `CH0117044948`. And the next IBKR sync (18:00 Berlin tomorrow, or 00:00 tonight if
today's failed) is the first real one to run beside 3a lots; step 5 above says it is safe,
but `/api/sync/status` open-lot count should stay 750.

---

## Shipped 2026-08-26 — cash, and a rotation that stopped looking like a loss

Asked as "I sold a lot of my portfolio for a restructuring and it shows a large dip — include cash
everywhere". The dip was real in the data and false in what it implied.

**What was actually wrong, measured before anything was written.** `external_flow_eur` on 08-21 is
−25,136 and on 08-24 is +12,682, so ~12,229 CHF was undeployed. Holdings 68,342 → 43,631 → 56,161.
Two independent derivations of the missing balance agreed: summing the trade/deposit/dividend ledger
gives **12,228.74**, summing the timeline's own `external_flow_eur` from `coverage_from` gives
**12,212** — a tenth of a percent apart. So the account is worth ~68,921 and the hero card said
56,708.

**What was NOT wrong, and it changed the scope.** The risk metrics already net the flow out:
08-21 reads **+0.76%** and the year's flow-adjusted max drawdown is **−11.49%, from March**. "Huge
drawdown" was the *picture*, not the statistics. So XIRR, Modified Dietz, the heatmap, Sharpe, beta
and both drawdowns are deliberately untouched — a trade is still an external flow to them. Making
cash part of the measured pot (only deposits as flows) is the textbook definition and a real
improvement; it is left undone on purpose and is the obvious follow-up.

**The design decision that carries everything: cash is derived from *trades*, not from lot events.**
This account's holdings arrived by in-kind transfer carrying open dates back to 2024, so a lot-event
derivation reads years of pre-IBKR purchases as cash leaving an account that had not been opened. A
transferred lot has no `Trade` row, so it costs nothing, and the balance anchors at a definitional
zero rather than at a guessed one. That is also why this is *not* spliced at `coverage_from` the way
contributions are.

**The chart pairs Total Value with Money In**, which is the only pairing with no step on a trade.
The two rejected alternatives are recorded in CLAUDE.md: cash as a third line leaves the cliff, and
total-value-vs-cost-plus-cash moves the step to the baseline (+6.1k, the realized gain). `money_in_eur`
reuses `_contribution_inputs`, the same event list the contributions strip consumes, pinned equal by
`test_the_chart_and_the_strip_agree_about_money_in`.

**One extraction came out of writing it, at the cheap moment.** The native→EUR→base two-step had
three copies (`ActivityService._to_base`, `PortfolioService._realized_from_trades`, and cash was
about to be the fourth) and two had already diverged — one memoized the rate, one issued a query per
call. Now `native_amounts.NativeToBase`; new row in CLAUDE.md's divergence table.

**Scope, as chosen:** value chart, summary hero card, positions weights (denominator now holdings +
cash — it had been inflating every row by 21%), and all three allocation breakdowns as a `Cash`
bucket. Not returns, not the dividend-yield or rebalance denominators.

**Ingestion for both of IBKR's cash sections shipped too, and the Cash Report one is now live** —
the owner enabled it the same evening and the first measured row is in (see *Needs a human*).
Enabling it found two defects the fixtures could not: the base-summary row's currency, and a
round trip that moved the figure. Both below. Empty is a supported steady state, not a pending migration.
`<CashReport>` was added after the owner looked and found only that one in the Flex editor: it is
per-currency and per-period rather than daily, so it yields one anchor per sync instead of a series,
which `_apply_measured` already handles because it interleaves corrections with derived movement
rather than holding a level flat. `resolve_cash_balances` collapses both schemas onto one shape.

**The real statement found three things no fixture had.** All three were invisible until a document
IBKR actually produced went through the pipeline, which is the reusable point:

- **`<AccountInformation>` is an optional section and this query does not have it**, so the
  base-summary row arrived with no currency. Read as EUR, its 12,501.58 **CHF** would have been
  projected to ~13,400 — a 7% overstatement replacing a derived figure that was already within a
  couple of percent. The base now comes from the unanimous `toCurrency` on `<ConversionRates>`,
  which the query already emits, and an unresolvable one is dropped rather than labelled.
- **`cash_balances_seen` reported 0 for a statement that stored a row**, because the counter still
  read the Equity Summary section alone. A zero there tells an operator the portal edit did not
  take, about an edit that did.
- **A balance already in the display currency was round-tripped through EUR**, and the stored
  native→EUR and EUR→base rates are not exact inverses: 12,501.58 came back 12,502.03.

**Verified end to end against a production snapshot**, which is what this needed rather than
fixtures. Migration `s2b9d6e3f7a8` applied to the real database and round-tripped (downgrade,
re-upgrade). Backend **1306** (was 1272), frontend **512** across 38 files, `tsc -b` and
`vite build` clean, and **160 browser checks** over all eight e2e scripts — a11y 17/17, sweep 18/18,
mobile 50/50 at 390x844, errors 18/18, ledger 8/8, axis 8/8, csp 4/4, chunks 37/37.

**`e2e/ledger.mjs` had a check that could never pass, and running it was how we found out.** Its
"the Cash filter narrowed the ledger" assertion tested `/Dividend/` against the whole tab panel's
`innerText`, which carries the filter buttons ("Dividends") and the card description ("Every trade,
dividend, deposit and corporate action"). The filter works; the assertion could not. It had been
written, reviewed and never executed, because the local database has no trades or cash flows to
filter — STATUS.md said so in as many words. It reads `tbody tr` now. **A check that has never been
run is not a passing check**, and this is the second one in this repo to be written against a bug it
could not see.

## Shipped 2026-08-26 (late) — the benchmark was still selling on every rotation

Reported as "the benchmark still looks weird" after the cash work landed, and it was a
real defect rather than a mismatch of style.

The hypothetical was built from **tax lots**: a lot's `close_date` emitted `-shares`,
unwinding at the number of shares originally bought while also removing its cost — so the
gain those shares had accumulated was discarded. Measured on production, S&P 500 in CHF:
`61,654 → 38,766 → 51,680`, and it never came back. **4,193 CHF of gain destroyed by a
day on which no money left the account.** It also cliffed on a chart whose portfolio line
no longer does, so the picture read as a ~6k outperformance that was entirely artefact.

It now invests the **same `money_in_legs` the chart draws**, which is rotation-neutral by
construction. Measured on production after the cache was cleared, over the same window:
`60,770 → 61,071 → 61,141 → 61,415` across the rotation, moving with the index and nothing
else. A full year shows no discontinuity; its only two >6% day-moves are the days money
was actually contributed, which legitimately move a hypothetical.

**Two things about it are deliberately *not* claimed.** Before `coverage_from` a rotation
still moves it, because that era has no deposit ledger and lot cost basis genuinely
cannot survive one — the same limitation `get_contributions` reports as `deployed`, and
it is pinned as a test so nobody "fixes" it into a false claim. And the benchmark's
baseline is still projected differently from the portfolio's (each point's date vs each
leg's own date), a few percent apart under CHF; invisible on the chart because only the
value line is drawn, so it stays *Worth doing next* rather than being smuggled in here.

**`benchmark_timeline_cache` was cleared on production** after the deploy — 1,572 rows.
The cache is sound only for a fixed basis, so changing the arithmetic requires it, and
there is **no route or CLI for a full clear**: the scheduler only calls
`clear_cache_recent_days(7)`. That partial clear is what made the state confusing for a
while — the last seven days had been recomputed on the new basis while everything older
was still on the old one, so the cliff was gone but the series had a seam at the 7-day
boundary. Clearing it is a `docker exec` running `BenchmarkService.clear_cache()`.

## Ran on production 2026-08-26 — deployed, ingested, verified

**Deployed `b326d0a` at 19:02 UTC**, health 200, migration `s2b9d6e3f7a8` applied on the live
database. `/health` reports `scheduler_jobstore_persistent: true` and `write_auth_enabled: true`.

**One thing to know about the deploy, because it looks like a fault and is not.** The 18:50 UTC
cron tick lost a race with the push by seconds: it fetched, saw `LOCAL = REMOTE`, and exited
**silently** — that path does not log. So for ten minutes the log's last entry was two days old
while cron was demonstrably running (`/var/log/syslog` shows the 18:40 and 18:50 ticks). The 19:00
tick deployed normally. If a push ever seems not to deploy, check `syslog` for the tick and
`git rev-parse origin/main` on the VPS before assuming the script is broken.

**The statement was then ingested offline** (`ibkr_manual_xml`), because today's 18:00 Berlin sync
had run at 16:00 UTC — *before* the portal edit — so it carried no Cash Report, and the 00:00 Berlin
slot would skip on the once-per-ET-day guard. Result exactly as rehearsed against a snapshot:
743 lots synced, **0 closed, 0 partial, 0 skipped**, and `cash_balances_seen 1`. The only new fact
in the file was the cash row; everything else was a confirmatory no-op.

**Verified on the live site**: summary `total_value_eur` 69,342.79 = 56,841.21 holdings +
12,501.583565 cash to the cent, `cash_source: ibkr`, `unpriced_holdings: 0`; the timeline reads
`derived` up to 08-24 and `ibkr` from 08-25; and a browser pass found **no console errors**, the
measured split on the hero card, and the derived caveat correctly **gone** from the chart note.
The 08-21 cliff is absent from the chart.

## Shipped 2026-08-24 (late) — the last unpriced holding, and a formatter that existed twice

Work that had been sitting uncommitted in the working tree; tested end to end and shipped.

**The Positions table was the family's blind spot.** `PositionsList` rendered a holding the backend
could not value as an ordinary, very bad position — `market_value_eur` is 0.00, so
`gain_loss_percent` is exactly **−100.00**, in red, at `0.00%` weight, unmarked. What makes it the
sharpest instance rather than just another one: the KPI cards *directly above* already report
*"N unpriced, not judged"*, so the app named the condition and then contradicted it on the same
screen — and this is the screen you open precisely to find out **which** holding.

Now the three derived cells show a dash, the tone goes muted rather than red, and a `role="alert"`
above the table names the symbols and says what to check. Two details carry weight: the weight cell
is `null` rather than `0` (a `0.00%` asserts the holding is a negligible part of the book — the
`concentrationPct` lesson), and unpriced rows leave the weight *denominator*, so it describes the
same set as the rows allowed a weight. It reads `isUnpriced` from `positionValuation.ts` rather
than testing `market_price === null`, because a missing FX rate is the second route into this state
and leaves the price populated.

**`formatMarketCap` existed byte-identically in `FundamentalsTab.tsx` and `watchlistColumns.tsx`.**
CLAUDE.md's opening failure mode, so it is extracted to `lib/utils.ts` beside a new `formatCount`,
both pinned to `en-US`. The pin is the point: its sub-million branch was the worst locale bug in the
app — a market cap of 850,000 rendering as **"850.000"** under a German runtime, which does not look
malformed, it looks like eight hundred fifty thousandths. The call sites that had grown their own
formatting inline were exactly the ones that were not pinned.

**Tested end to end before committing**, which is worth recording because most sessions here stop at
the unit suites: backend **1272**, frontend **505** across 37 files, `tsc -b` and `vite build` clean,
and **144 browser checks** over six e2e scripts — `a11y` 17/17, `sweep` 18/18, `mobile` 50/50 at
390x844, `csp` 4/4, `chunks` 37/37, `errors` 18/18. Nothing needed fixing.

**Two scripts could not run that session:** `ledger.mjs` and `axis.mjs` need real executions and
the local database has **0 trades and 0 cash flows**. Both were run against a production snapshot on
2026-08-26 and are green — `ledger.mjs` needed one fix first, see *Shipped 2026-08-26*.

## Shipped 2026-08-24 — a threshold derived from a number nobody had checked

Three things, from one banner on the dashboard.

**1. The gap alarm measured nothing and asserted everything.** `FLEX_GENERATION_GAP_WARN_DAYS = 2`
was derived, correctly, as N−1 for the `Last 3 Calendar Days` period CLAUDE.md documented. The live
period is **30 days**, so the constant was wrong by 27 and the alarm fired on the first ordinary
two-day gap — with a message telling the reader trades were "about to become unreachable from every
future statement" when about four weeks of margin remained.

The fix is not a bigger constant. `flex_generation.flex_window_days()` reads `data_to - data_from`
off the last successful statement, which every run already records, and the threshold is
`max(FLOOR, N-1)`. Three details earn their place:

- **It measures over `FLEX_API_SYNC_TYPES` only.** An `ibkr_manual_xml` ingest is the documented
  *recovery* for a gap and its period is whatever the operator typed into the portal — routinely
  wider than the query. Letting one hand-widened download define the window would relax the alarm
  for every day after it. Same distinction the two type sets already existed for.
- **The floor is 2, not 1.** IBKR issues no statement at the weekend, so a Friday success is two ET
  days old by Sunday through no fault of anything; a threshold of 1 would warn every weekend.
- **The message states the window it measured** (`reaches back 30 calendar days`, or
  `window length is unknown, assuming 3` when nothing is on record), and says whether the margin is
  *remaining* or *already gone* — those are different instructions to whoever reads it.

The lesson is the familiar one in a new place: a false alarm that names a data-loss risk is worse
than no alarm, because the next true one reads identically. And a threshold hand-derived from a
number a human maintains in a comment is wrong the moment somebody edits the portal and not the file.

**2. Two funds from the 08-21 rotation were undeclared, so a Nasdaq-100 ETF was a "company".**
The rotation out of the Ireland-domiciled sleeve bought **IQQ** (iShares Nasdaq 100, US-domiciled)
and, on 08-24, **QQQM** (Invesco NASDAQ 100). Neither had an `ETF_ALLOCATIONS` or `FUND_SOURCES`
entry, and an undeclared fund is not loudly broken — it takes the `securities.asset_type` column
default of `"Stock"`. So IQQ was drawn as a company in the allocation charts and appeared in the
look-through as a single company row (`ISHRS NASDAQ 100 ETF`, sector `Unknown`, 0.22%), while
`uncovered_fund_eur` stayed `0.00` and coverage read a cheerful 99.74%. Nothing reported it.

Both are declared now, with their allocation blocks **pinned identical to XNAS's** — three wrappers
around one index, so a different split would be three answers to one question. Both verified against
the issuers' own APIs before being written down, rather than from a web lookup: QQQM's CUSIP
`46138G649` is reproduced from its ISIN by `derive_north_american_isin` and echoed back by Invesco
(109 holdings, NVDA 8.32%), and the two independently-sourced baskets agree to about a basis point
on every top holding — which is the real check that both declarations name the right funds.

**3. iShares' `locale` decides whether a fund is findable at all.** IQQ needed one adapter change:
the varnish host serves every domicile, but the default `en_GB` catalogue answers a flat
`400 BAD_REQUEST_INVALID_PARAM_VALUES` for a US-domiciled fund, while `en_US` returns all 106 rows
from the identical URL. So a US entry declares `params={"locale": "en_US"}` rather than the adapter
growing a second endpoint — the payload shape is identical and `parse_ishares` needed no branch. A
family rule keyed on the ISIN's country prefix catches the next one by declaration.

Worth knowing for later: IQQ's `portfolio_id` equals the id in its product-page URL and the UCITS
entries' do not (IWDA is `/products/251882/` against 287737). Do not generalise from IQQ.

**Also done on production that evening, none of it code:** the 08-24 portal statement was ingested
offline (a confirmatory no-op — the server already held everything through 08-21), and GRID's and
SOXQ's baskets were refreshed from 08-14 to 08-21 with `resolve_identities --constituents` after.

**Not fixed, because it is not broken:** the 08-23 and 08-24 sync failures themselves. 08-23 18:00
ET got a reference code and timed out after the full 900 s of polling (a `ReadTimeout` from IBKR);
08-24 12:00 ET was a plain `1001` at the request step. Neither re-requested, so no `1025` budget was
spent, and the portal download succeeding 25 minutes after the 12:07 refusal is the two-channel
design working. Backend suite 1272 passing.

## Shipped 2026-08-17 (night) — four more incomplete valuations, and the basket alarm

A bug sweep using this repo's own lenses: *what would this metric's stand-in value claim?*, *which
other code publishes the same name?*, and an AST sweep for functions defined in more than one module.
Four defects, all one family — **a figure built from an incomplete valuation, presented as
complete.** CLAUDE.md records that family being closed for the timeline, `/summary`,
`/attribution`, `dailyReturnSeries`, `betaAndCorrelation` and `computeModifiedDietzReturn`; these are
the members missed each time.

Ranked by *plausibility*, not size, which is the rule that ranks them correctly:

1. **Current Drawdown claimed the portfolio never fell, in green, over a range it could not
   measure.** `maxDrawdownPct` returned `0` when `dailyReturnSeries` yielded nothing, and
   `RiskMetricsCards` read that zero as licence to print *"Never below its opening value"* — with a
   green tone, because the current drawdown was `0` too. Reachable when every point in the range is
   unmeasurable: a multi-day market-data stall on a 7D range, or MTD in the first days of a month
   over a freshly bought holding with no price — the same reachability that got Sharpe fixed on
   08-05. Both drawdowns are `number | null` now, plus a `sampleDays` count in
   `betaAndCorrelation`'s shape, and a measured zero still says "never fell" because that statement
   is true.
2. **XIRR discarded `unpriced_holdings`.** `calculate_xirr` values both window endpoints with
   `_calculate_daily_value` — which returns the count — and read only the value. The tax-lot
   purchases are unconditional flows while the endpoint valuation omits the unpriceable holdings, so
   **Annual Return (XIRR)** understated, and so did the **Calmar** built on it. Reported rather than
   excluded (dropping the security would leave a cost with no matching value, unlike
   `/attribution` where exclusion is right), via a `last_xirr_unpriced` latch — the documented
   precedent, because one router and six assertions unpack that 5-tuple. `AnnualizedReturnResponse`
   declares the field with the "a response_model is a filter" note its sibling already carries, and
   `test_api_smoke.py` pins it non-zero on the fixture's unpriced TSMC.
3. **Win Rate counted every unpriced holding as a losing position.** `get_positions_breakdown`
   values an unvaluable holding at 0.00, so its `gain_loss_eur` is `−cost` — it left the numerator
   and stayed in the denominator. Live rather than theoretical: `unpriced_holdings` read **3** on
   2026-08-07 in the gap before the new ETFs were priced, ≈7.7 pp on this book, and the card's own
   footnote stated it as fact ("36 of 39 profitable"). Now `winRate()` in `portfolioKpis.ts` beside
   the two concentration figures that already refused this condition, excluding from both sides and
   naming the count, `null` when nothing is priced. It uses the **two-clause** predicate
   (`market_price === null || market_value_eur <= 0`) cited from `rebalance.ts`, because a missing FX
   rate leaves the price populated and zeroes the value.
4. **`computeModifiedDietzReturn` fell through to `0`** when its denominator was not positive, while
   already returning `null` for its two other undefined cases. Defensive rather than observed — it
   needs outflows exceeding the opening valuation — but a `0.00%` cell reads as a quiet month.

Plus the improvement this file has listed as *Worth doing next* item 3: **`find_stale_etf_baskets()`**,
hung off the market-data job beside its four siblings (that slot succeeds while Flex is refusing).
Held funds only; the basket a fund actually reads comes from
`LookthroughService._alias_proxied_baskets` rather than a second copy of the proxy rule, and the
threshold from the existing per-adapter `stale_after_days`. **Nothing unclearable warns** — a fund
excluded by design, or one whose only route is a hand download with nothing to borrow, stays silent,
because a warning that can never clear is the always-present-Flex-banner pathology. But "has a route"
follows the proxy: VWCE's own adapter is `manual` while VT is fetchable, so a missing VT *is*
actionable. That last rule was a real gap in the first draft, caught by writing the test.

**One extraction came out of writing the fix, not out of reading the code.** `winRate` needed
"can this position be valued?", which was already inline in `rebalance.ts` *and*
`currencyExposure.ts` — so the fix was about to become the third copy of the predicate. It is
`positionValuation.ts` now, with a family test that names any `lib/` module testing the columns
itself. What makes it more than tidiness: those two copies had already needed correcting
**together** on 2026-08-05, when the one-clause `market_price === null` form turned out to miss the
FX case — the drift had happened once already and only stayed in lockstep by luck. The mutation
check is the sharp part: reintroducing a local copy leaves `rebalance.test.ts` **green** and only
the family test fails, which is what "a correct copy is still a copy" looks like in practice.

Verified: backend **1183 passed** (was 1168), frontend **485** (was 466), `tsc -b` and
`npm run build` clean. **Every fix here was mutation-checked** — revert it, watch the named test
fail, restore — because this repo has twice had a test pass against the bug it was written for. (No
count, deliberately: it would go stale the moment anyone adds a case, which is this file's own rule
about figures.) Two of those checks are worth knowing as *checks* rather than as fixes: an AST walk
over `sync_market_data` asserting all five detectors are actually called, since a detector nobody
calls is the same silence as no detector; and the `isUnpriced` family test, whose mutant leaves
`rebalance.test.ts` **green** and fails only the family assertion — which is what "a correct copy is
still a copy" looks like when you run it.

`e2e/ledger.mjs` is fixed but **not run** here — it needs a production DB snapshot, and none was
pulled. (Run on 2026-08-26, where the fix turned out to have added an assertion that could never
pass; see that session.)

## Shipped 2026-08-17 (night, second pass) — the Allocation tab had both classes of bug

Asked "is there any other bugs?" after the batch above, so the same two lenses were pointed at a
surface the first pass had not touched. Both hit, in one service:

**1. The app answered "is this holding a fund?" two ways.** `allocation_service` asked the
`ETF_ALLOCATIONS` table by **ticker** at three call sites while `lookthrough_service` asked it by
**ISIN** — so the Look-through tab and the Allocation tab used different rules for the same
question. Nothing was wrong on this account, because every held fund's symbol happens to be
unique; that is what *latent* means here, not a reason to leave it, because the symbol form decides
a **figure**. It bites two ways: the UCITS `SMH` held here shares its ticker with the far
better-known US fund and would get its sector/region split (both semiconductor funds, so the
numbers stay plausible), and a plain **stock** whose ticker collides with a row is spread across
eleven sectors as though it were a fund — fabricated, not approximate.

All three sites now share one `allocation_for_fund_isin`, resolved once per holding.
`test_fundness_predicate.py` fails any *service* that asks by ticker, so the next call site is
caught rather than these three. Safe by construction: all 13 table rows declare an ISIN, and the
look-through's **98.97% production coverage already proves** the ISIN path resolves every held
fund, since that number requires it.

Worth noting what is *deliberately* left asking by symbol: `currencyExposure.ts` matches funds by
ticker on purpose, because its output is a stated caveat rather than a number. A caveat may be
approximate; a percentage may not.

**2. The three charts silently excluded an unvaluable holding.** It was carried at a 0% weight, so
it vanished from a picture where every slice is labelled "% of portfolio" — and the breakdowns sum
to **exactly 100** either way, which is why nothing looked wrong. Live rather than theoretical:
three newly bought ETFs were unpriced for hours on 2026-08-07, and for that window this tab
described a smaller portfolio than the card above it. The endpoint now excludes-and-names
(`unpriced_holdings` / `unpriced_symbols`, the look-through's own rule) and `AllocationTab` renders
a `role="alert"` above the charts.

**The test-suite lesson is the part worth keeping.**
`test_an_unpriced_holding_does_not_break_the_percentages` had pinned those sums for months and was
structurally blind to this: summing to 100 is what being wrong looks like here. Its docstring now
says so, beside the test that covers the other half. **When a completeness bug can satisfy the
assertion you already have, the assertion is measuring the wrong thing.**

Verified: backend **1195**, frontend **489** (new `AllocationTab.test.tsx`), `tsc -b` and
`npm run build` clean, three mutants confirmed caught — including that reverting to the ticker
lookup fails the family scan by name.

## Shipped 2026-08-17 (late) — DBPG decomposes through VOO

Owner's instruction: use VOO's S&P 500 basket for DBPG, overwriting whatever DBPG itself
publishes; the position is being replaced with IQQ later anyway. So DBPG is no longer `excluded`
and **every held fund now decomposes**.

**Half the objection is answered and half is not, which is why the two were recorded separately.**
VOO's basket fixes "the disclosed names are collateral, not the index". It does nothing about the
**2x leverage**: DBPG is decomposed at its market value, so its real exposure to each company is
about double what the table shows. Scaling it is not available — the five buckets must sum to the
portfolio to the cent — so `leverage` now drives a `warnings[]` line instead, and clearing that
field would delete the second disqualifier silently.

`replication` stays `synthetic`, which makes the proxy **beat** a stored basket rather than merely
fill a gap: importing DBPG's collateral file can no longer un-proxy it. Pinned as a family rule
(`test_a_synthetic_fund_is_never_decomposed_from_its_own_basket`) so a second swap fund cannot
arrive without it.

**VOO is declared in both fund tables but is NOT held** — a basket donor only. Its `ETF_ALLOCATIONS`
blocks are byte-identical to SXR8's and DBPG's, which were already identical to each other: three
S&P 500 trackers, one set of numbers, change one and change all three. ISIN `US9229083632` verified
against OpenFIGI rather than recalled.

**Fetched and imported on production the same evening**: 503 rows, as-of 2026-07-31, 100% ISIN
coverage, weights summing to 99.94%. **Coverage went 95.16% -> 98.97% and `uncovered_fund_eur` is
now 0.00 — every held fund decomposes.** Both pages declared 503 and 503 arrived, so VT's torn-read
hazard does not reach VOO in practice: 2 page boundaries against 21, and none of VOO's rows carry a
0.00% weight.

## Ran on production 2026-08-17 — the runbook, and what it caught

**Identity resolution completed** (`manual_identity_resolve`, success): 1,998 ISINs resolved — 893
LEI, 1,988 shareClassFIGI — plus **108 fund constituent rows folded by CINS/SEDOL**, which is
GRID's and QTUM's issuer identifiers becoming companies. `unresolved_value_eur` fell from
**4,281 → 516 CHF** (0.7% of the book); coverage 95.16%.

Two things about the run worth keeping. It **commits once at the very end**
(`resolve_identities.py:165`), so an interrupted run loses everything including the `*_checked_at`
stamps — do not deploy while one is in flight. And **`pgrep` is not installed in the container**, so
a `pgrep -f … | wc -l` liveness check counts the *error line* and always returns 1; use the
`sync_runs` row to tell whether it finished.

**Coverage is 95.14% and DBPG is the only fund not looked through.** Fetched all 10 baskets
(0 failed) and imported 9; SMH, SOXQ, GRID and QTUM stored for the first time. Identity
resolution is running detached in the container (~37 min, GLEIF-bound) — **do not deploy until
it finishes**, because `docker compose down` kills it.

**VT's import refused, and the guard earned its keep.** Vanguard serves that endpoint from a
cluster holding different snapshots: the 21-page walk came back 13 pages declaring 10,055
holdings and 8 declaring 10,032, twice in a row. Because ~8,000 of VT's rows have a 0.00%
weight and no stable order between snapshots, the assembled set carried **9,114 distinct
holdings in 10,032 rows** against 10,025 in the stored basket — ~900 companies would have
disappeared from ~11% of the book, with weights summing to a plausible 91.60%.

Nothing is lost: the refusal keeps the 2026-06-30 basket, and VT publishes month-end so there
is no urgency. `parse_vanguard_us` now refuses a size disagreement as **its own named fault**
rather than reporting "a page is missing", which sends the operator hunting something that was
never missing. Committed, **not pushed** — see below.

**`OPENFIGI_API_KEY` went on the VPS later the same evening** and the app reads it. It was never
blocking — the run above needed it for 105 non-ISIN identifiers, ~11 keyless requests, while GLEIF's
one-request-per-ISIN is the whole 37 minutes.

## Shipped 2026-08-17 — a borrowed basket, and an 8% shortfall that was never cash

Two asks from the owner, plus the answer to a question that turned out to be a documentation gap.

**VWCE borrows VT's basket.** Declared in `etf_sources.py` as `basket_proxy_isin`, aliased at read
time so nothing downstream has a proxy branch and there is one stored basket with two readers —
a copy would drift the moment VT is refetched. Measured on a production snapshot: coverage
**78.6% → 87.02%**, partition closes exactly, VWCE reports 10,032 constituents with `proxy_for_symbol
= VT`. Once the four new baskets are imported it should land around **95%**, the rest being DBPG
(3.8%, excluded) and per-fund residuals.

It is an approximation and says so on every surface: an amber *Via VT* badge instead of a green
*Decomposed*, a `warnings[]` line carrying the declared reason verbatim, and the Coverage card
**staying amber** while any fund is proxied. That last one is the point — with VWCE resolved the
unresolved list empties, so the old rule would have turned the card green over a fund decomposed
from someone else's file. Unlike a percentage threshold this badge can clear: import a real basket
and the proxy is never consulted again.

Five guards in `test_etf_source_registry.py` (no reason, self-proxy, chain, excluded target,
`manual` target) and five in `test_lookthrough_partition.py`, including that a real basket wins and
that the partition still closes.

**Why VT's equity weight reads 91.86% — it is rounding, and now it says so.** Asked as "it should be
nearly 100% stocks", and it is. Vanguard publishes weights to 2dp, the smallest non-zero weight in
the file is `0.01`, and **8,007 of VT's 10,032 rows are printed at exactly 0.00%** — that tail *is*
the missing 8.14%. Nothing was truncated or misparsed (`stored_rows == source_rows == 10032`,
`skipped_rows = 0`, identifier coverage 100%). EMIM is the same shape at 2,279 of 4,042. The fund
table now shows `N rows at 0%` under the percentage, because an unexplained 8% next to a fund's
value reads as an uninvested cash balance — plausible, and wrong.

**`OPENFIGI_API_KEY` is set locally and verified** against the live API with an 11-job batch (which
a keyless request refuses). Still needs adding on the VPS — *Needs a human*.

One incidental fix: the new count used a bare `toLocaleString()`, which renders 8,007 as "8.007"
under a German runtime. Pinned to `en-US` like `formatCurrency` and `formatPercent`.

## Shipped 2026-08-16 (evening) — sector clustering, and four funds that were never unreachable

Two follow-ups on the charts: cluster the treemap **by sector** rather than by how a company is
held, and "take a look at the missing data too" — 5 funds worth 17.3% of the book publishing no
basket, and 9,930 constituent ISINs carrying no identifier.

**The missing-data half turned out to be a research failure, not a data gap.** `etf_sources.py`
recorded SOXQ, GRID and QTUM as unreachable single-page apps and SMH as needing a hand download.
All four have keyless, login-free routes, and each old note was wrong in its own way:

| fund | what the note said | what is actually there |
|---|---|---|
| SOXQ | "Invesco serves a single-page app" | true of the *product page*; the API behind it is keyless JSON, keyed by the fund's own CUSIP — derivable from its ISIN, so nothing to discover |
| GRID | "tickers but no ISINs, so a scrape could not fold" | there is a CUSIP column beside the tickers. The *fold* concern was right for a different reason — see below |
| QTUM | "a WordPress table carrying CUSIPs but no ISINs" | the table is on `/qtum-full-holdings/`, not `/qtum/`; and the identifiers are not all CUSIPs |
| SMH | needed a hand download | an XLSX with a real ISIN on **every** row — the best-identified feed of the seven |

**10 of 12 funds gained an automated fetch route.** VWCE has none (Vanguard Europe publishes
complete holdings by email on request) and DBPG stays excluded by design — VWCE decomposes anyway
as of the following day, by borrowing VT's basket.

**The trap that would have been silent, and the reason this took a live API check.** Three of the
four publish nine-character identifiers, and most of them are not CUSIPs:

- **77 of GRID's 128 rows are CINS** — the same numbering space extended to foreign issuers, marked
  by a leading letter — including its three largest holdings (Eaton, Schneider, Johnson Controls).
  `US` + a CINS produces a **check-digit-valid ISIN that belongs to nothing**, so a bulk prefix
  would have fabricated an identifier for 60% of the fund, each one passing every validity test.
- **20 of QTUM's 89 rows are SEDOLs** in a column headed "CUSIP".
- **`ID_CUSIP` with a CINS returns zero rows from OpenFIGI** — no error, just nothing. The plan for
  this session said to resolve CINS as `ID_CUSIP`; it would have resolved nothing and reported
  success. The idType is `ID_CINS`, verified with one live request before any code was written.
- **OpenFIGI's mapping endpoint returns no ISIN at all**, only FIGIs. So resolution writes
  `etf_holdings.constituent_share_class_figi`, which `IdentityMember` already unions on — Eaton's
  CINS and its ISIN both give `BBG001S5QZ45`, so the fold works without inventing an identifier.

**Sector clustering.** `sector_taxonomy.py` normalises four vocabularies into the eleven names the
Allocation tab already shows; a company's sector is a value-weighted majority of its contributing
rows, tie-broken on the name so the answer cannot depend on arrival order. Precedence was measured
rather than assumed: BlackRock's baskets classify 97 of the 103 top ISINs, Yahoo 27, and where both
answer they differ only by taxonomy. This narrows `etf_basket.py`'s "sector is deliberately
unserved" refusal rather than breaking it — grouping only, no rollup anywhere in the response, so
nothing states a portfolio-level sector figure.

**The old sector palette failed validation outright**, which is why one is now shared by both tabs:
`#3b82f6` Technology against `#8b5cf6` Communications measures **ΔE 1.3 deuteran** — the two largest
groups here, indistinguishable. Brute-forcing all 256 subsets of the categorical order found only
**four** hues that clear the all-pairs CVD pairlist simultaneously, so four sectors get a hue and the
rest fold into *Other sectors*. `Unknown` also took a positional colour and moved when the chart
reordered; it has a fixed grey now.

**What to check on prod, in order of how quietly it would be wrong:**

- run the CLI steps in *Needs a human* — **baskets first, identities second**. The four funds were
  8.1 pp of the book unattributed at the 08-14 snapshot, so coverage should move from ~78.6% to
  **just under 87%**, less each fund's own residual. Anything much below that means an import
  refused; anything above it means something is being renormalised, which nothing here may do.
- **TSMC must appear once.** SOXQ and SMH are the first baskets to carry the TSM ADR, so the
  `ISSUER_OVERRIDES` entry fires for the first time outside a test.
- GRID's and QTUM's companies should carry `key_type: share_class_figi` after step 2. If they read
  `unidentified`, `OPENFIGI_API_KEY` or the CINS pass is not doing its job.
- the treemap's sector legend, in **both themes**. Four hues plus a grey; each fill carries its own
  ink because dark `--viz-sector-2` measures 2.94:1 against white.

Verified: backend **1112 passed** (+27 adapters, +58 identifiers, +13 resolution), frontend 464,
`tsc -b` and `npm run build` clean, and all four adapters run against the real downloaded files —
SOXQ 33 rows/100.00%, GRID 128/100.00%, QTUM 89/100.04%, SMH 26/100.02%, each imported into a local
DB through the same CLI production uses. **The browser suite was not re-run** — nothing rendered
changed since the morning's run beyond the treemap's fills, which `sectorColors.test.ts` covers.

## Shipped 2026-08-16 — the Look-through tab's two charts

Asked for "some charts, maybe a treemap or a pie chart". Shipped the treemap; **did not ship a
pie**, and the reason is worth keeping: the company ranking is ~50 rows over three orders of
magnitude, which is the distribution angles are worst at. The part-to-whole that a ring *could*
have served is the coverage split, and even there three long-named segments read better as a
horizontal stacked bar at 390px.

- **`Where the value sits`** — one bar: held directly / through funds / not attributed, with each
  segment's value and a sentence saying what it is.
- **`Company exposure`** — a treemap above the existing table, sharing its Top 25/50/100 control.
  Tiles are coloured by how the company is held, and clicking one opens the drill-down that was
  already there. Companies reachable only through a fund are the point of the colour.

What to check on prod once it deploys, in order of how quietly it would be wrong:

- the treemap's grey `Not attributed to a company` tile is **present and large** (~21% of the book
  at today's coverage). If it is missing, the tiles have been renormalised and every company tile
  is overstated — with no axis to give it away.
- the two legends do not share a phrase. The bar says *Held directly*; the treemap says
  *Direct only*. They mean different things and a company held both ways is in both charts at once.
- both themes. The `--viz-*` palette is the first here that is stepped separately for light and
  dark; every other chart hardcodes one set. Verified locally that the two resolve to different
  hexes, which eyeballing the screenshots could not settle.

Verified before pushing: frontend **448 passed** (17 new), `tsc -b` and `npm run build` clean, and
the browser suite against a locally seeded DB — `mobile` 50/50 at 390x844, `a11y` 17/17,
`sweep` 18/18, `csp` 4/4, `chunks` 37/37, `errors` 18/18. The look-through chunk went 3.5 kB → 5.6 kB
gzipped; Recharts was already eager on the Performance tab, so nothing extra is downloaded.

Backend untouched — the endpoint already carried every field both charts read.

## Shipped 2026-08-14 — the Look-through tab: one company, one row

**Asked for a "seethrough" view: how much is actually invested in each company, with the ETFs broken
into their single stocks and share classes like GOOG/GOOGL/ABEA combined — "not by string match, but
a smarter way".** The smarter way is two keyless identifier services, and the interesting part is
that they are complementary rather than redundant.

**What it fixes on the direct side alone**, before any ETF is opened. One company occupies several
rows of Positions in three different shapes:

| shown as | folds on |
|---|---|
| `GOOGL@NASDAQ` + `ABEA@IBIS` | one ISIN — no provider needed at all |
| + `GOOG@NASDAQ` | the **LEI** (a different ISIN, share class C) |
| `ASML@NASDAQ` + `ASML@AEB` | the **LEI** (`USN070592100` NY registry vs `NL0010273215` Amsterdam) |

**Measured on this account's own 25 held ISINs:** OpenFIGI resolved 25/25, GLEIF 20/25 — it has no
ISIN record at all for TSMC, Samsung, SK Hynix, Credo or Marvell, and OpenFIGI covers every one.
GLEIF is what folds *share classes*; OpenFIGI's `shareClassFIGI` is what folds *venues* and is the
only tier that answers for the Asian ordinaries. Neither alone would do.

**Grouping is a union over every identifier, not a precedence chain.** `key = lei or figi or isin`
re-creates the bug the feature exists to fix: when one of a company's ISINs resolves an LEI and its
sibling does not, the two are keyed at different depths and split into two rows *while the response
reports nothing wrong*. Pinned by a test that shuffles the input 25 times.

**Verified end to end against a snapshot of production** (fetched, imported, resolved, deleted
afterwards). The two real bugs it found are worth knowing, because both were invisible on tidy
fixtures:

- **The partition missed by a cent.** Five independently rounded buckets summed to 70,842.40 against
  a total of 70,842.39 — the "never derive a total from rounded rows" rule, broken by my own
  verification. The residual is now published as the rounded remainder. **The first test written for
  it passed against the bug**; a mutation check showed it could not reach the condition, and the
  engineered version now fails 3 of 6 cases when the fix is removed.
- **XNAS produced company rows called *US DOLLAR* and *NASDAQ 100 E-MINI SEP26*.** Xtrackers
  publishes no asset-class column, so cash and futures rows counted as companies. It *does* mark them
  by identifier (`_CURRENCYUSD`, `___ADI34XYM5`), which the adapter now reads. Unidentified company
  groups went 21 → 8.

Two more shapes real files taught us, both now pinned: EMIM ships **five negative cash rows** (KRW
−0.10 and friends — ordinary overdrawn balances), so the negative-weight refusal is scoped to
*invested* rows rather than costing a 4,042-row basket; and XNAS ships a real ISIN with a **blank
name**, so the label falls back to the identifier.

**Coverage, and what it costs to read the page wrongly.** 78.6% of the book is attributed to
companies. The remaining 21.1% is six funds with no basket (VWCE 6,510, SMH 2,391, GRID 1,523,
SOXQ 1,456, QTUM 386) plus DBPG 2,703 excluded outright — it is a *synthetic 2× leveraged* swap ETF
whose published basket is substitute collateral (Mastercard 6.6%, Altria 5.7%, Tesla 4.9% for an
S&P 500 product). Every company figure is therefore an **understatement**, and nothing is rescaled
to disguise that: the coverage figure leads the panel as a `role="alert"` outside every collapsible.

Also worth knowing: VT's weights sum to **91.86%**, and that is *rounding*, not cash — Vanguard
publishes `percentWeight` to 2dp and thousands of its 10,032 holdings round to 0.00.

**The bounded identity rule earns its keep, measured.** The union of six baskets is ~10,400 distinct
constituent ISINs, and resolving all of them would be hours of paced requests. Resolving the **480**
that reach 99.5% of cumulative look-through value took unresolved value from **13,219 to 2,113** —
18.7% of the book down to 3.0% — while the 9,930 ISINs left unresolved are worth about 0.2 each and
cannot move any published figure. Companies folded by LEI went 18 → 355 and by shareClassFIGI 5 →
147. GLEIF had no record for 142 of the 480, which is the same one-in-three gap the held ISINs
showed, so both providers remain load-bearing at constituent scale too. Top ten companies = 49.2%
of the portfolio.

Backend 845 → 976 (+131), frontend 416 → 431.

**The browser suite ran against the snapshot and caught a defect nothing else could.** `mobile.mjs`
reported the Look-through tab pushing the page **1,282px sideways at 390px** and named the element:
the fund table's `reason` column landed in `DataTable`'s phone card as
`<dd class="shrink-0 tabular-nums">`, which is exactly right for a figure and exactly wrong for a
sentence. Prose is now rendered as prose below the table, visible on both viewports — it explains why
a fifth of the portfolio is absent, so hiding it on a phone was not an option. jsdom loads no CSS, so
no unit test could have seen this.

Final suite: `mobile` 50/50, `a11y` 17/17, `sweep` 18/18, `axis` 8/8, `csp` 4/4, `chunks` 37/37
(the new lazy chunk is requested on click, served 200, and mounts clean), `errors` 18/18.
`ledger` is 5/7 for a reason that predates this work — see *Known rough edges*.

**Two defects in the coverage card, both found by an adversarial audit of code written the same day,
both fixed before the push.**

- **The green threshold was above the achievable ceiling.** `coverage_pct >= 95` could not be reached:
  DBPG is 3.8% excluded by design and no basket attributes 100% of its fund, so the practical maximum
  is **95.08–95.86%** — the card would have sat amber forever with under a point of margin, which is
  the always-present-Flex-banner pathology. It now tones on whether any fund is *unresolved*: green
  when every held fund is decomposed or deliberately excluded, amber naming the count that are not.
  Reachable, and it goes amber again the day a fund is bought.
- **A single global staleness threshold meant two different things.** `BASKET_STALE_DAYS = 45` badged
  Vanguard US permanently for publishing month-end with a ~6-week lag, exactly as documented, while
  giving Xtrackers and iShares — which republish *daily* — six weeks of silence. `ADAPTER_STALE_DAYS`
  is per-source now (7 / 7 / 75 / 45), so `†` means "the issuer has newer holdings we failed to fetch".
  The card also names ageing baskets, because staleness deliberately does not move `coverage_pct` and
  a hand-imported VWCE basket would otherwise keep claiming ~9 pp while describing last quarter's index.

Three new frontend tests pin the tone from both sides, and a mutation check confirms all three fail
against the old threshold — the first version of the rounding test earlier in this session passed
against its own bug, so that check is now habit.

**One factual error corrected in docs committed hours earlier:** `etf_sources.py` claimed Vanguard
Europe's robots.txt "disallows automated agents". It does not — every Vanguard EU domain is
`User-agent: * / Disallow:`, allow-all, with only model-training crawlers named. VWCE's blocker is
that **the data is not published**, not that we are forbidden to read it, and stating a policy barrier
that does not exist would stop the next person looking for the route that does.

Two e2e corrections worth knowing. The tab count was hardcoded in **three** scripts plus `lib.mjs`'s
`TABS`, and `csp.mjs` held it **twice** — I updated one and its sibling diagnostic then fired a false
failure against a good build. All three now read `TABS.length`, so a tenth tab cannot desynchronise
them. And raising `errors.mjs`'s floor from 10 to 11 was **wrong**: that count is scoped to the
*Performance* panel, so a new tab can never contribute to it. Reverted, with the new tab given its own
three assertions instead — including that it must not publish a coverage figure during an outage.

**What to check once deployed** — the feature ships with empty tables, so it needs two operational
runs before the tab says anything interesting. See *Needs a human*.

## Shipped 2026-08-08 — the Flex sync stopped asking for a statement IBKR had already made

**Reported as "the flexquery always errors out".** It never was: the query succeeds every single
day. What failed was everything we asked *after* the day's success — IBKR issues about one
generation per US-Eastern calendar day and refuses the rest with `Code=1001` at the SendRequest
step. Two of three scheduled slots plus every manual Sync press, daily, twelve days of twelve with
no counterexample. Each refusal is a failed *generation*, which is exactly what the `Code=1025`
token lockout counts, so the red rows were not merely cosmetic.

**The guard** (`app/services/flex_generation.py`) answers "has today's generation been spent?" from
`sync_runs`, in ET days. `sync_ibkr_data(force=False)` and `POST /api/sync/ibkr` both return
`skipped` / `already_generated_today` without touching the network when it has. Expected effect:
scheduled IBKR errors go from ~2/day to ~0, and the Sync button reports *Already up to date* with
the next available time instead of a red failure.

Two type sets, deliberately different, and getting either backwards is a real bug in the opposite
direction: `FLEX_API_SYNC_TYPES` **excludes** `ibkr_manual_xml` (an offline browser ingest spends no
generation — proven twice, 07-28 and 07-31, where an offline ingest was followed by a *successful*
API generation the same ET day), while `IBKR_SYNC_TYPES` **includes** it (it genuinely refreshes the
data, so it must quieten the staleness alarms).

**The schedule moved at the owner's request**, reaffirmed after the trade-off was put to them twice:

| | before | after |
|---|---|---|
| IBKR primary | 06:00 Berlin (00:00 ET) | **18:00 Berlin (12:00 ET)** |
| IBKR recovery | 08:00 + 00:00 Berlin | 00:00 Berlin only, guarded |
| Yahoo repricing | 8, 11, 13, 15, 18, 20, 22 | **unchanged** |
| 730-day deep pass | 08:00 | 18:00, with IBKR |

Yahoo coverage is byte-identical — only which job makes the 08:00 and 18:00 touches changed. The
`full_sync` job moved rather than a new IBKR job being added at 18:00, because two jobs on one hour
collide on `single_flight`; pairing them also keeps the property that a security the statement
creates is priced by the same run.

**The concern was stated and overruled, which is why it is written down rather than argued again:**
18:00 Berlin captures no additional trades (the window ends yesterday *in US Eastern* and rolls at
midnight ET, so 12:00 ET covers exactly what 00:00 ET does), it is mid-session where the historical
rate is ~1/14, and it takes attempts from three per ET day to two against a 3-day window whose whole
margin is two failed days. `test_every_ibkr_job_avoids_us_market_hours` became
`test_ibkr_jobs_run_at_the_declared_hours` — an explicit allowlist with the reasoning attached, so
drift is still caught but the exception is recorded rather than the rule silently dropped.

**Because of that, `find_flex_generation_gap` shipped with it**: 2 ET days without a successful IBKR
sync, run from the market-data job. `find_stale_ibkr_sync` at 7 days cannot see the failure it was
written for once the period is 3 days — it fires four days after the trades have gone from every
future statement.

Also: `trigger_sync_now` was raising `SyncBusy` → 429 on any `skipped` status, which had only ever
meant a pipeline collision; it now keys on `reason == "pipeline_busy"`, because a day with nothing
left to sync is finished, not busy. And the header's sync panel became `SyncStatusMessage` — three
outcomes now, and the middle one carries no counts, so the old unconditional
`Securities: {securities_synced}` would have rendered "Securities: undefined" under a green tick.
Extracting it is what made that branch testable at all.

Backend 828 → 845, frontend 407 → 416.

## Shipped 2026-08-07 (evening) — two "avg monthly" figures that were never the same quantity

**PUSHED.** Asked whether the contributions averages disagree. **They do not**, and that is worth
recording so it is not re-investigated: the strip's `/deployed` suffix is byte-identical to the figure
`MonthlyDeploymentCard` renders as *12M avg*, and `Σ monthly[].net_eur` holds against the cost basis to
**0.03%** — the per-date FX residual on closed lots under a CHF base, exactly as CLAUDE.md's identity
check predicts, not a dropped lot.

What differed was **labelling**: the strip's headline is money *in* over **all time** (≈2,026) and the
card's is capital *deployed* over **12 months** (≈2,836). Four different (window, metric) pairs, all
called an average per month, with the only distinction in a `title` attribute. The strip now reads
**"Avg Monthly in"** and the `/` suffix has a rendered legend — `CHF2026/2023` reads like one broken
number until you know it is two.

The 3M figure being ~2× the all-time one is real, not an artefact: 12,852 of the last six months'
14,491 in deposits arrived in the last three.

Overflow at 390px was reasoned rather than measured — the widened title is ~185px of muted text in an
already-wrapping flex row, well inside the ~358px card interior and narrower than existing items — so
`e2e/mobile.mjs` was **not** run for it. Worth one pass next time that suite runs against a local
stack. Frontend 403 → 407.

## Shipped 2026-08-07 (afternoon) — a spinoff's tax lots predate the instrument, and it blanked half the returns table

**PUSHED** as `2bace4d`; the deploy guard deferred it past the 20:00 Berlin slot, so it lands ~20:20.
Reported as "the monthly returns are not right": December 2025 through May
2026 blank, November 2025 daggered, and a collapsed summary reading `Aug: +1.5% · YTD: +3.1%`.

The client's arithmetic was faithful — replaying it against the live endpoint reproduced the screenshot
exactly, which is what pointed at the data. **`MBGL` (Mobility Global, spun out of `SPGI` 1-for-1 on
2026-06-30) has tax lots dated 2025-11-06 and 2025-12-29**, because IBKR carries the parent's holding
period over and reallocates 4.84% of its cost basis. Its Yahoo history starts 2026-06-26, at listing.
So `unpriced_holdings` was **1 on 166 consecutive days**, and `isMeasurable` correctly dropped every
one of them.

**Nothing looked wrong anywhere else**, which is why it survived: the stub is 0.2% of the book, so no
total moved. The only surface that showed it was the one figure that depends on *whole* days.

The fix floors each security's valuation start at the corporate action that created it
(`_load_position_start_dates`). The reasoning, the four load-bearing details and why the action set is
much narrower than `SPLIT_LIKE_ACTIONS` are in CLAUDE.md under *A spun-off line is not held before the
action that created it*. Also fixed: the collapsed card summary carried the figure with **no partial
marker at all**, so the dagger and its footnote both lived inside the body almost nobody opens.

**Verified by A/B against a snapshot of the production DB**, same data, floor off then on — every
period that was already fully measured is byte-identical, so nothing else moved:

| period | before | after |
|---|---|---|
| 2025-11 | −0.96%† (3 days) | −1.36% |
| 2025-12 … 2026-05 | — (blank) | −0.10%, +2.32%, −5.50%, −3.64%, +14.41%, +7.46% |
| 2026-06 | +3.86%† (5 days) | +3.96% |
| **YTD 2025** | +30.77%† (to 5 Nov) | **+25.22%** |
| **YTD 2026** | +3.50%† (6 weeks) | **+23.54%** |

Days with `unpriced_holdings > 0`: **166 → 0.**

**What to check once deployed:** the Monthly Returns card should show no `†` and no `–` inside the
holding period, and `/api/portfolio/value-over-time?start_date=2024-05-28` should report
`unpriced_holdings: 0` on every point. If a dagger returns, it is a *different* holding — hover the
cell, which now names the days the figure covers. Backend 818 → 828, frontend 399 → 403.

**Left deliberately alone:** `/api/portfolio/attribution` still excludes-and-counts MBGL for windows
that start before the spinoff. A line that did not exist at the window start has no start value to
attribute against, and doing it properly means combining parent and child — a larger change than this
one. It shows as `unpriced_holdings: 1` on long windows there, and that is honest rather than wrong.

## Shipped 2026-08-07 — IBKR generates one statement a day, and it was quietly starving the deep price pass

**DEPLOYED** as `2c75004` at 06:32 Berlin; `/health` reports it healthy with the scheduler armed and
the job store persistent, and the old gate is confirmed absent from the running container. Backend
814 → 818.

**What to check tomorrow morning**, since this change is only observable on a day IBKR refuses: the
08:00 `full_sync` should record `market_result` as a real object rather than `null` even when its
`ibkr_result` is an error, and `/api/scheduler/history` should show `status: error` alongside it —
the IBKR verdict must not be masked by the Yahoo half succeeding.

Asked "why does the full sync fail, is there an issue with IBKR and Yahoo?" The answer to the second
half is **no** — 32 of 32 `market_data_only` runs succeeded, 40 of 40 securities every time, and not
one rate-limit event in the whole recorded history. The answer to the first half was not the one in
CLAUDE.md.

**IBKR generates this statement about once per ET calendar day, and refuses every later attempt with
`Code=1001`.** Six days of six, with all three slots inside the safe overnight window:

| ET day | 00:00 ET (06:00 Berlin) | 02:00 ET (08:00 Berlin) | 18:00 ET (00:00 Berlin) |
|---|---|---|---|
| 08-01 | **success** | error | error |
| 08-02 | error | **success** | error |
| 08-03 | error | **success** | error |
| 08-04 → 08-06 | **success** | error | error |

Always the earliest attempt that works; everything after it refused. The only two-success ET day in
the entire history is 07-31, the day the query definition was edited.

**This subsumes the mid-session theory rather than refuting it, and that is the interesting part.**
The 07-31 evidence (08:00 at 4/5, 13:00 at 0/6, 20:00 at 1/8) fits *both* readings equally well,
because the mid-session slots were also the later ones — one dataset, two theories, no way to tell
them apart. What discriminates is 08-01 onward, where every slot is overnight and still only one
succeeds. The hour rule stands; it is just no longer the binding constraint. **Adding IBKR slots
does not add freshness, it adds failed generations** — which is exactly what `Code=1025` counts.

**The fix: `full_sync` no longer gates its market-data half on its IBKR half.** That gate turned the
730-day pass into the rarest job in the schedule: 06:00 takes the day's one generation, so 08:00
fails, so the deep backfill had not run since **2026-08-03**. Two independent providers were wired
together for no reason — Flex refusing a statement says nothing about Yahoo, and the securities
needing prices are the ones already in the database.

**What made it invisible is the part worth carrying forward.** The six 7-day slots run
unconditionally and keep *current* value fresh, so no screen looked wrong — only the two-year
history quietly stopped extending, and nothing reports the age of a backfill. It was legible solely
as `market_result: null` buried in `details` on runs already flagged `error` for an unrelated
reason. And a skipped step emits no warnings, so `find_stale_priced_securities` could not fire on
those mornings either: an unpriced holding was structurally undiscoverable on exactly the days IBKR
had refused.

`status` still reports the **IBKR** verdict, so a green Yahoo half cannot paper over a refused
statement. Four tests pin it from every side — prices on failure, still reports the failure, still
prices *after* IBKR rather than before, and market warnings now survive a failed IBKR half.

**Verified on production the same morning** (owner's explicit permission for the Yahoo call): a
manual 730-day pass covered **43 of 43 securities, 1,615 prices, no rate limiting**, and the three
new ETFs each pulled a full two-year history. `unpriced_holdings` 3 → **0**.

## Shipped 2026-08-04 (later) — the chart was reserving a fifth of itself for nothing

**The portfolio chart's Y axis ran to −20,000 on a phone and −10,000 on desktop while no series was
ever negative.** Reported as "−20k will not be reached anyway"; it turned out to be worse than a
cosmetic preference, because the data could not reach it *even in principle*.

Read off `/api/portfolio/value-over-time`: the three default series spanned **+1,122 … +65,025** —
the profit line's minimum is positive, and it has never been negative in the whole series. But the
padding is `minValue − range × 10%`, a share of the **whole** range, which the market-value line
dominates. 10% of 63.9k is 6.4k, so the padded minimum came out at **−5,268**, and `niceTicks`
rounds the minimum *out* to a step multiple — against the 20k step a 4-tick phone axis picks, that
floors to **−20,000**. A fifth of the plot height, permanently blank.

`axisFloor()` now bounds it, with the cap the user asked for and one rule they did not:

- **Nothing negative in the data → floor at zero.** This is the case that was actually wrong, and it
  removes the whole band rather than shrinking it.
- **Something negative → cap at −5k, unless a real value is lower**, in which case the value wins.
  The cap is deliberately *soft*: empty space is cosmetic, a loss clipped off the bottom of the
  chart is a wrong number. `axisFloor` can never return a value above `minValue`, and that property
  is pinned across a range of minima rather than at one point.

The floor is applied **after** rounding, not to the input, because rounding-outward is precisely
what has to be overridden — clamping the input still floors back to the same multiple. A test
reproduces the −20,000 with no floor passed, so the fix cannot end up measuring itself, and another
asserts existing callers are byte-identical when the argument is omitted. Frontend 308 → 316.

## Shipped 2026-08-04 (late) — three things found by looking at what a pass reports

Deployed as `0117d76` (chart axis) plus follow-ups. All verified against production:
`axis` 8/8, `a11y` 17/17, `sweep` 16/16, `mobile` 45/45; backend 684 → 689, frontend 316.

- **`settings.log_level` configured nothing.** It was read in exactly one place —
  `echo=settings.log_level == "DEBUG"` for SQLAlchemy — and no code ever called into the
  logging module, so the root logger kept its default and Python's *last-resort* handler
  emitted WARNING and above only. **Every `logger.info` in the app was discarded in
  production**, confirmed by reading the container log: uvicorn's access lines and alembic
  present, not one line from `app.*`. That is worse than a missing feature because the
  docs assume otherwise — CLAUDE.md tells you to grep the container log for a request id,
  and the scheduler's `removing retired job` / `kept, next run:` lines (the only direct
  evidence that pruning and misfire recovery work) are INFO. Part of why a job store that
  had never opened looked healthy for two days. Needs `force=True`: uvicorn installs
  handlers before the app is imported, and `basicConfig` is a no-op when one exists.
  Chatty providers (yfinance above all) are held at WARNING or the volume would triple.
- **Docker had no log rotation**, which only became a problem once the above started
  emitting. The default `json-file` driver is unbounded and this VPS's disk also holds the
  database and its backups; now capped at 10 MB × 5.
- **`prices_fetched` counted rows in the window, not rows written** — 234 reported for
  ~80 real writes, via a second full-range SELECT per security whose result nobody could
  act on. The docstring already promised writes.

## Shipped 2026-08-04 (late) — the sixteen KPI cards are one component

*Worth doing next* item 0, done. `ui/KpiCard.tsx` replaces the hand-written card in
`PortfolioSummaryCards` (5), `PerformanceMetricsCards` (6) and `RiskMetricsCards` (5) — 526 lines of
component down to 357 plus a 111-line primitive, and more to the point one place to edit instead of
sixteen. `RiskMetricsCards.test.tsx`'s existing 13 assertions pass **unchanged** against the
primitive, which is what says the render was reproduced rather than reinterpreted.

Two judgements worth knowing:

- **`DividendKpiCards`' `Tile` was deliberately left alone**, though the old entry named it as a
  fourth copy. It is not the same card: `bg-card/50` rather than `bg-card`, an `text-xs` label, a
  `font-semibold` value, and a footer row whose `flex-wrap` and `min-h` exist because the
  month + MoM + YoY chips are wider than a 155px phone tile and pushed the page into horizontal
  scroll unwrapped. Folding it in would have merged two things that only look alike and changed the
  Dividends tab's appearance for no gain.
- **One visible change: `N/A` became `—`** on Annual Return and Calmar Ratio when those are null.
  Three of the four files already agreed an absent metric must not render as `0`, and then disagreed
  on how to say so — an em dash in `RiskMetrics`, `N/A` twice in `PerformanceMetrics`. Absence is now
  a single code path (`value={null}`), and it also **refuses to colour a dash**: a caller computing
  `tone` from a number it has not null-checked would otherwise paint the missing value green.

`KpiCard.test.tsx` carries the primitive's contract plus the backstop — a source scan for the value
class outside `ui/`, the `noRawTables` pattern from `tableFamily.test.tsx`. It uses
`import.meta.glob`, not `node:fs`: the frontend tsconfig is browser-targeted with no `@types/node`,
so fs type-checks under vitest and then **fails `npm run build`**, breaking the deploy rather than
the suite. Frontend 316 → 329.

## Shipped 2026-08-04 (late) — the portfolio's dividend rate — DEPLOYED and verified

Two cards on the Performance tab's risk row — *Dividend Yield* (projected next-12-month income over
market value) and *Yield on Cost* (the same income over cost basis) — in place of *Effective
Holdings*, which moved into the *Top 5 Weight* footnote so the metric survives without a card. The
row went 5-up to 6-up, matching the grid the row above already uses. Per-security `Fwd yield` column
on the Dividends tab is the audit of the headline. Backend 689 → 699, frontend 329 → 342.

**The premise was that Yahoo already gives us per-security yields; it does not, and that is now
written down in CLAUDE.md.** No dividend field exists anywhere in the backend, `fundamental_metrics`
is on-demand only, and adding a Yahoo yield would have meant a migration plus a fundamentals pass
before the cards showed anything — and a second annual-dividend-rate implementation beside the
forecast. Everything needed was already in one service call.

Three things a review caught that had already been written and were wrong:

- **Hoisting the positions fetch to build `growth` in one place was a latent 500.** It is allowed to
  degrade to "yields omitted" only because it is the *last* DB access in the method: a DBAPI error
  leaves the session needing a rollback, so the `earliest_open` query after it would have raised
  `PendingRollbackError` and taken the whole endpoint down. Reverted, with a comment saying why the
  uglier ordering is the correct one.
- **`0.00%` was reachable and was a lie.** The smoke fixture's only forecaster is its unpriced TSMC
  row, so the priced holdings projected nothing and the yield came out a confident zero meaning "the
  interesting holding is missing". The object is `None` whenever the numerator is zero.
- **Gating the row's `isLoading` on the dividend query hid four already-computed metrics** behind a
  third request. The cards carry their own three states instead.

**Verified on production**, in the browser as well as on the wire: both cards render, `pct` and
`on_cost_pct` each reproduce exactly when hand-divided against `/api/portfolio/summary`,
`unpriced_holdings` is 0, `basis` is `mixed`, and `mobile.mjs` passes 45/45 at 390px with the row's
longest footnote.

**The check worth repeating** if either card ever looks wrong:
`annual_eur ÷ summary.total_market_value_eur × 100` must equal `pct`, and `on_cost_pct ÷ pct` must
equal market value ÷ cost basis. The denominators come from a different code path than the *Market
Value* card's; they were byte-identical when this shipped, and if they drift the two disagree in a
way a user can see. Beware comparing a `pct` to an `annual_eur` fetched minutes apart — the window
rolls with `as_of`, so the total moves by a cent or two across a date boundary. That is the rolling
figure working, not a rounding bug.

## Shipped 2026-08-05 — the permanent sync warning, and a yield on cost that punished buying more

Two reported issues, both real, and neither where it looked.

**1. The 27-attribute warning on every sync.** The sanitizer was working exactly as designed — ibflex
0.15 cannot model `figi`, `serialNumber`, `weight`, `subCategory`, `Trade.notes` and the rest, and
dropping them is what stops one schema addition aborting the whole document. The defect was that all
of it went into `warnings[]`, so a healthy sync carried a permanent unreadable banner. **A warning
that is always present and never actionable trains the reader to skip the banner** — the same banner
that carries a skipped tax lot or an unconvertible dividend, which is the only reason it exists.

Drops are now classified by consequence: loud when the attribute is one the extractors read
(`INGESTED_ATTRS`), recorded in the run's `details` as `flex_schema_notes` otherwise. All 27 on this
account are cosmetic, so the banner should be **empty** after the next sync.

The guard matters more than the fix, because the two directions are not symmetric — a spurious entry
is merely noisy, a missing one makes a real problem silent. `tests/test_flex_attr_coverage.py`
AST-walks the extractors and intersects with ibflex's own dataclass fields rather than trusting the
map, **and caught a genuine omission on its first run**: `extract_transfers` reads `Transfer.date`,
which the hand-written map had discarded as a Python builtin.

**2. Yield on cost fell when you added to a holding.** Asked about sell-and-rebuy; the same defect was
already live on **nine of fifteen rows**. It divided income *already received* by *current* cost, and
those describe different positions once the size changes: MCO read 0.35% against a real forward rate
of 0.84%, SPGI 0.53% against 0.93%. Unbadged, too — the `†` partial marker was only ever on the
trailing yield column. It also disagreed with the Performance card, which has always been
forward-over-cost: one name, two definitions, two screens.

Now forward-over-cost everywhere, so the gap against the yield beside it is appreciation and nothing
else. Sell-and-rebuy at a higher price still lowers it, which is the honest answer — more capital
committed for the same income — but it now equals exactly the new cost's rate rather than a blend.

Backend 705 → 723.

**Yield on cost is verified on production.** All 17 rows carrying both figures satisfy
`yield_on_cost_pct ÷ forward_yield_pct == market value ÷ cost` to within 2dp rounding, and the
understated rows recovered as predicted: MCO 0.33 → 0.79, SPGI 0.46 → 0.80, MA 0.25 → 0.62,
MRVL 0.12 → 0.28. Four securities that had *no* yield on cost now have one, because they carry a
projection but no trailing income yet.

**The empty banner is verified against the deployed build**, without spending an IBKR request. The
08:00 run after the deploy returned a routine `Code=1001` and correctly declined to re-request, so
waiting on a real statement was not an option and retrying is precisely what trips `1025`. Instead
`IBKRService.parse_flex_xml` — the entry point every sync and the offline CLI both use — was run
inside the production container against a statement carrying this account's own drift:

| input | `flex_warnings` (the banner) | `flex_notes` |
|---|---|---|
| 38 unmodelled attributes across `<Trade>` / `<CashTransaction>` | **empty** | 1 compact line |
| `CashTransaction.type="Broker Fees"` (a field the ingest reads) | 1 warning, *data may be affected* | not filed as harmless |

Pure — no network, no DB, no token, nothing written. The last hop is confirmed present in the running
container (`sync_helper.py:176` copies `flex_notes` → `flex_schema_notes`) and is driven end to end by
`tests/test_manual_xml_ingest.py`, which goes through the same `parse_flex_xml` +
`ingest_flex_statement` pair the scheduled job does.

**Confirmed against a real statement on 2026-08-07** — the one thing the constructed document could
not settle. An `ibkr_manual_xml` ingest of a genuine Client Portal download returned `flex_warnings`
**empty** and filed all **26** unmodelled attributes (`figi`, `serialNumber`, `subCategory`,
`Trade.rtn`, `initialInvestment`, …) into `details.flex_schema_notes` as one compact line. The
account's real drift is the drift we modelled, and the permanent banner is gone.

Note `/api/scheduler/history` names the field **`type`**, not `sync_type` — reading the wrong key
makes every run look untyped, which briefly looked like a second bug and was not one.

## Shipped 2026-08-05 (later still) — the Activity ledger showed every dividend twice

**Found by reading the screen and disbelieving a number** — "why are there 2026 dividends marked *est.*
when the transfer happened in January?" The labels were the symptom. The defect: the same dividend was
listed **twice**, once as the yfinance estimate under its ex-date and once as the IBKR actual under its
pay date a fortnight later. `GOOGL est. 06-08` beside `GOOGL 06-15`, `SPGI est. 05-29` beside `06-10`.

`ActivityService._dividends` was the only reader not applying `_splice_by_era`. Measured before the fix,
era boundary 2026-02-18: **31 duplicate rows, 47 of 113 CHF — dividend income overstated 72%.**

Nothing the app *computes* was wrong. The breakdown, the summary card, XIRR and the tax report all
splice, which is exactly why this survived: the only wrong surface was the one that merely displays.

Two things worth carrying forward:

- **The boundary cannot come from the window.** `_splice_by_era` derives `min(ibkr_dates)` from the rows
  handed to it — right for readers that splice the whole history, wrong for the ledger, which windows
  first. So `DividendRepository.earliest_ibkr_payment_date()` now exists, mirroring
  `CashFlowRepository.earliest_flow_date()`. `_splice_by_era(get_between(...))` is the obvious-looking
  form and is the bug; a test fails if anyone writes it.
- **Partial alignment is the nastiest form of the duplication failure.** The docstring said zero rows
  are excluded "on the same test the two dividend readers use" — singular. It was written *with* the
  readers open and copied one of their two rules, so it reads as deliberate rather than forgotten.

**Expect the ledger to change visibly**: 31 fewer dividend rows and a dividend total falling from ~113
to ~66 CHF. That is the correction, not data loss. Pre-boundary estimates (29 rows before 2026-02-18)
are still there and still badged — dropping those is the mirror-image bug, which once blanked every
pre-IBKR month from the dividend card.

Also: **yield on cost is now a column in the Positions table**, from the breakdown the Dashboard already
fetches, so it costs no request. 19 of 36 rows show a figure and 17 show a dash — a holding that
distributes nothing has no rate, and every accumulating ETF reads that way correctly. Sortable, with
absent sorting below any real yield in the default descending order. `detailLimit` went 4 → 5 so the new
detail row does not push Weight behind the phone's "Show all" disclosure. `PositionsList` gained the
test file it never had, and the latent `useMemo` dep omission in its sort (`totalMarketValue` was
already missing) is fixed — adding a case that reads a separately-fetched map would have made it bite.

Backend 723 → 726, frontend 343 → 352.

## Shipped 2026-08-06 — the /loop audit batch, pushed after being held

**Pushed and deployed on 2026-08-06 evening.** They were batched rather than shipped one at a time
because `deploy.sh` does a full `down` + `build --no-cache`, so each push costs ~90s of downtime and a
10-minute audit loop pushing every pass would have taken the dashboard down ~9 minutes an hour. None
was wrong on current data, which is what made holding them safe.

The batch rebased onto one remote docs commit, conflicting only in this file's header. **Nothing in
the sixteen threads below is outstanding** — they are kept as the record of what moved and why.

**Checked on production 2026-08-06 straight after the deploy** — the first two are confirmed, the
rest are what to look at next:

- ✅ **The Activity tab lost about half its dividend rows** (Thread 14, compounded by Thread 13's
  boundary-duplicate match): **87 → 44**. That is the correction, not data loss. The check worth
  keeping is the *relationship* rather than a franc total, which goes stale silently: no row with
  `source == 'yfinance_estimate'` may be dated on or after the era boundary. Latest surviving estimate
  reads 2026-01-09 against a boundary of 2026-02-18.
- ✅ **`unpriced_holdings` is on the summary, the timeline and attribution**, and currently reports
  **0** — so the headline total covers the whole book. A yellow notice above the KPI cards is what a
  non-zero looks like.
- **Yield on cost should rise on nine of fifteen rows** (it was dividing received income by current
  cost). Not yet eyeballed.
- **Sharpe, Top 5 Weight and RSI now refuse rather than substituting a plausible number** — expect
  dashes where a `0.00` or a green `0.0%` used to sit on short ranges.

**Two were a matched pair** (`adb992c` adds the completeness signal, `557f82d` acts on it), so they
had to ship together; they did.

### Thread 1 — a zero standing in for "unknown"

The codebase's most repeated bug, found three more times. The refinement worth keeping is **what the
stand-in value would claim**: a `0` volatility looks broken and gets noticed, a `0.00` Sharpe looks like
an answer, and a `0.0%` concentration looks like a *good* answer. **Severity tracks plausibility, not
magnitude** — which is why the concentration one sat in plain sight beside two cards already fixed for
the identical flaw.

- `5f824c6` **Sharpe returned 0** below the minimum sample. Reachable in one click: MTD in the first days
  of a month leaves 2–3 daily returns, and the card drew a green `0.00` captioned *Risk-adjusted return*
  beside a dashed Volatility and Sortino. Its clamp test had also been passing vacuously off the same
  early return.
- `244baa8` **Top 5 Weight drew a green `0.0%`** when nothing was priced — the tone ladder calls anything
  under 50% good news.
- `03a3a48` **`days_held_in_ttm` measured time since first purchase**, not time held, so a sell-and-rebuy
  with a gap reported full coverage and the partial-yield badge never fired.

### Thread 2 — an incomplete sum presented as a complete one

`portfolio_service` values an unpriced holding at 0.00 while its cost still counts, so every total built
that way understates. `find_stale_priced_securities` guarded only the current snapshot.

- `adb992c` **the timeline** — measured on production: at +14 days past the last cached price the total
  read a plausible **+15.3%**, at +15 days **−100%**. That is what a stalled market-data sync looks like:
  a smooth decay to zero, not a gap. Each point now carries `unpriced_holdings`.
- `557f82d` **and it poisoned every risk metric**, not just the line — a complete→incomplete pair is a
  −100% daily return, and `dailyReturnSeries` feeds drawdown, volatility, Sharpe, Sortino. `beta` needed
  the guard separately.
- `eb21c9e` **the headline Market Value**, which is the SBI incident restated: 446.93 CHF once left that
  figure with only a sync warning to catch it.
- `b26f75f` **a missing FX rate slipped past the client's unpriced guard** as a 0% weight, so drift
  advised buying the whole target. It survived because its comment justified the narrow predicate with a
  *false* fact — that a fully-sold holding reaches the client, which `is_open == True` prevents.

### Thread 3 — a fix justified by a false reading of the code it copied

- **`sync_stale_fundamentals` could never bootstrap a security** (newest in the batch). It pre-filtered on
  `get_stale_metrics`, which selects rows that already **exist**, and bailed when that came back
  empty — while the union that would have caught a security with no row sat one call *below* the
  guard. So whenever every existing row was fresh, a newly-bought security never acquired
  fundamentals through `POST /api/fundamentals/sync-stale` at all.

  **`sync_stale_ratings` was fixed for this exact shape and cited this method as the sibling that
  "already unions the two sets".** So did CLAUDE.md's duplicated-logic table, and so did the ratings
  bootstrap test's opening paragraph. All three were describing the *inner* function while the entry
  point pre-filtered. Three places asserting a fix that was not there is what kept it alive; all three
  are corrected. **When citing a sibling as correct, read its entry point.**

  Masked on production today only because all 40 fundamentals rows are stale, so the guard happens to
  pass. One fundamentals run would have hidden the next new security indefinitely.

### Thread 4 — a latent 100× money error

- `976e15b` **a pence quote stored as pounds.** Yahoo reports London in `GBp`; the code `.upper()`'d it to
  `GBP` and left the amount alone. Worse than the factor: normalising the label **defeats the currency
  guard** rather than tripping it, since the normalised code matches the security's own. Latent — this
  account holds no GBP security and its one London line is a USD ETF — but three LSE codes already map
  to `.L`.

### Thread 5 — a failure rendered as an absence

- `bc3cbae` **twelve metrics vanished on a backend error instead of saying so.**
  `PerformanceMetricsCards` and `RiskMetricsCards` returned `null` whenever `metrics` was null, and
  Dashboard's memos return null when their query fails — so an outage did not produce an error state
  on those two rows, it produced **nothing**, and the twelve metrics were simply not on the page. A
  row that disappears is worse than one that fails visibly: a stated failure invites a retry, a
  missing row reads as a feature that was never built. `PortfolioSummaryCards`, in the same folder,
  had the correct branch all along.
  **`e2e/errors.mjs` is the proof and also shows how it hid:** its count of panels reporting the
  failure went **8 → 10**, so the two surfaces it existed to cover had never been in its own tally —
  under `hits >= 4`, a floor far enough below the real count that it could not fail. Now `>= 10`.
  The `null` return survives for the genuine no-data-yet case, pinned by its own test, or the fix
  would turn every empty portfolio into a reported outage.

### Thread 6 — a chart that summed to less than it claimed

- **Two of the three allocation charts dropped a holding whose category is unknown.**
  `get_portfolio_allocation` buckets each position three times; asset type used
  `security.asset_type or 'Unknown'` while sector and geography used `if security.sector:` /
  `if security.country:` and simply skipped it. So those two summed to under 100% while
  `AllocationTab` printed every slice as *"% of portfolio"* — and the treemap sizes by area, so it
  renormalised and still drew a full rectangle. The picture looked complete; only the printed
  percentages were short.
  **The trigger is routine, not theoretical.** `sync_helper` never writes `sector` or `country`, so
  every IBKR-ingested security starts with both NULL while `asset_type` has a `"Stock"` column
  default — a newly bought holding appeared in the asset-type chart and in neither of the others.
  Only `sync_allocation_data` fills them and **nothing schedules it** (it needs Yahoo), so the gap
  lasted until someone ran it by hand.
  Both now use `or 'Unknown'`, the convention three lines above them, and `AllocationTab` already had
  a grey colour defined for that bucket. Five tests, written against the **family** — *every*
  breakdown the endpoint returns must sum to 100% — so a fourth chart is held to the rule without
  anyone remembering to add a case.
  **Checked against production before fixing:** all three currently sum to 100.00/100.01, so nothing
  on screen is wrong today. This is hardening against the next purchase, not a live correction.

### Thread 7 — the consumer missed when its siblings were guarded

- **`computeModifiedDietzReturn` had no unpriced-day guard**, so the Monthly Returns heatmap and its
  **YTD column** were still exposed to the stalled-feed failure the risk row had just been protected
  from. Modified Dietz reads only the two endpoint market values and the flows between them, so an
  incomplete endpoint is not a small error but the whole answer: a stale month end reads as a loss, a
  stale start as a gain, and past the backend's 14-day lookback the period prints **-100%**. YTD is the
  worst case — it ends on *today*, exactly the day a stalled feed breaks.
  It now **trims** leading and trailing unmeasurable points rather than refusing the period. That is
  exact, not approximate: Dietz never reads an interior market value, so the result over the kept days
  is a true return for those days, and discarding a whole month over one stale day would lose more than
  it protects. `partial` rides on the result, the cell is badged `†`, and a footnote explains it —
  a caveat living only in a `title` attribute does not exist on a phone.
  **The lesson is the miss, not the fix.** "I guarded the consumers of `unpriced_holdings`" was true
  and incomplete on the same day: two were found by reading the risk row, and the third only turned up
  by listing every importer of the timeline type. Grep the type, not the screen.

### Thread 8 — two numbers under one name, on one screen

- **`MonthlyDeploymentCard` recomputed the 12-month deployment average** that
  `ContributionsStrip` — a few hundred pixels above it on the same tab — already renders from the
  server's `avg_deployed_per_month_eur`. On live data they read **2,546 and 2,530**: close enough
  that neither looks wrong, far enough apart to be visibly different once rounded.
  The client's version was wrong twice over, both times upward. `monthly` is built from a dict keyed
  only by months that had activity, so a quiet month is simply absent: `slice(-12)` takes the last
  twelve *rows* (which can span more than twelve months) and then divides by that row count rather
  than by the months covered. The server divides by the window's elapsed months, clamped to available
  history, and reports the clamp via `partial`.
  The card now reads the server's figure and names the shorter window when `partial` is set, so a
  four-month-old portfolio stops claiming a twelve-month average. Five tests, including one that
  seeds a six-month gap in `monthly` to prove the rows can no longer influence it.
  **Not currently wrong on this account** — 27 monthly rows from 2024-05 to 2026-07 with no calendar
  gaps, so only the rolling-vs-calendar boundary separated the two figures.

### Thread 9 — a sweep that mostly confirmed things, and two unpinned invariants

This pass found **no live miscalculation**. What it did find were two rules the codebase already
states, applied incompletely — both now pinned.

- **`Dashboard` trusted the *shape* of its stored benchmark selection.** `JSON.parse` was wrapped in
  a `try/catch`, which covers malformed JSON but not well-formed JSON of the wrong shape: `42` and
  `{"a":1}` both parse cleanly and were handed back as `string[]`. `selectedBenchmarks.map(...)`
  feeds `useQueries`, so a non-array throws inside the **root** component rather than a tab — and
  because the value is re-read on every mount, reloading cannot recover it. Clearing site data would
  be the only way out. `RebalanceCard.readTargets` already draws this line for one tab; this is the
  same reader with a larger blast radius. Six tests, including the property that matters: whatever is
  stored, the result must be mappable.
- **`MAX_RANGE_DAYS` was a constant written in two languages with nothing holding them together.**
  The ALL button clamps to `365 * 5` so it never asks for a span the router rejects with a 400, and
  the clamp lands *on* the boundary. Tighten the server and ALL starts 400ing for anyone with enough
  history, with both suites still green. `tests/test_range_limit_agreement.py` reads both files and
  pins them equal — same shape as `breakpoints.test.ts` and `test_deploy_guard_hours.py`.

**Verified correct and not worth re-chasing** (each looked like a defect and was not):
`allocation_service` has no `BaseFx` but inherits the projection from `get_positions_breakdown` —
its total matches `/summary` to the cent under CHF. `ActivityService`'s `counts_as_money_in` uses the
same `DEPOSITWITHDRAW` whitelist constant as `get_deposits()`. `cash_flows.amount_eur` is NOT NULL and
ingest skips rather than storing a null, so the ledger's conversion cannot meet a `None`. Benchmark
timeline points survive market holidays through the 14-day carry-forward (523 points for ~522 business
days), and its cost-basis line tracks the portfolio's to within a sign-changing FX residual, so no lots
are being dropped. The tax report and the dividend breakdown agree to the cent for 2026 (48.14 CHF),
so both apply the era splice. The 0.01 between the dividend chart's month totals and its per-symbol
stack is 2dp rounding across twelve rows, not a gap.

### Thread 10 — rule 1 was enforced in one service out of six

The largest finding of the loop, and it sits on the project's most important rule.

- **Five Yahoo loops kept asking after a 429.** `market_data_service` latches and
  abandons the pass; `fundamentals` (~5 endpoints per security), `analyst ratings`,
  `watchlist`, `allocation`, `dividends` and the scheduler's benchmark warm-up all
  caught the error, logged it, and hit the same IP again seconds later — the exact shape
  fixed for market data on 2026-08-04, five times over. Continuing is what turns a short
  block into a long one.
  Extracted to `app/services/yahoo_rate_limit.py`; `tests/test_yahoo_rate_limit_family.py`
  walks the **AST** for any module importing `yfinance` without consulting it, so a
  seventh service is caught automatically. **Allocation needed the most care**: its
  failure path stamps `allocation_last_updated` to bound retries, so a rate limit would
  have marked every remaining security attempted and suppressed its sector and country
  for the full staleness window — the check runs before the stamp.
- **A pre-existing crash the new tests exposed: two failures in one pass killed it.**
  The handlers call `db.rollback()`, which expires **every** object in the session, so
  the next iteration's `security.symbol` became a lazy refresh — and in async SQLAlchemy
  that raises `MissingGreenlet`. In fundamentals and ratings that read sits *outside*
  the try, so it propagated out of the sync entirely. One security Yahoo has no data for
  is completely ordinary, which made this reachable on any pass with two of them. Each
  loop now reloads through an awaited `db.get`.
- **`_to_eur`'s third site.** After the tax copy and then this file's own copy were both
  fixed to return `None` on FX failure, `compute_dividend_income` still carried the
  original `gross_eur = gross_amount  # fallback: store unconverted`, a few dozen lines
  below the helper it never called. Worse than the other two: it is an **ingest** path,
  so the foreign figure is *persisted* into `gross_amount_eur`/`net_amount_eur` and then
  read by the Dividends tab, the forecast, the forward yield and the tax report's DA-1
  income. The row is now left uncomputed — `shares_held IS NULL` is the sentinel the
  prune CLI already refuses to delete, so it retries once a rate exists.
  **Latent, not live**: TSMC reads 0.80% forward yield on production, so its TWD rows
  converted correctly. `WARM_CURRENCIES` keeps TWD fresh and pre-ownership history is
  skipped, which is what has kept it out of reach.

### Thread 11 — the completeness gap on the one chart that names each security

- **Performance attribution counted an unvaluable holding at zero.** `get_eur_value` returned `0.0`
  when the price *or* the FX rate was missing, and `value_change = end_mv - start_mv`, so a still-held
  position whose feed went stale read as **`-start_value`** — the same shape the disposal term fixed
  for sales, reached by the other route and never covered. An unvaluable *start* fabricates a gain the
  same size.
  This is the worst surface for it: one bar per security, so the fabricated number is the largest bar
  on the chart under the security's own name — not buried in a total. It also inflated every other
  security's `weight_percent` (the denominator was missing the zeroed holding's value) and shifted
  `contribution_percent` through a moved `total_pnl`.
  Unvaluable securities are now excluded from both sides and reported as `unpriced_holdings`, the same
  name and signal as the timeline and the summary. A lot held at *neither* endpoint never reaches the
  helper, so a fully-sold position keeps its legitimate zero — that is what makes exclusion safe.
  **The notice had to move out of the collapsible.** The card is collapsed by default and its
  collapsed summary shows `total_pnl_eur`, the figure the notice qualifies; a caveat you must expand a
  card to see is as good as absent.
  Also: `PerformanceAttributionResponse` had to declare the field or the `response_model` would have
  filtered it off the wire — the trap `test_dividend_summary_contract.py` exists for.
  The smoke fixture's unpriced TSMC makes the new assertion the **non-zero** case rather than one that
  would pass on any book.

### Thread 12 — the wealth-tax base could omit a holding and not say so

- **`holdings_snapshot_as_of` dropped an unvaluable lot silently**, and the tax report summed
  whatever it got. Omitting is the right arithmetic — CLAUDE.md says so, and counting the holding at
  zero would be worse — but the report already treated a snapshot that **raised** as a stated failure
  (`holdings_snapshot_total: None` plus a warning) while a snapshot that quietly returned fewer rows
  produced a plausible number that reads as the complete book. Loud on total failure, mute on partial
  failure, which is backwards: this is the same asymmetry that made the value timeline's `+15.3%` more
  dangerous than its `-100%`, on the one figure in the app that goes on a tax return.
  `last_snapshot_skipped` is a per-run latch naming the dropped securities; the report turns it into a
  `warnings[]` line — the surface `TaxTab` renders as a banner and `to_csv` writes as a WARNINGS block
  — and still serves the figure, because a partial base is the best available and must not be confused
  with the `None` that means no base at all.
  **The tests caught a bug in the fix**: the latch was not reset on the early-return path, so an empty
  snapshot would have inherited a previous date's skip list and reported the wrong date's
  completeness.
  **And `test_api_smoke` was pinning the bug** — it asserted `tax["warnings"] == []` on a fixture
  whose Steuerwert genuinely omits its unpriced TSMC. That expectation now asserts the warning is
  present and names the security; the clean-report direction moved to a fixture that is actually
  clean.

### Thread 13 — the era splice leaked one dividend per security, at the boundary

- **13.7% of 2026's dividend income was double-counted**, on every reader that splices.
  `_splice_by_era` keeps estimates strictly before the first IBKR payment — but the two sources file
  the *same* payment under different dates (yfinance by ex-date, IBKR by pay-date), so the first IBKR
  payment's own estimate sits before the boundary and survives beside the IBKR row it duplicates.
  Found by reading the Activity ledger, boundary 2026-02-18, ASML held on two exchanges:
  `02-09` + `02-10` estimates next to two `02-18` IBKR rows. **Four rows for two dividends.**
  Measured: 2026 read **48.14 CHF**, of which **5.80** is the duplicate pair → **42.34 CHF** correct.
  It moved the breakdown, the summary card, XIRR's dividend inflows, the tax report's DA-1 income and
  the ledger simultaneously, which is why nothing disagreed and nothing caught it.
  Now matched per security, nearest-first, **one-to-one**, bounded by `EX_TO_PAY_MAX_LAG_DAYS` (30).
  One-to-one is what makes the window safe for a monthly payer whose cycle is shorter than it.
  **The width errs toward keeping**: 45 was tried first and deleted a genuine dividend 45 days out —
  too wide removes real income from a filing aid, too narrow leaves a visible, already-badged
  duplicate, and understating taxable income is the worse failure.
  The account's one genuine pre-boundary estimate (MA, 40 days before the boundary) is preserved.

### Thread 14 — the fix from Thread 13 did not reach the ledger

- **`ActivityService._dividends` reimplemented the boundary rule inline**, for a good reason: it
  windows before splicing and so needs the whole-table boundary. That copy was correct the day it
  was written (Thread 13's predecessor) and silently wrong two days later, the moment the shared
  helper gained its duplicate match — every other reader stopped showing the ASML pair and the
  ledger kept showing it. **A copy of a rule stays correct only until the rule changes**, which is
  this codebase's oldest lesson, relearned here on a two-day-old copy of my own.
  `_splice_by_era` now takes an explicit `boundary`, so the windowing caller is a real caller
  instead of a copy, and the ledger widens its fetch by `EX_TO_PAY_MAX_LAG_DAYS` on both sides and
  narrows back afterwards — the IBKR row that pairs with a windowed estimate can fall outside the
  window even when the estimate does not, so asking for 1–15 February would otherwise resurrect it.
  `test_era_splice_boundary.py` now **fails any service that reads dividend rows without reaching
  the helper**, which is the guard that would have caught this class both times.

### Thread 15 — the other two rule-copies in the same file

- **`ActivityService` also reimplemented `_net_eur` and `_is_income`.** Both agreed with the helpers
  to the digit, which is why an earlier pass of this loop looked at them and moved on. That judgement
  was wrong for the reason Thread 14 demonstrated on a two-day-old copy: **agreement is what a copy
  looks like right until the rule moves.** `_net_eur`'s own docstring says "every consumer must" use
  it. Both now call the helpers, and the structural guard covers all three rules — no service may read
  dividend rows without reaching `_splice_by_era`, and none may decide the net-vs-gross fallback
  locally.

**Verified and not drifted** (checked rather than assumed, since CLAUDE.md asserts it):
`lib/dividendGrowth.ts` still matches `DividendService._pct` and the annual-row loop on all four
copied rules — adjacency, the zero-base refusal, 1-decimal rounding, and `yoy_vs_partial` reading the
*previous* row's flag only when adjacent. It remains the one duplicate that has survived, because both
ends write their reasoning down.

### Thread 16 — a stand-in that claimed a maximum rather than a zero

- **`_compute_rsi` returned 100 for a series that never moved.** `avg_loss == 0` was treated as one
  case when it is two: gains with no losses is a real, maximal RSI, while *nothing moving* leaves RSI
  undefined. Returning 100 claims the strongest overbought reading the scale has.
  The cost is concrete, because `_compute_buy_score` reads it: `rsi = 100` scores **0 of 10** on
  technical timing while `rsi = None` scores a neutral **5**. The fabricated value was ten points
  worse than admitting the metric could not be measured — and the neutral branch already existed,
  which makes this a substitution rather than a missing case.
  Reachable on a halted or delisted listing, a fixed-NAV fund or a very illiquid one, and the
  watchlist is where arbitrary tickers get added. Every previous instance of this lens found a zero
  standing in for unknown; this one is a **maximum**, so grepping for a suspicious `0` would not have
  turned it up.
  Seven tests, including a guard-on-the-guard: if the `rsi is None` branch ever stopped being the
  midpoint, refusing would stop being better than guessing and the fix would go inert.

**Verified correct on the same path:** `pct_from_52w_high` and `pct_from_ma200` only compute when
their divisor is present and positive, so an absent one falls through to its own neutral branch. RSI
was the only indicator substituting a confident extreme.

**Expect after deploy:** 2026 dividend income drops ~5.80 CHF across the Dividends tab, the
Performance card, the tax report and the ledger. That is the correction, not data loss.

**After they deploy, check:** the chart and hero row show no yellow notice (nothing is unpriced today);
`summary.unpriced_holdings == 0` and equals the last timeline point's; Sharpe and Top 5 Weight still show
numbers on a normal range and dashes on MTD early in a month; and `days_held_in_ttm` is unchanged for all
twenty rows carrying it, since every holding is continuously held.

## Shipped 2026-08-04 (latest) — Beta was structurally unreachable under a non-EUR base

**The Beta card had never once shown a number on production, and could not have.** It read
*Needs 20 flow-free days (9 so far)* — and 9 was not a thin window, it was an artefact.

`betaAndCorrelation` disqualified a day if *either* series saw a flow, inferring the benchmark's
from its cost-basis step because a benchmark point carries no `external_flow_eur`. That inference is
sound in EUR and false in CHF: **the backend projects the two cost-basis lines into the base currency
by different rules.** `_calculate_timeline_swept` converts each lot's cost at its own `open_date`, so
the portfolio's line is flat on a day nothing traded; `BenchmarkService._apply_base_currency`
converts the *running total* at each point's date, so the benchmark's line moves whenever EUR/CHF
did. Every such day was thrown away.

Measured on production over the 1Y window, replaying the real series through both rules:

| | flow-free pairs |
|---|---|
| portfolio `external_flow_eur` == 0 | **147** of 261 |
| benchmark cost-basis step == 0, in EUR (the cache) | **147** — the same days |
| benchmark cost-basis step == 0, after the CHF projection | **9** |

The 147/147 agreement is the point: both series are built from one set of tax lots, so the benchmark's
line carries no information the portfolio's does not, and consulting it only re-measured the exchange
rate. The fix is one line — test the portfolio's `external_flow_eur`, plus its own cost-basis step for
the one flow that field cannot see (a disposal whose proceeds netted to zero). Sample days go 9 → 147.

Predicted from the cached timelines before shipping: **β ≈ 1.04 / r ≈ 0.73 vs S&P 500**,
**β ≈ 0.82 / r ≈ 0.83 vs NASDAQ**, **β ≈ 0.54 / r ≈ 0.37 vs FTSE 100** — the ordering a growth-heavy
global book should produce. **Measured in the browser on production after deploy: β 1.03, r 0.74 vs
S&P 500**, which is the prediction landing within a hundredth and the strongest evidence the rule now
measures flow rather than the exchange rate.

`frontend/src/lib/portfolioKpis.ts` + its test. The old test *drops a day the benchmark saw a flow on*
encoded the removed rule and is replaced by both halves of the new one. Frontend suite 343 green.

**Committed and deployed 2026-08-05** after a full-diff review that re-derived the claim from the two
backend projection sites (`benchmark_service.py` converts a running total at each point's date;
`portfolio_service.py` converts each lot at its own `open_date`). Authored in a parallel session — the
review happened because a working tree carrying an unrecognised change is reviewed before it ships,
not because anything looked wrong with it.

- **The backend inconsistency behind it is NOT fixed**, deliberately (out of the requested scope). It
  is also visible on the chart: under a non-EUR base the benchmark's cost-basis line drifts from the
  portfolio's by pure FX — order of ~0.3% over the past year, so small and easy to miss. Fixing it
  means converting the benchmark's cost events per lot `open_date`, which the EUR-only timeline cache
  cannot do post-hoc. See *Worth doing next*.

## Shipped 2026-08-04 — DEPLOYED; a follow-up fix is committed but unpushed

**Market data now reprices seven times a day (08/11/13/15/18/20/22 Berlin) instead of three, and the
reason it took more than a cron edit is that adding slots alone would have made the numbers worse.**

`get_missing_dates()` returned only dates with **no row at all**, so whichever job wrote a date first
owned it forever. Read off `market_prices.created_at` on production, not inferred:

- every European close was its **15:00 Berlin mid-session price** — Xetra and Euronext run to 17:30,
  and the job named "after EU close" ran 2.5 hours before it;
- Korea's alternated between mid-session and final depending on whether the 08:00 or the 15:00 job
  happened to land the row first;
- the 22:00 job wrote US closes within ~40s of the bell and nothing ever restated them.

So an *earlier* slot would have frozen an *earlier* price. `PROVISIONAL_PRICE_DAYS` (3) re-fetches a
recent weekday even when cached and the existing upsert restates it — no extra Yahoo requests, just a
wider range on one already being made. CLAUDE.md has the durable rules (*Sync schedule*, and the new
paragraph beside the holiday rule); what follows is only what it does not say.

### Measured on real data, 2026-08-04 19:07-19:12 Berlin

One full market-data pass, **user-authorised** under rule 1 (the only thing that makes a live Yahoo
call permissible), run with the new code against a `.backup` snapshot of the production DB **from a
local machine, not the VPS** — Yahoo's limit is IP-based, so this spent this machine's budget and
could not have cost the server its evening slots. Snapshot deleted afterwards.

Timing chosen to make the bug visible: 19:07 Berlin is 90 minutes after Xetra closed, so the stored
row *had* to be wrong, and mid-US-session, so US names *had* to gain a price.

**40/40 securities, `status: success`, no errors, no rate limit, ~40 requests over ~5 minutes
(~8/min).** And the frozen prices were wrong by real amounts — each of these had been stored as its
15:00 Berlin mid-session value and was restated to the settled close:

| | was (15:00) | settled close | error |
|---|---|---|---|
| XAIX@IBIS2 | 201.25 | 205.50 | **+2.11%** |
| ABEA@IBIS (Alphabet, Frankfurt) | 320.25 | 326.15 | **+1.84%** |
| SMH@LSEETF | 106.14 | 107.82 | +1.58% |
| XNAS@IBIS2 | 58.58 | 59.46 | +1.50% |
| SXR8@IBIS2 (S&P 500) | 713.12 | 719.96 | +0.96% |
| EMIM / IWDA @AEB | | | +0.84% / +0.74% |
| AMZ@IBIS / ASML@AEB | | | +0.23% / +0.12% |

So the European sleeve was understated by up to ~2% every day, and the daily chart kept it
permanently. **22 US securities gained a price for today that they would not otherwise have had until
22:00** — prod's 15:00 Berlin job runs at 13:00 UTC, before the 13:30 UTC US open, so no row existed
at all. Three rows were correctly left alone: Korea and Taiwan close before 15:00 Berlin, so their
stored values were already settled, and the refresh re-read them and got the same number — the "does
not thrash an already-settled price" half working.

One reporting wart noticed, pre-existing and not touched: the pass reported `prices_fetched: 234`,
but that is `sync_security_prices` returning **rows in the window**, not rows written (~80). The field
has always over-reported; it will simply look larger now that a pass always writes something.

### Deployed and confirmed on production, 19:21 UTC

`5093be5` live. Nine jobs registered with the declared hours; the retired
`market_sync_eu_close` / `market_sync_us_close` were **pruned** from the persistent store, so nothing
runs twice. `scheduler_jobstore_persistent` and `write_auth_enabled` both still true.

**A pass was then run on production by hand at the user's request** (19:35 Berlin) and corrected the
live rows exactly as the snapshot predicted: nine European closes restated (XAIX +2.11%, ABEA +1.84%,
SMH +1.58%, XNAS +1.50%, SXR8 +0.96%) and **24 US rows created where there had been none** — half the
book by cost basis had no price for the day, because the old 15:00 Berlin slot fires at 13:00 UTC and
the US opens at 13:30. `status: success`, no rate limit, no errors.

It was run through `SchedulerService.sync_market_data` inside the container, **not** through
`POST /api/market-data/sync`, and that distinction turned out to matter — see below.

**Two things the live run exposed, both fixed the same evening (unpushed):**

- **`POST /api/market-data/sync` had its own copy of the securities loop and therefore no rate-limit
  breaker.** The breaker went into the scheduler's copy only, leaving the *public* route asking Yahoo
  for another ~38 securities after a 429. Now both delegate to
  `MarketDataService.sync_securities`. Two tests: the sweep stops, and a source check that neither
  caller has re-grown its own loop. **The AST lens in CLAUDE.md could not have found this** — the two
  copies shared no function name, and "router-to-service pairs are noise" argues for skipping it;
  what identified it was asking which paths reach the same *upstream*. That reasoning is now in
  CLAUDE.md beside the lens.
- **`created_at` is not bumped by a restatement.** `bulk_create` updates only the columns supplied,
  and the price dicts carry no `created_at` — right for the name, but it is the column that *proved*
  the freeze and it is useless for confirming the fix. A troubleshooting row added earlier the same
  day said to check it; that advice was wrong and is corrected. Verify by the price changing.

**Checked, and clean.** Every market-data pass since the deploy reports `rate_limited: false`,
`status: success`, 40/40 securities and zero warnings — including the 20:00 Berlin slot
(`2026-08-04T18:05:31Z`), the first real run on the new schedule. So 2.8× the Yahoo traffic from the
VPS's own IP is not provoking a limit, which was the open question.

**Still to check:** that the 11:00 slot settles Korea's close. The 08:00 run catches KRX mid-session
and 11:00 is the first pass after Seoul shuts, but the deploy landed at 19:21 so no 11:00 slot has
run yet. Compare a KRX row's value across the 08:00 and 11:00 passes — **not** its `created_at`,
which is not bumped by a restatement.

Also landed, both found while sizing the traffic increase rather than sought:

- **A Yahoo 429 no longer keeps asking.** This repo's guide credited `market_data_service.py` with
  "rate-limit detection that aborts the run"; it aborted only the ticker *variations* for the
  security in hand, so the caller logged a failure and moved on to the next of 40. Harmless at three
  passes a day, not at seven. `MarketDataService.rate_limited` latches and `sync_market_data` breaks
  with a warning.
- **Both `ops/finish-deploy.*` twins had the wrong slot hours for four days** — written with
  13:00/20:00 on the very day those were retired for 00:00/06:00, so the interactive guard would
  report "clear of every sync slot" at 05:58 Berlin. Only `auto-deploy.sh` was under test; all three
  copies are now read by `tests/test_deploy_guard_hours.py` against `ALL_SYNC_HOURS`, and the
  scheduler-side check no longer regexes literal `hour=` digits out of the source (which would have
  silently ignored any slot registered from a loop or at a half-hour). The twins also gained the
  midnight wraparound they needed from the moment 00:00 became a slot.
- Stale hour lists corrected in `app/main.py`'s startup log (now built from the constant) and
  `config.py`'s comment.

Suites: backend **664 → 682**, all offline. Frontend untouched.

## Shipped 2026-07-31 — DEPLOYED and verified

Live at 19:31 Berlin. Suites: backend 357 → 462, frontend 45 → 91, `tsc -b` and `npm run build`
clean. **Write auth is ON in production** — verified from outside the host: a write with no key and
a write with a wrong key both 401, reads still 200. All five scheduler jobs re-registered after the
rebuild — **which was read at the time as the persistent job store working, and was not**: the
in-memory fallback re-registers exactly the same five, and the store had never opened at all (see
*Recent sessions*, 08-01). **The guarded `auto-deploy.sh` is
installed** on the VPS at 20:11 Berlin (5140 bytes, `-rwxr-xr-x`, byte-identical to `ops/`), so
deploys now defer rather than landing inside a sync slot.

Two things about that deploy worth knowing, both cost time on the day:

- **`commit` reads `unknown` on this one deploy, and that is expected.** `deploy.sh` pulls the repo
  *itself* (line 13), so the copy already executing is the one from before the pull — bash does not
  reload a running script. Any deploy that changes `deploy.sh` therefore runs the **old** logic once,
  and the `GIT_COMMIT` export it gained is missing for exactly that run. The next deploy stamps the
  real sha. The finish-deploy scripts now accept `unknown` + the `write_auth_enabled` marker as
  proof the new build is live, instead of hanging 15 minutes over a cosmetic stamp.
- **`docker compose restart` does not reload `env_file`.** Compose reads it when it *creates* a
  container; `restart` reuses the existing one with its original environment. So `API_ADMIN_TOKEN`
  landed in `.env` and was silently ignored — `write_auth_enabled` stayed `false` while everything
  reported success. **`docker compose up -d`** is required. Both scripts now use it *and* re-check
  `/health` afterwards rather than assuming, because the failure is invisible: a site whose write
  API is still wide open looks exactly like one that is locked down.

**The bundle is now code-split**, which changed the shape of a deploy for users. It was one 891 kB /
264 kB-gzipped chunk; it is now four eager files (app 52 kB gz, react 57, charts 119, query 15) plus
one per deferred tab. Two separate wins: the seven non-default tabs no longer load at first paint,
and — the bigger one, given the VPS redeploys within 10 minutes of any push — **the chunk that
re-hashes on every deploy fell from 264 kB gzipped to 52 kB**, because vendor code now sits in files
that only change when a dependency does. nginx already serves `/assets/` `immutable` for a year, so
that caching is real rather than theoretical. Recharts stays eager on purpose: three components on
the *default* Performance tab use it, so deferring it would only move the wait.

`ui/LazyTabPanel.tsx` exists because splitting introduced a failure the eager imports could not
have. Chunks are content-hashed and the VPS redeploys constantly, so a browser holding the page
across a deploy requests a filename that no longer exists — unhandled, that rejection reaches the
app-level boundary in `App.tsx` and blanks the whole dashboard, which is strictly worse than before
the split. The panel-scoped boundary recognises the wording Vite/webpack/Safari each use for it,
says a new version shipped, and offers the reload that fixes it (`index.html` is `no-cache`, so a
reload genuinely resolves it). A non-chunk error still shows its real message — mislabelling a
genuine bug as a deploy race would have users reloading forever.

Verified in a real browser against the built output under the production CSP, since chunk boundaries
are a property of the build that no unit test can observe: 4 chunks at first paint, none of the
seven deferred ones; each tab fetching its own chunk on click, all 200; every panel mounting; zero
CSP violations.

**Verified against a production DB snapshot and in a real browser** (Playwright — now committed as
`e2e/`), not just through the test client. That is worth stating because it found three defects the whole green
suite did not:

- **Trades were converted wrong.** `trades.proceeds`, `trades.realized_pnl` and
  `corporate_actions.proceeds` are stored in the trade's **own** currency — there is no `_eur` column
  on either, unlike `cash_flows.amount_eur` and `dividend_payments.*_eur`. The ledger applied only
  the EUR→base factor, so a CAD 30.27 realized gain read as CHF 27.85 instead of CHF 17.15 and the
  ledger's realized total sat 6.8% away from `/api/portfolio/summary`. Both now agree to the cent.
- **67 BUY rows showed `CHF 0.00` realized.** IBKR sends `fifoPnlRealized=0` on every buy; rendering
  it asserts a realized result where there is none.
- **Fractional share counts rounded to `0`** (and a fractional sell to `-0`). This account trades
  0.5 SOXQ, 0.3 MU, 0.1 CSU routinely, so it was most rows, not an edge case.

Confirmed on the snapshot: 194 events in the default window; **22 in-kind transfer rows badged
*Transfer · not money in*** and 26 deposits + 1 withdrawal counted, matching the DB exactly; the
ledger's deposit total equals `/api/portfolio/contributions`'s `deposits_eur` to the cent (two
independent code paths). All eight tabs render with zero console errors. With the backend stopped,
eleven surfaces report the failure explicitly and none falls back to an empty-data message.
Keyboard: arrow/Home/End across the tab strip, Enter on all four collapsible headers, 9 headers
carrying `aria-sort`.

What landed, and why each was worth doing:

- **`_ttm_growth_from_quarterly` was duplicated and divergent**, so one security could report
  different earnings growth on `/api/fundamentals/portfolio` than on `/api/watchlist`. Now
  `app/services/ttm_growth.py`, with the 5–7-quarter tier the fundamentals copy lacked.
- **Every chart date boundary went through `toISOString()` on a local date**, so YTD/MTD started a
  day early in any positive-UTC-offset zone. `lib/dateRanges.ts` is local-calendar throughout.
- **Inception is read from the data**, not hardcoded twice (`2024-05-28` for ALL, `2024` for the tax
  year picker) — see *Needs a human*.
- **Four cards and every sortable column in Fundamentals/Watchlist were mouse-only**, and the tab
  strip had no ARIA at all. Shared `CollapsibleCardHeader` / `SortableTh`, full WAI-ARIA tabs, and
  21 jsdom tests pinning it.
- **Four more surfaces let a backend failure read as empty data.** That class is now closed.
- **The write API had no authorization anywhere** — off by default, see *Needs a human*. Alongside:
  a per-IP rate limit, `X-Request-ID` on every response with a redacting 500 handler, and `/health`
  reporting version/commit/scheduler/auth, rendered in a new footer.
- **An Activity tab.** `trades`, `corporate_actions`, `cash_flows` and `dividend_payments` were all
  ingested and depended on with no read surface at all. Cash rows carry `counts_as_money_in`, so the
  transfer audit CLAUDE.md prescribes is a UI action rather than an ssh command.
- **A deploy landing in a Berlin slot no longer loses that sync** — persistent APScheduler job store,
  which is *Worth doing next* item 9 from yesterday.
- **`prune_empty_dividends` was deleting the forecast's cadence basis.** Found while assessing
  whether to automate it — the answer turned out to be "fix it first". The CLI deleted any computed
  row carrying no income, on the stated grounds that it "deletes only rows the readers already
  ignore". That stopped being true when the forecast was changed to infer cadence from the **raw**
  history: a pre-ownership yfinance row is income-free *and* load-bearing, and dropping it is what
  the "only 15 of 36 payers project" bug looked like. Running the documented cleanup would have
  quietly reverted that fix for every recently-bought payer. Prune is now bounded by the ingest
  window it should always have mirrored — a row goes only if it is older than the history
  `sync_dividend_data` deliberately retains — and a security with no lots is left alone entirely,
  matching ingest's own refusal to guess a cutoff. The existing test fixture had masked it by
  holding 10 shares with no tax lot, which cannot happen in real data.
- **The browser checks are in the repo** as `e2e/`, a package deliberately separate from `frontend/`:
  `deploy.sh` runs `npm ci` inside `frontend/` on every `--no-cache` rebuild and Playwright's
  postinstall pulls ~150 MB of Chromium, which would tax a deploy that runs every 10 minutes. Nothing
  in the deploy path touches `e2e/`. Six scripts with a table of preconditions in its README —
  `a11y` (14 checks), `sweep` (16), `csp` (4), `chunks` (33), plus `errors` (backend deliberately
  down) and `ledger` (needs a prod snapshot). Screenshots are gitignored: real account data, public
  repo.

  Committing them surfaced one flaw. **`csp.mjs` used to run against the dev server, where it could
  not have been meaningful:** Vite injects an inline `<script type="module">` for react-refresh and
  `script-src 'self'` blocks it, so the app never boots and the script reports a violation that
  cannot exist in production. It now targets `vite preview`. The *conclusion* was never wrong — the
  CSP had already been verified against the real build (see *Watch after the first deploy*), and
  re-running it there passes 4/4 — but the reusable script was measuring Vite's HMR transport.

## Shipped 2026-08-03 — mobile layout — deployed and verified on production

Live as `c81d883`. The app is built to 390x844 now, and the checks below were re-run against
production rather than only against the local stack — the local DB has null prices, so it cannot
exercise the shapes that actually break.

`e2e/mobile.mjs` is the new check and the reason to trust the rest: 45/45, zero horizontal overflow
on all eight tabs. It went 36/45 on its first run against the then-current tree, naming
`div.flex.items-center.gap-2 w=493` — the nine chart range buttons in a 324px card, 204px of document
overflow. Everything else was verified at both viewports: unit 268 → 308, `a11y` 17/17, `sweep`
16/16, `errors` 15/15, `csp` 4/4, `chunks` 33/33 (so the code-split lazy panels still defer).

Every table below `sm` is a card list — symbol and description left, headline figure and delta right,
the rest as label/value pairs behind a disclosure — rendered from the **same** `Column[]` as the
desktop table. See CLAUDE.md *The mobile layout* for the rules; what follows is only what that does
not say.

**Two bugs found that were not about width at all:**

- The **Performance tab was permanently untappable on a phone**. `TabsList` is `justify-center`, and
  a centred flex row wider than its scroller overflows on both sides with no way to scroll left.
  Invisible to every check that opens at 1440px.
- **`mobile.mjs`'s own sticky assertion was vacuous** as first written — a non-sticky strip scrolls
  off the top and reports a large *negative* offset, which `<= 1` accepts.

**Worth knowing before the deploy:**

- **One deliberate desktop change**: `PortfolioValueChart`'s X axis no longer labels only the 1st of
  a month, so ticks will not land on month firsts. The old rule defeated recharts' own thinning
  (which drops ticks by *index*), leaving up to 24 full-length labels with empty strings between them.
- **Cell padding is unified** at `px-2`/`px-3` via `density`, so a few desktop tables shift by a few
  pixels of column gap. Deliberate; eyeball it if it looks off.
- **KPI cards are two-up below `md`** with Market Value as a full-width hero, and their values are
  `text-lg sm:text-2xl`. A 2-up cell has a 141px interior and `text-2xl` renders a seven-figure
  currency string at ~182px.

## The overnight batch — pushed and live

The autonomous loop of 2026-07-31 into 08-01 (`/loop 10m`, ~22 iterations) was pushed on request and
auto-deployed at **07:32 UTC**. `/health` reports the sha; `git log` is the record of what changed,
and **each commit message carries its own reasoning**. What follows is only what the log does not
give you. The durable rules are already in CLAUDE.md (*Client-side analytics*, *The dominant failure
mode*, the naive-UTC paragraph in *Database schema*, the Alpha Vantage note under rule 1).

**Verifying that deploy is what found the job-store bug above** — the one item in this file that had
been marked "watch after the next deploy" and was, until someone actually looked, believed fixed.

### Wants Simon's judgement

- **SOXQ's geographic split in `app/etf_mappings.py` is my estimate** (US 80 / Taiwan 10 / Netherlands
  8 / Korea 2), skewed more US than SMH's because the PHLX SOX index only takes US-listed names. The
  sector (100% Technology) is unambiguous; the geography is approximate like the rest of that file.
- **Allocation targets are stored in localStorage, not the database.** Chosen because `/api/` is public
  and auth-gated, so a route storing portfolio intent is more surface than the feature earns — but
  targets do not follow you to another browser. Reversible: the lib takes a plain map.
- **`safe_float` now rounds to 4 decimals on both endpoints** (previously only the watchlist). ≤5e-5 on
  any metric, and it makes Fundamentals and Watchlist agree, but it does change displayed digits.
- **`securities_without_data` now counts missing *data* rather than a missing timestamp.** Needed,
  because the timestamp had to start recording failed attempts to bound retries — but it changes what
  the Allocation tab's banner counts.

### Verified and **not** bugs — recorded so nobody re-chases them

`ActivityTab`'s `amount_base ?? 0` (unreachable — `BaseFx.convert` never returns `None`);
`DividendKpiCards`' `prev_net_eur ?? 0` (over-permissive TS type only);
`PortfolioValuePoint.external_flow_eur`'s `0.0` model default (the service supplies it on every row);
the first timeline row's flow (already fixed by the pre-window seeding loop); the success-path
`SyncRunRepository.record()` outside the `try` (identical in **all five** CLIs, so deliberate);
`expire_on_commit=False` making post-commit reads safe; `security.asset_type` and `asset_category`
both being real columns; `_add_to_category` merging by symbol (which is what correctly combines a
dual-listed ASML); `lib/monthlyReturns.ts` already routing through `externalFlow`; a currency switch's
unfiltered `invalidateQueries()` (~20 refetches against a 120/min limit, and it cannot reach Yahoo
because the benchmark cache stays EUR); `AnalystRating.consensus` already answering "No Rating" on five
zeros; and `market_price_repository.bulk_create` genuinely updating `source` on conflict.

**`benchmark_service.calculate_benchmark_value_over_time()` was audited line by line and is correct** —
it feeds the new beta metric and re-reading 200 lines is expensive, so: close events exclude **on** the
close date; pre-window events fold in without a seeding loop; share and cost events are appended *and
skipped* together, so a zero value can never be emitted against a live cost basis; and
`_apply_base_currency` converts all four money fields. One behaviour to know: when shares are held but
a price or FX rate is missing, that day is **omitted** rather than zeroed — `betaAndCorrelation` skips
such a pair by design and the date self-heals.

### State

Suites **backend 664 / frontend 268**, `tsc -b` and `npm run build` clean. `e2e/`: `a11y` 17/17,
`sweep` 16/16, `errors` 15/15, `chunks` 33/33, `csp` 4/4 — everything except `ledger`, which needs a
production snapshot.

The deploy is verified beyond `/health` returning 200: the five served asset hashes match a local
`npm run build` byte for byte, and the read endpoints answer 200 across portfolio, allocation,
contributions, dividends, activity and tax. `/api/portfolio/benchmark` and `/api/dividends/summary`
were deliberately **not** called — both can reach Yahoo on a cache miss.
