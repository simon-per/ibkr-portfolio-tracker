# Dividend raises, resizable forecast chart and base-currency market caps done; logos and daily fundamentals still to build

**Date:** 2026-10-17
**Status:** IN PROGRESS (3 of 5 asks implemented and committed on the branch; 2 planned, not started)
**Bead(s):** none
**Epic:** none (owner's 2026-10-17 request batch)
**Chain:** `standalone-f352c411` seq `1`
**Parent:** `none — first in chain`
**Prior chain:** none — first in chain
**Branch:** `worktree-moonlit-drifting-matsumoto` (3 commits on top of `origin/main` @ `e1b9a83`; `origin/main` had nothing newer at the time of writing)

> Public repo: this file deliberately carries **no account figures** (CLAUDE.md: never write
> figures into committed status docs; the base currency is switchable). Where the owner's
> screenshot showed amounts, they are described, not quoted.

---

## Reference Documents

- `CLAUDE.md`: the two never-break rules (no Yahoo without permission; never loop the IBKR Flex sync), conventions, and the docs index. Read it first.
- `STATUS.md`: the snapshot doc. It must be left accurate before stopping. It was **not yet updated** for this branch; see *Where We're Going*.
- `docs/dividends.md` § *What the forecast's growth was made of — `dividend_forecast_snapshot_lines`*: updated in this branch for raises/timing.
- `docs/flex-and-sync.md` § *Sync schedule*: must be updated by §4 (fundamentals slot).
- `docs/pricing-and-fx.md`: updated in this branch (market caps paragraph).
- `docs/frontend.md` § *The product pass (2026-10-16)*:
  - updated for the resizable chart;
  - its `TickerAvatar` bullet (line ~477, "never a fetched logo") must be rewritten by §3.
- `docs/troubleshooting.md` § accepted rough edges: updated (market_cap_base null cases).
- `docs/local-development.md` § *Traps*: worktree setup and the Node 26 `localStorage` trap; both bit this session.
- The approved plan lived at `C:\Users\simon\.claude\plans\moonlit-drifting-matsumoto.md` on the owner's PC. It is **not in the repo**, so its full content for the unbuilt sections (§3, §4) is reproduced in *Where We're Going* below.

## The Goal

The owner asked for five things on 2026-10-17. A separate cloud session on another device was restyling the UI at the same time, so **component edits had to stay functional and minimal**: no restyling and no layout changes beyond what each ask needs. Logic goes in `lib/` and the backend.

1. **§1 Dividend raises in the Dividends tab's "Next 12 months" header.** It showed only "+X from buys". Raises were buried in a "payouts and timing" part that also moves when payments enter or leave the 365-day window, and it read negative on a book whose payers had raised.
2. **§2 Forecast chart** too small to see growth (120 px). Make it taller when opened and drag-resizable.
3. **§3 Real logos** (Google, Amazon, Vanguard…) in place of the ticker monograms:
   - stored in a separate place in the DB;
   - shown cleanly in the round icon;
   - only the logo **mark** ("G", not the "Google" wordmark).
4. **§4 Fundamentals refreshed automatically**, not only the watchlist, without API-call spamming.
5. **§5 Market cap normalised to the chosen base currency** (CHF/EUR/USD), with FX coverage for JPY, CNY ("Chinese yen" = yuan), KRW and more "to be safe".

End state the owner wants: the branch on GitHub so another cloud session can continue §3 and §4 and get it merged and deployed.

## Where We Are

Committed on `worktree-moonlit-drifting-matsumoto`. **Not pushed yet** as of writing this file; the owner asked to push after this handoff.

- **§1 raises — DONE, commit `bdb5758`.**
  - `dividend_forecast.payout_level(history)`: new.
  - Per-line `level_eur` everywhere it flows: model, migration `e5b1d7f3a9c2`, breakdown line, snapshot lines.
  - Attribution parts are now `("shares", "raises", "timing", "newly_forecast", "fx")`. `payout` was renamed to `timing`.
  - The backfill fills missing levels.
  - Frontend shows "+X from raises" (or "from cuts").
  - Docs updated.
- **§2 chart — DONE, commit `e41f03c`.**
  - `frontend/src/lib/useResizableHeight.ts` (new): default 260, range 120–560, pointer and keyboard.
  - `DividendForecastTrend.tsx` uses it and has a grip under the chart.
  - `docs/frontend.md` bullet.
- **§5 market cap — DONE, commit `ad7607f`.**
  - `backend/app/services/market_cap.py` (new): shared converter, quoted-currency warm-up.
  - The Fundamentals tab and the Watchlist show the base-currency figure, falling back to native + code.
  - The watchlist API serves `market_cap_base`.
  - FX sync warms the quoted currencies, throttled to one refresh per 3 days each.
  - Family test.
- **§4 fundamentals daily — NOT STARTED.** The plan is fully specified below.
- **§3 logos — NOT STARTED.** The plan is fully specified below; the owner chose logo.dev's API.
- **Docs/status bookkeeping NOT done:**
  - STATUS.md: *Last updated*, *Needs a human*, *Watch after deploy*, *Recent sessions*;
  - a `docs/shipped-log.md` entry;
  - new `docs/logos.md` plus its CLAUDE.md index row (with §3).
- **Production steps owed after merge and deploy:**
  - the forecast backfill (command below);
  - for §3: `LOGO_DEV_SECRET_KEY` in the VPS `.env`, then the first `fetch_logos`;
  - for §4: refresh `/root/auto-deploy.sh` for the new hour 5.

**Tests run (all green at the end):**
- Backend subsets:
  - attribution 21/21;
  - forecast 37/37;
  - snapshot 21/21;
  - 187 passed: smoke + contract-drift + forecast files + migrations;
  - 175 passed: smoke + market-cap family + drift + market cap + crypto isolation + scheduler jobs;
  - market cap 7/7;
  - migrations + model metadata 9/9.
- Frontend subsets:
  - 45 passed (`dividendForecastTrend.test.ts`, `DividendForecastTrend.test.tsx`, `DividendsTab.test.tsx`);
  - 17 passed (`useResizableHeight.test.ts`, `DividendForecastTrend.test.tsx`, `storage.test.ts`);
  - 73 passed (`FundamentalsTab`, `tableFamily`, `ratingSortFamily`, `utils`);
  - `utils.test.ts` 41/41;
  - `npx tsc -b` clean.

**Not run:**
- the **full** backend suite after all three commits (only subsets, plus a serial baseline mid-edit);
- the **full** frontend suite;
- `npm run build`;
- any browser check.

## What We Tried (Chronological)

1. **Exploring where "from buys" comes from.**
   - `frontend/src/lib/dividendForecastTrend.ts` reads `breakdown.shares` as `fromBuys` and lists the other parts via `PART_LABELS` (`payout` → "payouts and timing", `newly_forecast`, `fx` → "currency", `unattributed` → "unexplained"). `PART_MIN = 0.5` hides small parts.
   - The backend split is `backend/app/services/dividend_forecast_attribution.py`: shares `(q1-q0)·n0·g0`, payout `q1·(n1-n0)·g0`, fx `q1·n1·(g1-g0)`, chained day to day in `DividendService.get_forecast_history`.
   - `docs/dividends.md` already admitted "`payout` is per share AND timing… The measure of raises is the per-share figure (`per_share_growth_pct`)".
   - Conclusion: a raise has to be told apart from timing **per day pair**, which needs a timing-free per-share measure stored per line.
2. **Candidate "level" definitions** (the measure of a payer's dividend that ignores the 365-day window):
   - (a) the "recent" side of `per_share_growth`, i.e. the latest payment for a steady payer and the sum of the last cycle for a varying one. **Rejected:** the two shapes give sums of different counts, so a steady↔varying flip would jump about 4× and read as a huge raise.
   - (b) `per_share_growth` year over year. **Rejected:** it is not "since `growth_from`", and it does not chain or sum with the other parts.
   - (c) **Chosen:** `project_dividends` itself over a horizon anchored on the payer's own newest scheduled payment, `(last_seen, last_seen + 12 months]` with `as_of=last_seen`, summed. This reuses the forecast's own sizing rules (no second rule), gives "a year's worth" for both shapes, and moves only on new amount evidence.
3. **Horizon boundary check.**
   - A horizon of `last_seen + 365 days` would include or exclude the +12-month slot depending on leap years.
   - Ending at `last_seen + 11 months` would drop a varying payer's newest slot, so its raise would never show.
   - Settled on `_add_months(last_seen, 12)`, inclusive.
4. **WHT neutrality.**
   - `hist_by_sec` per-share amounts are **net** (`_forecast_inputs` applies `_estimated_net_from_gross(gross, 1 - rate)`).
   - The level is taken back to gross: `level / (1 - wht_rate)` with `wht_by_sec.get(sid, (1 - net_factor, "assumed"))`.
   - So a change of withholding ladder rate lands in timing, not raises.
5. **Raises formula.** `raises = q1·n0·(L1/L0 − 1)·g0` (the old window at the new level) and `timing = payout − raises`.
   - With a level missing on either day, raises = 0 and everything stays in timing.
   - On a detected split (`_is_split`), `L0` is rescaled by `q0/q1` like `q0`.
   - Verified by hand-computed unit tests (table below).
6. **Storage of levels.** Computing levels at read time (replaying history for every day × security) was rejected as heavy and a second copy. Instead a nullable column `level_eur` goes on `dividend_forecast_snapshot_lines`.
   - Old lines get levels from the backfill CLI.
   - Recorded lines are filled **only where NULL** (`fill_levels`), so the "replay never replaces recorded" rule holds.
7. **Backfill coverage gap found.** Days 2026-10-07..12 have recorded totals and *replayed* lines. The old `unlined` logic ("recorded days with no lines at all") would never refresh them.
   - Changed to "recorded days with **no recorded lines**", so their replayed lines get re-replayed (now with levels) on every run.
   - This changed an existing test's expectation (second run used to fill nothing).
8. **Chart sizing.**
   - Considered a secondary axis or a non-zero baseline (the received line at ~⅓ of the forecast forces the axis to 0). **Rejected:** the doc's intent is to watch *received* climb toward *forecast* on one scale.
   - Considered CSS `resize: vertical`. **Rejected:** no touch support on iOS.
   - Chose a custom pointer-capture grip plus keyboard, with the height stored per viewer.
9. **Logo source research** (web-fetch agent read the official docs and terms):
   - Brandfetch's free Logo API **requires hotlinking**: "Programmatic access to logo images is not permitted", plus a Referer requirement. That is incompatible with storing in the DB and with CSP `img-src 'self'`.
   - logo.dev's **CDN** forbids copying, but its **API** allows storing: "On every plan, you can store anything the Logo.dev API returns".
   - Website favicons (no account) were offered as an alternative.
   - **Owner chose logo.dev API.**
10. **Fundamentals cadence.**
    - Discovered a weekly Sunday 04:00 Berlin job already exists (`e9bd864`, 2026-10-14) that had never run; its first run is 2026-10-18.
    - Offered "keep weekly" (recommended) or "daily Tue–Sat 05:00". **Owner chose daily Tue–Sat 05:00.**
    - Implication found: `STALE_AFTER_DAYS = 1` would refresh only every other day under a daily slot (a row written 05:02 is 23 h 58 m old at the next 05:00). Needs `timedelta(hours=20)`.
11. **Market cap.**
    - The Fundamentals tab already had `market_cap_base` (cache-only converter `FundamentalsService._market_cap_to_base`) but **displayed** native + code while **sorting** by base.
    - The watchlist printed a bare native number.
    - Extracted the converter to `app/services/market_cap.py` and showed base with native fallback on both tabs.
    - Discovered Frankfurter warm-up is **not incremental**: one request per ECB currency on every `sync_exchange_rates` call, which runs in several slots a day. So extra quoted currencies are added only when their newest cached rate is ≥ `QUOTED_REFRESH_DAYS` (3) days old.
12. **Environment trouble.**
    - The worktree had no `backend/venv`, no `backend/.env` and no `frontend/node_modules`.
    - Used the main checkout's venv by absolute path. It lacks `pytest-xdist`, so suites ran serially (about 5 min).
    - A junctioned `node_modules` from the main checkout lacked `@fontsource-variable/inter`. Replaced it with `npm ci` in the worktree.
    - Details are in *Risks & Blockers*.
13. **Encoding mishap.** A Python rewrite script used Windows' default cp1252, hit a `→`, and **truncated `dividend_forecast_attribution.py` to empty**. Recovered by rewriting the whole file and converting it to CRLF. Always pass `encoding='utf-8', newline=''`.
14. **Shell guard.** The worktree-isolation guard refused complex Bash: long heredoc Python scripts, `cd .. && git …`, commands with `$VAR`. Plain `git -C <absolute path> …`, the Edit tool and short commands work.

## Key Decisions

- **Rename `payout` → `timing` and add `raises`** rather than keeping `payout` with a narrower meaning. A word whose meaning changes silently is the "two copies drifting" failure mode (CLAUDE.md). The API contract changed: `DividendForecastChange` in `schemas/portfolio.py` and `frontend/src/lib/api.ts`. `test_api_contract_drift.py` pairs them and caught the frontend lag as expected.
- **Level = `project_dividends` over an anchored year** (no second sizing rule). Rejected alternatives are in *What We Tried* #2.
- **Gross level** (WHT divided back out), so a withholding change is never a "raise".
- **Missing level ⇒ no raise measured**, never guessed ("an unknown is absent").
- **`fill_levels` writes only NULLs, including on recorded lines.** A missing value is not a recorded one; nothing else on the line changes.
- **Backfill re-replays the replayed lines of recorded days** on every run, like any replay. Behaviour change; test updated.
- **Header shows raises beside buys.** The other parts (timing, newly forecast, currency, residual) stay in the collapsed sentence. Label: "from raises" / "from cuts" (negative). Parts label: `['timing', 'timing']`.
- **Chart stays on one Y scale.** Default height 260 px, drag range 120–560 px, keyboard step 20 px. Height stored under localStorage key `dividendForecastTrendHeight` through `lib/storage.ts` (`storage.test.ts` scans for bare `localStorage` use, so use the helper).
- **Market cap display:** base figure with the base prefix (`€26.0B`, `CHF 810.0B`). When `market_cap_base` is null (minor unit GBp/ZAc/ILA, or no rate fresher than 30 days), show the **native figure + code**, never a native number under the base symbol, and never "—" if the native exists. Sort is on the base figure on both tabs (watchlist column newly sortable).
- **One converter** in `app/services/market_cap.py`, pinned by `tests/test_market_cap_family.py`:
  - every module mentioning `market_cap_base` (except `schemas/portfolio.py` and `market_cap.py`) must import `market_cap_to_base`;
  - no other function named `*market_cap*base*`;
  - the family is non-empty.
- **Quoted currencies refreshed every ≥3 days**, not per slot (Frankfurter cost). Safety set `MARKET_CAP_CURRENCIES = {JPY, CNY, HKD, KRW, TWD, GBP}`. TWD is also in `CurrencyService.WARM_CURRENCIES` (fallback provider) and is skipped as "warmed anyway".
- **Logo source: logo.dev API** (owner). **Fundamentals: daily Tue–Sat 05:00** (owner; I had recommended weekly).

## Evidence & Data

### Commits on the branch

| hash | summary | files |
|---|---|---|
| `bdb5758` | feat(dividends): split the forecast's payout change into raises and timing | 18 |
| `e41f03c` | feat(ui): the dividend forecast chart opens taller and resizes by dragging | 5 |
| `ad7607f` | feat(fundamentals): market caps in the base currency on both tabs | 19 |

Total vs `origin/main`: 37 files, +1157 / −193.

### Attribution unit cases (hand-computed, all pass)

| case | prev (q, next12m, fx, level) | cur | expected |
|---|---|---|---|
| raise on steady quarterly payer | 100, 400, 1, 4 | 100, 420, 1, 4.2 | raises 20, timing 0 |
| payment leaves window | 100, 400, 1, 4 | 100, 300, 1, 4 | timing −100, raises 0 |
| raise + payment leaves same day | 100, 400, 1, 4 | 100, 315, 1, 4.2 | raises 20 (100×4×5%), timing −105 (one 1.05 payment × 100) |
| no level on one day | 100, 400, 1, None | 100, 420, 1, 4.2 | raises 0, timing 20 |
| USD payer, dollar falls, level flat in USD | 10, 90, 0.9, 9 | 10, 80, 0.8, 8 | raises 0, timing 0, fx −10 |
| 10:1 split + 2.5% raise | 10, 40, 0.9, 4 | 100, 41, 0.9, 0.41 | shares 0, raises 1, timing 0 |
| bought + raise, no levels (old test) | 100, 400 | 150, 630 | shares 200, raises 0, timing 30 |

Plus parametrised "parts always sum to the change" with 8 cases, 3 of them with levels.

### `payout_level` unit cases (all pass)

| history | level |
|---|---|
| quarterly 0.20 ×7, then 0.22 | 0.88 (4 × 0.22) |
| quarterly 0.50 ×4 (2025) | 2.00, independent of today |
| SEASONAL fund (2024/2025 quarters 0.40/0.70/0.45/0.90, 0.42/0.72/0.47/0.95) | 2.56 (the last cycle) |
| level 1.00 ×4 then 1.00 vs 1.05 | 4.00 vs 4.20 |
| 1.00 ×4 + accrual 1.10 (`cadence=False`) | 4.40 (counts on announcement) |
| a single payment / empty | None |
| paused payer (2023 only) | 1.20 (`as_of=last_seen`, not "stopped") |

### Forecast-history fixture after the change (`test_dividend_forecast_snapshot.py`, `_three_days`)

```
DAY_1 lines: #1 (100 sh, 400, fx1, level 4); #2 USD (10 sh, 90, fx .9, level 9); #3 (5 sh, 0)
DAY_2/3:     #1 (150 sh, 600, level 4);       #2 (10 sh, 100, fx .8, level 10); #3 (5, 0)
DAY_4:       as DAY_2 but #3 (5 sh, 50); stored total 10 EUR above the lines
EUR->CHF: 0.90 day 1, 0.80 after
points[1].breakdown = {since 2026-09-01, shares 180.0, raises 20.25, timing 0.0,
                       newly_forecast 0.0, fx -81.25, unattributed 0.0}
points[3].breakdown = {... newly_forecast 40.0, unattributed 8.0}
growth_from=DAY_2 case: shares 0, raises 0, timing 0, newly_forecast 40, fx 0, unattributed 8
```

### Smoke test expectations changed (`backend/tests/test_api_smoke.py`)

- `test_the_forecast_history_serves_its_breakdown`: `{"since", "shares": 7.52, "raises": 0.0, "timing": 0.0, "newly_forecast": 0.0, "fx": 0.0, "unattributed": 0.0}`. Line keys now include `"level_eur"`.
- New watchlist fixture: `WatchlistItem(yahoo_ticker="NVDA", market_cap=4_000_000_000_000, data_currency="USD")`. `/api/watchlist` returns `market_cap_base == int(4e12 × 0.9 × 0.94)` (fixture USD→EUR 0.9, EUR→CHF 0.94).

### Test-run log

| run | result |
|---|---|
| baseline, serial, whole backend (mid-edit) | 1 failed, 2008 passed, 9 skipped, 301 s; the failure was `test_migrations::test_the_migrations_produce_the_schema_the_models_describe`, caused by the model column existing before its migration; re-run after the migration: 9/9 |
| attribution | 21 passed |
| `test_dividend_forecast.py` | 37 passed |
| `test_dividend_forecast_snapshot.py` | 21 passed |
| drift + snapshot + attribution + forecast + smoke + migrations | 187 passed (61.6 s) |
| smoke + market-cap family + drift + market cap + crypto isolation + scheduler jobs | 175 passed (32 s) |
| `test_fundamentals_market_cap.py` | 7 passed (2 failed first: `fundamental_metrics.last_updated` NOT NULL in fixture; fixed) |
| frontend: trend lib + component + DividendsTab | 45 passed (87 s, first run on the junctioned node_modules) |
| frontend: resize hook + component + storage | 17 passed |
| frontend: Fundamentals + tableFamily + ratingSort + utils | 73 passed |
| `npx tsc -b` | clean, after `npm ci` in the worktree |

### logo.dev facts (from the official docs, read 2026-10-17)

| item | value |
|---|---|
| server-side route | `GET https://api.logo.dev/v2/brands/logo?isin=…` (or `ticker=`), `Authorization: Bearer sk_…` |
| response | signed `url` valid ~24 h, plus `etag`; download params `size` (max 800), `format` jpg/png/webp, `theme` |
| storing | allowed for API results ("you can store anything the Logo.dev API returns"); the CDN must NOT be copied |
| cost | Logo download $0.02/request; free Community plan has $5/month API credits (~250 downloads) and 100 API req/min |
| unknown | 404 `not_found` (not billed); 202 `not_indexed` + `Retry-After`; brand found but no image → 404 **billed** |
| attribution | not needed for "a project you own as an individual and that earns nothing" |
| square mark | "The CDN serves each company's square logo"; JPG puts it on a white box, `format=png` is transparent |
| ETFs | `/ticker/SPY` works like a share; **untested** whether VT/QQQM/SOXQ return the Vanguard/Invesco mark |

Brandfetch (rejected): "Programmatic access to logo images is not permitted", "hotlink… don't download, cache, or re-host". Caching is only by written agreement, max 30 days.

### FX facts relevant to §5

- `CurrencyService.SUPPORTED_CURRENCIES` is the ECB 30 + EUR (has JPY, KRW, CNY, HKD; **no TWD**).
- Fallback `https://open.er-api.com/v6/latest/EUR` is latest-only, `FALLBACK_MAX_AGE_DAYS = 7`.
- `WARM_CURRENCIES = {TWD, CNH, AED, SAR, QAR, KWD, RUB, CLP, COP}`.
- `CARRY_FORWARD_MAX_AGE_DAYS = 30`.
- `sync_exchange_rates` is called with `days_back=30` (full sync) and `days_back=7` (two other paths); `scheduler_service.py` ~1336/1425/1637.

## Code Analysis

- `backend/app/services/dividend_forecast.py`
  - `payout_level(history: Sequence[HistPayment]) -> Optional[Decimal]`, placed just before `_size_for`.
  - Uses `scheduled = [p for p in history if p.cadence]`, `last_seen = max(on_date)`, and `project_dividends(list(history), Decimal("1"), last_seen + 1d, _add_months(last_seen, 12), as_of=last_seen)`.
  - Steady payers size with `METHOD_LATEST` (latest regular payment, accruals included). Varying payers use the same slot a year back (`_size_for` limits the look-back via `last_seen + tol`).
- `backend/app/services/dividend_forecast_attribution.py`
  - `PARTS = ("shares", "raises", "timing", "newly_forecast", "fx")`.
  - `Holding(shares, next_12m_eur, fx_to_eur=None, level_eur=None)`.
  - In `attribute_change`, `level_scale = q0/q1` on a split. Raises need both `level_eur` > 0.
  - `SPLIT_MIN_RATIO = 1.5`, `SPLIT_VALUE_TOLERANCE = 0.05` (unchanged).
- `backend/app/services/dividend_service.py`
  - Imports `payout_level`. A `levels: Dict[int, Decimal]` is filled after `ps_growth` in `get_dividend_breakdown`.
  - `forecast_by_security[*]["level_eur"]` is `round(float, 8)` or None.
  - `forecast_figures` passes `level_eur` as a Decimal into lines.
  - `get_forecast_history` builds `Holding(ln.shares, ln.next_12m_eur, ln.fx_to_eur, ln.level_eur)`. The parts loop is driven by `FORECAST_CHANGE_PARTS`, so the new parts flow automatically.
- `backend/app/repositories/dividend_forecast_snapshot_repository.py`
  - `fill_levels(snapshot_date, {security_id: level}) -> int`: SQL UPDATE … WHERE `level_eur IS NULL`, any `reconstructed`.
  - `dates_missing_levels()`: recorded lines with NULL level.
  - `dates_with_recorded_lines()`.
  - `dates_with_lines()` was **removed** (its only user was the backfill).
- `backend/app/cli/backfill_dividend_forecast.py`
  - `backfill()` returns `levels_filled`. In a dry run that is the count of non-None replayed levels; in a real run, the rows updated.
  - Replays the union of overlap days, `unlined` (no *recorded* lines) and `unlevelled`.
  - Printout line "Gave N recorded line(s) the payer level…"; `sync_runs.details.levels_filled`.
- `backend/app/services/market_cap.py`
  - `market_cap_to_base(db) -> async (market_cap, currency) -> Optional[int]`: cache-only via `CurrencyService._get_most_recent_rate`, refuses stale (> 30 days) and minor units.
  - `quoted_currencies(db)`: fundamentals + watchlist `data_currency`, 3-letter, non-minor, minus EUR, plus `MARKET_CAP_CURRENCIES`.
  - `quoted_currencies_to_warm(db, today, warmed_anyway)`.
- `backend/app/services/scheduler_service.py` `sync_exchange_rates`
  - `today = date.today()` moved earlier (pre-existing use of `date.today()`, not `clock.utcnow()`; left as it was).
  - `currencies = held | WARM_CURRENCIES | quoted`; result gains `"quoted_currencies"`.
- `backend/app/routers/watchlist.py`
  - `async _responses(db, items)` computes the base cap per item.
  - `_item_to_response(item, market_cap_base=None)`.
  - GET, POST and PATCH all go through `_responses`.
- `frontend/src/lib/utils.ts`
  - `marketCapParts(cap, currencySymbol) -> {figure, code}`.
  - `currencySymbolUtil('CHF')` yields a prefix like `'CHF '` (the test uses `'CHF '`, giving `'CHF 810.0B'`).
- `frontend/src/lib/useResizableHeight.ts`
  - Exports `clampHeight`, `storedHeight`, `useResizableHeight({storageKey, initial, min, max, step=20})` → `{height, handleProps}`.
  - `handleProps`: role separator, aria values, `tabIndex 0`, and pointer down/move/up/cancel plus keydown handlers.

## Files Changed

### Source code — backend
- `backend/app/services/dividend_forecast.py`: `payout_level` (new).
- `backend/app/services/dividend_forecast_attribution.py`: raises/timing split, `Holding.level_eur`, docstring rewritten.
- `backend/app/services/dividend_service.py`: levels computed (gross) and served on `forecast_by_security`; `forecast_figures` lines carry `level_eur`; history passes the level; `get_forecast_history` docstring updated.
- `backend/app/models/dividend_forecast_snapshot.py`: `DividendForecastSnapshotLine.level_eur Numeric(18,8) nullable`, plus a docstring paragraph.
- `backend/alembic/versions/e5b1d7f3a9c2_add_level_to_forecast_snapshot_lines.py` (new): `down_revision 'd4a9c5e7f1b3'`; plain `add_column`; downgrade via `batch_alter_table`. **Now the head.** §3's migration must chain from `e5b1d7f3a9c2`.
- `backend/app/repositories/dividend_forecast_snapshot_repository.py`: `fill_levels`, `dates_missing_levels`, `dates_with_recorded_lines`; `dates_with_lines` removed.
- `backend/app/cli/backfill_dividend_forecast.py`: levels, re-replay of replayed lines, reporting.
- `backend/app/schemas/portfolio.py`:
  - `DividendForecastLine.level_eur`;
  - `DividendForecastChange.raises` + `.timing` (`payout` removed; docstring says six figures sum to the change);
  - `FundamentalMetricsResponse.market_cap_base`;
  - `WatchlistItemResponse.market_cap_base`.
- `backend/app/services/market_cap.py` (new): shared converter, quoted currencies.
- `backend/app/services/fundamentals_service.py`: `_market_cap_to_base` removed, uses `market_cap_to_base(self.db)`, unused `Decimal` import removed.
- `backend/app/routers/watchlist.py`: base cap on every response.
- `backend/app/services/scheduler_service.py`: `sync_exchange_rates` warms quoted currencies.

### Source code — frontend
- `frontend/src/lib/api.ts`: `DividendForecastLine.level_eur`; `DividendForecastChange` `raises`/`timing` (no `payout`); `WatchlistItem.market_cap_base`; doc comments on `FundamentalMetrics.market_cap(_base)`.
- `frontend/src/lib/dividendForecastTrend.ts`: `fromRaises`, `fromRaisesLabel`, `material()` helper, `PART_LABELS` timing.
- `frontend/src/components/DividendForecastTrend.tsx`:
  - one "+X from raises" span after the from-buys span;
  - `CHART_HEIGHT` constant;
  - chart box `style={{height}}`;
  - grip div (`aria-label="Resize chart"`, `-mt-1 h-3 cursor-row-resize touch-none`, inner `h-1 w-10 rounded-full bg-border`).
- `frontend/src/lib/useResizableHeight.ts` (new).
- `frontend/src/lib/utils.ts`: `marketCapParts`.
- `frontend/src/components/FundamentalsTab.tsx`: `metricsColumns(currencySymbol)`, cell via `marketCapParts`, `useCurrencySymbol` in `MetricsTable`.
- `frontend/src/components/watchlistColumns.tsx`:
  - `deps.currencySymbol`;
  - column `sortKey: 'market_cap_base'`, hint "In the base currency", cell via `marketCapParts` (`"figure code"` string);
  - `WatchlistSortColumn` gains `'market_cap_base'`.
- `frontend/src/components/WatchlistTab.tsx`: passes `currencySymbol`.

### Tests
- `backend/tests/test_dividend_forecast_attribution.py`: payout → timing; 6 new cases; the `H()` helper takes `level`.
- `backend/tests/test_dividend_forecast.py`: 7 `payout_level` cases. File is LF in the working tree; it already was.
- `backend/tests/test_dividend_forecast_snapshot.py`:
  - lines compare `level_eur`;
  - non-payer has no level; payer level 4;
  - `_line(..., level=)`;
  - three-days breakdown with raises/timing;
  - backfill second run re-replays;
  - new `test_fill_levels_fills_only_what_is_missing` and `test_the_backfill_gives_recorded_lines_the_level_they_lack`.
- `backend/tests/test_api_smoke.py`: breakdown keys, line keys, watchlist NVDA fixture and base-cap assertion; imports `WatchlistItem`.
- `backend/tests/test_fundamentals_market_cap.py`: uses `market_cap_to_base(db)`; quoted-currency and warm-throttle tests.
- `backend/tests/test_market_cap_family.py` (new).
- `frontend/src/lib/dividendForecastTrend.test.ts`: raises in the header, cuts, labels.
- `frontend/src/components/DividendForecastTrend.test.tsx`: "+€7.00 from raises", timing sentence, resize grip case.
- `frontend/src/lib/useResizableHeight.test.ts` (new): clamp, stored height, drag (stored once at end), keyboard, storage that throws.
- `frontend/src/lib/utils.test.ts`: `marketCapParts`.
- `frontend/src/components/FundamentalsTab.test.tsx`: base figure "€26.0B", fallback "900.0B" + "GBp".
- `frontend/src/components/ui/tableFamily.test.tsx`, `frontend/src/components/ratingSortFamily.test.tsx`: fixtures gain `market_cap_base: null`; `watchlistColumns({ formatCurrency, currencySymbol: '€' })`.

### Docs
- `docs/dividends.md`: one-line-per-security bullet, split formula, five parts, raises/timing bullet, backfill-levels bullet, surface bullet.
- `docs/frontend.md`: resizable chart bullet.
- `docs/pricing-and-fx.md`: market caps paragraph.
- `docs/troubleshooting.md`: `market_cap_base` rough edge extended.

### Local-only (gitignored, not committed)
- `backend/.env`: placeholders, `SCHEDULER_ENABLED=false`, `API_ADMIN_TOKEN=local-dev-admin-token`.
- `backend/portfolio.db`: written by the test suite.
- `frontend/node_modules`: real `npm ci`, not a junction.
- `tests/__pycache__`.

## User Feedback & Preferences (REQUIRED — never omit)

- "make sure to also include the raise from divident raises, currently it only includes the share from buys" (with a screenshot of the Next-12-months header).
- "We also have a cloud-session running on another device, that is working on the UI, so make sure to do as less changes on the UI as possible, only functional ones." **Keep component diffs minimal.**
- "also make the chart bigger when opening it, since the growth is barely visible, maybe make it dynamics (I can drag it to make it bigger/smaller)".
- Logos: "we should put the logos (google, amazon, Vanguard into the singular holdings). It should be saved in a seperate place in the DB and cleanly displayed into the Icon. Make sure it is only the logo, since G, instead of Google has enough space etc. => should look clean."
- "also update the fundamentaly metrics, not just the watchlist, since currently it is maintained manually. Make sure that the calls do not fun into an api-call spamming behaviousr."
- "also the marketcap of them is not accurate, we should normalize it to the currency we have chosen (CHF, EUR, USD), we already have all the conversion rates anyway (i think from frankfurter api), we might need ot add chinese yen, japanese jey, south korean currency and some more to be save."
- Answers to the plan questions:
  - logo source → **logo.dev API (Recommended)**;
  - fundamentals → **Daily Tue–Sat at 05:00** (I had recommended keeping weekly; the owner chose daily).
- "commit the changes and worktree to github, i will pick it up from another cloud sessoin". Then: "before you commit, write me a /handoff-code of all the changes done on this worktree."
- Standing conventions (CLAUDE.md, owner 2026-10-14): the UI carries figures, not explanations (hover texts a few words at most); never write account figures into committed docs (public repo).
- Owner's workflow (memory `vps-web-console`): runs VPS commands in **Hostinger's browser web console** (already root), not ssh/scp. Give short paste-ready blocks prefixed with `cd /root/IBKR_investment_tracker`, no heredocs, short output.

## Where We're Going

**Order: §4, then §3, then the bookkeeping, then verification, then merge.** One commit per section, each bundling backend, frontend and docs. Never push to `main` within ~10 minutes of a Berlin sync slot: 00/04/08/11/13/15/18/20/22, plus **05 once §4 lands**.

### Step 1 — §4 Fundamentals: daily Tue–Sat 05:00 Berlin, held securities only (owner's choice)

Existing guards that stay: 1–3 s + 2–4 s sleeps per security, abandon on a 429 via `is_rate_limit`, stale-only selection, ratings skipped after a fundamentals rate limit.

1. `backend/app/services/scheduler_service.py` ~line 249–261:
   - `FUNDAMENTALS_REFRESH_HOUR = 5` (today it is `= WATCHLIST_REFRESH_HOUR`, i.e. 4);
   - `FUNDAMENTALS_REFRESH_DAYS = 'tue-sat'` (today `'sun'`);
   - rewrite the rationale comment: once after each US close, an hour after the watchlist and never in the same pass, four quiet hours before the 08:00 repricing;
   - check the job registration comment at ~1912 ("weekly … never its day") and the job docstrings at ~1506 ("weekly").
   `ALL_SYNC_HOURS` derives from the constant automatically.
2. **The deploy guard learns hour 5 in all three copies:**
   - `ops/auto-deploy.sh:76` `SYNC_HOURS="0 4 8 11 13 15 18 20 22"` → add 5;
   - `ops/finish-deploy.sh` (its copy near `slot_guard`, ~line 73–87);
   - `deploy.sh`.
   - Pinned by `tests/test_deploy_guard_hours.py`, which is marked `deploy_scripts`, so run it **without** `-m "not deploy_scripts"`.
   - Update `ops/backup-db.sh:13`'s comment "03:17 UTC = 05:17 Berlin, which no slot uses". The backup now runs ~17 min after the 05:00 pass starts; it is an online `.backup`, so it is safe.
   - **Owner must refresh `/root/auto-deploy.sh`** (a copy) by atomic rename after merge. Add that to STATUS *Needs a human*.
3. **Staleness must fit a daily slot.**
   - `backend/app/repositories/fundamentals_repository.py:22` `STALE_AFTER_DAYS = 1` → one constant `STALE_AFTER = timedelta(hours=20)`;
   - `_stale_cutoff` and `get_stale_metrics` / `count_stale_metrics` use it, and so does `/api/fundamentals/status`;
   - rewrite the outdated comment ("fundamentals have no scheduled job: every pass is user-triggered");
   - check callers passing `days_old=` (grep `days_old`). Ratings keep their own 3-day rule (`analyst_rating_service.get_stale_ratings(days_old=3)`).
4. **Held securities only** (today it walks every `yahoo_eligible()` security, sold ones included, at about 5–6 Yahoo requests each):
   - new `SecurityRepository.held_ids()` from open tax lots (look at how `shares_at` / open lots are defined elsewhere; one definition);
   - use it in `FundamentalsService.sync_fundamentals_data` (`backend/app/services/fundamentals_service.py:186`; selects `select(Security).where(yahoo_eligible())`) and in `AnalystRatingService.sync_stale_ratings` (`analyst_rating_service.py:335`);
   - the manual Refresh (`POST /api/fundamentals/sync`) gets the same scope;
   - `get_fundamentals_for_portfolio` (`fundamentals_service.py:359`) lists **held securities only**, otherwise sold rows age and turn the tab's as-of line amber for nothing. Stored data stays.
   - Check that pillar 3a / `price_source` securities are still excluded by `yahoo_eligible()`, and decide whether 3a funds should be listed. The tab currently lists every `securities` row.
5. Frontend: `frontend/src/components/FundamentalsTab.tsx:26` `FUNDAMENTALS_STALE_AFTER_DAYS = 8` (comment cites weekly) → `4` (covers Sat→Tue). It is a constant; no markup change.
6. Tests:
   - `tests/test_scheduler_jobs.py`: the new slot, and `test_the_registered_slots_are_exactly_the_declared_ones`;
   - `tests/test_deploy_guard_hours.py`;
   - a sold position is neither refetched nor listed;
   - 20 h staleness;
   - update STATUS's existing watch item "The first `fundamentals_refresh`, Sunday 2026-10-18 04:00 Berlin". Note: that run **will** happen on production (current main) before this branch deploys.
7. Docs: `docs/flex-and-sync.md` *Sync schedule* (slot, scope, request estimate ≈ 6 × held companies + 2 × held funds, five days a week); `docs/local-development.md:136-137` lists the slot hours ("nine of them… 04:00 is the watchlist's"), so update it.

### Step 2 — §3 Holding logos via logo.dev API (owner's choice)

1. **Storage.**
   - New model `backend/app/models/security_logo.py`, table `security_logos`, keyed by **ISIN** so two listings share one logo.
   - Columns: `isin` String(12) PK, `image` LargeBinary NULL, `content_type`, `sha256`, `source` ('logo.dev'), `fetched_at`, `checked_at`. Naive UTC via `app.clock.utcnow()`; `tests/test_clock_convention.py` walks the tree.
   - A NULL image plus `checked_at` means "asked, nothing there", the `IsinIdentity` pattern (`app/models/isin_identity.py`).
   - Export it in `backend/app/models/__init__.py` (import and `__all__`, or Alembic won't see it).
   - Migration chained from **`e5b1d7f3a9c2`**. `tests/test_migrations.py` checks one head, schema = models and downgrade; `tests/test_model_metadata.py` checks for duplicate index names.
   - `backend/app/repositories/security_logo_repository.py`.
   - No `LargeBinary` exists anywhere in the codebase yet; this is the first.
2. **Fetch.** `backend/app/services/logo_service.py` plus a small client.
   - `GET https://api.logo.dev/v2/brands/logo?isin=…` with `Authorization: Bearer <LOGO_DEV_SECRET_KEY>`, then download the signed `url` with `?size=128&format=png`.
   - Fallback lookup by the security's Yahoo ticker from `ticker_mapping` when the ISIN is `not_found`.
   - **Refuse whole:** only `image/png|jpeg|webp`, **never SVG** (same-origin XSS); ≤ 100 KB; magic bytes must match.
   - **Bounded:** only ISINs with no row, or whose `checked_at` is older than 90 days; at most 30 lookups per pass, paced ~1 s; stop on 429 or 402; on `202 not_indexed` leave the row for the next pass, never sleep-loop.
   - Key `LOGO_DEV_SECRET_KEY` in `backend/app/config.py` and `backend/.env.example`, and add it to `app/redact.py`'s secrets (the signed URL may carry a signature too).
   - **No key ⇒ step reports `skipped: not configured`, no warning** (a warning that is always present teaches skipping).
   - **Not** gated by `price_source` (that is the Yahoo opt-out). Never import crypto modules (`tests/test_crypto_isolation.py`). It never touches Yahoo.
3. **Wiring.**
   - CLI `backend/app/cli/fetch_logos.py [--dry-run] [--force] [--isin X]` recording a `sync_runs` row, like the other CLIs. It does the first fill.
   - A third step in the `fundamentals_refresh` job (Tue–Sat 05:00 after §4). It runs even after a Yahoo rate limit (different upstream) and costs zero requests when nothing is missing.
4. **Serve.** New router `backend/app/routers/logos.py` registered in `app/main.py` under `/api/logos`.
   - `GET /api/logos` → `{"logos": {"<security_id>": "<sha256[:12]>"}}`, the manifest of securities whose ISIN has an image.
   - `GET /api/logos/{security_id}?v=…` → bytes with `Cache-Control: public, max-age=31536000, immutable` and `X-Content-Type-Options: nosniff`; 404 otherwise.
   - GETs are open (`app/auth.py` is method-keyed) but rate-limited at 120/min per IP (`app/rate_limit.py`). The manifest plus immutable versioned URLs mean only the first visit spends requests.
   - CSP `img-src 'self' data:` (`backend/frontend-nginx.conf:23,34,49`) already allows same-origin `/api/...` images.
5. **Show.**
   - `frontend/src/components/ui/TickerAvatar.tsx` (props `{symbol, className}`, h-9 w-9 `rounded-full`, hashed hue) takes an optional `securityId`.
   - It reads the manifest via a new `frontend/src/lib/useLogos.ts` (TanStack Query, `staleTime` ~1 h) and renders `<img src=… alt="" className="h-full w-full object-contain">` inside the same circle on a white fill. Monogram fallback, `onError` included. Same size.
   - Pass `securityId` at `PositionsList.tsx:158` and `DividendsTab.tsx:69` (one prop each). `ActivityTab` rows have no `security_id`, so they keep the monogram.
   - **Caveat:** `DividendsTab.test.tsx:494` reads the first cell's `.text-xs` element to find the ticker line, so the avatar must not use `text-xs`.
   - Rewrite the comment at `TickerAvatar.tsx:3-8` and `docs/frontend.md:477`: logos are the owner's 2026-10-17 call; privacy trade-off accepted (logo.dev sees the ISINs).
   - **"Clean" needs a visual check**: is a logo.dev square mark on white, `object-contain`, inside the circle tidy for e.g. GOOGL, VT, ASML, 2330? Adjust padding (`p-[2px]`) or `object-cover` after seeing real images.
6. **Tests.**
   - Repository and service on a fake client, offline: SVG/oversize/wrong-magic refusals, the 30-lookup budget, stop on 429, `checked_at` blocks a re-ask, no key means skipped.
   - Router: manifest, image, cache headers, 404.
   - `tests/test_api_smoke.py`: both reads.
   - Frontend: `TickerAvatar` with and without a logo, and the `onError` fallback.
7. **Docs.** New `docs/logos.md` (source, terms quotes, storage, budget, privacy decision) and a row in CLAUDE.md's *Where things are* table.

### Step 3 — Bookkeeping (CLAUDE.md requires STATUS.md accurate before stopping)

- `STATUS.md`:
  - bump *Last updated*;
  - *Needs a human*: set `LOGO_DEV_SECRET_KEY` (then `docker compose up -d` with `GIT_COMMIT` exported, never `restart`), run `fetch_logos`, run the forecast backfill, refresh `/root/auto-deploy.sh`;
  - *Watch after the next deploy*: raises on the card after the backfill, first logo pass, first Tue 05:00 fundamentals run (success, no rate-limit warning, sold rows not fetched), market caps in CHF on both tabs, `sync_exchange_rates` result shows `quoted_currencies` only every ~3 days;
  - *Recent sessions*: add one line and drop the oldest (cap 5).
- `docs/shipped-log.md`: newest-first entry for the batch.

### Step 4 — Verification

- Backend full suite **without** `-m` filter (ops/ and deploy.sh change in §4). Compare with the baseline 2008 passed / 9 skipped plus the new tests.
- Frontend: `npx tsc -b && npm run test && npm run build`.
- Browser check: forecast card ("from raises", drag and keyboard resize, persisted height), Fundamentals/Watchlist caps, logos.
  - **Do not proxy the local frontend to the live API casually:** `GET /api/dividends/summary` can enqueue a sync and `GET /api/portfolio/benchmark` can lazy-fetch Yahoo (rule 1).
  - Without a production snapshot, verify after deploy.

### Step 5 — After merge and deploy (owner, Hostinger web console)

```
cd /root/IBKR_investment_tracker
/root/backup-db.sh
docker exec backend-portfolio-backend-1 python -m app.cli.backfill_dividend_forecast --from 2026-08-28 --dry-run | tail -20
docker exec backend-portfolio-backend-1 python -m app.cli.backfill_dividend_forecast --from 2026-08-28 | tail -20
```

The backfill is offline (no Yahoo, no Flex). It restates replayed days 2026-08-28..10-06 with levels, re-replays the lines of 10-07..12, and fills levels on recorded lines from 10-13 on. Until it runs, raises before the deploy day stay in `timing`. `2026-08-28` is the owner's `growth_from`. `/root/backup-db.sh` is the host copy of `ops/backup-db.sh` (docs/deployment.md: it ends its stdout with the snapshot path); any hand-run write to production goes after a fresh backup.

## Risks & Blockers

- **Full suites not run** after the last commits; only targeted subsets passed (numbers above). Run both before merging.
- **The API contract changed** (`payout` → `raises`/`timing`). Anything outside this repo reading `/api/dividends/forecast-history` breaks. The other cloud UI session must pick up the new `DividendForecastChange` shape if it touches the forecast card. **Merge conflicts are likely** in `DividendForecastTrend.tsx`, `FundamentalsTab.tsx`, `watchlistColumns.tsx` and `api.ts` if that session restyled them.
- **Level semantics nuance:**
  - a varying payer's (fund's) raise counts when the distribution is **paid** (the level reads the last paid cycle); a steady payer's counts when IBKR lists the **accrual**;
  - replayed levels for past days cannot see accruals (`replay` withholds them), so raises on replayed days count on payment.
  - Documented in docs/dividends.md.
- **Environment traps:**
  - the worktree needs `backend/.env` (placeholders) and `npm ci`;
  - the main checkout's venv lacks `pytest-xdist` (`-n auto` fails with "unrecognized arguments"; run serially, ~5 min, or `pip install pytest-xdist==3.8.0`);
  - Node v26.4.0 needs `NODE_OPTIONS=--no-experimental-webstorage` for vitest (docs/local-development.md trap);
  - Windows Python defaults to cp1252, so always pass `encoding='utf-8'`;
  - the worktree-isolation guard refuses complex Bash.
- **logo.dev ETF coverage is untested.** VT/QQQM/SOXQ may not resolve to issuer marks, and Asian ISINs (TW0002330008, KR7005930003) may be `not_found`. The monogram fallback covers it, but check with `--dry-run` first.
- **The 05:00 slot is new to the deploy guard.** Until `/root/auto-deploy.sh` is refreshed on the VPS, a deploy could restart the container mid-pass and lose that day's run (the next day recovers).

## Open Questions

- Should the Fundamentals tab list pillar 3a funds and other non-Yahoo securities at all, once it is held-only? Today it lists every `securities` row.
- What is the exact "clean" logo styling: white circle with `object-contain` and a little padding, or full-bleed `object-cover`? It needs real images to decide.
- Does logo.dev's API resolve ETF ISINs to the issuer mark? Measure on the first `fetch_logos --dry-run`.
- Watchlist logos? Not asked: the owner said "singular holdings", so positions and dividends tables only.

## Quick Start for Next Session

```bash
# Get the branch
git fetch origin && git checkout worktree-moonlit-drifting-matsumoto
git log --oneline origin/main..HEAD        # expect bdb5758, e41f03c, ad7607f (+ the handoff commit)

# Reference docs
CLAUDE.md  STATUS.md  docs/dividends.md  docs/flex-and-sync.md  docs/frontend.md  docs/pricing-and-fx.md

# Key files to read first
backend/app/services/scheduler_service.py   # FUNDAMENTALS_REFRESH_* ~line 249; jobs ~1506; registration ~1912
backend/app/repositories/fundamentals_repository.py   # STALE_AFTER_DAYS
backend/app/services/fundamentals_service.py          # sync_fundamentals_data, get_fundamentals_for_portfolio
frontend/src/components/ui/TickerAvatar.tsx           # where logos will render
ops/auto-deploy.sh ops/finish-deploy.sh deploy.sh     # SYNC_HOURS copies (+5)

# Verify current state (from backend/, with a .env holding placeholders and SCHEDULER_ENABLED=false)
python -m pytest tests/ -q -n auto --dist worksteal          # no -m filter once ops/ changes
cd ../frontend && npm ci && npx tsc -b && npm run test && npm run build

# Next action
Implement §4 (daily Tue–Sat 05:00, held-only, 20 h staleness, hour 5 in the three deploy-guard copies).
```

## Session Closed
**Closed at:** 2026-10-17
**Commit:** the commit that adds this file (head of `worktree-moonlit-drifting-matsumoto` when pushed)
**Session status:** Handed off to next session
