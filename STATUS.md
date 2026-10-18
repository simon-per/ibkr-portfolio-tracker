# Working state

> The perishable half of the project guide: what needs a human, what is being watched, what is
> unverified, and what is next. CLAUDE.md's *Keeping STATUS.md current* says when and how to edit
> it. Subsystem reasoning is in `docs/<topic>.md`; shipped write-ups in
> [docs/shipped-log.md](docs/shipped-log.md); accepted rough edges in
> [docs/troubleshooting.md](docs/troubleshooting.md); local-dev traps in
> [docs/local-development.md](docs/local-development.md).

**Last updated: 2026-10-18.** Both of the day's branches are on `main`: the owner's batch
(dividend raises, resizable forecast chart, base-currency market caps, daily held-only
fundamentals; deployed, backfill run) and the web session's fintech pass
(`ccr-7289b752-aev50c`), merged with one held predicate (`security_repository.held`). See
docs/shipped-log.md 2026-10-17. The GitHub issues are being worked through one worktree each:
#2–#12 all shipped the same night; no issue is open. The fintech UI pass shipped the same night
on every tab (docs/frontend.md *Design foundation*; docs/shipped-log.md 2026-10-17 late night).

---

## Needs a human

- **Tell the `ibkr-sync-validator` routine about the weekend schedule (#9).** Its prompt lives
  outside the repo (claude.ai routines). Since 2026-10-17 Sunday has no 08:00
  `market_data_only` run and Saturday has only the 08:00 one; it may flag the missing rows.

- **Holding logos: look at them.** About 200 companies carry a logo (2026-10-18): every
  security ever held (the two Swisscanto funds by hand), the whole Nasdaq-100 and the top 100 of
  the MSCI World and VT. Every table listing securities shows them, the Watchlist included
  (docs/logos.md *Where*). Judge the avatar styling (white circle, hairline ring) with real
  logos; GOOG/GOOGL/ABEA carry Alphabet's red "A" — the Google "G" is one domain lookup away
  if wanted.

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

- **Add HSTS on the host nginx** (`/etc/nginx/sites-*`, not in the repo): one `add_header`, short
  `max-age` first — the header is sticky.

- **VPS tidy-ups from the 2026-10-04 database move (optional).** `rm /root/finish-hardening.*` and
  `backend/data/portfolio.db-{wal,shm}.pre-switchover-20261004T131635Z`; point the VPS
  `DATABASE_URL` at `/app/data/portfolio.db`. Keep the `backend/portfolio.db` link while a
  rollback past `9e57366` is conceivable. `failed-deploys/` is never pruned; check `df -h` and
  logrotate while there.

## Watching

- **IBKR: 18:00 Berlin (12:00 ET) is the primary slot, 00:00 the recovery.** A normal day: a
  `full_sync` success at 18:00, `ibkr_retry_1` `skipped` / `already_generated_today` at 00:00; an
  `error` at 00:00 means both failed. ET Sunday and Monday read `skipped` / `no_new_trading_day`.
  A missed window recovers by a Client Portal download through `app/cli/ingest_flex_xml.py`
  (docs/flex-and-sync.md).
- **TSMC once, not twice, on `/api/portfolio/lookthrough`** — the ADR override (`ISSUER_OVERRIDES`)
  has never met real data.
- **`full_sync` on a day IBKR refuses:** the 2026-10-04 18:00 run (`Code=1001`) should carry a
  real `details.market_result`, not `null`, with top-level `status` `error`.
- **MCO and MRVL forecast off 2 samples** — check `manage_mappings list` for `DIVIDENDS PREDATE
  MAPPING` before trusting either.
- **Misfire recovery never observed end to end.** `/health`'s `scheduler_jobstore_persistent:
  false` means the store fell back to memory.

## Watch after the next deploy

Only what is still unverified.

- **The fintech UI pass with the owner's own eyes** (2026-10-17): every tab was screenshotted in
  both themes against production data through a routed preview; the dividend summary, the
  benchmarks and the crypto reads were seen only with made-up fixtures (Yahoo / the key). The sticky
  strip's shadow on scroll was measured in both themes. Look-through's constituent logos were
  filled on 2026-10-18 (docs/logos.md *Filled*); companies beyond those baskets' top 100 stay
  monograms.
- **Watchlist logos for names never held (2026-10-18):** CATL (`300750.SZ`), SOFI, SNDK, the
  Taiwanese names and `8306.T` resolve through the baskets and are fetched by Tuesday's 05:00
  logo pass (or `fetch_logos` now); `SBI.TO` and any ticker no basket lists stay monograms.
  MBGL answered `not indexed yet` and is re-asked by the same pass.

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

- **Look-through identities after the CINS fix (`66cd9cd`):** ASML one row, not three; NXP,
  Tower, Nebius, Prysmian, Terna folded; `identity.unidentified_groups` well below 78. The capped
  evening pass drains the backlog over a few evenings — `app.cli.resolve_identities` on the server
  does it at once. The production half of the diagnosis (only late-sorting CINS unresolved) was
  read off the API, not the database.
- **The crypto chart's date axis (`dateAxis`)** — not seen in the browser (needs the admin key).
- **The Flex client after #5:** the next IBKR runs (Sun/Mon are ET skips, so Tue 18:00 Berlin
  is the first real generation) succeed about 7 s slower, with `not ready (Code=…)` lines in the
  container log that used to be silent and no `t=` token in any Flex log line. A
  `Code=1004`–`1009` or "failed after it may have reached IBKR" at 18:00 is now an `error` after
  one SendRequest, recovered at 00:00 — expected, not a regression.
- **The benchmark warm-up reaching Yahoo (#4):** the first weekday `market_data_only` (08:00
  Tue–Sat) shows `benchmark_result.benchmarks_fetched` > 0 in `/api/scheduler/history`, the
  later slots 0, and no "pipeline busy; serving cached prices" from the warm-up in the logs.
- **The Yahoo diet after #11 (migration `b8e5d2f0a4c7`):** `backend/data/yf-cache/` appears on
  the VPS and survives the next deploy; dividend estimates arrive from the price passes (new
  ex-dates within a day, not a week); Tuesday's 05:00 `fundamentals_refresh` reports
  `allocations_updated` and ratings `funds_not_asked`; PEG on Fundamentals, the watchlist and
  the buy score now prefers Yahoo's `trailingPegRatio` — a changed figure is expected, a blank
  one is not. The first watchlist run fetches every item in full.
- **The weekend schedule after #9:** Sunday 2026-10-18 shows no `market_data_only` rows and an
  18:00 `full_sync` whose `market_result` writes few or no rows; Saturday 2026-10-24 shows only
  the 08:00 `market_data_only`. Weekday slots unchanged.
- **Look-through after #8 (migration `a7d4c1e9f3b8`):** the first 18:00 `full_sync` fetches and
  rewrites every held basket once (no stamps or fingerprints yet) and runs the CINS pass; from
  the next evening `lookthrough_result.baskets` lists most funds under `unchanged` or
  `refreshed` daily, and no stale-basket warning appears on an ordinary week.
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
- **VT's basket on Vanguard's new route (#7):** the next 18:00 `full_sync` lists VT under
  `lookthrough_result.baskets.refreshed` with as-of ≥ 2026-09-30, nothing under `failed`, and
  `/api/portfolio/lookthrough` shows VT `stale: false`. Verified only from a residential IP; if
  the VPS gets an HTML body (Akamai), the refusal names it.
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
- **The 2026-10-17 outage (~15:28–15:40 UTC):** every port timed out from the owner's network
  while port 443 refused from outside; it recovered unaided before the deploy. Cause unknown —
  `uptime` on the VPS would show a reboot.
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

1. **Pin what every production build re-resolves:** `yfinance`, `lxml`, `pyxirr` float in
   `backend/requirements.txt` and `deploy.sh` builds `--no-cache`. A constraints file.
2. **Drop `crypto_daily`** once compared (model, migration, smoke fixture, probe), with a CLI to
   map a CoinStats coin to CoinGecko by hand.
3. **Tooling.** ESLint is clean; `positionColumns` and `lookthroughColumns` are its one named
   exception (`eslint.config.js`). Ruff unconfigured; neither in CI. `alembic check` fails on
   naming drift only. The `ResizeObserver` stub is in 17 test files — `src/testSetup.ts` is now
   the place for it. CI pins Node 20 (EOL).
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
10. **UI follow-ups from the fintech pass:** `/api/tax/report`'s realized and holdings rows carry
    no ISIN or security id, so their avatars are monograms; Activity's phone cards have no logo
    (`DataTable`'s card view); the earnings calendar and surprise history list some companies
    twice (seen on production 2026-10-17, not investigated); `SectionHeader`'s `aside` is
    `shrink-0`, so a wide one overflows a phone (Dividends moved its controls out).
11. **Smaller:** a currency breakout for idle foreign cash; *Prior Period Positions* is ticked in
    the Flex Query and never read (untick or use); fold `prune_empty_dividends` into a job only
    after reassessing, reusing the CLI's predicate.

## Recent sessions (last 5)

One line each, newest first; drop the oldest.

- **2026-10-18 (two UI fixes)** — Dividends *Expected next* lost its sideways scrollbar and got a
  themed thin one; Watchlist pins the Stock column (`DataTable` `pinFirstColumn`). Seen at 1366.
- **2026-10-17 (fintech UI, agent team)** — design foundation, then four tabs' areas in parallel
  worktrees; logos by ISIN; every `--viz-*` token replaced by one ten-slot palette.
- **2026-10-17 (merge)** — the fintech pass merged into `main` over the owner's batch; two
  held helpers made one (`security_repository.held`).
- **2026-10-17 (owner's batch)** — raises/timing (funds' changes are timing), resizable forecast
  chart, base-currency market caps, daily held-only fundamentals; deployed; logos next.
- **2026-10-17 (fintech pass, web session)** — surfaces, hero figure, quiet tables, one chart
  look; tax footers, held-only fundamentals, period return, tax estimates, base currency, test
  DB, CLI lookup, the copies of one job and ESLint closed; every e2e script green.
