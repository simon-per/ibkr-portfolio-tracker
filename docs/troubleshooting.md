# Troubleshooting — symptom-indexed

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `has no attribute` / `is not a valid` in a sync | IBKR schema drift. `_sanitize_flex_xml` should absorb it — if not, extend it generically (never patch one field) + add a test |
| A sync warns about dropped attributes | Only fields the ingest **reads** warn now, and that means data may really be affected — read the names. The ~27 harmless ones IBKR sends every time are in the run's `details` as `flex_schema_notes`, not the banner |
| Yield on cost looks low after buying more, or after selling and rebuying | Fixed 2026-08-05. It is the *projected* annual rate over cost, so it tracks the shares held now. A rebuy at a higher price genuinely lowers it — more capital for the same income. If it still looks off, `forward_yield_pct ÷ yield_on_cost_pct` must equal market value ÷ cost for that row |
| `Code=1025` | Token lockout, usually self-inflicted. **Wait**, don't retry. The schedule recovers it |
| `Code=1001` | Not ready. Polled while retrieving; **fatal at the request step** — never re-request, a later job handles it. Since 2026-08-08 the common cause of a *run* of these is gone: the guard skips a slot once the ET day's generation is spent, so a `1001` now means a genuine refusal |
| `Code=1004`–`1009`, `1019`, `1021` | The same as `1001` (incomplete; settlement, FIFO or MTM data not ready; heavy load; generation in progress; could not be retrieved): polled while retrieving, **fatal at the request step** since 2026-10-17 (`1019`/`1021` since 2026-10-18) — a later job handles it |
| `SendRequest failed after it may have reached IBKR` | A reset, dropped reply or read timeout after the GET went out, or (`BadResponseError`) a reply that is not a Flex envelope — an empty body or an HTML page — so a generation may be running: not re-requested, a later job handles it. The body is logged as `SendRequest reply not understood`. Only a connect timeout, DNS failure or refused connection is retried |
| I bought something today and it is not in the app | Expected on any day, and not a settlement delay — the rolling window ends at the last completed *trading* day, so today is structurally never in today's statement. A Monday purchase is first reachable Tuesday; a Friday one on Saturday. Nothing to force. Verify with `toDate` in the statement header rather than the generation time |
| The value chart dips hard after a big sale, or Market Value looks far too low | Fixed 2026-08-26 by tracking cash — check `/health`'s commit predates it before looking further. Selling moves value from holdings into a cash balance nothing used to record, so a rotation drew a cliff and the headline card understated the account by the idle balance. The chart now pairs **Total Value** (holdings + cash) with **Money In**, and neither steps on a trade. The risk metrics were always right about this: they net the flow out |
| The cash figure disagrees with IBKR's by a small amount | Expected while `cash_source` is `derived`: it is computed from the trade, deposit and dividend ledgers, so it cannot see broker interest, account fees or FX conversion spread. On this account that ran to about −250 CHF over eight months, 0.36%. Enable the **Cash Report** section (option: Base Currency Summary) or **Equity Summary in Base** in the Flex portal and IBKR's own figure takes over — expect one visible step on the first measured day, which is the accumulated difference becoming visible |
| The benchmark line starts exactly where the portfolio line does, and the gap is smaller than it used to be | Intended since 2026-09-07. The chart asks for `anchor=window`, so the hypothetical is seeded with the portfolio's own Total Value on the first day of the range and the gap is what happened *inside* it — the old starting gap was two years of history the range did not draw. ALL is where the since-inception meaning lives, and there the two anchors agree to 0.001%; `anchor=inception` on the API still serves the old series |
| The chart still says Cost Basis / Market Value | `cash_source` is `unknown`, meaning no trade or cash-flow row exists to derive a balance from — a fresh install, or a Flex query with none of the cash-bearing sections enabled. Deliberately not relabelled: a holdings-only line called "Total Value" over a 0.00 cash figure asserts a completeness nothing established |
| A Cash slice appears in the sector and geography charts | Intended. Cash has no sector and no country, but putting it only in the asset-type chart would give that endpoint two denominators and leave the other two summing to under 100 while every slice says "% of portfolio". Named `Cash` rather than folded into `Unknown`, which means something else there: a holding whose sector nobody has fetched yet |
| A negative cash balance, and a notice above the allocation charts | A margin debit. It cannot be drawn as a slice, so those charts leave it out of both the slice and the denominator rather than renormalising around it, and say so. The value chart still plots it — the cash line simply goes below zero |
| A newly bought **fund** is drawn as a Stock, or shows as a single company in Look-through | It is not declared. `ETF_ALLOCATIONS` / `FUND_SOURCES` are keyed by fund ISIN, and an undeclared one takes the `securities.asset_type` column default of `"Stock"` — so it is bucketed as a company at a plausible weight, `uncovered_fund_eur` stays `0.00`, and coverage still reads high. Nothing reports it, which is why it is here: IQQ sat in that state from 2026-08-21 to 08-24. Add both entries, pin the allocation blocks to any sibling tracking the same index, then fetch and import its basket |
| The Sync button says *Already up to date* | Working as intended, and not an error. IBKR issues about one statement per US-Eastern day; today's has landed, so there is nothing to fetch. The panel says when the next one becomes available. *Sync anyway* is only worth pressing after editing the Flex Query in the portal — the one thing that resets the daily generation — because a forced refusal spends `Code=1025` budget |
| A scheduled run reads `skipped` in the history | Two different causes, told apart by `reason`. `already_generated_today` is the 00:00 Berlin slot correctly doing nothing because 18:00 succeeded — the normal daily state. `pipeline_busy` is a `single_flight` collision that outlasted the scheduled job's 9-minute wait for the gate (a manual sync or a public GET's pass still running); the next slot recovers prices and IBKR, but an 18:00 skip loses that day's 730-day pass and look-through upkeep. `no_new_trading_day` is an IBKR attempt on an ET Sunday or Monday (Berlin Sunday/Monday 18:00, Monday/Tuesday 00:00): those statements reach the same Friday Saturday's did, so nothing is asked; Tuesday's generation brings Monday, and pressing Sync still asks |
| Sync 200 but 0 trades | Flex Query section/period not covering them |
| `dividend_source` stuck on `yfinance_estimate` | For **2026 or later**: no `<CashTransactions>` ingested — check the section + Withholding Tax option. For **2025 or earlier**: correct and permanent, not a gap. This account did not use IBKR before 2026 (everything transferred in-kind from Trading 212 / Scalable Capital / Trade Republic), so no Flex period can produce rows that were never at IBKR. Don't change the query period chasing it |
| Yahoo 404/429 | **Stop.** Wait 30-60 min. Check `yfinance >= 1.1.0` |
| A position is missing from the portfolio | Check `taxlots_skipped` + `warnings[]` on the sync run — usually a currency neither FX provider covers |
| Crypto mode says *locked* | Expected without the admin key: `/api/crypto/*` reads are private ([crypto.md](crypto.md)). Add the key with the lock button. A **403** instead means the server has no `API_ADMIN_TOKEN` at all — the crypto routes fail closed rather than open up |
| Crypto mode says *not configured* | `COIN_STATS_API_KEY` or `COIN_STATS_SHARE_TOKEN` is missing from **`backend/.env`** (the repo-root `.env` is never read). On the VPS, `docker compose up -d` with `GIT_COMMIT`, never `restart` |
| Crypto run `skipped` / `not_synced` | CoinStats answered 409 "Transactions not synced": it is still syncing the portfolio (new connection, or an exchange it re-imports). Not a fault — the next slot asks again. Nothing to force |
| Crypto run `error` / `quota` | HTTP 429, which CoinStats uses for the rate limit **and** for "Insufficient credits". Check the credits line in Crypto mode (`/api/crypto/status`); the free plan resets monthly. Don't press Sync repeatedly — every press is a paid call |
| Crypto run `error` / `too_many_pages` | The wallet lists more holdings than `MAX_COIN_PAGES` pages — nearly always airdropped spam tokens. Hide them in the CoinStats app; the run refuses rather than store a truncated portfolio |
| Crypto total is lower than the CoinStats app shows | Check the *plus … in DeFi positions* line beside the total: CoinStats reports DeFi separately from `totalValue`, so it is in no total, weight or chart line here |
| Crypto sync `error` / `quota` right after another call | CoinStats' real rate limit is tighter than its documented 2 requests/second; the client waits 2 s after each response. A 429 is still possible if something else uses the same key at the same moment — the next slot recovers |
| Crypto P&L differs from the CoinStats app | Expected: since 2026-10-03 the book computes its own P&L — yesterday's coins × the day's CoinGecko price move, summed from 1 Jan 2026 and converted at each day's rate — so a transfer or a buy never moves it. CoinStats' own cost basis and P&L are not used ([crypto.md](crypto.md), *Reading it back*) |
| Crypto runs are missing from `/api/scheduler/history` | Intended — the public scheduler endpoints show the stock pipeline only. Crypto runs are in the admin-gated `/api/crypto/status` |
| The value chart declines toward zero over recent days | The chart says so itself now — a yellow notice above it names the day and holding counts. It means `unpriced_holdings > 0`: holdings whose price fell outside the 14-day lookback are counted at cost but not at value, and past 15 days every one drops out and it reads −100%. The cause is a stalled market-data sync, not a loss. The risk metrics already exclude those days |
| A Monthly Returns cell reads `–` | One holding could not be valued on those days, so the whole day is dropped rather than counted at a wrong value. Hover the cell: the tooltip names the days the figure *does* cover, which can be far shorter than its column. A spun-off line whose lots predate its listing is handled (see *A spun-off line is not held…*); anything else is a real gap — find the security in the market-data sync's `warnings[]` |
| A position shows `—` for Value, Gain and Weight, with a yellow notice above the table | `isUnpriced`: the backend could not value it, from a missing price **or** a missing FX rate. Deliberately a dash rather than a figure — an unvaluable holding is valued at 0.00, so its gain is `−cost` and it used to render as a real **−100%** in red at `0.00%` weight. The notice names the symbols; fix that security's `ticker_mappings` row, or fill prices with `app/cli/import_prices.py`. Totals on the page exclude it too, so Market Value carries its own notice |
| Market Value looks low and a yellow notice sits above the cards | `summary.unpriced_holdings > 0`: a holding could not be valued, so the total omits it while Cost Basis keeps its cost. Not a loss — find the security via the market-data sync's `warnings[]` and check its `ticker_mappings` row |
| A position shows 0.00 / `market_price: null` | No cached price. The market-data sync's `warnings[]` now names it. Fix the `ticker_mappings` row, or fill it with `app/cli/import_prices.py` from IBKR bars |
| A price looks like an intraday value, not a close | Expected inside the session, and it self-corrects: a weekday within `PROVISIONAL_PRICE_DAYS` (3) is re-fetched at every slot, so the settled close lands after the market shuts. Still wrong **after** 3 days is a real freeze. **Don't diagnose it from `created_at`** — see below |
| A market-data run reports `rate_limited: true` | Yahoo returned 429 and the pass stopped deliberately, leaving the later securities on their previous prices. **Do not trigger a manual sync** — wait; the next slot resumes and re-fetches only what is missing |
| A benchmark's last point looks stale while the portfolio moves | The scheduled warm-up stops at the last finished session, so today's benchmark bar comes only from a chart load, which refreshes the one selected |
| A `market_data_only` run reports `benchmarks_fetched: 0` | Expected at every slot but the first after a session: each benchmark is asked once per finished session, so Sunday and Monday report 0 all day. Zero at 08:00 Tuesday to Saturday as well is the bug fixed 2026-10-17 (issue #4) — check the log for "pipeline busy" |
| The chart steps at a split date | Cached pre-split closes. A *new* split purges them automatically; for an older one delete that security's `market_prices` and let the 18:00 `full_sync` (the 730-day pass) refill them |
| A new currency appears | Nothing to do if it's in `WARM_CURRENCIES` or the ECB set. Otherwise add it there — one edit, no extra request |
| "Money added" spikes in one month | A transfer booked as a deposit. `manage_cash_flows list`, then `reclassify <ib_key> --as TRANSFER_IN`. **Never** trust an Added figure without eyeballing that list first |
| A month I know I invested in reads low on the Money In per Month chart | Working as intended: the bar is money *in*, so capital rotated between holdings is not in it. Selling one holding to buy another puts the same money to work twice, and only the second trip looks like activity — August 2026 put 30,617 CHF into new positions against 7,211 actually paid in, and September 3,639 against **zero**. Hover the bar for the deployed, released and net figures. Do not net the gap away and do not promote deployed back to a bar |
| "Money added" is blank or `—` | Expected before `deposits_from`: no IBKR deposit ledger exists for the pre-transfer years. Not a bug — Deployed covers that era |
| Realized gains look low + a `warnings[]` entry names a currency | No FX rate for that trade date, so the sale was **omitted** rather than mis-scaled. Check `WARM_CURRENCIES` covers it |
| Steuerwert reads `—` instead of a number | `holdings_snapshot_error`: the snapshot raised. Check the logs — this is deliberately *not* 0.00 |
| A dividend forecast looks invented | Check `forecast_samples` on the breakdown row — n≤2 is a guess. Then `manage_mappings list` for `DIVIDENDS PREDATE MAPPING`, and purge with `purge_dividend_estimates` |
| A trailing yield looks far too low | `trailing_yield_partial`: the position wasn't held a full year, so partial income is over a full position value. Not a bug, and deliberately not annualized |
| Dividend Yield reads a fraction of a percent | Expected on this book: most of it is growth equity plus seven accumulating ETFs, which correctly contribute nothing. Read the card's *N of M pay* sub-line before suspecting the figure |
| Dividend Yield reads `—` / *No projected dividends* | Nothing projects among the **priced** holdings. Check the market-data sync's `warnings[]` for an unpriced payer — deliberately not 0.00%, which would claim the portfolio pays nothing |
| Dividend Yield says *couldn't load* | The `/api/dividends/breakdown` query failed. The other four cards on that row still render, by design — it is not gated on this one |
| Yield on Cost ≠ Dividend Yield × some factor you expect | The factor is exactly market value ÷ cost basis, nothing else: same numerator, same securities. If it isn't, one denominator has picked up a different holding set |
| A position's value is far off IBKR's | Suspect the `ticker_mappings` row before the price feed: run `manage_mappings list` and look for a currency disagreement, then compare `market_prices.close_price` against IBKR's `market_price` in the *same* currency |
| A UK/Johannesburg/Tel Aviv position is ~100× out | A minor-unit quote (`GBp`/`ZAc`/`ILA`) that wasn't scaled. `MINOR_UNIT_CURRENCIES` handles it; note the currency guard **cannot** catch this one, because the normalised label matches the security. Check the sync log for "quoted in GBp (minor units)" — its absence on a London equity is the tell |
| App total ≠ IBKR total | Compare against `gross_position_value`, **not** net liquidation (which adds cash); and intraday the app holds the last *close* while IBKR quotes live |
| Site "down" in the browser | Often TIM home DNS, not the server — verify with `Test-NetConnection`, not `nslookup` |
| Deploy says health FAILED | Usually the premature check; re-curl `/health` after ~15s |
| The backend crash-loops and its log says `REFUSING TO START` | `app/db_guard.py`: the database file `DATABASE_URL` names (`/app/data/portfolio.db` in the container) is missing or empty, and starting would create an empty one. **Do not set `ALLOW_NEW_DATABASE`** unless this is a genuine fresh install. Usually an older `deploy.sh` brought the new compose file up without moving the file; run `bash ops/db-layout.sh preflight` on the VPS, then `./deploy.sh` — [deployment.md](deployment.md), *The database directory* |
| A deploy stops with `db-layout: REFUSE` | Nothing was moved; the message names the layout it did not recognise (two databases, a dangling link, a directory where the file belongs, a stray `-wal` in `data/`). Before `down` the old containers keep serving; after it, auto-deploy rolls back. Resolve by hand — [deployment.md](deployment.md), *The database directory* |
| `backend/portfolio.db` is a symlink | Intended since 2026-10-04: the database is `backend/data/portfolio.db` and the link keeps older scripts and rollbacks on the real file. Do not replace it with a file |
| A write returns 401 | `API_ADMIN_TOKEN` is set and the browser has no key. Lock button in the header; the same value goes in `backend/.env` |
| A request returns 429 with `Retry-After` | Either the sync cooldown (`single_flight`) or the per-IP limit (`RATE_LIMIT_PER_MINUTE`). The response body says which |
| "Which build is live?" | `curl /health` — it reports `version`, `commit`, `scheduler_enabled`, `write_auth_enabled` and `scheduler_jobstore_persistent`. Same line in the app footer |
| `/health` reports `commit: "unknown"` on a running site | Somebody ran `docker compose up -d` by hand without exporting `GIT_COMMIT` — only `deploy.sh` sets it, and it is not baked into the image. The code is whatever the last deploy built, so **`git -C /root/IBKR_investment_tracker rev-parse HEAD` is the real answer** meanwhile. It never self-heals on its own; re-run `up -d` with `GIT_COMMIT=<sha>` (no rebuild needed) or wait for the next push |
| A missed sync was **not** recovered after a restart | `curl /health` for `scheduler_jobstore_persistent`. `false` means the store fell back to memory, so misfire recovery is off — `/api/scheduler/status` still lists every job and cannot tell you this. Check the compose mount is the `scheduler-data` **directory** |
| A 500 with no detail | By design. Grep the container log for its `request_id`, which is in the body and the `X-Request-ID` header |
| The container log has no `app.*` lines | Fixed 2026-08-04. `settings.log_level` configured nothing, so Python's last-resort handler emitted WARNING+ only and every `logger.info` was discarded. If it recurs, check `_configure_logging()` still runs and `app/logging_setup.configure_logging` still passes `force=True` — uvicorn installs handlers first, and `basicConfig` is a silent no-op when one exists |
| A log timestamp disagrees with a Berlin slot by 2h | Expected: `%(asctime)s` is the container's local time and the image sets no `TZ`, so logs are UTC while the schedule is Europe/Berlin |
| A missed sync ran late after a restart | Expected: the persistent job store honours a misfire for 30 min. Older than that is dropped, and the next slot recovers |
| A drift row reads `—` instead of advice | No target (unmanaged — blank is not 0%), or the position has no cached price so it has no weight to compare. Both deliberate; see *Client-side analytics* |
| The Forecast tab's grey **Money In** band starts well below Portfolio Value at year 0 | Intended since 2026-09-08. It starts at what has been paid in — the all-time money in from `/api/portfolio/contributions` — and the gap is the gain already made. It used to start at the market value, which claimed every gain was contributed. `—` in the table's Money In and Gains columns means the contributions endpoint failed; Portfolio Value is unaffected |
| Drift says "no target could be compared" | Nothing had both a target and a weight. **Not** an all-clear — that wording exists because "0 outside the band" was one |
| A drift or currency panel says it couldn't load positions | The positions query failed. The panel refuses to build a plan from absent data rather than reporting a portfolio of unheld rows |
| Currency exposure looks wrong for an ETF | It is quote currency, not economic exposure, and deliberately not re-attributed — a EUR-listed S&P tracker is EUR-quoted with USD risk. The fund share is named on screen |
| A recently bought holding sits in an *Unknown* sector or region | Expected, and correct rather than missing. `sync_helper` never writes `sector`/`country`, so an IBKR-ingested security has both NULL while `asset_type` has a `"Stock"` column default. The daily 05:00 fundamentals pass fills them for held securities from the `.info` it already fetches (GitHub issue #11; Tuesday to Saturday), so a new holding is *Unknown* until the next such morning; `POST /api/allocation/sync` still fills them on demand. Before 2026-08-05 the holding was silently dropped from those two charts instead, which made them sum to under 100% under a "% of portfolio" label. A **mapped ETF** is the exception and needs no sync at all — `app/etf_mappings.py` supplies its sector, region *and* asset type live at read time |
| A fund's Equity weight reads ~92% and its Value looks fully invested | Rounding, not cash, and the cell says which: the muted `N rows at 0%` beneath it. Vanguard publishes weights to 2dp and VT has 10,032 holdings, so its 8,007 smallest are printed at 0.00% and its file sums to 91.86%. Nothing is missing and nothing is misparsed — check `stored_rows == source_rows` on the sync run if you want to confirm. Deliberately not renormalised |
| A fund's status badge reads *Via VT* rather than *Decomposed* | It publishes no basket of its own and is decomposed using another fund's — see *A borrowed basket*. Its companies are approximate and err **low**; the fund's `reason` in `/api/portfolio/lookthrough` says which basket it borrows and why. The Coverage card stays amber while any fund is in this state, and clears the moment a real basket is imported for it |
| Look-through coverage is below 100% | Expected, and it cannot reach 100%: no basket attributes 100% of its own fund, so each decomposed fund's residual is a permanent floor under the gap; the rest is each decomposed fund's own residual. Read the `funds` table: every fund is named with the reason its constituents are unknown. **Every company row is an understatement by whatever those funds hold**, and nothing is rescaled to hide it — not a bug |
| The Coverage card is amber at a high percentage | It tones on whether any fund is *unresolved*, not on a threshold — green means every held fund is either decomposed or deliberately excluded. That is deliberate: a percentage threshold could not be green even with every obtainable basket loaded, so it would have been a warning that never clears. Amber names the count of funds still missing a basket |
| A basket's as-of date reads stale (amber) but its issuer publishes slowly | `ADAPTER_STALE_DAYS` is per-source, so stale means *the issuer has newer holdings we failed to fetch* rather than *this feed is slow*: 7 days for the six that republish daily (Xtrackers, iShares, Invesco, First Trust, Defiance, VanEck), 75 for Vanguard US (month-end, ~6-week lag by design), 45 for a hand import. A quarterly source needs its own entry — do not raise the global default to silence it |
| A company appears twice in the look-through table | The two rows share no identifier, and there are three causes in order of likelihood. (1) `key_type: ISIN` — no LEI and no shareClassFIGI on record, so run `python -m app.cli.resolve_identities`. (2) The row came out of **GRID or QTUM**, whose issuers publish a CINS or a SEDOL and no ISIN, so it has no identity at all until `resolve_identities --constituents` runs — and note a **basket re-import deliberately clears that resolution**, so it is the second half of every import for those two funds. Set `OPENFIGI_API_KEY` first. (3) Both rows are already resolved, which is a genuine gap no identifier closes — an ADR against its ordinary, or a dual-listed company with two legitimate LEIs — and needs an `ISSUER_OVERRIDES` entry with its evidence |
| A look-through row is named `台灣積體電路製造…` or similar | GLEIF's legal name is the company's real one in its own script, and it is preferred only when Latin. That row has no OpenFIGI name cached — re-run `resolve_identities`, which fills `figi_name` |
| Look-through total ≠ the Market Value card | It cannot be: both come from `get_positions_breakdown`, and `test_api_smoke.py` pins them equal. If they differ, one of them is not the deployed build — check `/health`'s commit |
| A look-through fund reads `Basket rejected` | Its stored basket accounts for less than `MIN_BASKET_COVERAGE_PCT` (80) of the fund, which is a half-parsed file rather than a fund holding mostly cash. Re-import it; the previous basket is kept on a refused import |
| A VT import refuses with *the pages disagree about how many holdings* | A torn read — Vanguard answered a multi-page walk (VT's was 21 requests before the 2026-10 route move; one now) from cluster nodes holding different snapshots. **Not** a missing page, and not something to point `--force` at: `--force` overrides the row-collapse and backwards-as-of refusals in `replace_basket`, not a parse error. Re-fetch and re-import. The stored basket is untouched meanwhile and VT publishes month-end, so there are weeks of slack before it matters |
| Beta is blank with a benchmark selected | Fewer than the 20 flow-free days a regression needs; the card's sub-line gives the count so far. Flow days are excluded by design |
| Sharpe, Volatility and Sortino all read `—` on a short range | Expected, and now consistent: all three need 5 daily returns. MTD in the first days of a month gives 2–3. Sharpe used to show `0.00` here instead, which looked like a measurement |
| Top 5 Weight reads `—` / *No priced positions* | Nothing held resolved a price, so there is no weight to concentrate. Deliberately not `0.0%`, which the tone ladder would have drawn **green** — a reassuring all-clear from no data |
| Current Drawdown and Max Drawdown both read `—` | No daily return in the selected range was measurable, so there is no decline to measure. It is **not** "never fell": that sentence needs a *measured* zero, and printing it over a stalled price feed was the worst instance of the zero-for-unknown family this app has had. Find the security in the market-data sync's `warnings[]` |
| Win Rate says *N unpriced, not judged*, or reads `—` | Holdings the backend could not value are excluded from both sides rather than counted as losses — an unpriced position is valued at 0.00, so its gain is `−cost` and it used to read as an ordinary loser. `—` means nothing at all could be valued |
| A yellow notice sits above the Performance KPI row | `annualized-return`'s `unpriced_holdings > 0`: a holding had no usable price at one end of the selected period, so the return is measured against a partial valuation while the purchases still count as money in. Annual Return and Calmar both understate. Same cause and same fix as the notice above the summary cards |
| A yellow notice sits above the Allocation charts | `unpriced_holdings > 0` on `/api/allocation/portfolio`: a holding could not be valued, so all three breakdowns exclude it and every percentage is a share of what *could* be valued. The charts still sum to 100, which is why this needs saying — the holding is absent, not visibly zero. The notice names the symbols; fix the `ticker_mappings` row |
| A holding is missing from the Allocation charts but shows in Positions | Same cause as the notice above, and the notice should be there. If it is not, `/health`'s commit predates 2026-08-17 |
| A Swisscanto / pillar 3a holding shows a `3a` badge in Positions | Intended. It marks the account, and only 3a rows carry it — badging every brokerage row of a single-account book is noise that teaches you to skip the badge. Its assets are excluded from the tax report's wealth and income sections and its contributions reported separately; see *A second account* |
| The finpension import refuses the whole file | Two guards, and the message says which. An unrecognised `Category` needs a booking rule in `CATEGORY_KINDS`. A **balance disagreement** means either the export is a partial range (the replay starts from zero, because a 3a account does) or a row is not being booked for its full `Cash Flow` — that check is what makes a silently dropped category impossible. Neither is waved through: a partial import is indistinguishable afterwards from a complete one |
| The finpension import refuses as a truncation | The file holds fewer rows, or ends earlier, than the stored ledger. The export is full history, so a shorter one is a truncated download rather than a correction, and applying it would delete the difference. Re-export. `--force` only for a deliberate rollback |
| `manage_mappings set` refuses a fund ticker over a NAV disagreement | Working as intended, and it is the check that matters most here. A gap of that size is a **different share class of the same fund**, not a pricing difference — the EM fund's NT tranche quotes ~49% above this account's NMT one under a name that matches perfectly. Find the right `0P…` ticker; do not reach for `--skip-nav-check`, which exists for a Yahoo outage on a security with no NAVs on record |
| A pillar 3a fund reads as unpriced | Its NAVs come from your uploads, carried forward for a bounded window. Past that it goes unpriced **loudly**, which is the intended end state rather than valuing it at a months-old NAV for ever. Upload a newer export. The World ex CH tranche should instead be priced from Yahoo — check its `price_source` is `yahoo` and its mapping is pinned |
| Pillar 3a is missing from the Steuerwert | Correct, and the report says so. 3a capital is not taxed as wealth and its income is not taxable — both are taxed on withdrawal at a separate reduced rate. The Tax tab leads with a Pillar 3a card carrying the year's contributions, which *are* deductible from taxable income |
| A 3a fund shows Swiss companies it does not hold | The World ex CH tranche, by owner decision since 2026-10-04: it publishes no basket, so it borrows IWDA's MSCI World (`basket_proxy_isin` in `etf_sources.py`), which puts Nestlé, Roche and Novartis into its share at ~2% and every other company ~2% low. It was `uncovered_fund` until then; if it reads that again, IWDA's stored basket is missing or refused — check the 18:00 run's `lookthrough_result` |
| A market-data sync warns that a fund's basket is stale | `find_stale_etf_baskets`. Since 2026-10-17 the 18:00 full sync asks a daily issuer every evening, long before the warn threshold (fetch-after vs warn-after, docs/lookthrough.md), so a warning means the refresh has been **failing for the whole threshold** — read the 18:00 runs' `lookthrough_result` (`failed` / `refused`) for the error — or the issuer has served nothing newer (`unchanged` every evening), or the fund has no automated route (the message says which). A newly bought fund warns until its first evening. The by-hand route is unchanged: `fetch_etf_baskets`, the import line it prints, then `resolve_identities --constituents` for the new CINS/SEDOL rows the import brought. In the container: `docker exec backend-portfolio-backend-1 python -m app.cli.fetch_etf_baskets --all --out /tmp/baskets` and the import lines in one paste (a deploy recreates the container and empties `/tmp`); VT's line carries a `*.json` glob that `docker exec` does not expand, so wrap it in `sh -c`. `resolve_identities` commits after every answered batch, so a killed run keeps its progress — still do not deploy while it runs, and follow it through its `manual_identity_resolve` row in `sync_runs`, not `pgrep` (not installed in the container). A VT import that refuses on a torn read is not a fault to force past (see the VT row above). The threshold is the issuer's own cadence, so a Vanguard basket is not stale at 30 days and an iShares one is |

## Accepted rough edges — not bugs

Moved from STATUS.md on 2026-10-14. Each is known, measured and accepted, mostly by owner
decision; read the entry before "fixing" one.

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
  scale for them is verified. Those rows show their own figure with the code and sort last, on the
  Fundamentals tab and the Watchlist alike. So does a cap whose currency has no rate cached in the
  last 30 days, e.g. a watchlist name added since the last FX pass.
- **The monthly heatmap's YTD changed method on 2026-10-14**: it is now the months compounded,
  each measured from the previous month-end, so it differs from earlier reads by design.
- **A heatmap month or YTD cut by the selected range is unmarked on the surface** (owner,
  2026-10-14, the figures-not-explanations pass): `monthlyReturns.ts` still computes `partial`
  and the cell's hover names the days measured. The 2026-10-14 browser sweep flagged it without
  that context; do not re-add a marker.

---
