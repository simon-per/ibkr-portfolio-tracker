# Working state

> The perishable half of the project guide: what needs a human, what is being watched, what is
> accepted, what is next, and the local-dev traps. CLAUDE.md is the index and its *Keeping
> STATUS.md current* says when and how to edit this file; the reasoning behind each subsystem is
> in `docs/<topic>.md`, and every *Shipped* write-up, with what was verified on production, is in
> [docs/shipped-log.md](docs/shipped-log.md), newest first.

**Last updated: 2026-10-14.** Production was healthy on `978e65e` that day, with no
`warnings[]` in three days. Live and verified:
the UI carries figures, not explanations (every tab stripped of footnotes, markers and provenance
notes on 2026-10-14; the API still serves every flag); the watchlist refreshes itself on its own
04:00 Berlin slot, Tuesday to Saturday; the dividend forecast is IBKR-first, keeps a daily
history and says what its growth was made of (back-filled 2026-10-14); pillar 3a is the real
finpension export through the 2026-10-06 buys, with both funds priced without an upload deadline
and both looked through by index proxy; the database sits behind a directory mount with a
migration-aware rollback; the crypto book computes its own history on CoinGecko prices; the
Analytics tab is hidden (nothing deleted — docs/performance-analytics.md). **Merged the same
evening from an agent-team wave, unverified on production** (see *Watch after the next deploy*):
the scheduled IBKR generation skips ET Sunday and Monday (`no_new_trading_day`,
docs/flex-and-sync.md); a weekly fundamentals and analyst-ratings refresh, Sunday 04:00 Berlin;
the Dividends tab shows one Next-12M figure with MTD/YTD growth, "from buys", a received-income
line, payout raises/cuts and a sortable table; the Forecast tab projects at the owner's real
12-month pace with the entered rate as the effective annual rate; the KPI fixes (tax withholding
absent on estimates, Sortino, the heatmap chained to YTD, look-through Unresolved threshold, loss
P/E, market-cap sort in base currency, EPS growth on a loss base). What is owed: the browser
sweep and the read-only checks under *Watch after the next deploy*.

---

## Needs a human

- **Crypto history before 3 Oct 2026: run the rebuild or drop it — decide before ~2026-12-31.**
  Today the 3 Oct basket stands in for every earlier day at each day's price (owner's decision,
  2026-10-03; docs/crypto.md, *The reconstructed span*). The rebuild CLI would reconstruct the
  real quantities back to 23 Aug from CoinStats' transactions; its first dry run refused on BNB
  going negative (Binance's trading fees, taken in BNB), and the owner then asked for a retry:
  the CLI now accepts up to 0.05 BNB of fee dust and its refusal prints each coin's lowest
  amount. Next is `crypto_rebuild_holdings --dry-run --fees subtract` on the server, then the
  real run on the owner's word; if 23 Aug still refuses, `--reference 2026-09-01` is the
  accepted fallback. A run reads the whole transaction history (up to 400 CoinStats credits).
  **The deadline:** CoinGecko's Demo plan serves 365 days of history, so 1 January 2026 prices
  are out of reach after the end of 2026 (docs/crypto.md, *CoinGecko*); after that the rebuild
  can no longer price its days, and the stand-in becomes permanent.

- **The Flex Query period: confirm "keep 30" (nothing to do in the portal).** The live query is
  `Last 30 Calendar Days` (measured 2026-08-24 from the portal download's header and from
  `data_from`/`data_to` on every successful sync since 07-01); an older revision of the docs
  recorded an owner decision to narrow it to 3 that the account never ran. Nothing is broken
  either way — `flex_window_days()` measures the window, so the alarms are right at any N — and
  30 is the safer setting (weeks of margin rather than two failed days). Re-narrowing to 3 would
  need a portal edit, which also resets the day's generation. Whichever is chosen, never
  re-introduce a hardcoded threshold.

- **Upload the finpension export when it has new transactions in it — no pricing deadline.**
  Neither 3a fund depends on the upload for its price (`CH0117044948` from Yahoo,
  `CH1529078078` from its sibling share class — docs/pillar3a.md, *Prices*). An upload brings
  the transactions, and each buy's NAV re-anchors the derived prices. Three commands; then drop
  `--dry-run`. It replaces the account wholesale and refuses a file shorter than what is stored.

  ```bash
  scp transaction_report_YYYYMMDD.csv root@portfolio.srv1211053.hstgr.cloud:/root/p3a.csv
  ssh … 'docker cp /root/p3a.csv backend-portfolio-backend-1:/tmp/p3a.csv'
  ssh … 'docker exec backend-portfolio-backend-1 python -m app.cli.import_finpension_csv /tmp/p3a.csv --dry-run'
  ```

- **Add the HSTS header on the host nginx.** Production sends CSP, nosniff, X-Frame-Options and
  Referrer-Policy but no `Strict-Transport-Security`. TLS terminates on the VPS's own nginx
  (`/etc/nginx/sites-*`, not in the repo): one `add_header` line, starting with a short
  `max-age` because the header is sticky in browsers.

- **VPS tidy-ups from the 2026-10-04 database move (optional, harmless).** `rm
  /root/finish-hardening.sh /root/finish-hardening.log` and the two set-aside sidecars
  `backend/data/portfolio.db-{wal,shm}.pre-switchover-20261004T131635Z`; point `DATABASE_URL` in
  the VPS `backend/.env` at `/app/data/portfolio.db` (docs/deployment.md, *The database
  directory*). Keep the `backend/portfolio.db -> data/portfolio.db` link while a rollback past
  `9e57366` is conceivable. `/root/ibkr-backups/failed-deploys/` is never pruned. Also worth a
  look while there: `df -h` and logrotate.

## Watching

- **The IBKR primary slot is 18:00 Berlin (12:00 ET), with 00:00 Berlin as the recovery.** A
  normal day reads a `full_sync` success at 18:00 and `ibkr_retry_1` `skipped` /
  `already_generated_today` at 00:00; an `error` at 00:00 means both failed. On ET Sunday and
  Monday both read `skipped` / `no_new_trading_day` (since 2026-10-14): those statements reach
  the same Friday Saturday's did, and they held ~90% of every failed generation. Measurements and the
  fallback are in docs/flex-and-sync.md, *The 18:00 Berlin slot*; recovery from a missed window
  is a Client Portal download through `app/cli/ingest_flex_xml.py`, which spends no token budget.

- **The TSMC ADR override has never been exercised on real data.** `ISSUER_OVERRIDES` folds
  `US8740391003` (the TSM ADR) into the Taiwanese ordinary, pinned from both sides in
  `test_company_identity.py`; SOXQ and SMH hold the ADR. Check TSMC appears once, not twice, on
  `/api/portfolio/lookthrough`.

- **`full_sync`'s market-data half on a day IBKR refuses** (decoupled 2026-08-07). The 2026-10-04
  18:00 run got `Code=1001` and is the first such run on record: in `/api/scheduler/history`
  (the field is `type`) its `details.market_result` should be a real object, not `null`, with the
  top-level `status` still `error`.

- **MCO and MRVL each forecast off only 2 samples** (`forecast_samples` on the breakdown row). Not
  known-wrong, but two samples is the shape that once let SBI project a fake monthly cadence.
  Check `manage_mappings list` for a `DIVIDENDS PREDATE MAPPING` flag before trusting either.

- **Misfire recovery has never been observed end to end.** The persistent job store survives a
  recreate (every job logs `kept, next run:`), but no deploy has yet landed within
  `MISFIRE_GRACE_SECONDS` of a slot. `/health`'s `scheduler_jobstore_persistent: false` means the
  store fell back to memory; `/api/scheduler/status` cannot tell you.

## Known rough edges (accepted, not bugs)

- **Credentials: nothing open, by owner decision — do not re-raise.** The IBKR Flex token leaked
  through `/api/scheduler/history` until the 2026-07-28 scrub and was not rotated (2026-08-17:
  the Flex Web Service is read-only reporting, so the worst case is our own syncs locked out);
  `API_ADMIN_TOKEN` reached agent transcripts on 2026-08-07 and 2026-08-17 and both were
  accepted. The tell of misuse: successful generations at hours no job runs, or a `1025` with no
  failures of ours before it. The lesson: never dump bytes from a file that holds secrets.

- **VWCE is looked through via VT's basket, and that is closed** (owner, 2026-08-17): the Irish
  sleeve is being rotated into US-domiciled ETFs, so VWCE is converging on VT. The proxy errs low.
  A real basket, if one ever arrives, is stored by `import_etf_basket` and supersedes it; every
  route to one was ruled out on 2026-08-14 (docs/shipped-log.md, under *a borrowed basket*).

- **There is no 2025 tax year at IBKR to backfill** (owner, 2026-08-17): the holdings arrived in
  kind from other brokers in early 2026, so 2025's `yfinance_estimate` dividends are the only
  source that exists. Do not change the Flex period chasing it.

- **Off-site backups: declined** (owner, 2026-10-13). Losing the VPS means re-pulling every ledger
  row older than the Flex window by hand from the Client Portal.

- **After a split purge, `market_prices` history comes back only at the 18:00 `full_sync`** — the
  730-day pass; the 7-day slots restore the current value within hours
  (docs/data-model-and-valuation.md).

- **A few holdings the dividend forecast cannot see yet — accepted by the owner 2026-10-04.** IQQ (a
  new iShares Nasdaq-100 ETF) and SBI (Serabi Gold) have one payout each, and two make a cadence, so
  they project nothing until their second lands. IFX is the only holding on the `assumed`
  withholding rung, so at the WHT setting of 0% it reads gross (~1 CHF high; Germany withholds
  ~26%) until IBKR pays it in February. Together ~1–2% of the yearly forecast. The replay
  (docs/dividends.md, *Measured on production*) also showed SK Hynix sometimes classifying steady in
  history; no live effect, fix described there, left as is by owner decision.

- **`stack_order` is invariant across every range and NOT across `?forecast=false`** — deliberate,
  2026-09-20. Projections have to count toward the colour order: a realized-only ranking would fold
  the largest bars (payers with no realized income yet) into Other and leave a future planning year
  colourless. Nothing in the app sends `forecast=false`; the client fetches once with projections
  and hides them in the browser. Pinned both ways in `test_dividend_growth.py`; do not "fix" it by
  dropping projections from the ranking.
- **The Allocation tab's seven-step neutral ramp is stepped for a white card only** — measured
  2026-09-20. `NEUTRAL_STEPS` runs L*55 → 24, so against the dark card its last three steps measure
  **2.20 / 1.83 / 1.55:1** and those sector tiles sink into the surface; its top step also measures
  ΔE 6.0 against `--viz-sector-3` under red-green CVD. Fixing it means per-theme `--viz-muted-*`
  tokens and a visible dark-mode change to that tab, which is its own piece of work.
- **FX drifts under 0.3% are accepted — owner decision, 2026-09-12.** A security quoted in the
  base currency is valued through native→EUR→base with two independently rounded ECB quotes,
  ~0.12% high, at the five valuation sites the timeline-equivalence test pins; the same round trip
  sits in the tax report's SELL-trade conversion, and `BaseFx`'s rate cache starts at the first tax
  lot (a pre-lot deposit uses a rate up to a week off). **None of the three is a task.** If a
  figure ever drifts *past* 0.3%, the fix is the `NativeToBase` short-circuit at the valuation
  sites, and `test_timeline_equivalence.py` is the net.
- **The realized-P&L headline switches source all-or-nothing, unscoped by account** — found
  2026-09-12, dormant. `_realized_from_trades` uses `trades` for the whole book the moment
  *any* SELL exists there (the 3a importer writes them too), so closed lots of an account
  without `<Trades>` coverage would drop out. Today every IBKR sale is in `trades`, so nothing is
  wrong; `tax_service` already picks per account and per year, and the fix is to do the same here
  — a modelling change, not a one-liner.
- **A pillar-3a `LIQUIDATION` closes lots but books no realized P&L and no trade** — found
  2026-09-12, dormant (both 3a funds are accumulating). `finpension_ingest` computes the
  realized figure and discards it, writing an `INCOME` cash flow. Adding a SELL trade beside the
  flow would double the Balance-oracle replay; the right shape needs a decision about how a
  liquidation moves cash. It becomes real on the first fund switch.
- **The Dividends KPI strip does not follow the year filter.** Its labels are absolute ("2026 so
  far", "Last 12 months") and the growth block is unwindowed by design — so selecting 2027 still
  shows this year's figures. Pinned by `test_growth_is_identical_whichever_year_is_selected`. This
  is the most likely thing to read as a bug when it isn't.
- **"Next 12 months" stays visible with the Forecast toggle off**, because that card is inherently a
  projection and hiding it would collapse the four-up grid. The per-year panel *does* respect the
  toggle, since it is the one surface that mixes measured and projected into one bar.
- **Accumulating ETFs correctly show no dividends** — DBPG, EMIM, IWDA, SXR8, VWCE, XAIX, XNAS.
  Verified, not assumed. **Don't "fix" their absence.**
- **Activity's Market Value delta and the chart's "Value change" are the same number**, shown twice
  on purpose: the card answers "how much did the portfolio move" at a glance, the chart header pairs
  it with Period Gain so the difference between the two is visible. Neither is a return.
- **`/api/portfolio/activity` shows dividends net, with estimates badged.** A `yfinance_estimate` row
  is a gross guess with no withholding and reads *Dividend · est.* It splices like every other
  reader — pre-boundary estimates survive and stay badged.
- **`/api/portfolio/attribution` excludes-and-counts a line that did not exist at the window start.**
  A spun-off security has no start value to attribute against, so for a window opening before the
  spinoff it is dropped from both sides and reported as `unpriced_holdings: 1` — visible on the ALL
  range. Doing it properly means combining parent and child. The timeline, the summary and the
  Steuerwert all handle it.
- **A price inside the session is an intraday value, not a close, and that is normal.** Five of
  the seven market-data slots run mid-session, so the newest row is provisional until the market
  shuts; it is re-fetched at every slot for three days and settles on its own. Only a wrong value
  **older** than three days is a bug.
- **A benchmark's newest point can lag the portfolio's** if nobody opened the chart that day. The
  scheduled warm-up deliberately skips the provisional refresh — eight warm tickers × seven slots
  would multiply its burst for a value no one read — and the chart's own lazy fetch refreshes the
  benchmark actually selected. Deliberate Yahoo-budget trade, not an oversight.

- **US market holidays are not modelled** by the ET Sunday/Monday IBKR skip: the day after one is
  still attempted, as before.
- **`market_cap_base` is null for minor-unit quotes** (GBp/ZAc/ILA) until Yahoo's `marketCap`
  scale for them is verified, so those rows sort last on the Fundamentals tab.
- **The monthly heatmap's YTD changed method on 2026-10-14**: it is now the months compounded,
  each measured from the previous month-end, so it differs from earlier reads by design.

## Watch after the next deploy

Only what is still unverified. The verified halves of every entry that used to sit here are in
docs/shipped-log.md under the change that shipped them.

- **The browser sweep — every check below is owed, none can be answered by the API.** At 1440
  and 390 px, both themes. The 2026-10-14 text strip (~38 components) has never been seen
  rendered, so every tab is in scope; beyond that, by tab:
  - **Dividends** — colours: step All time → 24m → 2026 → 2025 → 2024 → 2027 and no holding
    changes colour or vertical position; the five muted steps read as a de-emphasis ramp (dark
    mode especially); a legend of up to thirteen entries plus Other wraps at 390; hover-dim,
    click-to-pin and the Other disclosure work on touch; calendar swatches match the chart in a
    past-year view (`SERIES_MUTED` in `lib/dividendColors.ts` is the one number to change; re-run
    `dividendColors.test.ts` after). Forecast ON: September's translucent segment roughly
    doubled by the calendar fold; Forecast OFF: the rolling chart still shows the 2026-08 window,
    nothing translucent, no series changes colour, the monthly axis and both year selectors show
    no future range. The WHT popover at 390, its save/refetch feedback and the Performance-card
    percentage. The calendar at 390: the *payment pending* badge, the `ex …` note beside a
    shifted date, "ex-date · pay date unknown" rows. The stacked TTM: solid/dashed split, the
    legend in TTM mode, the toggle dropping open windows. The growth strip per range against
    the API (`ttm_coverage_start` `2025-05` in every range), and whether the All-time figure is
    worth keeping once seen (dropping CAGR on coverage-limited ranges is a two-line change). The
    *Average per month* chips wrap rather than scroll. The forecast trend card: its breakdown and
    its collapse; its header and the KPI tile print the same Next-12M (also right after a WHT
    change), the MTD/YTD chips, "+X from buys", the muted received line; "Payouts · N raised ·
    M cut" above the table; every figure column sorts, unknowns last.
  - **Forecast** — opens at the 12-month pace, not 1000, in a browser holding the old
    auto-written value; a value typed earlier survives; *My pace* follows the pace. The
    *Money In per Month* card reads "so far · 12M avg · YTD".
  - **Tax** — "—" withholding on estimated rows, the total "—" or "excl. N est.".
  - **Fundamentals** — the as-of line (amber past 8 days), a loss P/E in red, market-cap sort.
  - **Performance** — the heatmap's `MTD` tag; Sortino reads "Not enough history" on a thin
    range. On 3M/1Y the Profit/Loss line starts at 0 on the day the benchmark lines
    start and ends at the change in Total Value minus the change in Money In; no 09-28 → 09-30
    hump in the benchmark lines; the visual hierarchy refresh (`4bd3f7f`: content shell, KPI
    panels, hero grids) has never had a browser pass.
  - **Look-through** — an alert naming any unvaluable position above the KPIs.
  - **Watchlist** — the as-of line (amber past four days); the Analyst sort puts `strong_buy`
    first descending; EPS figures carry no `$`.
  - **Positions** — the 3a EM fund's row carries its *sibling NAV* badge.
  - **Crypto** — the four tiles (Total value, Today, P&L since 1 Jan 2026, Coins), 1W · MTD ·
    YTD, the *peg* badge if USDC is held.

- **The crypto book on CoinGecko prices — the API half.** With the admin key:
  `/api/crypto/history` starts on 2026-01-01, `reconstructed` ends on 2026-10-02, and there is no
  jump in December; the total is within price drift of the CoinStats app (CoinStats' total
  includes exchange cash, shown here beside it); `/api/crypto/status` credits fall by about 35 a
  day less than before; `crypto_coin_ids` maps BNB with `method = symbol+price+cap`; no run warns
  about an unmatched coin; `/api/scheduler/history` still has no `crypto_*` rows. Then compare
  `crypto_daily` (no longer written) with the new series once, and drop it (*Worth doing next*).

- **The IBKR-first forecast's last two checks** (live on `48b1f9c` since 2026-10-04; the rest is
  verified, docs/shipped-log.md). After a successful IBKR sync, over ssh against
  `/app/data/portfolio.db`: IBKR dividend rows carry `per_share_native` / `dividend_kind` (the
  statement re-delivers them), and the stored accruals' withholding is positive (the sign fix).

- **The deploy hardening (2026-10-04) — two halves only a later event can show.** Whether the
  45-minute bound (`DEPLOY_SLOT_WAIT_MAX_MIN`) covers the 18:00 full sync's real runtime in
  `sync_runs`. The rollback restore shows only when a migrating deploy fails; every deploy should
  log `db snapshot of … is …` — a `NOTE: … did not report its snapshot` means
  `/root/backup-db.sh` went stale again. The symlink case is rehearsed on CI only.

- **The EM fund's rescale (`CH1529078078`, `d2411b5`) first runs on the next finpension
  upload.** Expect no "could not be moved" warning and the `sibling_scaled` row count unchanged
  but for the new NAV days.

- **The ET Sunday/Monday IBKR skip (`b88faa9`).** Berlin Sunday 18:00, Monday 00:00 and 18:00,
  Tuesday 00:00 read `skipped` / `no_new_trading_day` with no Flex request; the 18:00 runs still
  carry `market_result`; Tuesday 18:00 generates normally. The daily validator will see no IBKR
  success from Saturday to Tuesday ET — its expectation to update, not a fault.

- **The first `fundamentals_refresh` (Sunday 04:00 Berlin).** A success row in
  `/api/scheduler/history`, no rate-limit warning, and the Fundamentals tab's as-of within a
  week. The validator may flag the new `sync_type`.

- **The watchlist slot's neighbours.** A clean 08:00 market-data run after the 04:00 refresh. If
  the daily validator flags the new `sync_type` in the history, that is its expectation to
  update, not a fault.

- **The 2026-09-28 withdrawal fixes' last leftover:** the stale `er-api-latest` CHF→EUR rows for
  09-28 and 09-30 replaced by the published ones (`exchange_rates.source`).

## Worth doing next

- **The KPI audit of 2026-10-02 — low priority by owner decision (2026-10-04):** fine as long as
  the figures point the right way. Re-verify each in code before fixing; until 2026-10-04 these
  lived only in a session note.
  - **Deposit-driven % read as growth:** the Total Value chip, Period Gain %, the dividend
    2026 / TTM / Next-12M chips.
  - **The dividend chart tooltip:** its MoM/YoY chips.
  - **XIRR** is annualised over short ranges.
  - **1W risk figures** come from 5 returns, and the risk-free rate is a 3% EUR rate on a CHF base.
  - **PEG** mixes three methods.
  - **Sector "Unknown":** 35% in Analytics against 2% in Allocation — moot while Analytics is hidden.
  - **"Rose after sale"** counts ETF rotations.
  - **Two gain figures**, 410 apart; cost basis labelled "total invested".

Rough priority from here.

1. **The test suite writes into the developer's database.** `tests/conftest.py:42` blanks only
   the job store; the scheduler-job spy tests and the WAL-lifespan test write `sync_runs` rows,
   seed rows and a checkpoint through the app's own `AsyncSessionLocal`, i.e. into whatever
   `DATABASE_URL` names — a production snapshot, if that is what local work points at. Since
   xdist became the default (2026-10-14), N workers write one SQLite file at once: a plausible
   cause of the one flaky parallel run. Fix: a per-worker temp database before `settings` is
   imported. Until then, never run the suite against a snapshot you still need.
2. **Pin what every production build re-resolves.** `backend/requirements.txt:24-29` floats
   `yfinance`, `lxml` and `pyxirr`, and `deploy.sh:90` builds `--no-cache`, so an upstream
   release can change figures with no commit (yfinance has changed scale before). A constraints
   file.
3. **Copies of one job — the dominant failure mode.** `_to_eur` three times
   (`dividend_service.py:542`, `tax_service.py:41`, `finpension_ingest.py:452` — the last
   swallows errors); a fourth inline FX path with a `"USD"` placeholder
   (`dividend_service.py:471`); the CLI "resolve a security by symbol and exchange" three times,
   and **`manage_mappings.py:79-83` takes `.first()` without refusing ambiguity**, so a mapping
   could land on the wrong security; `_pct` drifted (`lookthrough_service.py:808` returns 0.0,
   `performance_analytics_service.py:77` None); the two crypto clients' `_pace`/`_get`. Extract,
   and test the family.
4. **`BaseFx.convert` returns the EUR amount when no rate exists** (`base_fx.py:58-60`) — a
   stand-in that claims a value, under ~57 call sites; only crypto guards it (`backfill=False`).
5. **The tax report's estimated rows, backend half.** The tab shows an estimate's withholding as
   "—" since 2026-10-14, but `dividend_by_country` carries no per-country estimate count (so a
   country is tagged by the year's era), `to_csv()` still writes an estimate's withholding as 0,
   and in a mixed year the net total includes estimates with nothing withheld (`tax_service`).
6. **Drop `crypto_daily`** once compared on production (no longer written or read): a migration
   plus the model, the smoke fixture's import and the probe's mention. With it, a CLI to set a
   CoinStats→CoinGecko mapping by hand for a coin `/coins/list` cannot match.
7. **Four nulls-last sort comparators** — `FundamentalsTab.compareValues`,
   `cryptoColumns.sortCryptoHoldings`, `WatchlistTab` and `lib/dividendTable.ts` — and `ABSENT`
   defined in both `lib/utils.ts` and `ui/KpiCard.tsx`. Extract each into one `lib/` helper with a
   family test.
8. **Tooling.** ESLint is red (19 problems) and ruff has no config (3 dead assignments, 1 bare
   `except`); neither runs in CI. `alembic check` fails on naming drift only (the unnamed
   `ticker_mappings` unique constraint, `ix_corp_actions_*`, `index=True` on primary keys), so it
   cannot guard a forgotten migration until one reconciliation pass makes it green. The
   `ResizeObserver` stub is copied into 17 test files and belongs in a vitest `setupFiles`. CI
   pins Node 20, which is end-of-life. `FundamentalsTab.tsx` carries four pre-existing
   setState-in-effect lint errors.
9. **`coverage_pct` reads 0.0 on an empty book** (`lookthrough_service.py:853`) — an unknown
   served as a figure.
10. **Project the benchmark's baseline the way the portfolio's is projected.**
    `_apply_base_currency` converts the running cost basis at each point's date;
    `_calculate_timeline_swept` converts each lot's cost at its own `open_date`. Same tax lots,
    two conversion rules: under half a percent, invisible on the chart, and it does not reach
    beta. The timeline cache is EUR-only on purpose, so the cost *events* have to be re-projected
    at read time. Do not "fix" it by making the portfolio convert per date — that breaks the
    contributions identity.
11. **Decide whether a trade should still count as an external flow, now that cash is tracked.**
    With cash inside the pot only deposits and withdrawals are external, which makes TWR exact
    and frees beta's flow-day exclusions — but returns would then include the drag of idle cash,
    a different question. Likely answer: serve both, labelled. `externalFlow`/`isMeasurable` in
    `portfolioKpis.ts`, `dailyReturnSeries`' five consumers and `betaAndCorrelation` move
    together or the tab disagrees with itself.
12. **Look-through projects.** Measured sector/geography from the baskets, replacing
    `etf_mappings.py`'s hand estimates (`etf_holdings.sector` is already normalised and serves
    the treemap grouping; `country` needs a country→region map with its own test). An exact
    World ex CH (IWDA minus its Swiss rows). Nested funds. An identity detector:
    `unresolved_value_eur` is today the only figure showing what the evening pass has not reached.
13. **Crypto follow-ups.** (a) A finer value history for the short ranges: a daily `type=3m`
    pull, 10 credits a day. (b) A Binance / Phantom split: one share token per connection,
    summed. (c) A transactions ledger (4 credits a page), storing no wallet addresses or hashes.
    (d) `app.main` registers CORS innermost, so in local cross-origin dev a 401 arrives as a CORS
    failure; outermost would let the locked state show locally without the Vite proxy.
14. **Brinson allocation / selection attribution — needs data first** (and the Analytics tab is
    hidden). It needs the benchmark's per-sector weights and returns: an index ETF basket plus
    one price series per sector ETF, or a factor provider. Budget it against rule 1. Design in
    `docs/performance-analytics.md`, *What is deliberately not here*.
15. **Smaller ideas.** A currency breakout for idle foreign cash. *Prior Period Positions* is
    ticked in the Flex Query and never read: untick it (a cheaper statement) or use it.
16. **SEC N-PORT as the generic fallback for a future US fund with no issuer route** (every fund
    held has an issuer feed; structurally unreachable for Irish and Luxembourg UCITS). The
    research is expensive to redo and two points are silent-corruption traps:
    - **Key on ISIN, never ticker.** `company_tickers_mf.json` maps `SMH → CIK 1137360` (VanEck's *US*
      fund) and `XAIX → CIK 1503123` (a US namesake of the Xtrackers UCITS we already fetch).
      Verified: SOXQ `(1378872, S000072470)`, GRID `(1364608, S000026919)`, QTUM `(1540305, S000062478)`.
    - **`data.sec.gov/submissions/CIK*.json` is series-blind.** Use
      `efts.sec.gov/LATEST/search-index?q=%22S000026919%22&forms=NPORT-P`, then
      `/Archives/edgar/data/{cik}/{accession}/primary_doc.xml`. `/cgi-bin/` is
      robots-disallowed; `/Archives/` is allowed. A descriptive User-Agent is **mandatory** —
      omitting it is a 403 plus a ~10-minute IP block.
    - **Assert `formData/genInfo/seriesId`** before using a row — a sibling series is a silently
      100%-wrong basket.
    - **As-of is `repPdDate`, not `repPdEnd`**, and fiscal quarters are not calendar quarters.
    - **QTUM's `pctVal` sums to 104.61%** (sec-lending collateral). Filter to `assetCat ∈ {EC, EP}`
      with an `<isin>`, then renormalise.
    - Arrives **75–136 days old**: its own `ADAPTER_STALE_DAYS` entry, not a raised default.
17. **Commercial holdings APIs are not viable free — checked 2026-08-16, do not re-shop.** The
    holdings array is the paywalled field at FMP, API Ninjas, EODHD and Intrinio. **Never wire up
    FMP's "ETF Holder" endpoint** — it returns who owns shares *of* the ETF, the reverse
    direction, and passes a smoke test.
18. **Fold `PRE_OWNERSHIP_HISTORY_YEARS` pruning into a scheduled job — reassess before
    building.** `prune_empty_dividends.py` is manual and the ingest window already prevents new
    junk; automating a deleter over financial rows has a small upside and a silent downside. If
    built, it must reuse the CLI's predicate.

## Local development traps

Each of these cost real time at least once.

- **Pushing works from an agent session** — it is not blocked by the permission classifier, so
  the guard is the brief: only `main` goes to GitHub, and in this repo the owner (or the lead of
  an agent wave) pushes it. `git log --oneline origin/main..main` is the only trustworthy count of
  what is unpushed. Never push within ~10 minutes of a Berlin slot; `ops/finish-deploy.ps1` and
  `ops/finish-deploy.sh` are equivalent twins that check it (keep them in step), and the
  shorthand variables they define live only inside one session.

- **A file under the container's `/tmp` does not survive a deploy.** `fetch_etf_baskets --out
  /tmp/baskets` then `import_etf_basket` in a second step lost both files on 2026-10-04: a push
  auto-deployed in between (`*/5`), `down`/`up` recreated the container, and the import said
  "No such file(s)" (nothing was written — it refuses first). Run fetch and import back to back
  in one paste, and don't push while someone is mid-way through a manual CLI sequence on the VPS.

- **Node 26 breaks every frontend test that touches `localStorage`** (60 failures in 5 files,
  seen 2026-09-27 and again 2026-09-28: `Cannot read properties of undefined (reading 'clear')`).
  Node's own experimental `localStorage` global shadows jsdom's. Not a regression in the code:
  `NODE_OPTIONS=--no-experimental-webstorage npx vitest run` from `frontend/` runs the suite green,
  as does `npx -y node@22 node_modules/vitest/vitest.mjs run`. CI uses Node 20 and is unaffected.
  Read the failing files before trusting a red count.
- **A fresh checkout or worktree has no `backend/venv`, no `backend/.env` and no
  `frontend/node_modules`.** Use Python 3.11, which the image and CI run (docs/local-development.md
  has the `uv` commands; 3.12 has also worked); the global interpreter's FastAPI is too old to
  collect `test_api_hardening.py`. Start `backend/.env` from `backend/.env.example` with
  placeholder IBKR values, `SCHEDULER_ENABLED=false` and an `API_ADMIN_TOKEN` — Crypto mode
  refuses to open without one. `npm ci` in `frontend/`.
- **Vite's dev proxy forwards `/api` only.** Running with `VITE_API_URL=` (same-origin, the only way
  a 401 reaches the page locally — see *Worth doing next*, crypto follow-ups) leaves `/health`
  unproxied, so the footer and the lock button disappear. Normal work uses the default
  `VITE_API_URL`.
- **A service-level benchmark test can reach Yahoo.** `calculate_benchmark_value_over_time`
  calls `_ensure_prices_available` itself, which fetches whenever the fixture leaves a gap it
  considers missing — a leading gap at the range start, or a trailing day inside
  `PROVISIONAL_PRICE_DAYS` of today — and its `except Exception` swallows a raiser, so the smoke
  test's `yf.Ticker` stub alone is *silent* here. This cost exactly one real `^GDAXI` request on
  2026-09-07 from a fixture whose index started three days after the window. Stub both fetch
  steps for the module and assert the raiser was never reached on teardown; the `_offline`
  fixture in `test_benchmark_window_anchor.py` is the pattern.
- **`SCHEDULER_ENABLED=false` in `backend/.env` for any local run.** Otherwise starting uvicorn arms
  every job in `ALL_SYNC_HOURS` against the live Flex token, Yahoo and, at 18:00, the issuer
  sites. Defaults to `True` so production is unaffected.
- **Check which port Vite actually took.** If 5173 is occupied it moves to 5174 and says so once. A
  stray dev server on 5173 configured against production means you are reading prod data and issuing
  requests to the live site — including `/api/dividends/summary`, which can enqueue a Yahoo sync.
- **Use a snapshot of the production DB, not the local `backend/portfolio.db`** — the latter predates
  trades, cash flows and the IBKR dividend era, so it exercises none of the interesting shapes.
  `sqlite3 .backup` on the VPS, copy down, point `DATABASE_URL` at it, **delete it afterwards** (it
  is real account data; `*.db` is gitignored but it should not linger). And mind *Worth doing
  next* item 1: the test suite writes into whatever `DATABASE_URL` names.
- **The base currency is whatever the user last picked** (`/api/settings`, EUR/CHF/USD). Every money
  figure moves with it, so never compare a number across sessions without checking it — hold it
  fixed by comparing payloads built from one snapshot, or ratios inside one payload.
- **Don't push within ~10 minutes of a Berlin sync slot** — nine of them, on the hour at
  00/04/08/11/13/15/18/20/22 (`ALL_SYNC_HOURS`; 04:00 is the watchlist's, Tuesday to Saturday).
  Since 2026-09-08 `deploy.sh` builds before it stops anything, so the outage is the seconds of
  `down`/`up`, it waits out a running sync, and the persistent job store recovers a slot missed by
  under 30 minutes. `ops/finish-deploy.*` checks the window for you.
- **`curl 127.0.0.1:<vite port>` fails while the browser works.** Vite binds `localhost`, which
  resolves to `::1` first on this machine, so the IPv4 literal gets connection-refused and looks like
  a dead dev server. Use `http://localhost:<port>`.
- **`pkill` is not installed** (Git Bash has no procps). `pkill -f uvicorn` prints
  "command not found" and exits non-zero — easy to miss inside a `&&` chain — so the old server keeps
  the port and the new one dies on bind while `--strictPort`'s error scrolls past in a log file. The
  result is a dev server quietly answering from the *previous* config. Kill by PID:
  `netstat -ano | grep ':8000 ' | grep LISTENING` then `taskkill //F //PID <pid>` (double slashes —
  MSYS eats single ones).
- **A dev server against a prod snapshot must run on port 5173.** `frontend/.env` points
  `VITE_API_URL` at `localhost:8000`, so the browser calls the backend cross-origin and only the
  ports in `CORS_ORIGINS` work. Any other port fails every request with a CORS error and looks like
  a backend outage.
- **Every position in the local `backend/portfolio.db` has `market_price: null` and a market value of
  0.00.** So the currency-exposure card reports *no priced positions* and the rebalance panel shows
  every row unpriced — both correct, and both easy to mistake for a broken feature. Anything that
  depends on a valued portfolio can only be browser-verified against a production snapshot.
- **A jsdom test that renders `ScrollableTable` (anything through `DataTable`) needs a
  `ResizeObserver` stub**, copied inline into 17 test files today (*Worth doing next*, tooling);
  and a component test that renders anything using `useBaseCurrency`/`useCurrencySymbol` needs a
  `QueryClientProvider` *above* `CurrencyProvider`, or it throws `No QueryClient set`. Neither is
  a component defect. `RebalanceCard.test.tsx` has both patterns to copy.
- **uvicorn can die mid-Playwright-run with `OSError: [WinError 64] The specified network name is no
  longer available`** — a Windows asyncio-proactor reaction to an abruptly closed connection, not a code
  fault. The e2e script then reports `ERR_CONNECTION_REFUSED` and a shrunken panel, which reads exactly
  like a regression in whatever you just wrote. **Confirm `/health` still answers before believing an
  e2e failure.**
- **A test helper with a default parameter swallows an explicitly-passed `undefined`**, so
  `renderCard(undefined)` renders the default fixture and any "backend down" assertion silently tests the
  loaded state instead. `CurrencyExposureCard.test.tsx` keeps a separate `renderUnloaded()` for this.
- **A test that starts a scheduler drops a `scheduler_jobs.db` wherever it runs.** `tests/conftest.py`
  blanks `scheduler_jobstore_url` for the whole suite, so an in-memory store is the default; a test
  that wants persistence points it at `tmp_path` itself.
- **`sqlite:////tmp/x.db` in Git Bash lands in `C:\tmp`, not the shell's `/tmp`.** The SQLAlchemy URL
  is read by Python, which does not apply the MSYS path translation, so a stray file goes somewhere
  `ls /tmp` will not show it.
- **`TZ=Europe/Berlin date` silently returns UTC in Git Bash.** It does not error and it does not
  warn — `TZ=America/New_York` prints the same time — so any script reasoning about the Berlin sync
  slots from the shell clock is two hours out in summer, in the direction that permits a collision.
  Use Python's `zoneinfo` (`ops/finish-deploy.sh` has the helper). Container logs are UTC too.
- **Renaming the working-copy directory breaks `backend/venv`, and the documented test command is the
  one thing that hides it.** `activate`/`activate.bat` hardcode `VIRTUAL_ENV` and every `.exe`
  console-script shim embeds the interpreter's absolute path, so after a rename
  `venv\Scripts\activate && uvicorn ...` and a bare `alembic upgrade head` fail while
  `./venv/Scripts/python.exe -m pytest` keeps passing — python.exe resolves its own prefix, the shims
  do not. Recreate rather than patch, and **`pip freeze` first**: `requirements.txt` floats
  `yfinance`, `lxml` and `pyxirr`, so reinstalling from it quietly drifts the local env. Then
  `python -m venv venv --clear` and install the freeze. A plain `python -m venv venv` regenerates
  `activate` and leaves the shims broken, which is the worst of the three states.

## Recent sessions (last 5)

One line each, newest first. **Drop the oldest rather than growing this list** — `git log` holds the
detail; this exists so the next session knows what just moved without reading it.

- **2026-10-14 (agent-team wave)** — an open-points catalogue; the owner picked growth and
  forecast first, then six tracks ran in parallel worktrees and were merged after review: the
  Dividends growth pack, the Forecast at the real pace, the KPI fixes, the ET Sunday/Monday IBKR
  skip (production `sync_runs`: Sun/Mon 12:00 ET failed 15 of 20 since 08-08, Tue–Sat 1 of 48),
  the weekly fundamentals refresh, and this file pruned from a 1,738-line log to a snapshot.
  Hover texts: the owner chose "shorten", done in every track.

- **2026-10-14 (test speed)** — "make the suite quicker", local runs being what the owner
  waits on: the backend runs under `pytest-xdist` (`-n auto --dist worksteal`), and the local
  command deselects the new `deploy_scripts` marker (20 Git Bash tests; CI runs them). Locally
  5:35 → ~25 s (~55 s with them); on CI only 58 s → 50 s (4 workers, each test ~3.5× slower in
  parallel there — not diagnosed). One of ten local parallel runs, during heavy
  background load, failed two tests that never reproduced (nine clean runs, including `-n 32`);
  output not captured. If it recurs, suspect the 60 s subprocess guards in the deploy-script tests.

- **2026-10-14 (push, then strip the text)** — pushed and deployed `4c1d654`; the forecast
  backfill dry-run clean, the real run refused by the permission classifier (owed). Then, at
  the owner's word, the forecast card made collapsible and every tab's explanatory text
  stripped, the convention in CLAUDE.md rewritten so it is not repaired back. Then merged the
  parallel `simon-per/strip-comments` session (data-quality notes, the USDC peg note, the
  contributions strip's `~`/`*`, dead `coverage_limited`) and removed its worktree and branch.
  Evening "resume": pushed that merge (`f048a94`) and merged `simon-per/test-speed`. Then an
  open-points catalogue for an agent-team wave; pre-flight ran the forecast backfill,
  refreshed `/root/auto-deploy.sh`, synced AGENTS.md (it still carried the old caveat rule)
  and deleted a stray 3a CSV from the repo root.

- **2026-10-13 (watchlist freshness)** — a brainstorm of open points, of which the owner gave
  this session the watchlist (another session took the heatmap and the forecast-history chart).
  A nightly refresh on its own slot, the deploy guard taught the hour, crypto hours re-derived
  so they did not grow, and an as-of line on the tab. On branch `feat/watchlist-nightly-refresh`.

- **2026-10-13 (forecast breakdown)** — "brainstorm more high-ROI features" narrowed by the
  owner to one: the forecast tracked from the post-switch baseline, visibly growing, with the
  growth broken down quietly. Per-security snapshot lines, the split on `forecast-history`,
  per-share growth per payer and for the book, the trend card and a Growth column. A
  production snapshot caught two wrong figures before shipping (a fund's single distribution
  as its growth; a book figure carried by one +2400% payer). Merged into `main`, unpushed;
  backfill owed.
