# Working state

> The perishable half of the project guide: what needs a human, what is being watched, what is
> unverified, and what is next. CLAUDE.md's *Keeping STATUS.md current* says when and how to edit
> it. Subsystem reasoning is in `docs/<topic>.md`; shipped write-ups in
> [docs/shipped-log.md](docs/shipped-log.md); accepted rough edges in
> [docs/troubleshooting.md](docs/troubleshooting.md); local-dev traps in
> [docs/local-development.md](docs/local-development.md).

**Last updated: 2026-10-14.** Production healthy on `f7ebbf5`. The agent-team wave of 2026-10-14
is live and browser-checked (docs/shipped-log.md); its scheduled halves are under *Watch after the
next deploy*.

---

## Needs a human

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

- **The first `fundamentals_refresh`, Sunday 2026-10-18 04:00 Berlin:** a success row in
  `/api/scheduler/history`, no rate-limit warning, the Fundamentals as-of within a week (it reads
  2026-08-25 until then).
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

0. **What the 2026-10-14 browser sweep found.**
   - Watchlist scrolls 211 px sideways at 1440: the `sr-only` "Actions" header span is absolute
     with no positioned ancestor inside the table scroller.
   - Chart y-axis labels clipped: Performance at 1440 (leading "C" of "CHF") and 390; the Forecast
     chart at 390, whose "Years" title also overlaps the legend.
   - Tax at 390 truncates "Withholding" in the DA-1 country cards.
   - The heatmap's range-clipped first month and its year's YTD are unmarked as partial.
   - ASML shows as two companies in look-through (one LEI-folded, one via a fund with identity
     "none").
   - Leftover explanatory text: the sibling-NAV tooltip (`PositionsList.tsx:86`), the YoC
     definition (53 words), the Watchlist column popover (165 words), the 3a row tooltip, the WHT
     popover hint, two look-through lines ("A further N companies…", "not folded by an
     identifier").
   - Nits: the 3M x-axis repeats month labels; "1191.1TKRW" lacks a space; Allocation's region list
     mixes regions and countries. Confirm NVDA's +2400% payout growth is a real raise.
1. **The test suite writes into the developer's database.** `tests/conftest.py:42` blanks only the
   job store; the scheduler-job spy tests and the WAL-lifespan test write through
   `AsyncSessionLocal` into whatever `DATABASE_URL` names, and under xdist N workers share one
   file (a plausible cause of the one flaky run). Fix: a per-worker temp DB before `settings` is
   imported. Until then never run the suite against a snapshot you still need.
2. **Pin what every production build re-resolves:** `yfinance`, `lxml`, `pyxirr` float in
   `backend/requirements.txt` and `deploy.sh` builds `--no-cache`. A constraints file.
3. **Copies of one job.** `_to_eur` ×3 (`dividend_service`, `tax_service`, `finpension_ingest` —
   the last swallows errors) plus an inline FX path with a `"USD"` placeholder
   (`dividend_service.py:471`); the CLI security resolver ×3, and **`manage_mappings.py:79-83`
   takes `.first()` without refusing ambiguity**; `_pct` drifted (look-through 0.0, analytics
   None); the crypto clients' `_pace`/`_get`. Extract and test the family.
4. **`BaseFx.convert` returns the EUR amount when no rate exists** (`base_fx.py:58-60`), under ~57
   call sites; only crypto guards it.
5. **Tax report estimated rows, backend half:** no per-country estimate count, `to_csv()` writes an
   estimate's withholding as 0, and a mixed year's net total includes estimates.
6. **Drop `crypto_daily`** once compared (model, migration, smoke fixture, probe), with a CLI to
   map a CoinStats coin to CoinGecko by hand.
7. **Four nulls-last sort comparators** (`FundamentalsTab`, `cryptoColumns`, `WatchlistTab`,
   `lib/dividendTable.ts`) and `ABSENT` in both `lib/utils.ts` and `ui/KpiCard.tsx` — one helper
   each, with a family test.
8. **Tooling.** ESLint red (19, incl. four setState-in-effect in `FundamentalsTab.tsx`), ruff
   unconfigured; neither in CI. `alembic check` fails on naming drift only. The `ResizeObserver`
   stub is in 17 test files (belongs in `setupFiles`). CI pins Node 20 (EOL).
9. **`coverage_pct` reads 0.0 on an empty book** (`lookthrough_service.py:853`).
10. **KPI audit 2026-10-02 — low priority (owner):** deposit-driven % read as growth (Total Value
    chip, Period Gain %, dividend chips); MoM/YoY chips in the dividend tooltip; XIRR annualised
    over short ranges; 1W risk from 5 returns and a 3% EUR risk-free rate; PEG mixes three methods;
    "Rose after sale" counts ETF rotations; two gain figures; cost basis labelled "total invested".
11. **Project the benchmark's baseline like the portfolio's** — the running cost basis is converted
    at each point's date, the portfolio's per lot at `open_date`; under 0.5%. Re-project the cost
    events at read time; **do not** make the portfolio convert per date (breaks the contributions
    identity).
12. **Should a trade still count as an external flow, now that cash is tracked?** Likely: serve
    both TWRs, labelled. `externalFlow`/`isMeasurable` in `portfolioKpis.ts`, `dailyReturnSeries`'
    five consumers and `betaAndCorrelation` must move together.
13. **Look-through projects:** measured sector/geography from baskets replacing `etf_mappings.py`'s
    estimates (needs a country→region map); exact World ex CH; nested funds; an identity detector.
    Researched future sources (N-PORT, commercial APIs — do not re-shop) are in docs/lookthrough.md.
14. **Crypto follow-ups:** a daily `type=3m` pull (10 credits/day); a Binance/Phantom split; a
    transactions ledger (no addresses or hashes); CORS registered outermost so a 401 shows locally.
15. **Brinson attribution** needs benchmark sector data first (docs/performance-analytics.md).
16. **Smaller:** a currency breakout for idle foreign cash; *Prior Period Positions* is ticked in
    the Flex Query and never read (untick or use); fold `prune_empty_dividends` into a job only
    after reassessing, reusing the CLI's predicate.

## Recent sessions (last 5)

One line each, newest first; drop the oldest.

- **2026-10-14 (docs trim)** — STATUS.md cut to a snapshot (rough edges → troubleshooting, traps →
  local-development), CLAUDE.md slimmed, AGENTS.md made a pointer.
- **2026-10-14 (agent-team wave)** — growth/forecast pack, KPI fixes, ET Sun/Mon IBKR skip, weekly
  fundamentals refresh, STATUS pruned; browser sweep on production.
- **2026-10-14 (test speed)** — backend under `pytest-xdist`, `deploy_scripts` deselected locally:
  5:35 → ~25 s.
- **2026-10-14 (strip the text)** — every tab's explanatory text stripped; forecast backfill run.
- **2026-10-13 (watchlist, forecast breakdown)** — nightly watchlist slot; per-security forecast
  lines and the growth split.
