# Working state

> The perishable half of the project guide: what needs a human, what is being watched, what is
> unverified, and what is next. CLAUDE.md's *Keeping STATUS.md current* says when and how to edit
> it. Subsystem reasoning is in `docs/<topic>.md`; shipped write-ups in
> [docs/shipped-log.md](docs/shipped-log.md); accepted rough edges in
> [docs/troubleshooting.md](docs/troubleshooting.md); local-dev traps in
> [docs/local-development.md](docs/local-development.md).

**Last updated: 2026-10-25.** The **Company tab** (one company's SEC EDGAR fundamentals history,
docs/company-financials.md) is deployed and filled for the held filers **and the Nasdaq-100**
(102 companies with figures, keyed by CIK), with key figures, valuation multiples (weekly Yahoo closes) and ROE, gross margin, operating income,
buybacks & dividends, cash & debt and 3/5/10Y CAGRs (docs/shipped-log.md 2026-10-25). PR #80 (Swiss residency)
is merged and deployed (`08de1a0`); the residency date still has to be entered. On 2026-10-17 both of the day's branches went
to `main`: the owner's batch
(dividend raises, resizable forecast chart, base-currency market caps, daily held-only
fundamentals; deployed, backfill run) and the web session's fintech pass
(`ccr-7289b752-aev50c`), merged with one held predicate (`security_repository.held`). See
docs/shipped-log.md 2026-10-17. #2–#12 shipped the same night, and the fintech UI pass on every
tab (docs/frontend.md *Design foundation*). **A five-area audit (2026-10-18) filed #13–#51**, each
verified first by an offline repro; Tier 1 (#13–#18, #41) and waves 2–4 (#19–#37, #52) shipped
the same day, then wave 5 (#40, #42–#46) and #39, #48–#50 (docs/shipped-log.md 2026-10-18).
#47, #51 and #38 followed: every audit issue is closed. **A second round (five code reviewers
and a product review) filed #53–#78** the same afternoon, each verified by an offline repro or a
code trace; all of them shipped that evening (waves 1–3, the product issues #74–#78, #72).

---

## Needs a human

- **Decide on the non-root backend container (#51).** The plan, the per-deploy `chown` and what
  happens if it is missed are in docs/deployment.md *Not done: non-root container*.
- **Raise HSTS's `max-age`** in steps (on at 300 s since 2026-10-18) once nothing has broken;
  edit the live vhost, then copy it to `ops/nginx-portfolio.conf.example`.
- **Install the SQLite pre-commit hook in your clone (#36):** `git config core.hooksPath ops/hooks`.
  This clone's `core.hooksPath` still points at the pre-rename folder
  (`…\IBKR Investment Tracker\.git\hooks`), which no longer exists, so no git hook runs here
  today. CI refuses a tracked SQLite file either way.
- **Enter the Swiss residency date** (deployed 2026-10-25; Tax tab → *Swiss residency*, or
  `PUT /api/settings/swiss-residency`): until it is set the 2026 report still counts the months
  before the move, and 2025 still reads as a Swiss year. Then check that 2026 starts on the move day and
  2025 shows only its one warning (docs/tax.md *Swiss residency*).
- **Confirm Taiwan's DA-1 treaty rate (#62).** The tax report splits TW withholding at 15%
  (art. 10(2) of the 2007 agreement), added on review with medium confidence; check it against
  the ESTV's DA-1 country list before a filing relies on the split.
- **Tell the `ibkr-sync-validator` routine about the weekend schedule (#9)** — and that
  `/api/scheduler/history?limit=0` (or negative) answers 422 since #64 (it used to clamp to 1). Its prompt lives
  outside the repo (claude.ai routines). Since 2026-10-17 Sunday has no 08:00
  `market_data_only` run and Saturday has only the 08:00 one; it may flag the missing rows.

- **Holding logos: look at them.** 235 companies carry a logo (2026-10-18), measured on the
  live listings: Look-through top 100 100/100, Watchlist 24/24, Positions 35/35; the S&P 500's
  top 100 100/100 and the Nasdaq-100 94/95 (the miss a cash line). Every table listing securities shows them, the Watchlist included
  (docs/logos.md *Where*). Judge the avatar styling (white circle, hairline ring) with real
  logos. GOOG/GOOGL/ABEA carry the Google "G" (owner's choice, 2026-10-18; logo.dev's
  `google.com` mark imported as `manual`, so the pass never puts Alphabet's "A" back).

- **Crypto history before 3 Oct 2026: run the rebuild or drop it — decide before ~2026-12-31.**
  After that, CoinGecko's Demo plan (365 days) can no longer price 1 Jan 2026 and the stand-in (the
  3 Oct basket at each day's price) becomes permanent. Next: `crypto_rebuild_holdings --dry-run
  --fees subtract` on the server, then the real run on the owner's word; `--reference 2026-09-01`
  is the accepted fallback if 23 Aug still refuses. Up to 400 CoinStats credits a run
  (docs/crypto.md, *The reconstructed span*).

- **The Flex Query period: confirm "keep 30" (nothing to do in the portal).** The live query is
  `Last 30 Calendar Days` (measured); an older doc recorded a narrowing to 3 the account never ran.
  `flex_window_days()` measures the window, so nothing breaks at any N. Never re-introduce a
  hardcoded threshold.

- **Upload the finpension export when it has new transactions** — no pricing deadline
  (docs/pillar3a.md, *Prices*). `scp` it to `/root/p3a.csv`, `docker cp` it to `/tmp/p3a.csv`,
  then `docker exec backend-portfolio-backend-1 python -m app.cli.import_finpension_csv
  /tmp/p3a.csv --dry-run`, then without `--dry-run`. It replaces the account wholesale and refuses
  a file shorter than what is stored.

- **VPS tidy-up from the 2026-10-04 database move (optional).** Point the VPS `DATABASE_URL` at
  `/app/data/portfolio.db` (the leftover files went on 2026-10-18). Keep the `backend/portfolio.db` link while a
  rollback past `9e57366` is conceivable. `failed-deploys/` is never pruned; check `df -h` and
  logrotate while there.

## Watching

- **IBKR: 18:00 Berlin (12:00 ET) is the primary slot, 00:00 the recovery.** A normal day: a
  `full_sync` success at 18:00, `ibkr_retry_1` `skipped` / `already_generated_today` at 00:00; an
  `error` at 00:00 means both failed. ET Sunday and Monday read `skipped` / `no_new_trading_day`.
  A missed window recovers by a Client Portal download through `app/cli/ingest_flex_xml.py`
  (docs/flex-and-sync.md).
- **`full_sync` on a day IBKR refuses:** the 2026-10-04 18:00 run (`Code=1001`) should carry a
  real `details.market_result`, not `null`, with top-level `status` `error`.
- **MCO and MRVL forecast off 2 samples** — check `manage_mappings list` for `DIVIDENDS PREDATE
  MAPPING` before trusting either.
- **Misfire recovery never observed end to end.** `/health`'s `scheduler_jobstore_persistent:
  false` means the store fell back to memory.

## Watch after the next deploy

Only what is still unverified.

- **The Company tab (2026-10-25):** the first Sunday 05:00 `company_financials_refresh` row in
  `/api/scheduler/history` (a success, `fetched` 0 when the first fill is under six days old;
  `universe` 100); beside it the first `company_prices_refresh` row (a success, no
  `rate_limited`, `fetched` 0 after a fill under six days old);
  the owner's eye on the charts (enlarged the same day: 2 per row, hover headline, legend
  toggles, expand, Total | Per share) and whether the default 10Y TTM reads right. NVDA's TTM FCF
  starts at Q3 FY2024 by design (older capex is an extension concept or survives only as nine
  months and a year; docs/company-financials.md *Known gaps*).
  SPGI and NXPI end at Q1 2026 because EDGAR's companyfacts held no later 10-Q on 2026-10-25;
  the Sunday pass should bring Q2.

- **The fintech UI pass with the owner's own eyes** (2026-10-17): every tab was screenshotted in
  both themes against production data through a routed preview; the dividend summary, the
  benchmarks and the crypto reads were seen only with made-up fixtures (Yahoo / the key). The sticky
  strip's shadow on scroll was measured in both themes. Look-through's constituent logos were
  filled on 2026-10-18 (docs/logos.md *Filled*); companies beyond those baskets' top 100 stay
  monograms.
- **The two UI fixes (`bluegill`, 2026-10-18), in a real browser:** verified on production at
  1366 dark — the Watchlist's Stock column stays pinned while the table scrolls sideways, and
  Dividends *Expected next* no longer overflows sideways. Unseen: the calendar's thin themed
  scrollbar (headless Chromium draws none) and both fixes in light mode.
- **Watchlist logos for names never held:** fetched 2026-10-18 and seen on production (CATL,
  TSM, SOFI, SNDK); a ticker no basket lists stays a monogram.

- **The fintech pass on real data** (merged 2026-10-17; verified only on a synthetic book): the tax
  report's totals under their own columns in all four tables; the hero's 1Y return close to the
  XIRR card; Fundamentals and `/api/fundamentals/status` listing held securities only; the
  benchmark lines in their new colours; the bundled font and card shadows under the production
  CSP (`e2e/csp.mjs`). On the estimate-era years (2025 and the boundary year): estimated rows'
  withholding blank in the CSV, `excl. N est.` / `incl. N est.` on the totals, and `excl. est.`
  only on the countries that hold estimates. `e2e/ledger.mjs` and `e2e/axis.mjs` against a
  snapshot — both were rewritten against the synthetic book. `/api/settings`
  `base_currency_unavailable` is `null` (production has EUR→CHF rates), and no ingest log line
  reports a dividend or trade skipped for a missing currency.

- **The product pass on production** (verified only on a local build proxied to the live API):
  the bundled Inter font loads under the production CSP (`e2e/csp.mjs`), the favicon renders, and
  the Dividend Income card and benchmark lines — aborted in the local check because they can
  reach Yahoo — render in the new styling. Crypto unlocked was not seen (needs the admin key).

- **The crypto chart's date axis (`dateAxis`)** — not seen in the browser (needs the admin key).
- **The audit's Tier 1 (#13–#18) on production:**
  - A scheduled job that met a dashboard-started pass shows a late start, not `skipped /
    pipeline_busy` (#16); `skipped` now means the gate stayed held for 9 minutes.
  - `fx_result.fallback_available` is on every row (seen 2026-10-18) and `frankfurter_failed` names
    any currency both providers refused (absent when none did); a "no published exchange rate since" warning appears only after three
    ECB publication days without a rate for a held or base currency (#17).
  - Fundamentals: no "N holdings have no data yet" line for the 3a funds; Update/Refresh never
    leaves the banner up (#18).
  - The IBKR staleness messages now read "the newest data is from the sync of …" (#13).
- **The audit's wave 2 (#19–#25) on production:**
  - Switching the base currency blanks the figures to loading states, never old numbers under
    the new symbol (#22); the manual Sync refreshes Dividends, Tax and Allocation too (#23).
  - Positions: unvalued rows sort last both ways, Name starts A→Z (#24).
  - A cold Look-through at 100 rows loads every logo with no 429 on data requests (#25). If the
    whole site starts answering 429 for everyone, the limiter is seeing a public peer — set
    `TRUSTED_PROXY_HOPS=0` or check the host nginx (docs/deployment.md *Peer address*).
  - FX carries at most 14 days now, from the newest *published* rate (#20): a currency silent
    for 15–30 days refuses where it used to carry; old carried copies are re-derived or deleted
    on their next read. Since #56 the dividend accruals and forecast apply it too: an accrual in
    such a currency is skipped with the ingest's warning, never converted at the old rate.
  - `/api/analyst-ratings/status` `stale_ratings` covers held, Yahoo-eligible rows only (#21).
- **The audit's wave 3 (#26–#31) on production:**
  - Look-through (#28): a Cash row in the table, treemap and composition bar; company shares now
    match Allocation's for the same money; "Not attributed" is **striped** now (Cash took the
    plain grey) — the owner's eye on that paint.
  - Percentages with nothing to divide by show a dash, not 0.0% (#29); the hero's Realized gain
    carries "N sale(s) unpriced" only if a sale could not be priced or converted (#26).
- **The audit's wave 4 (#32–#37, #52) on production:**
  - The next push after `/root/auto-deploy.sh` is refreshed logs `WAIT: no CI checks yet … deferring`
    for a tick or two instead of "deploying ungated" (#37); `/root/.auto-deploy-ci-wait` exists
    only while one is pending.
  - `/api/watchlist` answers noticeably faster; rows keep their logos (#32).
  - The 18:00 `market_result` stops re-listing a sold security whose close date has no bar (#33).
- **The audit's wave 5 (#40, #42–#46) on production:**
  - The tax report's realized figures for a sale in the base currency itself are now exact (moved
    by under 0.3%); other currencies unchanged (#46).
  - `fx_result.frankfurter_requests` may read N+1 once on a day Frankfurter rejects a code, then 0–1
    (#40); Easter weekend slots ask nothing.
- **Audit round 2, wave 1 on production:**
  - Reconcile (#54): the next sale books its closed lot by open date; a sell-and-rebuy inside one
    window keeps the old lot's history. Lots already lost to the old FIFO rule do not come back.
  - Cash (#59): if a trade ever lacks an FX rate, the hero reads "Cash is unknown — N trade(s)
    …" and cash is absent everywhere, never a partial sum (none lacked one on 2026-10-18).
  - FX (#56): an accrual in a currency with no rate in 14 days is skipped and counted.
  - Flex (#55): the first served statement (Tue 18:00) leaves an `ibkr_generation` row in
    `/api/scheduler/history` and a file in `/app/data/flex-statements/` (newest 5 kept); a
    `1025` holds presses and slots 12 h, a request-step `1001` holds presses until midnight ET.
  - Charts (#67): money axes print exact, distinct labels; bar charts' ticks follow the shared
    ladder, so their positions moved.
- **Audit round 2, wave 2 on production:**
  - Tax (#62, #63): by-country rows show Rate / Creditable / Excess; holdings and realized rows
    carry logos; a Cash line sits under the holdings. `dividend_payments.withholding_country` fills
    from the next statement (Tue 18:00, 30 days back); older rows need an `ingest_flex_xml`
    re-run of a Client Portal download covering them.
  - Trades (#58): no cancellation row exists on production (2026-10-18), so nothing moves; the
    assumed sign convention (a `SELL (Ca.)` row carries negated amounts) is unverified until one does.
  - Fundamentals (#66): `/api/fundamentals/status` gains `securities_unavailable`; a logo the CDN
    refuses with a 4xx is stamped, a 5xx retried; OpenFIGI's two passes share one paced client.
  - Look-through (#68): Top 25/50/100 keeps the previous rows dimmed while loading.
  - Tax (#77, #78): the current year shows gross and withholding through 31 Dec (`est.`); every
    year shows the KS 36 row — turnover as a multiple, sales held under six months, whether IBKR
    cash went negative. Not tax advice: the owner judges what the figures mean for a filing.
    2026 reads `margin_used` true on a small negative minimum: check the IBKR statement for a
    real debit before trusting it — a derived balance between measured days can dip a little
    on FX alone (docs/tax.md).
  - Dividends (#74, #75): an announced raise carries "+x%" in the calendar; "Raises & cuts N"
    beside the payouts count opens the feed (annual level per share, old → new, %, /yr). A
    reverse split would read as a raise there (documented gap).
  - Dividends (#76): the Next 12 months tile shows the forecast "per 1,000 in" with its own line
    and change since the first measured day — the owner's eye on whether it reads as growth.
  - Shares (#71): nine published percentages (dividend yields and shares, crypto weights) now
    round half-to-even through `share_pct`; only exact ties at the published precision move.
  - UI polish (#70): every table's phone cards carry logos; Positions weights show a dash until
    the summary loads; a zero prints unsigned ("CHF 0", "0.0%") everywhere.
  - Earnings (#69): one calendar/history row per company and day (GOOG folds with GOOGL/ABEA
    once Alphabet's LEI is cached); a moved event's old row clears on the security's next fetch.
  - Sync routes (#65, #79): a weekend manual market sync no longer re-downloads closes;
    `dividend_sync` rows appear in `/api/scheduler/history`; the 18:00 record warns only about
    baskets still stale after its refresh.
  - The health-watch cron runs at `2-59/5` since 17:2x UTC 2026-10-18: on `*/5` it took the deploy
    lock first every tick and nothing deployed for an hour. Pushes since deploy normally.
  - Dividends (#61): estimate-era years now read net of the ladder's withholding, so the
    2025 and boundary-year nets, TTM and the YoY chips across the splice drop by roughly the
    withholding rate, and the forecast history's TTM line steps down once at the deploy (older
    snapshots keep the old basis). #60: a later WHTAX correction is folded into its dividend;
    a refused one warns once (`withholding_adjustments_unmatched` on the run).
- **#38's first deploy:** a full uncached build (the dependency layer is new), the log shows
  `docker compose build --pull`, `docker builder prune` and, from the second deploy on,
  "Skipping npm ci"; `docker system df` on the VPS a week later shows build cache bounded to about
  seven days of layers instead of growing.
- **The VPS on Node 22 (2026-10-18):** the next deploy re-runs `npm ci` (the stamp carries the
  Node version) with no `EBADENGINE` warning. The health-watch cron (every 5 min) and an empty
  `CORS_ORIGINS` went in the same afternoon; `/root/auto-deploy.sh` is the #53 copy, so the next
  push logs `WAIT: no CI checks yet …` for a tick or two.
- **The Flex client after #5:** the next IBKR runs (Sun/Mon are ET skips, so Tue 18:00 Berlin
  is the first real generation) succeed about 7 s slower, with `not ready (Code=…)` lines in the
  container log that used to be silent and no `t=` token in any Flex log line. A
  `Code=1004`–`1009`, `1019`, `1021`, an unparseable SendRequest reply (#15) or "failed after it
  may have reached IBKR" at 18:00 is now an `error` after one SendRequest, recovered at 00:00 —
  expected, not a regression. "SendRequest reply not understood" in the log is #15's new line.
- **The Yahoo hold and the benchmark fetch window (#57):** after any Yahoo 429 the log shows
  "Yahoo refused a request; no Yahoo request until HH:MM UTC" and, until then, passes report
  `rate_limited` without asking; `app_settings.yahoo_refused_at` holds the stamp across a
  deploy. The benchmark chart's last point now carries the last finished session's close (no
  intraday benchmark value) — the owner's eye on whether that reads right.
- **The benchmark warm-up reaching Yahoo (#4):** the first weekday `market_data_only` (08:00
  Tue–Sat) shows `benchmark_result.benchmarks_fetched` > 0 in `/api/scheduler/history`, the
  later slots 0, and no "pipeline busy; serving cached prices" from the warm-up in the logs.
- **The Yahoo diet after #11 (migration `b8e5d2f0a4c7`):** `backend/data/yf-cache/` appears on
  the VPS and survives the next deploy; dividend estimates arrive from the price passes (new
  ex-dates within a day, not a week); Tuesday's 05:00 `fundamentals_refresh` reports
  `allocations_updated` and ratings `funds_not_asked`; PEG on Fundamentals, the watchlist and
  the buy score now prefers Yahoo's `trailingPegRatio` — a changed figure is expected, a blank
  one is not. The first watchlist run fetches every item in full.
- **The weekend schedule after #9:** Sunday 2026-10-18 had no `market_data_only` row and its
  18:00 `full_sync` skipped IBKR (`no_new_trading_day`) with a `market_result`; Saturday
  2026-10-24 should show only the 08:00 `market_data_only`. Weekday slots unchanged.
- **Look-through after #8:** the first 18:00 run (2026-10-18) refreshed all nine baskets, VT on
  Vanguard's new route (#7); from 2026-10-19 `lookthrough_result.baskets` should list most funds
  under `unchanged` or `refreshed` daily. The run record still warns about baskets it refreshes
  (#79).
- **Price passes after #10 (migration `a2e8c4f6b0d3`):** the short `market_data_only` passes
  report `securities_skipped_sold` > 0 and "N of M" over held and just-sold securities only; the
  18:00 pass still prices sold ones; `securities_backing_off` stays small; no AV `Information`
  warning in the logs beyond one a pass.
- **FX after #2:** `/api/scheduler/history` `fx_result.frankfurter_requests` is 1 on the first
  sync after ~16:00 UTC on a weekday and 0 on the others; no `frankfurter_failed`. One request
  now carries every ECB currency, so a provider refusing the whole request sends all of them to
  the fallback for that sync — a currency dropping off the ECB list would look like that.
- **Crypto after #3** (admin key): the first `crypto_sync` of a Berlin day spends value + coins,
  the later ones coins only (`credits_spent` in the run details), and the DeFi figure stays put
  through the day.
- **The dividend pass after #6 (migration `f6c2e8a4b1d9`):** the first 18:00 pass after the
  deploy asks every held, Yahoo-eligible security once (all stamps start NULL); the next day's
  pass reports `securities_processed: 0`, and dashboard loads stop showing "sync in progress".
  `securities.dividends_checked_at` filled for held securities only.
- **The first `fundamentals_refresh`, Tuesday 2026-10-20 05:00 Berlin** (the weekly Sunday run is
  gone): a success row in `/api/scheduler/history`, no rate-limit warning, the Fundamentals as-of
  line within a day; Wednesday's pass refreshes every held row again (20-hour rule).
- **The owner's batch in the browser:** "from raises" on the Next 12 months card (after the
  backfill), the forecast chart's drag and keyboard resize and its remembered height, market caps
  in the base currency on Fundamentals and the Watchlist.
- **The ET Sunday/Monday IBKR skip (`b88faa9`):** Berlin Sun 18:00, Mon 00:00 and 18:00, Tue 00:00
  read `skipped` / `no_new_trading_day` with no Flex request; the 18:00 runs still carry
  `market_result`; Tue 18:00 generates. The daily validator will see no IBKR success Sat–Tue ET
  and may flag the new `sync_type`s — its expectation to update, not a fault.
- **Not yet seen in the browser:** Crypto (needs the admin key) and a loss P/E in red (none held).
- **The crypto book on CoinGecko prices, API half** (admin key): `/api/crypto/history` starts
  2026-01-01, `reconstructed` ends 2026-10-02, no December jump; credits fall ~35/day; BNB mapped
  `symbol+price+cap`; no unmatched-coin warning; no `crypto_*` rows in scheduler history. Then
  compare `crypto_daily` with the new series once and drop it.
- **The IBKR-first forecast, over ssh after an IBKR sync:** dividend rows carry
  `per_share_native` / `dividend_kind`; stored accruals' withholding is positive.
- **Deploy hardening:** whether `DEPLOY_SLOT_WAIT_MAX_MIN` (45) covers the 18:00 full sync's real
  runtime in `sync_runs`; every deploy logs `db snapshot of … is …` (a `NOTE: … did not report its
  snapshot` means `/root/backup-db.sh` is stale).
- **The EM fund's rescale (`d2411b5`) on the next finpension upload:** no "could not be moved"
  warning; `sibling_scaled` count unchanged but for the new NAV days.
- **The 2026-09-28 leftover:** the `er-api-latest` CHF→EUR rows for 09-28 and 09-30 replaced by
  published ones (`exchange_rates.source`).

## Worth doing next

Rough priority. Re-verify each `path:line` in code before fixing; lines move.

1. **Low leftovers from audit round 2** (not filed): analyst ratings re-ask a security on every
   non-429 error and the GLEIF loop continues past a 429 (#66's siblings); the rate limiter logs
   every rejection (two lines per refused request); `allocation_service` builds percentages from
   float fractions outside `share_pct`; `currencyExposure.ts` publishes 0 for "nothing priced";
   the Starlette TestClient warns to move to `httpx2` (the last 16 test warnings); KS 36's
   derivatives indicator needs the Flex ingest to keep non-STK trades; accumulating funds'
   taxable income (ESTV Kursliste) is not in the tax report (2025/2026 filings). (`gh issue list`), in
   number order: bugs first, then tooling, then the product enhancements (#74–#78).
2. **Drop `crypto_daily`** once compared (model, migration, smoke fixture, probe), with a CLI to
   map a CoinStats coin to CoinGecko by hand.
3. **Tooling.** CI runs ESLint, ruff `E9,F` (#72), the image build and, through the suite,
   `alembic check`.
4. **KPI audit 2026-10-02 — low priority (owner):** deposit-driven % read as growth (Total Value
    chip, dividend chips; Period Gain % fixed on the fintech branch); MoM/YoY chips in the dividend tooltip; XIRR annualised
    over short ranges; 1W risk from 5 returns and a 3% EUR risk-free rate; PEG mixes three methods;
    "Rose after sale" counts ETF rotations; two gain figures; cost basis labelled "total invested".
5. **Project the benchmark's baseline like the portfolio's** — the running cost basis is converted
    at each point's date, the portfolio's per lot at `open_date`; under 0.5%. Re-project the cost
    events at read time; **do not** make the portfolio convert per date (breaks the contributions
    identity).
6. **Should a trade still count as an external flow, now that cash is tracked?** Likely: serve
    both TWRs, labelled. `externalFlow`/`isMeasurable` in `portfolioKpis.ts`, `dailyReturnSeries`'
    five consumers and `betaAndCorrelation` must move together.
7. **Look-through projects:** measured sector/geography from baskets replacing `etf_mappings.py`'s
    estimates (needs a country→region map); exact World ex CH; nested funds; an identity detector.
    Researched future sources (N-PORT, commercial APIs — do not re-shop) are in docs/lookthrough.md.
8. **Crypto follow-ups:** a daily `type=3m` pull (10 credits/day); a Binance/Phantom split; a
    transactions ledger (no addresses or hashes); CORS registered outermost so a 401 shows locally.
9. **Brinson attribution** needs benchmark sector data first (docs/performance-analytics.md).
10. **Smaller:** a currency breakout for idle foreign cash; *Prior Period Positions* is ticked in
    the Flex Query and never read (untick or use); fold `prune_empty_dividends` into a job only
    after reassessing, reusing the CLI's predicate.

## Recent sessions (last 5)

One line each, newest first; drop the oldest.

- **2026-10-25 (Company tab: P/E)** — owner-approved weekly Yahoo closes per CIK (about a
  hundred requests a Sunday, 5.1 MB first fill) for a P/E in the key figures and a P/E card; the
  fill exposed EPS gaps (COST's 16-week Q4, continuing-ops and BasicAndDiluted EPS, EXC's
  untagged years, DXCM's lone slip) and a spin-off booked as a split (SPGI). P/E on 88 of 102;
  then P/S, P/FCF, dividend yield and return on equity (new `equity` concept), a Valuation card.

- **2026-10-25 (Company tab: figures and Nasdaq-100)** — key figures, four more series, CAGRs;
  net cash built then dropped (cash excludes investments); figures re-keyed by CIK so 100
  constituents need no `securities` rows; storage came in at 17.6 MB, not the 8–12 MB estimated;
  an audit of every company's series fixed splits between filings (BKNG, CRWD, KLAC, NFLX), two
  revenue concepts (CRWD, ODFL) and million-fold unit slips (PCAR, STX).

- **2026-10-25 (Company tab, agent team)** — two worktree teammates (backend, frontend) against
  one contract; a 22-company EDGAR audit moved the revenue/NI chains (IBKR), fixed ORCL's
  year-as-quarter and ONDS's thousands; SEC contact only in the VPS `.env`.
- **2026-10-25 (PR #80 merged)** — Swiss residency deployed; the move's country and date
  kept out of the public docs (the setting holds it); Trading212 lots need nothing (in Jan).
- **2026-10-24 (Swiss residency)** — the tax report takes a "Swiss taxpayer from" date
  (docs/tax.md); the same morning's sync check found the Flex ingest healthy.
