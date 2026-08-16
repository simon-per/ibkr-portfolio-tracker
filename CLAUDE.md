# IBKR Portfolio Analyzer — Project Guide

Full-stack portfolio tracker for an Interactive Brokers account. Tracks securities, tax lots, trades,
corporate actions and dividends; renders cost-basis vs. market-value charts; and produces a Swiss tax
report. All values are stored in EUR and projected into a base currency the user switches at will
(`app_settings.base_currency`, EUR/CHF/USD) — so **read it from `/api/settings`, never from this file**;
it has been both CHF and EUR, and every money figure moves with it.

**Live:** https://portfolio.srv1211053.hstgr.cloud · **Repo is PUBLIC** (never commit account data)

**Read [STATUS.md](STATUS.md) too, and leave it accurate before you stop — see
[Keeping STATUS.md current](#keeping-statusmd-current), which is not optional.** This file is the
durable half — architecture and the invariants that were each a bug first. STATUS.md is the
perishable half: what is in flight, what is known-broken, what needs a human, and the local-dev
traps that keep costing time.

---

## ⚠️ Two rules that must never be broken

### 1. Never call Yahoo Finance without explicit user permission
`yfinance` powers market prices, dividend estimates, fundamentals and benchmarks. Yahoo rate-limits
hard (~500-2,000 requests/hour, ~10-20 in a burst, IP-based). A full market-data sync is **50-150+
requests**. Symptoms of a limit: HTTP 429, HTTP 404 with `Expecting value: line 1 column 1 (char 0)`,
empty JSON, timeouts. Recovery: **stop immediately, wait 30-60 min.**

Protections in `market_data_service.py`: random 1-3s delay per request, 2-4s between securities, Chrome
User-Agent, rate-limit detection that aborts the run, and incremental caching (only missing dates).
`yfinance` must stay **>= 1.1.0**.

**Every service that loops against Yahoo must abandon the pass on a 429, and until
2026-08-05 only `market_data_service` did.** Six services import `yfinance`;
`fundamentals` (~5 endpoints per security, the most expensive), `analyst ratings`,
`watchlist`, `allocation`, `dividends` and the scheduler's benchmark warm-up all caught
the error, logged it, and asked the next one seconds later — the exact shape fixed for
market data, five times over. The predicate now lives once in
`app/services/yahoo_rate_limit.py` and `tests/test_yahoo_rate_limit_family.py` walks the
AST for any module importing `yfinance` without consulting it, so a seventh service is
caught without anyone remembering.

Two details are load-bearing. **The marker list stays narrow** (`429`, `too many
requests`, `rate limit`, `blocked`) and deliberately excludes the `Expecting value: line
1 column 1` and 404 symptoms named above: `_try_fetch_yahoo` uses the same verdict to
decide whether to keep trying ticker *variations*, so matching those would abort
auto-discovery for every ticker Yahoo simply does not know. And **allocation checks
before it stamps** — its failure path writes `allocation_last_updated` to bound retries
against securities with no `.info`, which is right for a real "no data" answer and badly
wrong for a rate limit, where it would mark every remaining security attempted and
suppress its sector and country for the whole staleness window.


The IBKR Flex sync (`POST /api/sync/ibkr`) is Flex-only and touches **no** Yahoo — it's always safe.

**Yahoo is not the only price provider, which this rule used to imply.** `market_data_service.py`
falls back to **Alpha Vantage** when Yahoo returns nothing for a security on NASDAQ/NYSE/ARCA/AMEX,
and `ALPHA_VANTAGE_API_KEY` is set in `backend/.env`, so the path is live rather than dormant. It
does not widen the rule — it only ever runs *inside* a market-data sync that already needs
permission, and never on the Flex path — but "no Yahoo" is not the same as "no network", and a
reader checking only for `yfinance` will miss it. Two things about it are load-bearing:

- **Its rows are tagged `source='alpha_vantage'`.** The caller hardcoded `'yahoo_finance'` for every
  row until 2026-08-01, so fallback prices claimed a provenance they did not have — in the column
  every pricing diagnosis reads first.
- **It refuses a non-USD security.** The endpoint quotes US listings in USD and its response carries
  no currency to read back, so unlike Yahoo there is nothing to verify a label against. Stamping the
  security's own currency onto a USD quote is exactly how SBI was carried 61% high; that path was
  fixed for Yahoo in July and not here. Don't "restore" the fallback for non-USD listings.

### 2. Never loop the IBKR Flex sync
IBKR allows **1 request/second and 10 requests/minute per token**, and one `ibflex.client.download()`
is *several* HTTP requests (request statement, then poll until ready, each with 3 internal tries). An
eager retry loop blows the cap and triggers **`Code=1025: Too many failed attempts`** — an undocumented
token lockout lasting **hours** (observed ~14h) that blocks all syncing. This has happened **three times,
every one self-inflicted** — twice by looping `download()`, once by re-requesting after a `1001` (below).

Retrying *during* a lockout can extend it. When locked: do nothing and let the schedule recover it.

Budgets in `ibkr_service.py`: `_FLEX_RETRY_DELAYS = [30]` (2 attempts, interactive path) and
`FLEX_RETRY_DELAYS_PATIENT = [120, 300, 600]` (scheduled jobs). `1025` fails fast with guidance; `1018`
always backs off >= 60s. Pinned by `tests/test_flex_retry_policy.py`.

**`1001` means the opposite thing at each step, and getting that wrong caused a third lockout
(2026-07-26).** While *retrieving*, it means "not ready" — keep polling the same reference. From
*SendRequest*, it means IBKR tried to generate and failed, which is exactly what `1025` counts:

```
Code=1001 ...; attempt 1/4, re-requesting in 120s   ->   Code=1025 Too many failed attempts
```

So `1001` is in `_RETRIEVE_PENDING_CODES` but deliberately **not** in `_REQUEST_RETRYABLE_CODES` — it
fails fast and the next scheduled job asks for a fresh statement. Giving up costs hours of freshness;
re-requesting costs a lockout of every sync. The codes still retryable at the
request step (`1009`/`1018`/`1019`/`1021`) all mean "throttled or busy, no generation job was created".

Genuine transport errors (`requests.RequestException`: DNS, reset, timeout) **are** retried — they never
reached IBKR, so they cost nothing against the token. Mid-poll they re-retrieve the same reference; before
a reference exists they re-issue SendRequest. A DNS blip used to kill a whole job.

### The two-step rule (why we don't use `client.download()`)

Flex is a two-step protocol: **`SendRequest`** asks IBKR to generate the statement and returns a
`ReferenceCode`; **`GetStatement`** retrieves it, answering "try again shortly" until it's ready. IBKR's
docs are explicit:

> "If statements are still being generated when you submit your request to retrieve them, you should
> **not re-initiate the Flex request**, but instead keep trying to **retrieve** the statement."

`ibflex.client.download()` polls correctly for `1009`/`1019`/`1018` but **not `1001`** — it raises, so any
outer retry calls `download()` again and starts a *brand-new generation job*. Do that a few times and
IBKR blocks the token: **`1025` counts failed *generations*, not request volume** (volume is `1018`,
which we never saw). The first two lockouts came from this; the third came from re-requesting on `1001`,
the same mistake one layer up.

So `IBKRService._download_statement()` drives the two steps itself using ibflex's public pieces
(`request_statement`, `submit_request`, `check_statement_response`, `STMT_URL`): **SendRequest once**,
then poll the *same* `ReferenceCode` for every `_RETRIEVE_PENDING_CODES` hit, bounded by a deadline
(120s interactive / 900s scheduled) rather than an attempt count. The outer loop only ever re-issues
SendRequest, and only for failures raised *before* a reference code exists — the one case where
re-initiating is unavoidable.

**Flex error codes:** `1001` statement not ready (transient, expected — poll, don't re-request),
`1003` not available (terminal), `1018` rate limit (1/sec, 10/min per token), `1019`/`1021` transient,
`1025` **undocumented** token lockout from repeated failures (fatal, never retry), `1012` token expired,
`1013` IP restriction, `1015` bad token. The official table stops at 1021 — 1025 appears nowhere in it.

---

## Keeping STATUS.md current

STATUS.md answers "where does this actually stand?", and it is only worth reading if it is true.
**Updating it is part of the work, not a courtesy afterwards.** The previous wording — "update it when
you finish a session" — named no trigger a session could recognise, so it got skipped.

**Leave it accurate before you stop, on any turn where your work changed what it should say:**

- code, config or a migration changed — and *Worth doing next* should lose whatever you just finished
- you shipped, reverted, or left something that needs watching after the next deploy
- you found something known-broken or flaky, or something only a human can do (rotate a token, change
  a Flex Query period, click through the IBKR portal)
- a *Known rough edge* stopped being true, or a new accepted-not-a-bug appeared
- you lost time to a *local-dev trap* that isn't in the list yet

A turn that only answers a question and finds nothing new needs no edit — but **discovering something
is a change of status even when no code moved**, so an audit that turns up real defects belongs in the
file whether or not they get fixed the same day.

**It is a snapshot, not a log.** Bump `Last updated`, add what became true, and **delete what stopped
being true** instead of stacking corrections. `Recent sessions` is the one append-only part, capped at
five one-liners — drop the oldest rather than letting it grow. And never accumulate **figures** (public
repo, user-switchable base currency, and a pasted total goes stale silently) or **what git already
records** — the log has what changed; STATUS.md has what is now true and what it costs the next person.
A count of unpushed commits is both, which is why it is written as `git log --oneline origin/main..main`
rather than a number: as a figure it went stale three times in one session, each time inside the very
commit that corrected it.

---

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
| "is this holding a fund?" | the same three call sites again, a rule later. Sector and geography ask `is_known_etf(symbol)` — a live look-through-table lookup needing no sync — while the asset-type chart read `securities.asset_type`, which **only** `POST /api/allocation/sync` writes and nothing schedules. `sync_helper` never writes it, so an IBKR-ingested fund keeps the `"Stock"` column default indefinitely and was drawn as a Stock in one chart while being distributed across eleven sectors as an ETF a few pixels below. Two sources for one predicate, one of them needing a manual step the other doesn't; the table wins now |
| the 12-month deployment average | `ContributionsStrip` renders the server's `avg_deployed_per_month_eur`; `MonthlyDeploymentCard`, on the same tab a few hundred pixels below, recomputed it as `monthly.slice(-12)` divided by its own length. `monthly` omits months with no activity, so that takes the last twelve *rows* — which can span more than twelve months — and divides by a count smaller than the period covered. Both errors push it up. **Two numbers under one name on one screen** is the cheapest instance of this failure to find and the easiest to leave: neither is obviously wrong on its own |

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

## Tech stack

**Backend** — FastAPI (async), SQLAlchemy 2.0 + aiosqlite (WAL), Alembic, APScheduler,
`ibflex` **0.15** (pinned), `yfinance` >= 1.1.0, Frankfurter API for FX.
**Frontend** — React 19 + TypeScript + Vite, TanStack Query, Recharts, Tailwind + shadcn/ui.
Vitest runs in `node` by default; component tests opt into jsdom per file with a
`// @vitest-environment jsdom` docblock, because the pure `lib/` tests are the large majority and
paying jsdom's startup for all of them is the wrong default.

**The bundle is code-split, and two of the boundaries are deliberate.** The eight non-default tabs
are `React.lazy` in `Dashboard.tsx` (safe because `TabsContent` returns `null` while inactive, so a
panel is not mounted until selected). **Recharts stays eager** — `PortfolioValueChart`,
`PerformanceAttribution` and `MonthlyDeploymentCard` are all on the default Performance tab, so
deferring it would only move the wait; don't "optimise" it into a lazy chunk. `manualChunks` in
`vite.config.ts` splits `react` / `charts` / `query` mainly for **caching**: the VPS redeploys within
10 minutes of any push and nginx serves `/assets/` `immutable`, so keeping vendor code out of the
app chunk took the per-deploy re-download from 264 kB gzipped to ~52 kB. List chunk members by the
specifier that actually appears in the graph (`react-dom/client`, `react/jsx-runtime`) — naming the
bare packages emits a 0-byte chunk and leaves React in the app bundle.

Because a lazy chunk can 404 after a redeploy (content-hashed names, page held open across a
deploy), every lazy panel is wrapped in `ui/LazyTabPanel.tsx` — a *scoped* boundary. Without it that
rejection reaches `App.tsx`'s app-level boundary and blanks the whole dashboard, which is worse than
the eager import it replaced. Chunk boundaries are a build-output property no unit test can see, so
the end-to-end check is `e2e/lazychunks.mjs`.

**`e2e/` is the browser-check package**, deliberately separate from `frontend/`: `deploy.sh` runs
`npm ci` inside `frontend/` on every `--no-cache` rebuild and Playwright's postinstall pulls ~150 MB
of Chromium, which a 10-minute deploy cadence cannot absorb. Nothing in the deploy path touches it.
It covers what the unit suites structurally cannot see — keyboard/ARIA on the assembled page, the
production CSP, chunk boundaries, and the backend-down pass asserting no surface falls back to an
empty-data message. Read `e2e/README.md` first: **the preconditions differ per script**, and two of
them (`csp`, `chunks`) must run against `vite preview` rather than the dev server, because the dev
server emits an inline react-refresh script that `script-src 'self'` correctly blocks and does not
produce chunk boundaries at all.

---

## IBKR Flex Query integration

### The Flex Query (`App_OpenLots`, ID 1389408)

Required sections and the fields the parsers actually read:

| Section | Options | Key fields |
|---|---|---|
| **Open Positions** | **Lot** | `conid`, `symbol`, `isin`, `description`, `currency`, `listingExchange`, `position`, `costBasisPrice`, `costBasisMoney`, `openDateTime`, `reportDate` |
| **Trades** | **Execution** | `conid`, `symbol`, `tradeDate`, `buySell`, `quantity`, `tradePrice`, `proceeds`, `ibCommission`, `currency`, **`fifoPnlRealized`** (= "Realized P/L"), `transactionID` |
| **Cash Transactions** | Dividends, Payment in Lieu, **Withholding Tax**, **Deposits & Withdrawals** | `type`, `conid`, `symbol`, `settleDate`/`dateTime`, `amount`, `currency`, `transactionID` |
| **Corporate Actions** | Detail | `type`, `conid`, `symbol`, `dateTime`/`reportDate`, `quantity`, `value`, `proceeds`, `actionDescription`, `transactionID` |
| **Transfers** | — | `type`, `direction`, `date`/`reportDate`, `cashTransfer`, `positionAmount`, `symbol`, `conid`, `company`, `transactionID` |

**Deposits & Withdrawals** feeds the contributions report; without it there is no record of external
money at all. **Transfers** exists only so an incoming broker transfer can be told apart from a deposit
— see the contributions section. Both are inert until parsed: `extract_cash_transactions` filters to the
three dividend types, so ticking them early cannot disturb anything.

**General config that matters:** Format **XML**; Period **Last N Calendar Days, N=3** (see below);
Date `yyyyMMdd`, Time `HHmmss`, separator `;`. **Never use `dd/MM/yyyy`** — ibflex assumes US
`MM/dd/yyyy` for ambiguous formats and would silently swap month and day.

**The period was Year to Date until 2026-07-31 and must not go back** — that is what caused the
`Code=1001` failures (see *Sync schedule*). Trades/CashTransactions contain only rows *inside* the
period, so the window has to exceed the longest plausible run of failed syncs.

**It has been `Last N Calendar Days` with N=3 since 2026-08-06, narrowed from 30 — a deliberate
choice by the account owner, reaffirmed after the trade-off was put to them.** Know what it costs
before widening or narrowing it again:

- A statement generated on day *D* covers **D−3 … D−1**. So a trade on day *T* is reachable from a
  statement generated on *T+1*, *T+2* or *T+3*, and **unreachable from T+4 onward**.
- The account gets about **one successful IBKR sync per day** (see the once-per-day rule under
  *Sync schedule*), so the margin is roughly **two consecutive failed days**, not the ~90 that 30
  days bought. Two-day gaps have happened: 08-02 and 08-03 both failed at the day's first attempt
  and were recovered by the second.
- Only the `<Trades>` / `<CashTransactions>` rows are at risk. **OpenPositions is
  period-independent**, so the lots and holdings still arrive whatever the window — what would be
  lost is the execution record the Activity ledger, XIRR's flow terms and realized P&L read.
- Recovery is a browser download with a wider period ingested through `app/cli/ingest_flex_xml.py`,
  which is idempotent. That is the reason a short window is survivable at all, and
  `find_stale_ibkr_sync` (7 days) is **too slow to be the alarm for it** — at N=3 the data is gone
  four days before that warning fires. `find_flex_generation_gap` (2 ET days) is the one that
  fires in time; see *Sync schedule*.

Prior tax years need a one-off period change (e.g. 2025), then set back. Ingestion is idempotent
(upserts keyed on `ib_key`), so re-syncing is safe — which is also the recovery if a bounded window
ever *does* miss something: download a wider statement from Client Portal and ingest it offline.

**The rolling window ends *yesterday*, so today's activity cannot be ingested today.** "Last N
Calendar Days" means the N days ending on the last *completed* statement day, not the N up to now.
So a same-day buy is absent from every section at once: no trade row, no lot, and no deposit for
the cash that funded it. This is the expected answer to "I bought today, update the data" —
**nothing needs forcing and nothing is broken**.

**And "yesterday" is measured in US Eastern, which decides whether a download is worth taking.**
`whenGenerated` is stamped in ET, not Berlin and not UTC — `20260806;122722` on a file downloaded
at 18:27 Berlin, `20260806;234042` on one downloaded at 05:40 Berlin the next morning. Both came
back `to=20260805`. The window rolls at **midnight ET**, so the honest rule is:

| generated | Berlin | reaches |
|---|---|---|
| 12:27 ET 08-06 | 18:27 08-06 | through 08-05 |
| 23:40 ET 08-06 | **05:40 08-07** | still only 08-05 |
| 00:00 ET 08-07 | 06:00 08-07 | **08-06** |

That middle row is the trap and it cost a morning on 2026-08-07: a statement downloaded at 05:40
Berlin *looks* like today's and is still yesterday's, because 05:40 Berlin is 23:40 the previous
day in New York. Twenty minutes later the 06:00 Berlin `ibkr_only_sync_job` — 00:00 ET — got the
missing day without anyone doing anything. **Check `toDate` in the file header before ingesting
a manual download**; the generation time tells you nothing on its own. Only a **custom date range**
ending today can reach the current day, which is a portal edit that has to be set back afterwards —
worth it for a prior-year backfill, not for waiting one night.

### Offline ingest — the escape hatch from a locked token

The Flex **Web Service** and the **download button** in Client Portal serve the same statement over
independent channels. Only the API path spends the token's request budget, so only it can trip `1025`.
So a statement saved from the browser can be ingested *during* a lockout — and since the browser download
uses whatever period you set, it is also the practical way to reach a prior tax year:

```bash
docker cp stmt.xml backend-portfolio-backend-1:/tmp/stmt.xml
docker exec backend-portfolio-backend-1 python -m app.cli.ingest_flex_xml /tmp/stmt.xml --dry-run
docker exec backend-portfolio-backend-1 python -m app.cli.ingest_flex_xml /tmp/stmt.xml
```

`app/cli/ingest_flex_xml.py` reuses `IBKRService.parse_flex_xml()` and
`sync_helper.ingest_flex_statement()` — the *same* functions `POST /api/sync/ibkr` and the scheduled jobs
use — so reconciliation order, the empty-statement wipe guard and the idempotent upserts all apply
identically. It records a `sync_runs` row with `sync_type='ibkr_manual_xml'`. Touches no network at all
(no Flex, no Yahoo). `--dry-run` reports counts without writing. Tests: `tests/test_manual_xml_ingest.py`.

There is deliberately **no upload endpoint**: `/api/` is proxied publicly, and a route that rewrites tax
lots is a far larger surface than a CLI run over ssh — write auth (below) narrows that surface but does
not change the judgement.

### Offline price import — the escape hatch from a wrong or missing feed

`app/cli/import_prices.py` is the price-side twin, for when Yahoo can't be called (rule 1) or can't
resolve a listing at all. It takes a JSON file of daily closes and writes them through the same
`MarketPriceRepository.bulk_create()` upsert the sync uses, so it is re-runnable and a later Yahoo
fetch overwrites cleanly. Records `sync_type='manual_prices'`. Touches no network.

```bash
docker cp prices.json backend-portfolio-backend-1:/tmp/prices.json
docker exec backend-portfolio-backend-1 python -m app.cli.import_prices /tmp/prices.json --dry-run
docker exec backend-portfolio-backend-1 python -m app.cli.import_prices /tmp/prices.json
```

```json
{"symbol": "SBI", "exchange": "TSE", "currency": "CAD", "source": "ibkr",
 "prices": [{"date": "2026-07-27", "close": 4.79}]}
```

**IBKR's Client Portal is the good source for this** (`get_price_history` via the MCP connector):
independent of Flex, so it spends no token budget and can't trip `1025`, and it quotes the listing's
own currency. That's how SBI was refilled — 2 years of daily CAD bars, `source='ibkr'`.

A **currency mismatch against the security refuses the whole file**, mirroring the guard on Yahoo
auto-discovery, because writing USD closes under a CAD security is the exact bug this CLI repairs.
A malformed row also rejects the whole file: a partially-applied series is indistinguishable
afterwards from a complete one. Ambiguous `symbol` (ASML is two rows) refuses rather than guessing —
pass `--security-id`. Tests: `tests/test_price_import_cli.py`.

Note the trade-off: once a date has a price, `get_missing_dates()` never re-fetches it, so an
imported window stays imported (visible in `market_prices.source`) until something deletes it.

### `_sanitize_flex_xml()` — why it exists

ibflex 0.15 (released 2021) converts **every** XML attribute onto a frozen dataclass and raises
`FlexParserError` on the first thing it can't handle, which **aborts the entire document** — so one
unrecognised field kills the whole sync, open positions included. IBKR has drifted well past it:

- **Unknown attribute names**, e.g. `subCategory` on `<Trade>` (modelled only on `SecurityInfo`).
- **Unknown enum values**, e.g. `type="Broker Fees"` — the query can enable 17 cash-transaction types
  but `enums.CashAction` models 10. Also `CorporateAction.type` (`Reorg`) and `notes`/`code` (`Code`).

`IBKRService._sanitize_flex_xml()` runs before `parser.parse()` and:
1. drops **any attribute ibflex's own `parser.parse_element_attr()` rejects** — that single call covers
   unknown names, bad enum values, unparseable dates/decimals and unknown currencies, and can't drift
   out of step with ibflex;
2. drops aggregate duplicate rows (`levelOfDetail` in ORDER / SYMBOL_SUMMARY / CLOSED_LOT / …) when
   real execution rows sit beside them, since IBKR gives each its own `transactionID` and ingesting
   both would **double-count trades and realized P&L** — but keeps them if they're all there is, so a
   populated section is never emptied.

It returns the original bytes untouched when nothing changed, never raises, and reports drops that
**can change ingested data** via `warnings[]` (surfaced in the sync response). **Don't patch attribute
names one by one** — it's generic.

**Only material drops are warnings, and that distinction is the point.** ibflex 0.15 cannot model most
of what IBKR now sends, so every statement drops ~27 attributes — `figi`, `serialNumber`, `weight`,
`subCategory`, `commodityType`, `Trade.notes`… — and until 2026-08-05 all of them went into
`warnings[]` as a single unreadable line, on every sync, for ever. A warning that is always present
and never actionable is worse than no warning: it is exactly what teaches the reader to skip the
banner that also carries a skipped tax lot or an unconvertible dividend, which is the whole reason
`warnings[]` exists.

So `INGESTED_ATTRS` maps each element to the attributes the extractors actually read, and a drop is
loud only when it hits one. Everything else lands on `IBKRService.last_schema_notes` → `flex_notes` →
the sync run's `flex_schema_notes` in `details`, where "did a *new* kind of thing start being
dropped?" stays answerable without a permanent banner.

The two failure directions are not symmetric, which decides how it is guarded. Listing a field we
don't read merely re-creates the noise; **omitting one we do read makes a real problem silent** — an
unparseable value on `CashTransaction.type` means `extract_cash_transactions` skips the row and a
dividend never arrives. So `tests/test_flex_attr_coverage.py` doesn't trust the map: it AST-walks each
extractor for attribute access, intersects with ibflex's own `__dataclass_fields__` (so `.append` and
local names can't masquerade as schema), and fails if the map has drifted behind. It caught a real
omission on its first run — `extract_transfers` reads `Transfer.date`, which a hand-written pass had
discarded as a Python builtin.

A latch (`last_schema_notes`) rather than a third return value only because eighteen call sites
unpack a 2-tuple; same shape as `MarketDataService.rate_limited`, and declared in `__init__` so a
service built via `__new__` in a test still has it.
`_fix_currency_codes()` still runs first so `RUS`→`RUB` is repaired rather than dropped.

Degradation is graceful: an unknown cash `type` becomes `None` and the row is skipped by
`extract_cash_transactions` (which only wants dividends/withholding); an unknown reorg `type` lands as
`'UNKNOWN'` with quantity and date intact.

Tests: `tests/test_flex_xml_sanitizer.py`, `tests/test_flex_ingestion_e2e.py`.

---

## Database schema

- **securities** — composite identity `isin + exchange` (same stock on two exchanges = two rows, e.g.
  ASML on NASDAQ *and* AEB). Also `symbol`, `description`, `currency`, `conid`.
- **taxlots** — one row per purchase: `open_date`, `quantity`, `cost_basis`, `cost_basis_eur`
  (pre-converted), `is_open`, `close_date`, **`close_source`** ∈ `trade` | `corporate_action` |
  `heuristic`.
- **trades** — authoritative executions, idempotent on `ib_key`: `buy_sell`, `quantity`, `price`,
  `proceeds`, `commission`, **`realized_pnl`** (IBKR's own FIFO). `security_id` is nullable — a fully
  sold security is no longer in OpenPositions.
- **corporate_actions** — `action_type` (Reorg name), `quantity`, `value`, `proceeds`, `description`.
- **cash_flows** — external cash, idempotent on `ib_key`: `flow_date`, **`flow_type`** ∈
  `DEPOSITWITHDRAW` | `TRANSFER_IN` | `TRANSFER_OUT` | `TRANSFER`, `amount` (signed as IBKR reports:
  deposit +, withdrawal −), `amount_eur` (pre-converted at `flow_date`). Only `DEPOSITWITHDRAW` counts as
  money added — see the contributions section.
- **dividend_payments** — `gross_amount_eur`, **`withholding_tax_eur`**, **`net_amount_eur`**,
  `pay_date`, **`source`** ∈ `ibkr` | `yfinance_estimate`.
- **exchange_rates** / **market_prices** — caches. **ticker_mappings** — IBKR→Yahoo symbols.
- **isin_identities** — ISIN → company identity, keyed by ISIN: `lei` + `issuer_name` +
  `lei_source` + `lei_checked_at` (GLEIF), `share_class_figi` + `composite_figi` + `figi_name` +
  `figi_source` + `figi_checked_at` (OpenFIGI). Per-provider stamps because they fail
  independently in both directions; a NULL identifier beside a non-NULL stamp means *asked, no
  record*. See *Look-through*.
- **etf_baskets** / **etf_holdings** — one fund's constituent basket, keyed by **fund ISIN** (a
  basket belongs to the share class, not the venue, so one fund held on two exchanges shares it).
  Metadata is split from the rows on purpose; `weight_pct` is a **percent**;
  `UNIQUE(fund_isin, line_no)` because `constituent_isin` is nullable and SQLite treats NULLs as
  distinct. Replaced wholesale, never merged row-by-row.
- **app_settings** — `base_currency`, `last_sync_to_date`. Plus fundamentals + earnings tables.
- **sync_runs** — one row per sync attempt (`sync_type` ∈ `ibkr` | `ibkr_sync` | `full_sync` |
  `market_data_only` | `ibkr_manual_xml` | `manual_prices` | `manual_mapping` | `manual_cash_flow` |
  `manual_dividend_prune` | `manual_dividend_purge` | `manual_identity_resolve` |
  `manual_etf_basket`,
  `status`, `message`,
  `details`, `warnings`). Timestamps are
  serialized UTC-aware via `utc_iso()` — a bare naive `isoformat()` is parsed as *local* by the browser,
  which once made an 08:00 sync display as 06:02.
  `SchedulerService.last_sync_result` is in-memory only and auto-deploy restarts on every push, so
  without this the daily validator can't tell "no sync ran" from "the container restarted".
  `/api/scheduler/status` falls back to it; `/api/scheduler/history?limit=N` lists recent runs.
  Recording is best-effort — it must never fail a sync or mask the real error.

**Every stored timestamp is a naive UTC datetime, and `app/clock.py` is the only way to make one.**
The columns default to `func.now()`, which SQLite answers in UTC, and `utc_iso()` serializes by
stamping UTC onto a value it *assumes* is already UTC. The stdlib's argument-less `now()` returns naive
**local** time, so it was correct at ~49 call sites only because `python:3.11-slim` sets no `TZ` — an
undeclared dependency on the base image that nothing would have noticed breaking. Off the container it
was already wrong three ways at once: cache cutoffs (`now - timedelta(days=7)` against a naive-UTC
column) expired an offset early, ages (`now - row.last_synced`) read an offset too old, and
`utc_iso(datetime.now())` labelled local time as UTC so the browser converted it a second time — the
"two clocks on one line" failure `utc_iso` exists to prevent, reintroduced through its own argument.
`utcnow()` stays **naive** on purpose: these values are compared against naive columns, where an aware
value raises `TypeError` rather than degrading. `tests/test_clock_convention.py` walks the source tree
so the old call cannot return, and asserts against real UTC rather than local — the assertion that
passes vacuously on the container and catches the bug everywhere else.

Migrations: `cd backend && alembic upgrade head` (the container CMD runs this on every start).

---

## Reconciliation & realized P&L

`reconcile_taxlots()` (`sync_helper.py`) explains quantity changes in priority order:
1. **SELL trades** → close lots FIFO with the real date/proceeds/`fifoPnlRealized`; `close_source='trade'`
2. **Corporate actions** → deterministic reclassification (split/spinoff/merger), not a sale
3. **Fallback heuristic** (quantity drop + `COST_CONSERVED_RATIO`) → `close_source='heuristic'`

**That order is the code's order, and it wasn't until 2026-07-30.** The trade lookup used to run
*third*, supplying only a date and a provenance tag after 2 and 3 had already decided — so a trim of
**≤1% of cost basis** took the cost-conserved path and recorded **no closure at all** while IBKR's own
SELL sat in `trades` for that window (200 shares at 20,000 sold down by 2 leaves 19,800, and
`19800 >= 20000 * 0.99`). The disposal then never reached `calculate_xirr()`'s `+proceeds` inflow, the
attribution's disposal term, that day's `external_flow_eur`, or the tax report's `closed_lot_estimate`.
The two inferences now run **only when no SELL is on record**, so the reverse-split and cash-in-lieu
protections are untouched — pinned from both sides in `tests/test_sync_helper.py`.

**`open_date` comes off `position.openDateTime`, parsed by ibflex.** It used to be scraped from the raw
XML into a separate list and matched back by position index, but that list kept only STK rows carrying
the attribute while the loop enumerated *every* `OpenPosition` — so one bond/option row, or one lot
without the attribute, shifted every index after it, and the `conid` check fell back to `reportDate`
rather than realigning. One stray row silently stamped **every later lot** with the statement date.
ibflex 0.15 does parse the field (`parse_element_attr` returns a `datetime`); the comment claiming
otherwise was wrong. Don't reintroduce index matching. Tests: `tests/test_ibkr_parsers.py`.

`restamp_unsourced_closed_lots()` then fixes lots closed *before* `<Trades>` existed: those carry the
date the sync **noticed** the drop, not the sale date (CRM and NFLX read 2026-04-17 for a 2026-03-13
sale). A lot is only re-stamped when exactly one SELL for that security matches its quantity and isn't
newer than the recorded close date — ambiguity is left alone rather than guessed. Idempotent (a stamped
lot has a `close_source`), so it self-disables.

Note `conid_to_security_id` is built from the statement's **OpenPositions**, so a security sold out
entirely isn't in it; `persist_transactions` falls back to a DB lookup by conid, otherwise every SELL
trade lands with `security_id = NULL`.

**Empty-statement wipe guard:** if incoming tax lots are empty but the DB holds open lots, the sync
**aborts** instead of marking everything sold. A successful-but-empty statement is treated as a failure.
This guard has already saved the data through several failed syncs.

`get_realized_totals()` prefers `trades` (exact) and falls back to a market-price approximation over
closed lots. `realized_rows_from_closed_lots()` is **shared** by the portfolio totals and the tax report
so the two can never disagree — they did once, and that was a bug. The picker keys on **SELL trades
specifically**, not on the table being non-empty: a BUY-only statement returned hard zeros and
permanently suppressed the fallback, while the tax report (which decides per year) still showed real
gains. Pinned by `tests/test_realized_totals.py`.

**A lot sold on D is not held at D's close.** One convention, everywhere: `_calculate_daily_value`,
`holdings_snapshot_as_of()` and the attribution gates all exclude **on** the close date, matching what
the benchmark always did. Under the old include-on-close rule a same-day rotation double-counted that
day (the sold lot still "active" beside its replacement) and a position sold on 31 December landed in
both the year's Steuerwert *and* its realized gains. Consequence to expect: a sale's value leaves the
chart on the sale date itself, one day earlier than before. The disposal windows in `calculate_xirr()`
and the attribution endpoint are therefore `(start, end]` — a lot sold *on* the window end yields
proceeds precisely because the end valuation no longer carries it. Change these together.
Tests: `tests/test_close_date_boundary.py`.

**A spun-off line is not held before the action that created it, even though its lots say it
was.** IBKR reports received spinoff shares against the *parent's* tax lots — the child
inherits the parent's `openDateTime`, because the holding period carries over, plus the slice
of cost basis IBKR reallocates to it. So the lot asserts ownership from a date when the
instrument had no listing and no price, and every valuation in that gap counted a held
security it could not value. Read off production on 2026-08-07: **MBGL, spun out of SPGI
1-for-1 on 2026-06-30 against lots dated 2025-11-06 and 2025-12-29**, made
`unpriced_holdings = 1` on **166 consecutive days**, and the client correctly refused every
one of them.

The damage was entirely in what the flag *implies*, which is why it survived: the position is
0.2% of the book, so no total looked wrong. What broke was the monthly-returns table —
December 2025 through May 2026 blank, November 2025 measured over three days, and a **"YTD"
of +3.1% that covered 26 June to 7 August**. The full-year figure is +23.5%.

`PortfolioService._load_position_start_dates()` reads
`sync_helper.POSITION_CREATING_ACTIONS` and floors the date each security counts as *valuable*
at the action that created it. Four things about it are load-bearing:

- **Excluding the child is the arithmetic, not a workaround for the missing prices.** We fetch
  `auto_adjust=False` and Yahoo does not rebase raw `Close` for a spinoff (the same reason
  `PRICE_RESTATING_ACTIONS` excludes one), so the parent's own close still carried the
  spun-off business on those days. Valuing the child beside it double-counts — which it did,
  for the four when-issued days between the first MBGL bar and the distribution.
- **Its cost stays where the lot puts it.** IBKR reduced the parent's basis by exactly what
  the child received (verified: 4.84% of each SPGI lot), so the pair sums to the original
  outlay on the original date, and the day's `external_flow_eur` is the real purchase.
  Deferring the cost too would understate the cost line before the spinoff and book a phantom
  purchase on the day of it.
- **The action set is far narrower than `SPLIT_LIKE_ACTIONS`, in both directions.** A forward
  split also adds shares, but to a security already held, so flooring on one would drop a
  long-held position out of every valuation before it. `MERGER`/`ISSUECHANGE` are left out
  because either can be recorded against the position being *replaced* rather than the one
  received, and the row does not say which side it is.
- **Every ambiguity resolves to not flooring**, because not flooring only leaves the existing
  warning in place while flooring wrongly deletes a holding from its own history. Hence
  `quantity > 0` (a parent-side spinoff row carries no quantity change, and flooring *SPGI*
  is the catastrophic direction) and the requirement that the action account for the whole
  position at that date (a distribution of something already held must not floor the shares
  held before it). A spinoff whose `<CorporateActions>` row was never ingested keeps the old,
  loud behaviour — the floor is driven by recorded fact, and no record is no licence to drop
  a holding quietly.

It reaches `_calculate_timeline_swept`, `_calculate_daily_value` and
`holdings_snapshot_as_of` — the last because a 31 December before the spinoff was serving a
Steuerwert badged *partial* over a holding that did not exist. `/api/portfolio/attribution`
deliberately still excludes-and-counts it: a line that did not exist at the window start has
no start value to attribute against, and combining parent and child is a larger change than
this. Tests: `tests/test_spinoff_position_start.py`, plus the floor case in
`tests/test_timeline_equivalence.py` — the two walks must skip identically or the chart and
every point query disagree about the same day.

**Realized proceeds are inflows, not absences.** `calculate_xirr()` books each lot closed in the window
as a `+proceeds` flow (plus net dividends, era-spliced) alongside the `−cost` of lots opened. Without
that, selling A to buy B added a fresh outflow with no matching inflow, so every rotation crushed the
reported return — the planned IE→US ETF switch would have roughly halved it. Its guard is a
**sign change**, not `start_mv > 0 and end_mv > 0`: a window may legitimately start at zero (before the
first purchase) or end at zero (fully liquidated, where the proceeds carry the whole return). Windows
under 30 days return `method="simple_period"` and the UI labels that tile *Period Return* rather than
annualizing a few days of noise. Attribution takes the same disposal term
(`pnl = value_change + disposals − new_investment`), without which a position sold at a profit read as
`−start_value`. Tests: `tests/test_xirr.py`, `tests/test_attribution.py`.

**The timeline is swept once, not rebuilt per day.** `_calculate_timeline_swept()` folds each lot's
date-independent parts (base-converted cost at `open_date`, quantity) into running sums via open/close
events, so a day prices each *security* once instead of once per lot — O(days × securities) rather than
O(days × lots) with a 15-probe price walk inside. `_calculate_daily_value()` remains for point queries.
The two are numerically identical by construction and pinned that way
(`tests/test_timeline_equivalence.py` asserts exact equality across closed lots, a same-day rotation,
price gaps, a USD security and a CHF base) — **keep them in lockstep**.

**A split also invalidates the cached prices.** Yahoo restates historical `Close` after a split, but
`get_missing_dates()` only fetches dates we *don't* have, so pre-split rows are never refreshed while
IBKR restates the lot quantity immediately — leaving a step change in the chart that nothing detects.
So `invalidate_prices_for_splits()` deletes `market_prices` up to and including the action date
(`MarketPriceRepository.delete_up_to`), and the next market-data sync refetches them — one extra
request per security, since fetching is range-based.

**Holidays are not "missing" forever.** `get_missing_dates()` skipped weekends but not market holidays,
so a date the exchange never traded (4 July, Good Friday) stayed missing permanently — and one such date
makes `fetch_and_cache_prices` re-request that security's **entire range** on every one of the
daily jobs, indefinitely, against an IP-based rate limit. An **interior** weekday hole (cached data on
both sides) older than `HOLIDAY_GRACE_DAYS` (30) now counts as a holiday: every sync since has already
failed to fill it. Younger holes stay missing so late data can arrive, and **leading/trailing gaps stay
missing at any age** — which is exactly what a purge-and-refill repair looks like, so `--purge-prices`
and the split invalidation above still heal normally. `BenchmarkService` applies the same rule via its
own `_missing_business_days()`, shared by its price *and* FX paths. Tests:
`tests/test_missing_dates_holidays.py`, `tests/test_benchmark_fx_window.py`.

**A recent close is provisional, so it is re-fetched even when cached.** Yahoo answers a daily-interval
request with a bar for the session *in progress*, whose `Close` is merely the last trade so far — and
because `get_missing_dates()` returned only dates with **no row at all**, whichever job wrote a date
first owned it permanently. Read off `market_prices.created_at` on 2026-08-04: every European close in
production was its **15:00 Berlin mid-session price** (Xetra and Euronext run to 17:30), and Korea's
alternated between mid-session and final depending on whether the 08:00 or the 15:00 job happened to
land the row first. So a weekday within `PROVISIONAL_PRICE_DAYS` (3) of today is reported missing even
when cached, and `bulk_create`'s `ON CONFLICT DO UPDATE` lets the settled close overwrite it.

Three is what covers the two cases "today" does not: the 22:00 slot writes a US close within seconds
of the bell and only the next morning settles it, and **Friday's close must still be refreshable on
Monday**. It cannot collide with the holiday rule above — that needs a date older than 30 days and this
one needs it newer than 3 — and it cannot re-request forever, because a date Yahoo has no bar for ages
out of the window and falls back under the holiday rule. It costs **no extra requests**, only a wider
range on one already being made.

**`market_prices.created_at` records the first insert and is NOT bumped by a restatement**, because
`bulk_create` only updates the columns the caller supplies and the price dicts carry no `created_at`.
That is right for a column with that name, and it is a trap worth knowing twice over: it is the column
that *proved* the freeze (a row dated today, written at 13:00 UTC while Xetra had 2.5 hours to run),
and it is useless for confirming the fix — a restated row still reads 13:00. Verify a restatement by
the **price** changing, not by the timestamp. Nothing records when a price was last rewritten; an
`updated_at` column would, and would need a migration.

This is the **precondition for the intraday market-data slots**, not a separate cleanup: adding an
earlier slot on top of the freeze pins an *earlier* price. If the refresh is ever removed the slots
have to go with it. `BenchmarkService` opts in per caller — the read path yes, the scheduled warm-up no
— for the Yahoo-budget reason given in *Sync schedule*, and the **FX path deliberately never** does: an
ECB reference rate is published once a day, so there is no intraday rate to converge on, and
`_batch_fetch_rates` dedups per row and could not rewrite it anyway. Tests:
`tests/test_provisional_price_refresh.py`.

The fetch is also **span-narrowed**: the Yahoo request starts a few days before `min(missing)`
(`PRICE_FETCH_BUFFER_DAYS`), not at the window start — the 08:00 730-day job used to re-download two
years per security every morning purely because today's close hadn't published (~19.5k rows rewritten
to gain a few hundred). A split purge or a newly added security still pulls the whole span, because
that is where `min(missing)` then sits.

Two details carry the weight. `PRICE_RESTATING_ACTIONS` is a deliberate **subset** of
`SPLIT_LIKE_ACTIONS`: we fetch with `auto_adjust=False` and Yahoo rebases raw `Close` for splits only,
so `SPINOFF`/`STOCKDIV`/`ISSUECHANGE` are excluded as pure churn. And it fires **only for actions
newly inserted on this sync** (`CorporateActionRepository.existing_ib_keys()`) — the statement
resends every action inside its period every time, so without that check every daily job would wipe and refetch the
same history forever. Reported in `warnings[]` and as `prices_invalidated`.
Limitation: the 7-day jobs restore the current value, but the full history only comes back at the next
**08:00** 730-day `full_sync`. (There are six 7-day jobs now, not two, so the current value returns
within a couple of hours rather than by the evening.) Tests: `tests/test_split_price_invalidation.py`.

---

## Tax report (Swiss framing)

`GET /api/tax/report?year=YYYY` and `.csv`. Switzerland doesn't tax private capital gains but does tax
dividend income and allows reclaiming foreign withholding via **DA-1** — so the report leads with
dividend income + withholding, then realized gains, then a year-end holdings snapshot (Steuerwert).

Two honesty flags, both badged in the UI and CSV:
- `dividend_source`: `ibkr` (real withholding) vs `yfinance_estimate` (gross guess, no withholding)
  vs `mixed` (the boundary year). Dividends are **era-spliced with the same `_splice_by_era` the
  card uses** — estimates strictly before the globally first IBKR payment (`dividend_ibkr_from`),
  IBKR rows from there on — then windowed to the year. Both simpler schemes were bugs: a global
  "prefer ibkr" made 2025 report **0.00 labelled authoritative**, and a per-year boolean dropped
  the boundary year's real January income (the ledger starts mid-February). A per-year *boundary*
  would be wrong too — yfinance stores a dividend under its ex-date, IBKR under its pay date, so
  it would resurrect the double-count. The two sources are never summed for the same period; each
  income row carries its `source`.
- `realized_source`: `trades` (IBKR FIFO) vs `closed_lot_estimate` (market price at close date — was
  ~8% off on a spot check, hence the badge)

**A partial Steuerwert says so.** `holdings_snapshot_as_of` omits a lot it cannot price or
convert — correct, and stated below — but until 2026-08-05 it omitted it *silently*, so a wealth-tax
base missing a holding was served as though it covered the book. The report already handled the
snapshot **raising** (`holdings_snapshot_total` becomes `None`, plus a warning), and that asymmetry
is the bug: total failure was loud, partial failure was mute, which is backwards. A missing figure
reads as a fault; a plausible one reads as an answer — the same reason the timeline's `+15.3%` was
more dangerous than its `−100%`, and it matters more here than anywhere else in the app because this
number goes on a tax return. `PortfolioService.last_snapshot_skipped` is a per-run latch (same shape
as `MarketDataService.rate_limited`) naming the securities dropped; the report turns it into a
`warnings[]` line and still serves the figure, because a partial base is the best available and must
not be confused with the `None` reserved for no base at all. Note the latch resets on the
**early-return** path too — an empty snapshot inheriting a previous date's skip list would report the
wrong date's completeness.

**Steuerwert is valued at 31 December**, not today: `holdings_snapshot_as_of()` rebuilds the holdings
for `holdings_as_of` (= 31 Dec for a past year, today for the current one) using the same
`open_date`/`close_date` window as `_calculate_daily_value`, so it can't disagree with the portfolio
timeline. Positions with no resolvable price near that date are **omitted**, not counted as zero.

**A figure this report cannot justify is absent, not invented** (both halves were bugs until
2026-07-30, and both broke the rule the rest of the codebase follows: skip the row, log it, report it).

- `_to_eur()` returned the **unconverted foreign amount** when FX failed, so a TWD sale whose
  trade-date rate fell outside `FALLBACK_MAX_AGE_DAYS` read ~35× high while `realized_source` still
  said `trades` — the badge that means *authoritative* — with no `logger` call anywhere on the path. It
  returns `None` now and the realized loop omits the row. If **every** SELL is unconvertible the
  closed-lot approximation takes over (it reads `cost_basis_eur`, converted at ingest, so it needs no
  trade-date rate) and the warning says *that* rather than claiming a hole.
- An `except Exception: pass` turned any snapshot failure into a Steuerwert of **0.00**, served 200
  with the note still claiming the holdings were valued at that date's closes. `holdings_snapshot_total`
  is now **`None`** on failure — a missing wealth-tax base, not a zero one — alongside
  `holdings_snapshot_error`, and the note says so.

`warnings[]` on the report is the surface for both. It rides on a **successful** response, so it is
structurally invisible unless rendered: `TaxTab` shows it as a banner and `to_csv()` writes a WARNINGS
block. Tests: `tests/test_tax_service.py`, plus the shape assertions in `tests/test_api_smoke.py`.

Frontend: `TaxTab.tsx`. It's a filing aid, not tax advice.

---

## Dividends — history and forecast

`GET /api/dividends/breakdown?year=&forecast=` → `DividendService.get_dividend_breakdown()`, rendered by
`DividendsTab.tsx`: net dividends by month **stacked by symbol**, a year filter (All time + the years
with data), a Forecast toggle, and a per-stock table (payouts, net, projected, trailing-12M yield).
Unlike `/summary` it **never enqueues a sync**, so it cannot reach Yahoo (rule 1) — everything comes
from `dividend_payments`, `taxlots`, `market_prices` and `exchange_rates`.

**The boundary itself leaked one dividend per security until 2026-08-05, and the reason is the
splice's own premise.** The rule keeps estimates strictly *before* the first IBKR payment — but the
two sources file the **same** payment under different dates, yfinance under its ex-date and IBKR under
its pay-date. So the first IBKR payment's own estimate sits before the boundary and is kept, beside
the IBKR row it duplicates. Measured on production (boundary 2026-02-18, ASML held on two exchanges):
`02-09` and `02-10` estimates surviving next to two `02-18` IBKR rows — four rows for two dividends,
**13.7% of the year's dividend income**, on every reader that splices at once (breakdown, summary
card, XIRR inflows, DA-1 income, ledger).

`_splice_by_era` now also matches estimate to IBKR row **per security, nearest-first, one-to-one, and
bounded by `EX_TO_PAY_MAX_LAG_DAYS`** (30 — Mastercard's 29-day lag is the widest real one here).
Never by amount: one side is gross and the other net, so equal amounts are exactly what cannot be
relied on. One-to-one is what makes the window safe for a monthly payer, whose cycle is shorter than
the window — each IBKR payment consumes at most one estimate, so earlier months survive.
**The width errs deliberately toward keeping.** 45 was tried and matched a genuine dividend 45 days
out; too wide deletes real income from a filing aid (understating taxable income), too narrow leaves
one dividend double-counted (overstating it, visibly, already badged `mixed`). For a filing aid the
understatement is the worse failure.

**The two sources are era-spliced, never mixed or dropped.** `_splice_by_era()` keeps
`yfinance_estimate` rows strictly *before* the first IBKR payment date and IBKR rows from there on.
`get_dividend_summary()` used to call `has_ibkr_dividends()` **unwindowed** and then filter to
`source='ibkr'`, so the moment July 2026's real rows landed, every pre-IBKR month vanished from the
card — the repository's own docstring warns against exactly that. The boundary is reported as
`ibkr_from`.

**`get_dividend_summary()` returns NET, and its `source` is three-way.** Both were wrong on the
Performance tab's *Dividend Income* card until 2026-07-30: the service annotates its own return
`# NET per month`, but the card said "Gross dividend income by month" and footnoted "Estimated gross
dividends via Yahoo Finance — withholding taxes … not reflected" over IBKR actuals net of real tax, so
it silently disagreed with the Dividends tab and anyone reconciling DA-1 read net income as pre-tax.
`total_gross_eur` / `total_withholding_eur` / `source` / `ibkr_from` were already on the wire and simply
undeclared in `DividendSummaryResponse`. `source` was also binary — `'ibkr'` the moment any IBKR row
existed, while the splice still carries the estimated months ahead of the boundary — and is now the
same `ibkr` | `mixed` | `yfinance_estimate` flag the tax report uses (`_summary_source()`), which the
footnote reads off. `/api/dividends/summary` now carries a `response_model`, but **completing the
model had to come first, and that order is the whole point**: a `response_model` is a *filter*, so
attaching one to a model still declaring five of the ten keys would have deleted the provenance
fields from the wire and blanked the footnote — shipping the exact bug as a hardening change. For
that reason `tests/test_dividend_summary_contract.py` compares the service's key set against the
model's in both directions rather than spot-checking names: an undeclared key is dropped silently,
and a declared-but-unsupplied one silently takes its default (0.00 withholding, estimate
provenance). Add a key to the service and you must add it to the model.

**Only dividends that could have been earned are ingested.** `sync_dividend_data()` skips ex-dates
before the security's earliest lot `open_date` (reported as `pre_ownership_skipped`); a security with no
lots yet keeps its whole history, since there is no cutoff to infer. Before that, yfinance's full
history meant **1355 of 1446 rows** on this account were zero rows reaching back to 1985 — the reason
the card once reported 439 months, and the reason relaxing one read-side filter broke
`/api/dividends/breakdown` outright. Both readers still filter via `DividendService._is_income()`
(gross **or** net positive) and `_net_eur()` falls back to gross for rows predating the
withholding-fields migration, which carry a NULL net — **use those two helpers rather than touching
the columns directly.** Existing junk is removable with `app/cli/prune_empty_dividends.py --dry-run`.
Run on prod 2026-07-29 (`manual_dividend_prune`): 1350 zero rows removed, real payments remain.

**"Carries no income" is not sufficient grounds to delete a row, and treating it as such was a bug
(fixed 2026-07-31).** The forecast infers cadence from the **raw** history — see *Size from
`amount_per_share`, not from income received* below — so a pre-ownership yfinance row is
simultaneously income-free and load-bearing: it is exactly what lets a recently-bought payer project
at all. The old predicate deleted precisely those, silently reverting the "20 payers project" fix
toward the old 15, and the CLI's own docstring claimed it "deletes only rows the readers already
ignore" — true when written, false once the forecast became a reader of them. Prune is now
additionally bounded by **the ingest window it should always have mirrored**: a row goes only when it
is older than `PRE_OWNERSHIP_HISTORY_YEARS` before the security's first lot, i.e. exactly what
`sync_dividend_data` would no longer create. A security with **no lots** is left entirely alone,
matching ingest's own refusal to guess a cutoff. Rows awaiting computation
(`shares_held IS NULL`) are still never touched. Tests:
`tests/test_prune_preserves_forecast_basis.py`.

### A wrong mapping poisons dividends too, and only prices were ever purged

`dividend_payments` rows tagged `yfinance_estimate` are keyed to whatever Yahoo ticker resolved when they
were written. Correcting a mapping does not retire them, and until 2026-07-30 nothing could:
`manage_mappings disable --purge-prices` cleared `market_prices` only, `DividendRepository` had no delete
method at all, and `prune_empty_dividends` never deletes a row carrying income — so a poisoned row
with a plausible positive amount was unreachable by every tool.

That is the second half of the SBI failure. `SBI@TSE` is **SERABI GOLD PLC** (CAD, Toronto). The mapping
was corrected to `SBI.TO` and the prices purged and refetched on 2026-07-27; its two dividend rows,
computed 06-24 and 07-25 under the bare-ticker US listing, survived. Because cadence comes from whichever
series carries `amount_per_share` — and skips the IBKR rows when one exists — **those two rows alone
projected five monthly payouts for a company that does not pay monthly, while its one real payment was
discarded.** Realized income stayed correct throughout, since the era splice drops estimates after
`ibkr_from`; the damage was confined to the forecast, which is why it survived a month unnoticed.

Three things close it:

- **`app/cli/purge_dividend_estimates.py`** — deletes a security's estimates, never an `ibkr` row (those
  carry real withholding and no mapping can invalidate them). `--dry-run`, ambiguous symbols refused,
  records `manual_dividend_purge`. Run on prod 2026-07-30: 2 rows, income unchanged to the cent, forecast
  went 5 payouts → 0. **Nothing projected is the correct outcome** until a genuine series exists.
- **`disable --purge-prices` now purges estimates too**, and `set` warns when a ticker change makes
  existing estimates suspect. `list` flags `DIVIDENDS PREDATE MAPPING`.
- **`find_dividends_predating_their_mapping()`** runs after every market-data sync, warning when a held
  security's estimates were computed before its mapping's `updated_at`. That comparison is why
  `ticker_mappings` gained `created_at`/`updated_at` — it had **no timestamps at all**, which is what made
  "did this data come from the current ticker?" unanswerable for months.

The identity is `(security_id, source, ex_date)`, not `(security_id, ex_date)`: the column holds an
ex-date for yfinance rows and the **pay** date for IBKR ones, and one shared slot let them overwrite each
other whenever a payer's lag landed on another record's date — producing a single row with IBKR's gross
and the estimate's per-share. Mastercard's 29-day lag already exceeds a monthly cycle. The downgrade in
`o8d5f2a9b3c4` refuses (before any DDL) when cross-source same-day rows exist, because re-narrowing a key
over data the wider one allowed is lossy.

### The forecast — four rules that were each a bug first

**Size from `amount_per_share`, not from income received.** The payout schedule belongs to the company,
not to how long we have held it. Keying on realized income meant a payer bought weeks ago looked like a
non-payer, and **only 15 of 36 held securities could be forecast** — TSMC, Samsung, SK Hynix, HPE and the
**SOXQ ETF** each had 20–59 per-share records and projected nothing. Now 20 payers project.

**Infer cadence from ONE dated series.** The same dividend is stored twice — yfinance under its ex-date,
IBKR under its pay date, weeks apart — which halves the apparent gap: ASML's quarterly schedule read as
74 days, 5 payouts a year instead of 4. Deduplication cannot fix it, because Mastercard's ex-to-pay lag
of 29 days exceeds a monthly payer's whole cycle. Where yfinance rows exist (`amount_per_share is not
null`, ≥2 of them) they alone define the schedule; IBKR rows still supply the net amounts.

**The cost of that rule, and the guards added 2026-07-30:** the chosen series is trusted absolutely,
*including over the IBKR rows it then discards*. So two bad estimate rows can define a schedule outright
— which is exactly what SBI did (see *A wrong mapping poisons dividends too* below). The rule stays,
because the alternative resurrects the double-count; what changed is that a thin or suspect inference now
declares itself. `forecast_samples` and `forecast_cadence_days` ride on each breakdown row (badged at
n≤2), and `find_dividends_predating_their_mapping()` warns when the rows came from an older ticker.
**Earlier revisions of this file cited "SBI's monthly read as 28 days" as an example here. That was the
poisoned data, not a real schedule — don't reinstate it.**

**Step by the calendar.** Dividends pay on a day of the month, so a fixed day-step drifts — 31 days gives
a monthly payer 11 payouts a year instead of 12, and 91 days walked a quarterly payer from the 15th to
the 14th to the 13th. A gap near a calendar period snaps to it (`CALENDAR_PERIODS`), keeping the
schedule's own day and clamping at month end; anything else keeps day-stepping.

**`days_held_in_ttm` is the union of the lot intervals inside the trailing year**, not
`as_of - min(open_date)`. Those agree only while a holding is unbroken, and diverge in the one case
the flag exists for: sell out entirely, rebuy months later, and the older form still reported a full
year because the *first* purchase was a year ago — so a yield built from two partial stretches of
income was presented unqualified. Intervals are `[open_date, close_date)`, matching the
exclude-on-close convention, and overlapping lots are merged so three lots open across one month
count as one month held rather than three. Pinned from all three sides in
`tests/test_dividend_breakdown.py`: the gap case, the overlap case, and a continuously-held position
that must keep reporting full coverage.

**Judge staleness from *now*, not from the horizon.** The stopped-payer guard compares against `as_of`,
because the distance to a future horizon is a property of the question. Otherwise asking about 2027 made
every payer look stopped and returned an empty year.

`forecast_basis` reports which amount was used: `net` when a dividend has actually been received (net of
withholding), `gross_estimate` when only yfinance's gross per-share exists — the latter runs a little
high and the UI badges it. Future years are selectable (`years` offers `as_of.year + 1`) and a future
year is forecast in full rather than from today.

**Accumulating ETFs correctly show nothing** — DBPG, EMIM, IWDA, SXR8, VWCE, XAIX, XNAS (the `1C`/`ACC`
suffixes), alongside genuine non-payers (AMD, Amazon, Arista, NU, Credo, Ondas). Verified rather than
assumed: each has 600+ cached prices, so the Yahoo ticker resolves and the empty dividend series is real.
**Don't "fix" their absence.**

**Forecasts are inferred, because nothing forward-looking is cached** — no announced dividends
anywhere, and the fundamentals/earnings tables carry no dividend fields. `dividend_forecast.py` is a
pure module (no DB, no network, fast unit tests): cadence is the **median gap** between recent
payments, the amount the **median** of recent payments scaled to the current holding — median so one
special dividend doesn't inflate every projection. It refuses rather than guesses: nothing held, fewer
than two payments, a gap outside 20–400 days, or a payer that has already skipped ~2.5 cycles all
project nothing. IBKR rows carry no `amount_per_share` and a `0` `shares_held` sentinel, so the
scaling falls back to shares held at the pay date, then to the unscaled amount.
Tests: `tests/test_dividend_forecast.py`, `tests/test_dividend_breakdown.py`.

**One projection pass, sliced per consumer.** `_forecast_inputs()` assembles the cadence/per-share
inputs once, and `project_dividends()` then runs a single wide horizon (to the end of *next* calendar
year) which each reader filters: the chart, the rolling next-12-months figure, and the per-year
comparison. This is safe only because the projection steps deterministically from the last known
payment, so a wide projection sliced to a window equals projecting that window directly.
**The chart's reach is deliberately narrower than the projection's** — without a selected year it
still stops at 31 December. Coupling the two tripled the all-time chart's forecast total (46 → 162)
by pulling next year's payments into it.

### The forward yield — the portfolio's dividend rate

`forward_yield` on `/api/dividends/breakdown`, rendered as the *Dividend Yield* and *Yield on Cost*
cards on the Performance tab (they replaced *Effective Holdings*, which moved into the *Top 5 Weight*
footnote). Projected next-12-month income over market value, and the same income over cost basis.

**It reuses `growth.next_12m_eur`'s projection rather than a Yahoo dividend field, and that is not a
shortcut.** Nothing stores one: `dividendYield` / `dividendRate` / `payoutRatio` appear nowhere in the
backend, and `fundamental_metrics` has no dividend column. Adding one would need a migration plus a
fundamentals pass to populate — that table is written only on demand, so the cards would read blank
until someone spent ~5 Yahoo requests per security — and it would put a *second* annual-dividend-rate
implementation beside the forecast, which is this file's opening warning. `yfinance`'s
`dividendYield` has also silently changed scale (fraction vs percent) between releases.

**Weighting is arithmetic, not code.** A market-value-weighted average of per-security yields *is*
Σ(income)/Σ(value) — `Σ (Vᵢ/V × Dᵢ/Vᵢ) == Σ Dᵢ / V`, every non-payer entering at zero. So the service
divides two figures it already holds and nothing is weighted by hand. `forward_yield_pct` on each
breakdown row is the audit of the headline; the *Fwd yield* column on the Dividends tab shows it.

Four rules, each of which would be a wrong number the other way:

- **An unpriced holding is excluded from both sides.** `portfolio_service` values a position with no
  cached price at 0.00, so leaving it in adds its projected income to the numerator and nothing to the
  denominator — reading the yield **high**. The SBI shape, and the same refusal `rebalance.ts` makes.
  Its cost basis *is* known and is dropped anyway, because the gap between the two cards is only
  readable as appreciation while both denominators cover the same securities. Counted as
  `unpriced_holdings` and named on the card.
- **A zero numerator yields nothing, not 0.00%.** The whole object is `None`. Three states produce a
  zero — no projection was run, nothing held has a schedule, or the one security that does is unpriced
  and was excluded — and a 0.00% reports all three as *this portfolio pays no dividends*. The last is
  the dangerous one, and it is what a green suite served first: the smoke fixture's only forecaster is
  its unpriced TSMC row.
- **`paying_holdings: 0` is the same lie in miniature**, which is why absence is carried by the object
  and not by its fields. A count of 0 of 0 holdings on an account with 36 is worse than no answer.
- **It is a sibling of `growth`, not a member.** `growth` is defined as derived from the unwindowed
  payment *history*; a figure that moves with a market price is neither growth nor history, and
  `DividendKpiCards` — which answers *is this growing* — must not be handed a valuation ratio. Both are
  year-invariant, and `test_api_smoke.py` pins both from either end.

**Yield on cost is forward-over-cost, and was trailing-over-cost until 2026-08-05.** That is the same
numerator as the yield beside it, so the gap between the two is the holding's own appreciation and
nothing else. The old definition divided *income already received* by the *current* cost, and those
describe different positions the moment the position size changes inside the window:

- **Adding to a holding** divides a small position's income by the finished position's cost, so the
  figure reads far too low. Live on **nine of fifteen rows** when it was found — MCO showed 0.35%
  against a real forward 0.84%, SPGI 0.53% against 0.93% — and *unbadged*, because the `†`
  partial-coverage marker was only ever on the trailing yield column, never on yield on cost.
- **Selling and rebuying** is the same defect at its most extreme: the income was earned on shares
  bought cheaply and then divided by the cost of the shares that replaced them, so the result is
  neither the old holding's yield nor the new one's. Under the current definition a rebuy at a higher
  price lowers yield on cost to exactly the new cost's rate, which is the honest answer — more capital
  committed for the same income.
- **Trimming** runs the error the other way: the full position's income over the remnant's cost.

It also silently disagreed with the Performance tab's *Yield on Cost* card, which has divided the
forward projection by cost since it shipped — one name, two definitions, on two screens.
Pinned equal on a single-security book by `test_the_row_and_the_card_agree_on_yield_on_cost`.

`basis` is the same three-way flag as elsewhere (`net` | `mixed` | `gross_estimate`) with
`gross_estimate_eur` quantifying it, because a projection sized from yfinance gross per-share deducts
no withholding — and a yield is the figure most likely to be checked against a broker's own, which
quotes gross. The card shows it as *projected, part gross* in the **footnote**: a caveat reachable only
by hovering does not exist on a touch device, which `DividendsTab` already learned once.

Note the deliberate asymmetry on a row: `forward_yield_pct` always covers the next twelve months while
`forecast_net_eur` beside it is bounded by the selected window, so a row can show **no forecast and a
real yield** when its next payment falls past the year being viewed. Don't reconcile them — keying the
yield to the window would make it read 5/12 of the truth when asked in August.
Tests: the forward-yield block in `tests/test_dividend_breakdown.py`.

### Growth — MoM / YoY, and the five ways it lies

`growth` and `upcoming` on `/api/dividends/breakdown`, rendered as the KPI strip, the per-year panel
and the calendar (`DividendKpiCards`, `DividendYearComparison`, `DividendCalendar`, `DeltaChip`).

Computed from the **unwindowed** history, deliberately: with `?year=2026` the response carries no
2025 months, so no client could derive year-over-year at all. It is byte-identical whichever year is
selected, and pinned that way. Keeping it server-side also keeps the era splice and the per-date FX
projection in one place instead of growing a second implementation to drift.

**Rolling 12 months leads; raw MoM cannot.** This account's payers are quarterly, so March pays and
April does not: month-over-month swings ±90% on cadence alone and says nothing about the portfolio.
MoM survives as a labelled figure on the latest realized month and in the chart tooltip.

Each of these was a wrong number before it was a rule:

1. **YTD is compared day-for-day.** Jan 1 → today against Jan 1 → *the same calendar day* last year.
   Measuring a part year against a whole prior year turned +527% into +99%. 29 February has no
   counterpart, so `_same_day_last_year()` falls back to the 28th.
2. **The first year of income is coverage-limited.** It starts whenever the first dividend landed, not
   in January — seven months here — so its successor's percentage overstates growth and carries
   `yoy_vs_partial` (badged `†`). The first year itself gets no percentage.
3. **The TTM comparison straddles the era splice.** The current 12 months are IBKR actuals while the
   prior 12 are yfinance estimates: comparable in size, not in provenance. `ttm_crosses_era` says so
   rather than presenting a change of source as growth.
4. **Forecast is never silently compared against measured.** `next_12m_vs_ttm_pct` and any annual row
   containing projection are marked `est.`.
5. **A zero base yields `null`, never a percentage.** Growth against zero is undefined, not large, and
   a quarterly payer produces zero months constantly. `_pct()` returns None and the UI renders a dash.
   Per-month growth is realized-only for the same reason — a projected month's "change" would be an
   artifact of the forecast's own flat median.

The per-year panel is the one surface that mixes measured and projected into a single bar, so with the
Forecast toggle **off** it rebuilds from realized income alone (`lib/dividendGrowth.ts`), which is the
only client-side growth arithmetic and copies the server's two rules exactly: adjacent years only,
never divide by zero. Tests: `tests/test_dividend_growth.py`, `src/lib/dividendGrowth.test.ts`,
`src/lib/delta.test.ts`.

---

## Contributions — money in per month

`GET /api/portfolio/contributions` → `PortfolioService.get_contributions()`, rendered as a slim strip
(`ContributionsStrip.tsx`) below the KPI cards: all time / 12M / 6M / 3M, each trailing window shown as
a delta against the all-time average, so a change in savings rate is visible at a glance.

**`money_in_eur` is the answer**, and it is **spliced at `coverage_from`** because no single source is
authoritative for the whole history. This design took three attempts; the reasoning below is why.

### The splice

| Era | Source |
|---|---|
| before `coverage_from` | **lot cost basis** (`Σ cost_basis_eur` of lots opened) |
| from `coverage_from` | **real deposits** (`cash_flows`, `DEPOSITWITHDRAW` only) |

`money_in_method` reports which applied: `deposits` \| `spliced` \| `deployed`.

Lot cost basis reaches back through the pre-IBKR years because the early-2026 portfolio transfer from
Scalable Capital and Trading 212 carried every lot across with its **original `openDateTime` and original
cost basis** — verified, not assumed: securities have as many distinct `costBasisPrice` values as they
have lots (DBPG 75 lots / 72 prices, XNAS 110/107, XAIX 54/54), which a transfer-date re-basing could not
produce. Lots then survive indefinitely because reconciliation deletes **open** lots only and splits a
partial sale **pro-rata** under the original `open_date`.

Deposits take over the moment a ledger exists, because **lot cost basis cannot survive a rotation**:
selling one ETF to buy another closes lots and opens new ones for the same money, so it is counted twice.
That is not hypothetical — the Ireland-domiciled sleeve is being switched to US-domiciled ETFs for tax
reasons. Simulated on the real lot set (every EUR lot rotated into a USD one, no new cash), `money_in`
held **exactly flat in all four windows** while deployment roughly **doubled** all-time and went up
**~4x** over 3M — the shorter the window, the worse the distortion, because the rotation fills more of
it. A deployment-led headline would have claimed four times the real 3-month contribution. Pinned by
`test_a_rotation_does_not_inflate_money_in`.

**No double-count at the boundary**, because it is a single date: lots are summed strictly `< coverage_from`
and deposits strictly `>= coverage_from`. A purchase funded by a deposit in the boundary month contributes
the deposit only. The two ranges also leave no hole, which is why the divisor is the window's **full**
elapsed months and every window carries a meaningful number.

`coverage_from` lives in `app_settings` (`cash_flows_covered_from`), set from the statement's `from_date`
and **only when deposit rows were actually present** — an export taken without the Deposits option must
not claim coverage it has no data for. It only ever widens backwards
(`widen_cash_flows_covered_from`), so neither a later statement nor the 2026-07-31 switch to a
30-day window can shrink what a prior-year import established — the narrower `from_date` is simply
ignored, which is what kept `coverage_from` at 2026-01-09 through that change. It must be the period start, not the first deposit's date: a covered week with no deposits is
still covered, and using the first row would hand that week's purchases to the lot side *and* count its
deposits.

**But the period start is a claim, not evidence, and `get_contributions()` clamps it forward to
`CashFlowRepository.earliest_flow_date()`** — the first row the ledger holds, of *any* type. **The account
is younger than the statement that reports it**: a YTD query in the first year begins on 1 January while
the account was funded weeks later, and in that gap the deposits table is empty because the money was
still going to the previous broker. Believing the claim drops those purchases from **both** sides — past
the lot cutoff, with no deposit standing in for them — so they vanish from money in with nothing
reporting it. Clamping hands the gap back to lot cost basis, which is the right source for any era the
ledger doesn't reach.

Two details. It keys on the earliest row of **any** type, not the earliest deposit: an account opened by
an in-kind transfer can trade before any cash is deposited, and anchoring on the first deposit would
leave that window on the lot side where a rotation inflates it. A transfer is never money in, but it *is*
evidence the account existed. And the clamp is applied at **read** time rather than stored, so it needs no
migration, a later YTD sync can't undo it, and a prior-year import — planned for the 2025 tax backfill —
can't silently move the boundary back into an era the ledger has nothing for.

Do **not** "simplify" this by splicing at the transfer date instead. Deposits into the new account
routinely start *before* the positions arrive, and those deposits fund purchases made after it; a
transfer-date boundary drops them from the deposit side while their lots sit past the lot cutoff. On this
account that is the larger error of the two. Tests:
`test_coverage_cannot_start_before_the_ledger_has_any_row`,
`test_a_transfer_row_alone_anchors_the_ledger_start`.

### `deployed_eur` — secondary, and deliberately still shown

Cost basis of lots opened, the old headline. Once rotation starts it exceeds `money_in_eur`, and **that gap
is the useful part**: it is capital churn, not saving.

**Do not promote it back, and do not "fix" it by averaging `net_eur` instead.** Both were tried. Averaging
net moves the error rather than removing it — a window then gets debited for a sale of something bought
*before* it began. The two are duals; `net_eur` survives only for the tooltip and the identity check.

### Transfers are never money in

An incoming transfer moves capital saved years earlier somewhere else, and the transferred lots already
carry their own `open_date` — so counting it would both invent savings in a month that had none *and*
double-count purchases already recorded. `CashFlowRepository.get_deposits()` therefore selects
`flow_type == DEPOSITWITHDRAW` by **whitelist**, so no new transfer-ish type can leak in.

IBKR may book a transfer's cash leg as an ordinary "Deposits & Withdrawals" row. `persist_cash_flows()`
catches that by matching `(flow_date, amount, currency)` against the `<Transfers>` rows — exactly, never
on description text — and reclassifies, reporting it in `warnings[]`. Zero-cash (in-kind) transfers are
left out of the match keys, or every no-cash transfer would collide on `(date, 0)`.

**This is the highest-risk number in the feature**: an unexcluded transfer shows a portfolio-sized fake
contribution. `app/cli/manage_cash_flows.py` is the manual override — `list` marks which rows count as
added, `reclassify <ib_key> --as TRANSFER_IN` fixes one, `--dry-run` on the mutating path, and every
edit records a `sync_runs` row (`manual_cash_flow`).

Three things this account's real data settled, so nobody re-investigates them:

- **The 2026 transfer was entirely in-kind** — all 22 rows carry `cashTransfer=0`. So there is no
  transfer cash to misclassify and `deposits_reclassified_as_transfer` is legitimately **0**. Read a zero
  there as correct, not as the guard failing to fire.
- **`Transfer.type` arrives as `FOP`** (Free Of Payment), which ibflex's `TransferType` enum
  (`INTERNAL`/`ACATS`) cannot convert, so the sanitizer drops it and `transfer_type` is always
  `'UNKNOWN'` here. `_transfer_to_flow` leaves `UNKNOWN` out of the description. **Do not extend the
  enum** — same reasoning as everywhere else in the sanitizer.
- **`deliveringBroker` is not modelled by ibflex** either, and `company` comes through empty, so a
  transfer row cannot name Scalable Capital / Trading 212. `direction` *does* survive, which is what
  `TRANSFER_IN` and `earliest_transfer_in_date()` depend on.

### Currency

Every amount is stored EUR-converted at its own date (`cash_flows.amount_eur` via
`convert_to_eur(amount, currency, flow_date)`, `taxlots.cost_basis_eur` at `open_date`) and projected into
the base currency once at read time by `BaseFx` — deployment at each lot's `open_date`, deposits at each
flow's `flow_date`. A **zero amount skips conversion entirely**: zero is zero in every currency, and
demanding an FX rate would drop the in-kind transfer rows, which are exactly the ones with no cash. An
unconvertible non-zero currency skips that row with a warning rather than failing the sync.
`test_base_currency_projection_scales_both_metrics` pins the CHF path, since the tests otherwise run on EUR
while production runs on CHF.

### Shared mechanics

The divisor is **clamped to elapsed history** (`partial: true` when clamped), so a
four-month-old portfolio can't report a 12-month average divided by 12; all-time divides by exact days,
not whole months, so a part-month isn't rounded away. `as_of` is injectable purely so tests can pin the
windows. Cash-flow ingestion is a pure additive upsert with no delete, so it needs **no** empty-statement
wipe guard, and an unconvertible currency skips one row rather than failing the sync.

The cheap correctness check: `Σ monthly[].net_eur` must equal the current total cost basis, since every
lot is either still open or was released. **Exact in EUR; approximate once projected.** Each leg is
converted at its own date, so a lot bought and sold months apart contributes `+convert(cost, open_date)`
and `−convert(cost, close_date)` — which cancel to zero only if the rate didn't move. Under CHF the
residual is a fraction of a percent of *closed* cost basis and grows with FX drift, not with error. So
run the identity against `taxlots.cost_basis_eur` (`Σ` of open lots) when you want it to the cent, and
read a small non-zero gap in the base currency as FX, not as a dropped lot. Tests:
`tests/test_contributions.py`, `tests/test_cash_flow_ingest.py`.

---

## Sync schedule (Europe/Berlin)

| Time | ET | Job | Touches Yahoo? |
|---|---|---|---|
| 08:00, 11:00, 13:00, 15:00, 20:00, 22:00 | | `market_data_only_sync_job` (7d) | yes |
| **18:00** | **12:00** | `full_sync_job` — IBKR + FX + 730d market data + dividends | **yes** |
| 00:00 | 18:00 | `ibkr_only_sync_job` — IBKR + FX, **skips unless 18:00 failed** | no |

**Yahoo is repriced at seven hours — 8, 11, 13, 15, 18, 20, 22 — and that set has not
changed** since the 2026-08-04 widening. What moved on 2026-08-08 is only *which job* makes the
08:00 and 18:00 touches: the 730-day pass travelled with IBKR to 18:00, and 08:00 became a plain
7-day slot. Don't read the reshuffle as a change in pricing coverage.

**Only one IBKR slot can succeed per ET day, so the second one now asks first.**
`app/services/flex_generation.py` holds the rule: `last_generation_today()` reports whether a
successful Flex sync already landed in the current **US-Eastern** calendar day, and
`SchedulerService.sync_ibkr_data(force=False)` returns `status="skipped"`,
`reason="already_generated_today"` without touching the network when it has. So 00:00 Berlin is a
no-op on a normal day and a real recovery attempt on a day 18:00 failed — the case that saved the
data on 2026-08-02 and 08-03, when the day's first attempt failed and a later one succeeded.

Three details are load-bearing:

- **`ibkr_manual_xml` does not count as a generation**, and that is measured rather than assumed:
  the browser download and the Web Service are independent channels, so an offline ingest spends
  nothing. Twice in this account's history an offline ingest was followed by a *successful* API
  generation in the same ET day (07-28: 00:16 ET then 02:05 ET; 07-31: 00:07 ET then 02:07 ET).
  Counting it would suppress the day's real attempt on exactly the days someone had just recovered
  by hand. `FLEX_API_SYNC_TYPES` excludes it; `IBKR_SYNC_TYPES` — which answers the *different*
  question "was the data refreshed?" — includes it, and is defined as an extension of it so the two
  cannot drift into separate literals.
- **A failure does not spend the day either.** The guard keys on a *successful* run, so a day full
  of `1001`s leaves every later slot free to try.
- **The job's status becomes `skipped`, not `success`**, so `find_stale_ibkr_sync` (which counts
  successes) is untouched by it. `trigger_sync_now` had to be narrowed at the same time: it raised
  `SyncBusy` → 429 on any `skipped` status, which had only ever meant a pipeline collision, so it
  now keys on `reason == "pipeline_busy"`. A day with nothing left to sync is finished, not busy.

**The hours live in one place** — `IBKR_ONLY_HOURS` / `FULL_SYNC_HOUR` / `MARKET_DATA_HOURS`, and
`ALL_SYNC_HOURS` over them — because three other files carry a copy: `ops/auto-deploy.sh` defers a
deploy that would land in a slot, and both `ops/finish-deploy.*` twins warn a human before pushing.
All three had drifted by 2026-08-04, the finish-deploy pair for four days, in the direction that
*permits* a collision: they warned about the retired 13:00/20:00 and never mentioned the live
00:00/06:00. `tests/test_deploy_guard_hours.py` reads all three against `ALL_SYNC_HOURS` and
`test_the_registered_slots_are_exactly_the_declared_ones` checks the triggers come from it, so the
chain is closed end to end. Whole hours only — the guards reason in hours, so a half-hour slot
could not be expressed on their side and would run unprotected.

The two IBKR-only jobs exist because a transient `Code=1001` at 08:00 used to cost a full day of
freshness. They deliberately **skip** market data and yfinance dividends — see rule 1. Pinned by
`tests/test_scheduler_jobs.py`. Status: `GET /api/scheduler/status`.

**Market data was repriced three times a day until 2026-08-04 and now runs seven times, five of
them mid-session.** Before, a value read mid-morning could be seven hours old, and Xetra's *close*
was never captured at all: the job named "after EU close" ran at 15:00, 2.5 hours before the 17:30
close. The worst gap inside either session is now ~2.5h. **This is only correct because a recent
close is re-fetched** (`PROVISIONAL_PRICE_DAYS`, below) — without it an earlier slot freezes an
*earlier* price and makes the number worse rather than fresher, so the two changes cannot be
separated. Coverage is pinned as a property (`≤3h between slots, plus one after each close`) rather
than as a list of hours, so re-timing a slot stays free while dropping back to two a day does not.

Cost, since rule 1 makes this the question: **one Yahoo request per security per pass either way** —
the refresh only widens a range on a request already being made. 40 securities × 7 passes ≈ 280
requests/day at ≤48 in any hour, against a documented ~500–2,000/hour, and the ~7.5s per-security
pacing keeps a pass at ~8 requests/minute, under the ~10–20 burst tolerance. Slots are ≥2h apart, so
two passes never share an hour. The scheduled *benchmark* warm-up deliberately does **not** refresh —
it loops all eight warm benchmarks 1-2s apart, so doing it seven times a day would multiply that
burst for a value nobody read; the chart's own lazy fetch refreshes the one being viewed.

**A rate limit now abandons the rest of the pass.** This file already credited
`market_data_service.py` with "rate-limit detection that aborts the run" and it only ever aborted the
ticker *variations* for the security in hand — the caller logged a failure and moved on to the next of
40, asking the same IP again seconds later. `MarketDataService.rate_limited` latches on the first 429
and `sync_market_data` breaks, reporting `rate_limited: true` plus a `warnings[]` line. What was
already written stays written and the next slot resumes, since the dates it never reached are simply
still missing. That mattered little at three passes a day.

**Every IBKR attempt must sit outside US market hours, and this is measured rather than assumed.**
IBKR builds a Year-to-Date statement from *finalised* daily data, so `SendRequest` succeeds overnight
and fails mid-session — the failure surfaces as `Code=1001` **at the request step**, which is the
fatal-fast kind, not the "keep polling" kind. This account's own `sync_runs`, read on 2026-07-31:

| Berlin | ET | ok/total | |
|---|---|---|---|
| 00:00 | 18:00 | 1/1 | after the US close |
| 06:00 | 00:00 | 2/2 | |
| 08:00 | 02:00 | 4/5 | |
| 09:00 | 03:00 | 1/1 | |
| 13:00 | 07:00 | **0/6** | pre-market |
| 20:00 | 14:00 | **1/8** | mid-session |

Overnight 8/9; afternoon and evening 1/15. The retries used to sit at **13:00 and 20:00**, where they
were not merely weak but **negative**: every failure is a failed *generation*, and failed generations
are exactly what `Code=1025` counts. Two jobs whose purpose was protecting freshness were spending
lockout budget twice a day to recover nothing. They moved to 00:00 and 06:00 on 2026-07-31.

That argued for keeping every IBKR slot inside roughly **22:00–09:00 Berlin**, and it held until
2026-08-08, when the account owner moved the primary slot to 18:00 Berlin (12:00 ET) — see
*The 18:00 Berlin slot* below. `test_ibkr_jobs_run_at_the_declared_hours` now checks an explicit
allowlist rather than that range, so an hour still cannot drift unnoticed; it simply records a
deliberate exception instead of a rule the schedule no longer follows.

**But the hour is the weaker constraint. IBKR generates this statement about once per ET
calendar day, and every attempt after the day's success is refused with `Code=1001`.** Read
off `sync_runs` on 2026-08-08, with all three slots sitting inside the safe overnight window:

| ET day | 00:00 ET (06:00 Berlin) | 02:00 ET (08:00 Berlin) | 12–13 ET (manual) | 18:00 ET (00:00 Berlin) |
|---|---|---|---|---|
| 08-01 | **success** | error | | error |
| 08-02 | error | **success** | error | error |
| 08-03 | error | **success** | error | error |
| 08-04 | **success** | error | | error |
| 08-05 | **success** | error | | error |
| 08-06 | **success** | error | error | error |
| 08-07 | **success** | error | | error |
| 08-08 | **success** | error | | |

Exactly one success per day, twelve days of twelve, always the earliest attempt that works and
everything after it refused. The one two-success ET day in the whole history is 07-31 — the
day the query definition was edited, which appears to reset it, and the reason both the guard
and the manual endpoint keep a `force` escape hatch.

Note the manual column: those are Sync-button presses, and they are why this read as "the Flex
query always errors out" from the UI. **The code enforces the rule now** — see the guard under
*Sync schedule* above.

**This subsumes the mid-session reading rather than contradicting it, and that is the part
worth understanding.** The 07-31 table above is equally well explained by "the later slots had
already spent the day's generation": 08:00 went 4/5 while 13:00 and 20:00 — both *after* it —
went 0/6 and 1/8. Two theories, one dataset, because the mid-session slots were also the
later ones. What discriminates is 08-01 onward, where every slot is overnight and only one
still succeeds. So the hour rule stays as evidence (a midday slot is bad for both reasons), but
**adding IBKR slots does not add freshness** — it adds failed generations, which is exactly what
`Code=1025` counts. The account has two slots and can use one.

The concrete consequence used to be that **the earliest job starves the later ones**, which is
why `full_sync`'s market-data half must not be gated on its IBKR half. Since the guard, the later
ones skip instead of failing, but the gating rule stands for its own reasons.

### The 18:00 Berlin slot — chosen against the evidence, on purpose

The primary IBKR slot moved from 06:00 to **18:00 Berlin (12:00 ET)** on 2026-08-08 at the account
owner's request, reaffirmed after the trade-off was put to them twice. It is written down here
because the schedule now contradicts the measurements above, and the next person to read a run of
`1001`s must know this was a decision rather than a drift:

- **It captures no additional trades.** The window ends yesterday *measured in US Eastern* and rolls
  at midnight ET, not at generation time — so 12:00 ET and 00:00 ET on the same day both cover
  D−3…D−1. Identical statement, ~12 hours later. Reaching *today's* trades needs a custom date range
  set by hand in the portal, which is not a schedule change.
- **12:00 ET is mid-session**, the band where the table above shows 13:00 Berlin 0-for-6 and 20:00
  Berlin 1-for-8. It is unproven at this specific hour.
- **Attempts drop from three per ET day to two**, both in the weaker band, against a 3-day window
  whose entire margin is two consecutive failed days.

What makes it survivable is that the guard reduces the cost of a doomed attempt to zero, and that
`find_flex_generation_gap` (below) now alarms in time to act. **If generations start failing, moving
the primary back to 06:00 Berlin — the instant the window rolls — is the fix.**

**`find_flex_generation_gap` warns after `FLEX_GENERATION_GAP_WARN_DAYS` (2) ET days with no
successful IBKR sync**, and it exists because `find_stale_ibkr_sync` at 7 days *cannot see the
failure it was written for* once the query period is 3 days: trades fall out of the window after
two missed days, four days before the 7-day alarm says a word. Warning at 7 about a 3-day window is
warning after the loss. It counts in **ET days** rather than elapsed hours — a failure at 23:00 ET
and one at 01:00 ET the next day are two missed generations two hours apart — and it runs from the
**market-data** job for the same reason its sibling does: those slots succeed while Flex is
refusing. Re-derive the 2 if the Flex Query period changes; it is N−1, not a preference.

**`full_sync` runs its 730-day market-data pass whether or not IBKR succeeded, and gating it
was a silent outage (fixed 2026-08-07).** `_full_sync_job_locked` used to wrap step 3 in
`if ibkr_result["status"] == "success"`, which reads as prudent — no new securities, no new
prices — and is wrong twice. It confuses two independent providers: Flex refusing to generate a
statement says nothing about Yahoo, and the securities needing prices are the ones already in
the database. And combined with the once-per-day rule above it made the deep pass the **rarest**
job in the schedule rather than a daily one: the 06:00 slot takes the day's generation, so 08:00
fails, so the 730-day backfill did not run between 2026-08-03 and the fix.

**What made it invisible is the thing to remember.** The six 7-day `market_data_only` slots run
unconditionally and keep *current* value fresh, so nothing on any screen looked wrong — only the
two-year history quietly stopped extending, and no surface reports the age of a backfill. The
failure was legible solely as `market_result: null` inside `details` on runs whose top-level
`status` was already `error` for an unrelated reason. A skipped step also reports **no
warnings**, so `find_stale_priced_securities` could not fire on those mornings either: an
unpriced holding discovered by the 08:00 pass was structurally unreachable exactly when IBKR
had refused.

Cost of decoupling: one Yahoo request per security on the mornings it now runs where it used to
skip — the span narrowing in `fetch_and_cache_prices` starts the range a few days before
`min(missing)`, so an already-backfilled security re-requests almost nothing, and only a newly
bought one pulls its full history. `status` still reports the **IBKR** verdict, so a green Yahoo
half cannot paper over a refused statement and `find_stale_ibkr_sync` still counts correctly.
Pinned from all four sides in `tests/test_scheduler_jobs.py` — it prices on failure, it still
reports the failure, it still prices *after* IBKR rather than before, and market warnings now
survive a failed IBKR half.

**Market data reprices at 13:00 and 20:00 and that is not those slots coming back.** The prohibition
is specific to Flex: Yahoo has no statement to generate and no `Code=1025` budget to spend, so a
mid-session market-data request is ordinary while a mid-session *IBKR* request is self-harm. The test
above keys on `IBKR_JOB_IDS`, not on the hour, for exactly this reason.

**Read "we suddenly get constant 1001s" as *we added slots that never worked*, not as a regression.**
The 13:00/20:00 jobs were introduced in `67e6a59` on **2026-07-25** — the same day `sync_runs`
persistence landed, so the oldest record we have (20:00:11 Berlin, a failure) *is* the first retry
ever attempted. Before that only the 08:00 `full_sync` ran, and its success rate has not moved (4/5
that week). Two changes arriving together — new failing slots and, for the first time, a record of
every attempt — read as IBKR getting worse. It had not.

That accounts for the *volume* of failures. It does **not** account for why `1001` started at all,
and the answer to that is the query itself:

| | Flex Query contents |
|---|---|
| before 2026-07-24 | **Open Positions only** |
| `6cccdab` 07-24 | + Trades, CorporateActions, CashTransactions |
| `86960aa` 07-28 | + Deposits & Withdrawals, Transfers |

**One section became six in four days, and five of them scan the whole YTD period.** Open Positions
does not — it is an as-of snapshot, which is why it never provoked this. The 08:00 `1001`s that
motivated adding the retries in the first place began the day *after* the first expansion. So the
chain is: sections added → `1001` appears → retries added at hours that can never work → `1001`
everywhere.

**Do not reason about statement cost from row counts.** Open Positions is ~70% of the *rows* (979
lots) and ~0% of the *scan work*. An earlier revision of this file used the row share to argue that
shortening the period would barely help; that was measuring the wrong quantity.

One hypothesis this does kill: **"failures accumulate into a throttle."** The autocorrelation is
*inverted* — the next attempt succeeds 5/15 after a failure and 0/5 after a success. That is
schedule position, not contagion: 08:00 follows a failed 20:00, and 13:00 follows a successful
08:00. A success 4½ hours after a failure (2026-07-26 00:30) rules out a cooling-off period.

**The period is `Last 30 Calendar Days` as of 2026-07-31, and that is what fixed it.** The evidence
is a clean A/B: a 20:00 failure and a 21:08 success 68 minutes apart, same token, same hour band —
15:08 New York, mid-session, where the day had gone 0-for-8. Statement shape dropped from ~290 trade
rows to 103 and ~107 cash transactions to 17.

It is safe because `reconcile_taxlots` reads trades from the *database*, ingestion is idempotent and
additive, `widen_cash_flows_covered_from` only moves the boundary earlier (a 30-day `from_date` is
ignored, so January coverage stands), and Open Positions is period-independent. Verified after the
switch: all 71 YTD trades still on record, `coverage_from` still 2026-01-09, 979 lots, 0 skipped.

**Do not restore the YTD period to "be safe".** That reintroduces the failure. What it bought — a
statement that re-delivers the whole year every time — is available on demand instead: a browser
download ingested through `app/cli/ingest_flex_xml.py` is idempotent, so it simply fills whatever a
bounded window missed. `find_stale_ibkr_sync` exists to tell you when to do that.

**One pipeline at a time (`app/single_flight.py`).** `/api/` is public — and was unauthenticated when
this was written; `app/auth.py` (below) can now gate the writes, but throttling and authorization are
different jobs and this one is still needed. Nothing stopped concurrent or rapid-fire triggers: APScheduler's `max_instances=1` only fences jobs *it*
dispatches, and `POST /api/scheduler/trigger` ran `full_sync_job()` as a bare coroutine outside the job
store entirely — so a stranger could overlap the 08:00 run or spam Flex requests toward a `1025`
lockout. Everything that can reach IBKR or Yahoo shares the `sync-pipeline` gate; two pipelines racing
is the failure mode regardless of which endpoint started them. Scheduled jobs enter with **no cooldown**
and, on collision, record a `status="skipped"` run rather than running concurrently (the next slot
recovers freshness). The public routes add cooldowns (ibkr 120s; market-data / trigger / fundamentals /
ratings / allocation / watchlist 300s) and answer **429 with `Retry-After`**. In-process by design —
single uvicorn worker, and the check-and-set has no `await` between test and set. A backgrounded route
(fundamentals `/sync`) checks `is_running()` in the handler and holds the lock inside the task, so the
gate spans the actual work rather than the enqueue. **`POST /api/watchlist` (add) is gated too** (60s
cooldown — a single-ticker fetch, lighter than the 300s bulk sync): it fires a per-add `force=True`
yfinance fetch and was the one Yahoo-triggering route the rollout missed, so rapid-fire adds of
distinct tickers were an unthrottled fetch storm. The add itself stays *outside* the gate — busy or
cooling, the row is created with `last_synced` null and the next sync fills it in, rather than a
running 08:00 job turning a bookkeeping action into a 429. Tests: `tests/test_single_flight.py`.

**`GET /api/portfolio/benchmark` is gated at the *fetch*, not the handler** (added 2026-07-30 — it was
the last route with no gate at all; `portfolio.py` never imported `single_flight`). It lazy-fetches
Yahoo and tiles Frankfurter on a cache miss, so looping the 8 keys in `BENCHMARKS` over the 5-year span
the route allows could run beside the 08:00 `full_sync`. It must **not** wrap the handler:
`sync_benchmark_prices()` only refreshes benchmarks that *already have rows*, so this route bootstraps
the warm set — a cache-only GET leaves a first-time selection empty forever, and gating the read would
429 the chart every morning. So the gate sits inside `_ensure_prices_available` /
`_ensure_fx_rates_available` around the network call, and `SyncBusy` serves what is cached.

Two consequences worth keeping straight. **Entering the gate bumps the shared last-start clock every
other route's cooldown reads**, which is why the gate wraps only the actual fetch — a warm chart load
must not 429 a manual IBKR sync. And **the gate cannot stop a sequential loop**, so each ticker and
currency carries its own `UPSTREAM_RETRY_COOLDOWN_SECONDS` (300) attempt memo: trailing weekdays the
provider has no bar for stay missing *by design*, so the range end is otherwise re-requested on every
request forever — the same shape the holiday rule fixed for `market_prices`. Keyed **per upstream
target**, because warming eight distinct benchmarks is legitimate and re-hitting one is not.
`reset_upstream_throttle()` exists for tests, since the memo is process-lifetime state.

`_ensure_fx_rates_available` also asks what is missing before tiling. `_batch_fetch_rates` issues its
request **unconditionally** (it dedups per row, *after* the response), so a five-year chart load cost
~60 provider requests every time regardless of the cache. It now uses the same holiday-aware
missing-days rule as the price path, extracted to `_missing_business_days()` so the two can't drift.

`POST /api/fundamentals/sync` finally carries the 300s this file already claimed for it. `is_running()`
fences only *overlapping* runs, so a poller that waited for each pass to end ran them back to back
indefinitely at ~5 Yahoo calls per security per pass. `cooldown_remaining()` lets a BackgroundTasks
handler answer 429 honestly instead of replying `"started"` to a run the background half then drops.

**Errors are redacted before they are stored or served (`app/redact.py`).** Flex sends the token as a
`t=` URL parameter and `requests` transport errors stringify with the full URL, so a plain `str(e)` from
a failed SendRequest carries it — and those went verbatim into `sync_runs.message`, which the public
`/api/scheduler/status` and `/history` re-serve forever. Production really did leak it (found and
scrubbed 2026-07-28; **rotate the token if this ever recurs**). `SyncRunRepository.record()` redacts on
write and `to_dict()` again on read, so rows written before the fix or restored from a backup can't leak
either; the routers redact their `HTTPException` details. The `q=` query id stays readable — public in
these docs and useless alone. Tests: `tests/test_secret_redaction.py`.

**A price that never arrives is otherwise silent.** `portfolio_service` values a position with no price
at **0.00** and moves on, so deleting SBI's poisoned prices took 446.93 CHF off the total with nothing
reporting it. `find_stale_priced_securities()` now runs after every market-data sync and warns when a
security **with open lots** has no cached price at all, or none newer than `STALE_PRICE_DAYS` (5 —
enough to absorb a weekend plus a holiday). Closed-out holdings are excluded: they legitimately stop
getting prices, and warning on them would be permanent noise.

**The timeline had the same silence, and it renders as a loss rather than a gap.** `find_stale_priced_securities`
guards the *current* snapshot; `/api/portfolio/value-over-time` drops an unpriced holding from
`market_value_eur` while its cost stays in `cost_basis_eur`, so the point understates by that holding's
whole value. Measured against production by querying dates past the last cached price:

| date | market value | gain/loss % | |
|---|---|---|---|
| today | 64,944 | +33.7 | correct |
| +7d | 64,944 | +33.7 | carried forward inside `PRICE_LOOKBACK_DAYS` (14) |
| **+14d** | **56,009** | **+15.3** | **partial — some securities still resolve, some are zero** |
| +15d | 0.00 | **−100.0** | every holding out of lookback: a fabricated wipeout |

**The partial row is the dangerous one**, by the same rule as the zero-for-unknown cards: `+15.3%` looks
like an answer, `−100%` looks like a bug. And this is precisely what a **stalled price feed** looks like
— a smooth decay to zero rather than a missing line — which is the failure mode where nobody is watching
the sync warnings either.

So each point now carries **`unpriced_holdings`**; anything above 0 means the valuation is incomplete. It
was previously only a `logger.warning`, which at up to ~29k lines for a 730-day window over 40 securities
is noise rather than a signal. Counted in **both** `_calculate_timeline_swept` and
`_calculate_daily_value`, and `test_timeline_equivalence.py` pins them equal — note the point query walks
tax *lots* while the swept one walks *securities*, so it counts a **set of ids**; incrementing would
report 110 for a holding split across 110 lots and break that equivalence. The field is declared on
`PortfolioValuePoint`, without which the `response_model` would have dropped it silently.

**`/api/portfolio/attribution` carries the field too, and it was the worst place it was
missing.** `get_eur_value` returned `0.0` when either the price or the FX rate was absent, so a
still-held position whose feed went stale read as **`-start_value`** — the exact shape the disposal
term was added to fix for *sales*, arriving by the other route and never covered. The mirror case (an
unvaluable start) fabricates a gain of the same size. This endpoint renders one bar per security, so
the fabricated figure is not buried in an aggregate: it is the largest bar on the chart, under the
security's own name. Two knock-ons made it worse — `weight_percent` divides by a `total_end_mv` the
zeroed holding is missing from, inflating every *other* security's weight, and `contribution_percent`
divides by a `total_pnl` the phantom loss moved.

Unvaluable securities are now **excluded from both sides** and counted, as the forward yield already
did. What makes exclusion safe is that a lot held at *neither* endpoint never reaches the valuation
helper, so a fully-sold position keeps its legitimate zero and is never confused with an unpriced one.
The notice sits **outside** the collapsible body: this card is collapsed by default and its collapsed
summary shows `total_pnl_eur`, the very figure the notice qualifies — a caveat you must expand a card
to reach is as good as absent, the same rule that put the dividend basis in a footnote rather than a
tooltip.

**`MonthlyReturnsHeatmap` was the same shape and was fixed on 2026-08-07**, which is what makes this
a rule rather than one card's detail. The table inside badged every trimmed figure `†` and explained
the dagger in a footnote — both *inside* the collapsed body — while the collapsed summary rendered
`Aug: +1.5% · YTD: +3.1%` with no marker at all. So the qualifier was present on the surface almost
nobody opens and absent from the one everybody reads. The summary now carries the dagger **and spells
the legend out inline** (`† part of the period only`), because the footnote that would explain it is
not rendered yet; and `cellTitle` names the days the figure actually covers, since on a trimmed figure
the wrong thing is the label, and "part of the period" alone cannot distinguish a lost day from a lost
half-year.

**The client acts on it in three places, and the metric ones matter more than the chart.** A pair spanning
a complete day and an incomplete one manufactures a move that never happened — 64,944 then 0 is **−100%
in a single day** — and `dailyReturnSeries` feeds *everything*: max drawdown, current drawdown,
volatility, Sharpe, Sortino. So a stalled feed did not merely bend the line, it moved the whole risk row
to match, with each number looking individually reasonable. `isMeasurable()` now disqualifies a pair with
either end incomplete, exactly as a flow disqualifies a day for beta: excluding it costs a point and
biases nothing, while keeping it invents one. `betaAndCorrelation` needs the guard **separately**,
because it derives its own returns rather than going through `dailyReturnSeries`.

**`computeModifiedDietzReturn` is the third, and it was missed when the other two were written** —
which is the part worth remembering, because "I guarded the consumers" was true and incomplete on the
same day. It behaves differently from them by necessity: Modified Dietz reads only the two **endpoint**
market values and the flows between, so an incomplete endpoint is not a small error but the entire
answer, while an incomplete *interior* day cannot affect it at all. Dropping a whole month over one
stale day would also lose far more than it protects. So it **trims** leading and trailing unmeasurable
points instead of refusing, which is exact rather than approximate — a true Dietz return over the days
it kept — and sets `partial` so `MonthlyReturnsHeatmap` can badge the cell `†` with a footnote. The
**YTD column is the one that matters**: it ends on *today*, precisely the day a stalled feed breaks.

The chart still *plots* those days — a hole in the line would be its own kind of lie — so
`PortfolioValueChart` renders a `role="alert"` naming how many days and how many holdings, and saying the
dip is missing price data rather than a loss.

**Absent means complete**, deliberately: the field only exists from 2026-08-05, so reading `undefined` as
unmeasurable would put a permanent warning on every chart served by an older backend. Same choice
`externalFlow` makes about its own optional field.

**`/api/portfolio/summary` carries the same field, and that is the more important one.**
`total_market_value_eur` is a sum over the holdings the backend could price — the *headline* figure, on
the hero card — and an unpriced holding leaves it while its cost stays in `total_cost_basis_eur`, so the
total and both gain figures understate. This is the SBI incident restated: deleting one security's
poisoned prices took 446.93 CHF off exactly this number and the only thing that said so was a sync
warning nobody has to read. `PortfolioSummaryCards` now renders a `role="alert"` above the row.

It comes off `_calculate_daily_value`, the same helper the timeline uses, so the headline and every chart
point agree about their own completeness instead of each deciding — pinned by `test_api_smoke.py`, which
asserts the summary's count equals the last timeline point's.

**Do not recompute this on the client from `market_price === null`.** The backend fails to value a
holding for *two* reasons — no price **or** no FX rate — so a client-side count under-reports, and
`currencyExposure.ts`'s own `unpricedCount` is a different (narrower) question about quote currency.

**A sync that never *succeeds* is silent in the same way.** Individually a failed IBKR run is
unremarkable — `1001` is routine and the schedule shrugs it off — so the thing worth alarming on is
the **absence of a success**, not any single failure. `find_stale_ibkr_sync()` warns after
`IBKR_SYNC_STALE_DAYS` (7) with no successful run of an `IBKR_SYNC_TYPES` sync. Three details carry
the weight:

- It runs from the **market-data** job, not an IBKR one. Market data succeeds while Flex is refusing,
  so the warning still reaches `warnings[]`; hanging it off the IBKR job would silence it in exactly
  the outage it exists to report.
- **`ibkr_manual_xml` counts as a success.** Ingesting a browser download genuinely refreshes the
  data, so the documented escape hatch from a token lockout must reset the clock — otherwise the
  alarm blares through the correct recovery and trains the reader to ignore it.
- An **empty** history is quiet (fresh install), but *attempts with no success ever* warns. Those are
  different states and collapsing them would either cry wolf on day one or hide a broken token.

This is what makes **shortening the Flex Query period** safe. Under Year-to-Date a gap costs only
freshness, because every statement re-delivers the year; under a bounded window trades that fall out
of it before a sync succeeds are gone from every future statement. Seven days against three IBKR
attempts a day is ~21 consecutive failures, so it cannot fire over a `1025` lockout (~14h).

`_collect_warnings()` hoists each step's warnings to the top of the job's result, because `_record_run`
reads `result["warnings"]` and a job's own dict never had that key — so warnings were being buried in
`details` and never rendered as warnings.

**The job store is persistent, and three details make it actually work.** APScheduler runs in-process, so
a `docker compose down` overlapping a Berlin slot used to drop that slot outright — which is what
happened to the 2026-07-30 08:00 `full_sync`. A `SQLAlchemyJobStore` (`settings.scheduler_jobstore_url`,
a **separate** sqlite file: the store is synchronous SQLAlchemy while the app is aiosqlite/WAL) plus
`coalesce=True` and `MISFIRE_GRACE_SECONDS` (1800) runs the missed job on startup instead.

- **The registered targets are module-level functions** (`full_sync_job_entry` and friends), not the
  bound methods they were. A persistent store serializes each job, and pickling `self.full_sync_job`
  drags the live `AsyncIOScheduler` in with it.
- **`_add_or_keep()` exists because `add_job(replace_existing=True)` recomputes `next_run_time` from
  now** — it would overwrite the missed timestamp on the way in and make the persistence pointless. An
  identically-triggered job is left alone; comparing `str(trigger)` is what lets a genuine schedule
  change still replace one. Both directions are pinned in `tests/test_scheduler_jobs.py`.
- **docker-compose mounts the store's *parent directory*, never the `.db` file.** Docker creates a
  missing bind-mount source **as a directory**, so `./scheduler_jobs.db:/app/scheduler_jobs.db` — the
  mount from 2026-07-30 to 08-01 — could only ever produce an empty directory at the database path
  and `sqlite3.OperationalError: unable to open database file` on every boot. sqlite creates the file
  but never its parent, which is why the URL lives inside `scheduler-data/` and
  `ensure_jobstore_parent()` runs first. Pinned structurally by
  `tests/test_scheduler_jobstore_path.py`, which reads the compose file — no runtime assertion can.

Thirty minutes is chosen from both ends: long enough for a `build --no-cache` rebuild, short enough
that a real outage doesn't dump four stale slots onto a cold container.

**The failure mode to fear here is that the fallback looks exactly like success.** When the store
won't open, `start()` degrades to an in-memory scheduler — deliberately, because an exception out of
the lifespan handler costs the whole site while losing misfire recovery costs one late sync. But the
fallback **re-registers every job**, so `/api/scheduler/status` reports a fully-armed scheduler
either way, and the sole symptom is one `logger.error` nobody had reason to read. It was inert for
two days behind exactly that appearance, while STATUS.md recorded it as verified working. `/health`
therefore reports **`scheduler_jobstore_persistent`**: if it is `false`, a deploy overlapping a
Berlin slot still loses that sync, whatever `/api/scheduler/status` says.

**`/api/` protections that are not `single_flight`.** Three middlewares in `app/main.py`, innermost
last, so a rejection from any of them still carries a correlation id:

- **`app/auth.py`** gates every `POST/PUT/PATCH/DELETE` under `/api/` on `settings.api_admin_token`
  (`X-API-Key`, or `Authorization: Bearer`, compared with `secrets.compare_digest`). It is
  **middleware, not a per-route dependency**, deliberately: every router takes only `Depends(get_db)`,
  so a dependency would have to be added to ~14 routes and remembered on every route added later —
  keying on the HTTP method means a new `POST` is covered the moment it exists, and
  `test_every_mutating_route_is_covered_without_being_annotated` walks the live route table to prove
  it. **Empty token = disabled**, so shipping it could not 401 the running site; startup warns loudly
  while it is off, the same treatment `SCHEDULER_ENABLED` gets. Reads stay open because the frontend
  has no login and gating them would black out the UI.
- **`app/rate_limit.py`** is a fixed-window per-client counter (`RATE_LIMIT_PER_MINUTE`, 0 disables).
  `single_flight` fences the sync *pipelines*; nothing bounded the expensive anonymous reads. Keyed on
  the first `X-Forwarded-For` entry, since nginx makes `request.client.host` always loopback — forging
  it only splits the forger's own bucket. `/health` is exempt: the deploy script polls it.
- **`app/observability.py`** stamps `X-Request-ID` (reusing a plausible inbound one so it correlates
  across a proxy) and installs the handler for unhandled exceptions — the one path where `str(e)`
  still reached the client unredacted. The body is a fixed string plus the id; the log line goes
  through `redact_secrets`.

`tests/conftest.py` neutralises the limiter's process-lifetime window state and the job-store path for
the whole suite, or one module's traffic would 429 another's. Tests: `tests/test_api_hardening.py`.

---

## Activity ledger

`GET /api/portfolio/activity` (+ `.csv`) → `ActivityService`, rendered by `ActivityTab.tsx`. It unions
**trades, corporate actions, cash flows and dividends** into one chronological list with date-range,
kind and symbol filters.

It exists because all four tables were ingested, reconciled and depended on — the tax report reads
`trades`, the contributions splice reads `cash_flows`, `reconcile_taxlots` reads `corporate_actions` —
with **no read surface at all**. The sharpest consequence: the transfer audit this file prescribes
before trusting any money-added figure was `manage_cash_flows list` over ssh. Every cash row now
carries `counts_as_money_in`, badged *Transfer · not money in*.

Five rules, each of which would be a bug the other way:

- **Paging is applied to the merged list, not per table.** The four sources are separately ordered, so
  a per-table limit would silently drop every dividend in a busy trading month.
- **Dividends are era-spliced, exactly as every computing reader does it** — and this one was missing
  until 2026-08-05, which is the whole reason the rule is written here now. The same dividend is stored
  twice, yfinance under its ex-date and IBKR under its pay date a week or two apart, so without the
  splice the ledger listed **both**: 31 duplicate rows and a dividend total of 113 CHF against a real
  65, overstated **72%**. Nothing the app *computes* was affected — the breakdown, the summary card,
  XIRR and the tax report all splice — which is exactly why it survived: the only wrong surface was the
  one that merely displays.

  **And it calls `_splice_by_era` rather than reimplementing its rule**, which it did
  not until 2026-08-05. The inline copy was correct the day it was written and silently
  wrong two days later, when the helper gained its boundary-duplicate match: every other
  reader stopped showing the pair and the ledger kept showing it. `_splice_by_era` takes
  an explicit `boundary` so the windowing caller can comply, and the fetch widens by
  `EX_TO_PAY_MAX_LAG_DAYS` on both sides — the IBKR row that pairs with a windowed
  estimate can fall outside the window even when the estimate does not, so asking for
  1–15 February would otherwise resurrect the duplicate. `test_era_splice_boundary.py`
  fails any service that reads dividend rows without reaching the helper.

  **The boundary must come from the whole table, not the window.** `_splice_by_era` derives
  `min(ibkr_dates)` from the rows handed to it, which is right for readers that splice the full history
  and wrong here, because the ledger windows *first*: fed a slice, a window opening after the era began
  would treat its own earliest IBKR row as the era start and resurrect superseded estimates. Hence
  `DividendRepository.earliest_ibkr_payment_date()` — same shape and same reason as
  `CashFlowRepository.earliest_flow_date()`. Do not "simplify" this to
  `_splice_by_era(get_between(...))`; that is the obvious-looking form and it is the bug.
  Pre-boundary estimates are still kept and still badged — dropping those is the mirror-image bug, and
  it once blanked every pre-IBKR month from the dividend card.
- **Dividends are dated by `pay_date` falling back to `ex_date`** — the same `coalesce`
  `has_ibkr_dividends` uses. yfinance stores under the ex-date and IBKR under the pay date, and
  Mastercard's 29-day lag exceeds a monthly cycle, so the column asked decides the window.
- **Amounts convert at each row's own date** through the same `BaseFx` every other read endpoint uses.
- **A field a kind cannot fill is `None`, never 0** — in JSON and in the CSV. A corporate action has no
  price, and a `0.00` would assert one.

Zero-value dividend rows are excluded on the same test the two dividend readers use, so yfinance's
pre-ownership history never surfaces. Tests: `tests/test_activity_service.py`, plus cases in
`tests/test_api_smoke.py` — including one comparing the ledger's dividend total against
`DividendService`'s over the same span, since two readers of one table that nothing compares is how
the 72% got there.

---

## Look-through — company-level exposure

`GET /api/portfolio/lookthrough?limit=50` → `LookthroughService.get_lookthrough()`, rendered by
`LookThroughTab.tsx` (the 9th tab). Direct holdings plus every held fund decomposed into the
companies inside it, folded across listings and share classes. Pure DB read — no provider, safe
at any hour.

It exists because **48% of this account sits in twelve ETFs**, so "how much do I own of Nvidia"
was unanswerable — and the *direct* side was already fragmented, because one company routinely
occupies several rows of Positions. The three shapes are all different, which is why this is not
a string match on names or symbols:

| shown as | really | folds on |
|---|---|---|
| `GOOGL@NASDAQ` + `ABEA@IBIS` | Alphabet | **one ISIN** (`US02079K3059`) — no provider needed |
| + `GOOG@NASDAQ` | Alphabet class C | the **LEI** (`US02079K1079` is a different ISIN) |
| `ASML@NASDAQ` + `ASML@AEB` | ASML | the **LEI** (`USN070592100` NY registry vs `NL0010273215` Amsterdam) |

### Grouping is a union, never a precedence chain

`company_identity.company_groups()` unions any two members sharing **any** of ISIN /
shareClassFIGI / LEI / a declared override. The obvious design — `key = lei or share_class_figi
or isin` — **re-creates the bug the feature exists to fix**: when one of a company's ISINs
resolves an LEI and its sibling does not, the first is keyed by the LEI and the second by the
ISIN, so they land in two rows while the response reports a cheerful "1 unresolved". A weaker
identifier had already folded them and the stronger one pulled them apart. The key is chosen
*after* grouping (LEI > shareClassFIGI > lowest ISIN) purely so the API has a stable id.

Order-independence is a property, not an accident: the module is pure and
`test_company_identity.py` shuffles the input 25 times and asserts the grouping and every
display name are byte-identical.

### Both identity providers are required, and they fail in opposite directions

Measured against this account's own ISINs on 2026-08-14, both keyless:

| | folds | has no record for |
|---|---|---|
| **GLEIF** `api.gleif.org/api/v1/lei-records?filter[isin]=` | share classes and multi-ISIN companies (both Alphabet ISINs → `5493006MHB84DD0ZWV18`; both ASML ISINs → `724500Y6DUVHQD6OXN27`) | **TSMC, Samsung, SK Hynix, Credo** — the Asian ordinaries held here |
| **OpenFIGI** `POST api.openfigi.com/v3/mapping` | one share class across venues (GOOGL@US + ABEA@Xetra) | nothing held here; resolves all four GLEIF misses |

So a single-provider design loses either the Alphabet/ASML folds or every Asian ordinary.
`isin_identities` stores both with **separate `*_checked_at` stamps**: a NULL identifier beside a
non-NULL stamp means *asked, that provider has nothing*, distinct from *never asked* (no row).
Without the split, every run re-asks the four permanently-absent ISINs forever — the loop the
holiday rule and `UPSTREAM_RETRY_COOLDOWN_SECONDS` both exist to stop. Separate `*_source`
columns because a bulk-file LEI is not an API LEI, the `source='alpha_vantage'`-as-
`'yahoo_finance'` lesson.

**`compositeFIGI` is stored and must never enter the union.** It is per-exchange-composite, so
it folds nothing the ISIN does not, and unioning on it would merge unrelated lines.

**Neither provider folds an ADR to its ordinary**, and this is live rather than theoretical:
TSMC's ordinary is `BBG001S6Q004` while the `TSM` ADR is `BBG001S5WWW4` (as it must be — an ADR
is a distinct instrument), and GLEIF gives the ADR a LEI while giving the ordinary none. `2330@TWSE`
is held directly and `TSM` is a top constituent of the US-listed funds. Hence `ISSUER_OVERRIDES`
in `app/etf_sources.py` — ISIN-keyed, **reason mandatory by test**, currently one entry. Growth
there is the signal a tier below is being misused. `test_company_identity.py` checks it from
*both* sides: without the override TSMC is two rows, with it one, which is what proves the
override carries the fold rather than masking a broken tier.

**Display name is a separate decision from the key.** GLEIF returns the *legal* name in its own
script — TSMC's is `台灣積體電路製造股份有限公司`, correct and unreadable on a dashboard whose every
other row is Latin — while OpenFIGI's is clean ASCII truncated to ~30 chars and better only for
those. So: Latin-script legal name, else FIGI name, else non-Latin legal name, else the
highest-weight contributing row's name, else the ref. Deterministic, because a label that
changes with sort order is this codebase's signature bug. The Latin test is codepoint-based and
deliberately permissive about accents, so "Société Générale" keeps its own name.

### The partition, and why it is the only assertion that matters

```
direct_equity + looked_through_equity + fund_residual + nested_fund + uncovered_fund
  == total_market_value
```

To the cent. One equality catches a dropped holding, a double-counted constituent, a
renormalisation and a mis-scaled weight alike. `test_lookthrough_partition.py` pins it **and**
fails when the response grows a top-level `*_eur` field classified as neither a bucket nor a
non-bucket — so a bucket added later cannot escape the identity. (`test_allocation_completeness.py`
claims to be family-level and hardcodes its tuple; this is that shape done properly.)

Positions come from `PortfolioService.get_positions_breakdown()` and nowhere else, so the
base-currency projection is inherited and the total **cannot disagree with
`/api/portfolio/summary`** — pinned in `test_api_smoke.py`.

### Nine rules, each a wrong number the other way

- **Nothing is renormalised onto covered value.** Every percentage is a share of the *whole*
  portfolio, so while some funds have no basket every row is an understatement — and rescaling
  would convert a stated gap into a confident lie. Same refusal `rebalance.ts` makes about
  targets. `coverage_pct` leads the panel as a `role="alert"` **outside any collapsible**, the
  `MonthlyReturnsHeatmap` lesson.
- **Equity asset classes are a whitelist**, never a blacklist — the reasoning behind
  `get_deposits()` selecting `DEPOSITWITHDRAW` by name. A blacklist admits the next label an
  issuer invents (`Rights`, `Warrant`, `Preferred`) as a company.
- **`asset_class_available=False` is a real state.** Xtrackers/DWS publishes no asset-class
  column at all, so the whitelist cannot run: every row counts, the residual becomes
  `100 − Σ(all rows)`, and the API says the filter did not apply. Never infer "equity" from "has
  an ISIN" — a bond has one too.
- **A basket below `MIN_BASKET_COVERAGE_PCT` (80) is reported as absent**, with its measured Σ.
  A residual is right for EMIM's real 98.04 and catastrophic at Σ=3, where "this fund is 97%
  cash" is a *plausible* figure — the dangerous kind.
- **A constituent that is itself a fund gets its own bucket, not a company row.** Live risk:
  iShares baskets carry the BlackRock ICS liquidity fund as a line item. v1 does not recurse —
  that needs a depth cap and a visited set, because a feeder can hold its own share class.
- **A repeated ISIN inside one basket is summed.** Copying `import_prices.py`'s last-row-wins
  rule for a repeated date would silently drop weight; hence `UNIQUE(fund_isin, line_no)`.
- **The truncated tail is a named row carrying a value**, or the visible percentages sum to well
  under 100 under a "% of portfolio" header. Sort is `value desc, company_key asc`, because
  thousands of constituents tie near zero and without a total order the tail would shuffle
  between requests and a company would appear and vanish.
- **An unvaluable holding is excluded from both sides and named**, on the server-side test
  `market_value_eur > 0` alone — cited from `dividend_service`'s forward-yield filter rather
  than reinvented. An unpriced *fund* is the sharpest silent failure here: it renders not as a
  visible zero but as the **absence** of its companies from rows that still say "% of portfolio".
- **There is deliberately no cost or gain column.** Splitting a fund's cost across constituents
  needs the basket as it stood on each purchase date, which nothing stores — so any per-company
  cost would be fabricated, not merely approximate. Stated in the schema docstring so it is not
  "completed" later.

### The two charts, and why neither is a pie

`lib/lookthroughChart.ts` builds both; `LookThroughCharts.tsx` draws them. The arithmetic is in
`lib/` for the reason `portfolioKpis.ts` is — the interesting mistakes are numeric and testable
in `node` without paying jsdom's startup.

**A treemap for the companies, a stacked bar for the partition, and a pie for neither.** The
company ranking is ~50 rows spanning three orders of magnitude, where comparing angles fails
completely; the partition is three segments with long names, where a horizontal bar reads left
to right at 390px and a ring does not. A pie is only ever right for a part-to-whole of ≤6
roughly-comparable slices, which describes neither.

Four rules, three of which restate rules this feature already has:

- **The treemap's tiles cover the whole portfolio**, so the truncated tail and the unattributed
  remainder are tiles. Drawing only the companies would fill the card with the ~79% that could
  be attributed and render it as the whole — the renormalisation refused everywhere else, this
  time in a form where nobody would notice, since a treemap has no axis to disagree with.
- **The composition bar folds `fund_residual` + `nested_fund` + `uncovered_fund` into one
  segment.** They are one thing to a reader — value no company row accounts for — and three
  categorical hues spent on a distinction the fund table below makes in full is how a chart
  becomes a worse table. The partition still closes; it is summed one level up.
- **The two legends must not share a phrase.** The bar splits *value* while the treemap
  classifies *companies*, so a shared word would mean a share of the book in one and a
  property of a company in the other, forty pixels apart — and Alphabet is in both charts at
  once. Pinned by `test_does_not_reuse_one_phrase_for_two_meanings`.
- **A zero total draws nothing**, not an empty frame. Same refusal as `concentrationPct`.

### Grouping the treemap by sector, and the one prohibition it narrows

Tiles cluster **by sector**, not by whether the exposure was chosen directly. The owner asked
for it, and the direct/indirect split it replaced is still on the page as the composition bar,
where it answers a question about *value* rather than about companies.

Three things make it more than a display change:

- **The sector comes from the issuer basket first and Yahoo second**, measured rather than
  assumed: of the 103 ISINs behind the top-100 rows, BlackRock's baskets classify **97**, DWS's
  34, and `securities.sector` (Yahoo, direct holdings only) **27** — and BlackRock never
  contradicts itself across IWDA/SXR8/EMIM. Where Yahoo and BlackRock overlap they differ only
  by taxonomy, systematically, which is what `app/services/sector_taxonomy.py` exists to
  reconcile: one `normalise()` over four vocabularies, mapping to the eleven names the
  Allocation tab already displays. Unmapped input becomes `Unknown` rather than raising, and
  `Unknown` never votes.
- **A company's sector is a value-weighted majority of its contributing rows, tie-broken on the
  name.** A folded company can be described differently by two funds, and anything
  order-dependent here is this codebase's signature bug — `test_lookthrough_partition.py`
  shuffles the input and asserts the answer does not move.
- **This narrows a documented refusal rather than breaking it.** `etf_basket.py` said `sector`
  was stored and *deliberately unserved*, because serving it would put a second sector answer
  on the Allocation tab. It is served now for **grouping only**: per company, with no
  portfolio-level rollup anywhere in the response, so nothing says "this portfolio is 35%
  tech". The Allocation tab keeps `ETF_ALLOCATIONS` as its answer, and the two are not
  derivable from each other — this one covers only the ~79% that decomposes. The comments on
  both sides say so; `country` stays unserved for its own reason (it is a country, and every
  geographic bucket in the app is a region).

**The palette is the first in this app that is *chosen* per theme rather than one set of hexes
taking its chances against both surfaces.** `--viz-*` in `index.css`, two sets, each run through
a CVD/contrast validator against the surface it actually renders on (`#ffffff` / `#020817`) on
the **all-pairs** pairlist, because a treemap can seat any tile next to any other. Four details
are load-bearing:

- **Only four sectors get a hue; the rest fold into `Other sectors`.** Not a shortcut — the
  all-pairs pairlist is brutal, and brute-forcing all 256 subsets of the eight-slot categorical
  order found that **only four** of them clear it simultaneously (slots 1, 4, 5 and 6). Five
  hues cannot be made to pass by re-stepping. Four covers 68% of this book by value and the
  fold is where the 8-hue ceiling would have bitten anyway.
- **`AllocationTab` and the treemap share `lib/sectorColors.ts`**, so a sector is one colour
  across both tabs. The old `SECTOR_COLORS` failed validation outright: `#3b82f6` Technology
  against `#8b5cf6` Communications measures **ΔE 1.3 deuteran** — the two largest groups here,
  indistinguishable to a deuteranope. `Unknown` also took a *positional* fallback colour and
  moved whenever the chart reordered; it has a fixed grey now, because colour follows the
  entity and never its rank.
- **Every fill carries its own ink.** Dark-mode `--viz-sector-2` measures 2.94:1 against white,
  so one hardcoded label colour makes one group unreadable in exactly one theme — which nobody
  testing the other will see. Re-stepping that swatch to fix the contrast broke its CVD
  separation from green instead (ΔE 4.1), so the *ink* was flipped rather than the fill.
- **`sectorPaint` must consult both the sector record and the structural one.** It looked at
  only the first, so `unattributed` and `other_companies` both fell through to the `Other
  sectors` grey: three meanings, one colour, three identical legend swatches, and nothing
  failed — falling back *is* correct for a genuinely unknown group. Found by sampling pixels
  out of a screenshot, pinned now by a test that fails on the mutant.

**Only depth 2 is drawn, and that is a finding rather than a shortcut.** The first version
painted a frame and a header strip at depth 1 to delimit each sector; neither ever appeared,
because Recharts paints a parent *before* its children and the children tile the parent exactly.
No unit test can see paint order. So the grouping is carried by adjacency and fill, and each
group's name and share by the legend underneath, which nothing can paint over.

The remaining estimate is the tile label's *fit*: SVG text cannot be measured before layout, so
`UPPERCASE_ADVANCE_PX` budgets for the worst glyphs, because IBKR names arrive shouted
(`NU HOLDINGS LTD/CAYMAN ISL-A`). Budgeting for mixed case put `ARISTA NETWOR…` over both edges
of its own tile. Under-filling costs a character; overflowing costs the tile boundary.

### One fundness predicate, keyed on ISIN

`ETF_ALLOCATIONS` entries now declare `"isins": [...]`, and `fund_isins()` /
`is_known_etf_isin()` / `symbol_for_fund_isin()` are **the** fundness predicates. The live
collision that forced it: this account holds the **UCITS** VanEck fund `IE00BMC38736` (LSE), not
the far better-known US `SMH`, and a symbol-keyed lookup cannot tell them apart. `app/etf_sources.py`
holds only *how to fetch* and is cross-checked against `ETF_ALLOCATIONS` in both directions by
`test_etf_source_registry.py`; a third census of what counts as a fund would be the same
divergence a third time. `_build_isin_index()` raises **at import** on a duplicate ISIN, because
two funds sharing one would silently give one the other's basket.

### Baskets, and DBPG

`etf_baskets` (one row per fund: as-of, source, row counts, weight sums,
`asset_class_available`) is split from `etf_holdings` deliberately — a `SUM(weight_pct)` over
holdings cannot tell "98.04 because the file carries cash and futures rows" from "98.04 because
2% of rows failed to parse", and those need different responses. Denormalising the as-of onto
every row would make "the fund's as-of" a `MAX()` that one stale row ages.

`weight_pct` is a **percent**, matching `etf_mappings.py`, and normalising at the adapter
boundary is mandatory: DWS ships fractions summing to 1.0 and iShares percents summing to ~100,
so a fraction landing here makes the fund contribute 1/100th of its value and read as 99% cash.
`sector` is captured and served to **one** consumer — grouping the look-through treemap, per
company, with no rollup (see *Grouping the treemap by sector* above). `country` remains
unserved: it is a country while every geographic bucket in the app is a region, so using it
needs a hand-maintained country-to-region map. See `etf_mappings.py`'s docstring for the full
precondition for switching the three allocation charts onto either.

**"Stale" is per-adapter (`ADAPTER_STALE_DAYS`), and a single global threshold was wrong.** The badge
is only useful if it means *the issuer has newer holdings we failed to fetch* — Xtrackers and iShares
republish daily, so 7 days there is a missed fetch worth acting on, while Vanguard US publishes
month-end and lags ~6 weeks *by design*, so one 45-day rule badged it permanently for behaving
exactly as documented. A badge that can never clear is the always-present-Flex-banner pathology.
A future quarterly source (SEC N-PORT arrives 75–136 days old) needs its own entry rather than a
raised default. Staleness deliberately **does not reduce `coverage_pct`** — the percentage answers
"how much is attributed", not "how current is it" — which is exactly why the age has to be surfaced
on the card rather than only in the fund table.

**Nothing schedules any of this, and that is the feature's weakest property.** Baskets and
identities are populated by a deliberate CLI run and then decay in place: `fetch_etf_baskets --all`
plus the import lines it prints, and `resolve_identities --constituents`. The read path is pure DB,
so a basket nobody re-downloads keeps contributing its full share of `coverage_pct` while describing
an older index — which is exactly why staleness is surfaced on the card and not only in the fund
table. Two clocks matter: the five daily feeds (Xtrackers, iShares, Invesco, First Trust, Defiance,
VanEck) badge `†` within a week, Vanguard US publishes **month-end with a ~6-week lag** (75 days).
Identities never expire but are also never *extended* — a fund rebalance brings in constituent ISINs
nobody has asked about, and a newly bought security's ISIN is unresolved until the CLI is re-run, so
it will not fold with an existing holding of the same company. `unresolved_value_eur` is the figure
that shows this drifting. A `find_stale_etf_baskets()` warning hung off the market-data job is the
missing piece; until it exists the badge is the only signal and someone has to look at it.

**`as_of_date` is the issuer's own where it publishes one, and the fetch date where it does not** —
and one issuer publishes a date that is *worse* than none. Xtrackers publishes nothing at all: not
in the CSV, not in a `Last-Modified` header (verified). Defiance publishes `Data as of 08/17/2026`
on a file downloaded on the 16th describing Friday the 14th's close — a T+1 **effective** date, the
same convention Invesco makes explicit by shipping `effectiveDate` beside `effectiveBusinessDate`
(and `parse_invesco` takes the business one for exactly this reason). Both cases err the same way:
the true as-of can only be *older*, so a stood-in or forward date makes a basket look **fresher**
than it is, which is the wrong direction for a staleness alarm. So `parse_defiance` clamps to
`min(stated, fetched_on)`, and both cases set `as_of_is_issuer_stated=False` rather than hiding it.

**DBPG is excluded and carries two independent disqualifiers.** It is a synthetic swap-based S&P
500 **2× leveraged** ETF: the 46-name basket it publishes is substitute collateral (measured top
holdings Mastercard 6.6%, Altria 5.7%, Tesla 4.9% — the tell), *and* even the real index basket
would understate its exposure by half. `replication` and `leverage` are recorded **separately**,
so a future "we found the swap reference" change cannot clear one and silently reintroduce the other.

### Sources, and what still needs a hand download

**Seven adapters, all keyless and login-free, all verified against live files.** Only **VWCE**
has no route at all.

| adapter | route | the thing that bites |
|---|---|---|
| **DWS** | `etf.dws.com/etfdata/export/GBR/ENG/csv/product/constituent/<FUND_ISIN>/` | keyed by fund ISIN, nothing to discover; echoes `ShareClass ISIN` on every row for the parser to check |
| **BlackRock** | a varnish JSON API needing a per-fund `portfolio_id` | several share classes are distinct product ids over **one** portfolio and return byte-identical baskets, so a copy-pasted id is *invisible* — `test_etf_source_registry.py` checks uniqueness |
| **Vanguard US** | profile API, 500 rows/page | as-of lags ~6 weeks, which is normal, not stale |
| **Invesco** | `dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/<CUSIP>/holdings/fund` | keyed by the fund's **own CUSIP**, derived from its ISIN — nothing to discover, unlike BlackRock. The *product page* is a single-page app, which is what an earlier note recorded as "no route" |
| **First Trust** | `ftportfolios.com/retail/etf/etfholdings.aspx?Ticker=<T>` | HTML, and the holdings table is the sixth of eight — `_pick_table` matches on headers, never on position |
| **Defiance** | `defianceetfs.com/<ticker>-full-holdings/` | **not** `/<ticker>/`, whose table is rendered client-side and absent from the response |
| **VanEck** | `vaneck.com/nl/en/investments/<slug>/downloads/holdings/` | XLSX. Needs a **cookie jar** (a cookieless GET loops the consent redirects) and the `/nl/en/` locale **pinned**. Parsed with `zipfile` + `ElementTree`; do not add `openpyxl` for one 5 kB file a week |

**10 of 12 funds decompose.** The remainder is VWCE (a real gap — Vanguard Europe publishes
complete holdings by email on request, month-end + 15 days) and DBPG (excluded by design). The
read path never dereferences `adapter`, so a declared-but-unimplemented one degrades to "no
basket yet", never to a wrong number.

**Do not re-derive the four US routes from the old comments in `etf_sources.py`'s history.**
They said SOXQ/GRID/QTUM were unreachable, and each was wrong in a different way: Invesco's SPA
hides a keyless API, First Trust's "tickers but no ISINs" missed the CUSIP column beside them,
and Defiance's table is on a different path. What the old note got right by accident is that
those identifiers do not fold on their own — see below.

**Fetching is split from parsing, and that is not tidiness.** `app/cli/fetch_etf_baskets.py`
writes response bodies to disk; `app/cli/import_etf_basket.py` parses and stores them. It is the
same relationship `ingest_flex_xml.py` has to a browser download, it makes every scraper's
failure mode a committable fixture, and **it is the only way to test the lying-content-type trap
at all** — a fetcher that parsed inline would have nothing to hand a test. The import path works
identically on a file downloaded by hand, which is the whole route for VWCE and the fallback for
any adapter whose route breaks.

### The identifier column that is not all CUSIPs

Three of the four US feeds publish a nine-character identifier where the others publish an ISIN,
and treating that column as CUSIPs is a **silent fabrication**, which is why
`app/services/security_identifiers.py` exists. Measured 2026-08-16:

- **77 of GRID's 128 rows are CINS**, not CUSIPs — the same numbering space extended to foreign
  issuers, marked by a *leading letter*. Its three largest holdings are all of them
  (`G29183103` Eaton, `F86921107` Schneider, `G51502105` Johnson Controls). `US` + a CINS is a
  **check-digit-valid ISIN belonging to nothing**, so a bulk prefix would invent an identifier
  for 60% of the fund and every one would look right.
- **20 of QTUM's 89 rows are SEDOLs** (`B056381`, `6640400`, `BZ1DZ96`) — seven characters, no
  vowels, their own check digit — sitting in a column headed "CUSIP".
- **Digit-leading is still not unambiguously US**: `82509L107` yields valid `US` and `CA` forms
  and only one exists. `derive_north_american_isin` names the assumption in its own signature.

So `identifier_kind()` classifies by *shape and check digit*, the ISIN is derived only for a
plain CUSIP, and everything else is kept verbatim in `etf_holdings.constituent_identifier` for
OpenFIGI. The failure direction is deliberate: a company that cannot be identified stands alone
as its own row (an understatement) rather than merging into another's (a fabrication).

**Resolution writes a FIGI, not an ISIN, because OpenFIGI has no ISIN to give.** Its `/v3/mapping`
answer is entirely FIGIs, tickers and exchange codes — `ID_CINS G29183103` returns 104 venue rows
sharing shareClassFIGI `BBG001S5QZ45`, which is exactly what `ID_ISIN IE00B8KQN827` returns, so
Eaton folds across GRID and MSCI World on the FIGI alone. `IdentityMember` already unions on
`share_class_figi`, so `etf_holdings.constituent_share_class_figi` needs no new grouping logic.
**And the idType matters more than it looks: asked as `ID_CUSIP`, that same CINS returns zero
rows** — no error, just nothing — so `OPENFIGI_ID_TYPES` maps CINS→`ID_CINS` and SEDOL→`ID_SEDOL`.
Verified against the live API before it was written, which is the only way to learn it.

**Everything the parsers refuse, they refuse whole.** `import_prices.py`'s rule, and here the
stakes are higher: a partial parse *succeeds*, replaces a real 1,338-row basket with plausible
rows, and every figure shrinks silently. On top of that `replace_basket` refuses a basket whose
row count collapses below half the stored one, or whose as-of moves backwards, keeping what is
already there. Both overridable with `--force` for a genuine index reconstitution.

**Nine things real files taught us, all now pinned by tests.** The first four came off the
European feeds, the rest off the four US ones:

- **A negative weight is refused only on an *invested* row.** EMIM publishes five negative cash
  lines (THB −0.01, TWD −0.01, CNH −0.01, HKD −0.02, KRW −0.10) — ordinary overdrawn balances.
  Refusing 4,042 rows over −0.10% of cash trades a whole fund's look-through for nothing. A
  negative *security* weight is the real hazard and still refuses. An issuer that states no class
  at all still refuses any negative, since there is no way to tell the two apart.
- **A nameless row falls back to its identifier.** XNAS ships `IE00BYQNZ507` with an empty name at
  0.008% of the fund. Nameless *and* unidentifiable still refuses.
- **Xtrackers' cash and futures rows are read off its identifier convention** (`_CURRENCYUSD`,
  `___ADI34XYM5`), because the export has no class column — without which XNAS produced company
  rows called *US DOLLAR* and *NASDAQ 100 E-MINI SEP26*. Only the negatives are derived; a real
  holding is left unclassified rather than asserted to be equity, which would be wrong for a bond
  fund. This is why `counts_as_invested` lets a **stated** class decide even when the issuer
  publishes no column.
- **`fund_residual_eur` is the rounded remainder, not its own rounded sum.** Five independently
  rounded buckets summed to a cent more than the rounded total on the real book — a partition
  that misses by a cent still misses. The residual carries the correction because "whatever is
  left" is its definition. Note the residual is mostly *rounding*, not cash, for a broad fund:
  Vanguard publishes weights to 2dp, so thousands of VT's 10,032 holdings round to 0.00 and its
  weights sum to ~92%. Not renormalised — that would invent the attribution.
- **Invesco HTML-escapes its names.** `Invesco Government &amp; Agency Portfolio` is the only
  place in seven feeds that happens, and an unescaped name reaches the screen.
- **`Money Market Fund, Taxable` is deliberately absent from `INVESCO_ASSET_CLASSES`.** An
  unmapped value passes through verbatim so `counts_as_invested`'s `"fund"` marker catches it;
  coercing unmapped values to `Equity` would make AGPXX a top-50 *company*.
- **First Trust's `Classification` column looks like an asset class and holds an industry**
  (`Diversified Industrials`, `Electrical Components`). Its nine currency lines are marked only
  by a `$`-prefixed ticker — the Xtrackers convention again, hence `asset_class_available=False`
  with the negatives still derived. The industry itself is stored raw and normalises to
  `Unknown`, which is correct: BlackRock classifies those companies anyway.
- **Do not date a basket from the first date-shaped string on the page.** QTUM's page carries
  five, including a bond maturity inside a holding's own name (`... Obligations Fund
  12/01/2031`). An unanchored search happened to pick a `17/02/2022` and refuse — the lucky
  outcome; the maturity would have parsed cleanly and dated the basket to 2031. `_us_date_after`
  takes an anchor phrase.
- **A declared count is worth checking wherever an issuer publishes one.** Invesco's
  `totalNumberOfHoldings` is the same guard as Vanguard's `size` — a truncated response whose
  weights still sum plausibly is the shape that gets through everything else.

`app/cli/resolve_identities.py` resolves identity: a CLI rather than a route, following the
precedent that there is no upload endpoint for Flex XML and no route for price import. Being a
separate process it needs no gate — and if it ever becomes a route it must **not** use
`SYNC_PIPELINE`, because entering that gate bumps the shared last-start clock every other route's
cooldown reads, so a look-through refresh would 429 a real IBKR sync. Held ISINs are resolved
unconditionally (~25); `--constituents` adds the head of the constituent ranking, down to
`IDENTITY_COVERAGE_TARGET_PCT` (99.5) of cumulative look-through value and capped at
`IDENTITY_MAX_ISINS`, plus the CINS/SEDOL pass above. **The obvious form of the ranking rule is
circular** — it wants a company's value to decide whether to resolve its identity, and identity is
what builds companies — so it ranks by *raw constituent ISIN* first, which needs no identity at
all. The threshold is a share rather than an amount because the base currency is user-switchable
and a fixed floor would change the resolved set when a display toggle is flipped.

**`OPENFIGI_API_KEY` is free, optional, and the precondition for the cap being worth raising.**
It takes OpenFIGI from 10 mapping jobs per request at 25 requests/minute to 100 at 250 — roughly
250 identifiers a minute to 25,000. Empty by default, so the feature keeps working without it,
and the CLI says when it is missing. **GLEIF is what actually bounds a run**, though: it has no
batch form for an ISIN filter, so it costs ~1.1s per ISIN — about 45 minutes at
`IDENTITY_MAX_ISINS` (2500) against a couple of minutes for OpenFIGI's share. Hence the runtime
estimate printed before the first request, and `--limit`: a definitive answer is cached either
way, so three bounded runs come to the same thing as one long one.

Tests: `test_company_identity.py`, `test_lookthrough_partition.py`, `test_etf_source_registry.py`,
`test_etf_basket_import.py` and `test_us_issuer_adapters.py` (the poison fixtures),
`test_security_identifiers.py`, `test_constituent_identifier_resolution.py`, plus the look-through
case in `test_api_smoke.py` and `LookThroughTab.test.tsx`.

---

## Client-side analytics — risk, targets, currency

Three pure `frontend/src/lib/` modules with no endpoint of their own: they compute from series and
positions the page has already fetched, which is why they add **no request and cannot reach Yahoo**.
`portfolioKpis.ts` feeds the Performance tab's two card rows; `rebalance.ts` and
`currencyExposure.ts` feed two panels on the Allocation tab.

**One rule spans all three, and it was a real bug before it was a rule.** `undefined` data means
*not loaded*; an empty array means *nothing held*. Collapsing them lets a panel build a confident
answer out of an outage — the rebalance panel did exactly that, reporting *0 positions outside the
band* above rows reading *Not currently held*, i.e. that nothing needed rebalancing and that held
positions were not held. Unit tests could not see it because the shape needs a saved target;
`e2e/errors.mjs` covers it now. Any new panel here takes `isError` and treats absent data as a
stated failure.

**The two KPI rows this module feeds did not follow it until 2026-08-05**, which is the variant worth
naming: `PerformanceMetricsCards` and `RiskMetricsCards` returned `null` whenever `metrics` was null,
and Dashboard's memos return null when their query fails — so a backend outage did not render an error
state on those rows, it rendered *nothing*, and twelve metrics were simply absent from the page. A row
that disappears is worse than one that fails visibly: a stated failure invites a retry, while a missing
row reads as a feature that was never built. `PortfolioSummaryCards`, in the same directory, had the
correct branch all along. The `null` return survives for the genuine no-data-yet case — an empty
portfolio is not a failure and must not claim to be one — so the two states are now distinct props
rather than one absent value.

**And `e2e/errors.mjs` shows how it hid.** Its count of panels reporting the failure went **8 → 10**
when these were fixed: the two surfaces it existed to cover had never been in its own tally, under an
assertion (`hits >= 4`) loose enough not to notice. A floor that far below the real count cannot fail,
which is the same "passes vacuously" shape as the Sharpe clamp test. It is `>= 10` now.

### Risk metrics (`portfolioKpis.ts`)

Sharpe, Calmar and top-5 weight predate the rest. Volatility, Sortino, beta/correlation, drawdown
detail and Herfindahl effective holdings were added because the tab reported return *per unit of
risk* without ever reporting the risk, and because a top-5 weight cannot tell five equal positions
from one dominant one.

- **Beta is measured only over days when NEITHER series saw a flow.** The benchmark is a flow-matched
  hypothetical carrying the portfolio's own cost-basis line but no `external_flow_eur`, so netting a
  flow out of it means inferring one from the cost delta — the same asymmetry that fabricates a loss
  on every sale date in `externalFlow`'s fallback. That would bias beta on exactly the days the
  portfolio traded. Dropping the pair costs a few days a month and biases nothing. `sampleDays` rides
  along so a thin window declares itself instead of showing a confident slope.
- **Volatility, Sortino *and Sharpe* are `null`, not `0`**, below the minimum sample or with nothing to
  divide by. A `0` meaning "unknown" being read later as a fact is the most repeated bug in this
  codebase.

  **Sharpe was the exception until 2026-08-05**, because it predates the risk row and was missed when
  the other two were written. It returned `0`, and that was reachable in one click: selecting **MTD** in
  the first days of a month leaves 2–3 daily returns, so the card drew a green `0.00` captioned
  *Risk-adjusted return* beside a dashed Volatility and Sortino. Sharpe is the worst possible place for
  this substitution — `0.00` is a *plausible* Sharpe, so unlike a 0% volatility nothing about the number
  invites doubt. Its negligible-volatility branch is `null` now too: return per unit of risk is
  undefined when there is no risk, and a flat series is what a stale feed looks like.

  Its clamp test had also been passing vacuously for the same reason — the old series' σ tripped the
  negligible-volatility early return, so it asserted `|0| <= 10`, true of anything. It now uses a
  high-return/low-σ series that reaches the clamp.

- **`concentrationPct` is `null` too, and it was the worst of the three**, because its zero does not
  merely fail to inform — it *reassures*. The tone ladder calls anything under 50% good news, so an
  unpriced portfolio drew a green `0.0%` asserting the five largest holdings are none of the book.
  `herfindahlConcentration` already returned `null` on that exact condition, and the two now share a
  card (the effective count moved into the Top-5 footnote when *Effective Holdings* was replaced), so
  the green zero appeared with the one qualifier that would have explained it silently dropped out.
  Both absences are the same condition, so the footnote says *No priced positions* rather than leaving
  a bare "Concentration risk" under a dash.

  **The same lens caught `_compute_rsi` on the backend, where the stand-in was a *maximum*, not a
  zero.** `avg_loss == 0` returned `100.0`, conflating "an unbroken advance" with "nothing moved at
  all" — RSI is undefined on a flat series, and 100 is the strongest overbought reading the scale
  has. `_compute_buy_score` then scored it **0 of 10** on technical timing, while its own `rsi is
  None` branch scores an unknown at a neutral **5**: the fabricated value was ten points worse than
  admitting the metric was unmeasurable, and the honest branch already existed. Reachable on a
  halted, delisted or fixed-NAV listing, which the watchlist is far more exposed to than the
  portfolio. Every earlier instance of this lens found a zero, so grepping for a suspicious `0`
  would not have found this one.

  **The lens worth reusing:** when a metric can be unknown, ask what its *stand-in value would claim*.
  A `0` volatility looks broken and gets noticed; a `0` Sharpe and a `0%` concentration both look like
  answers, and the concentration one looks like a *good* answer. Severity tracks plausibility, not
  magnitude.
- **`dailyReturnSeries` exists because `dailyReturns` drops days with nothing to divide by**, so the
  nth return is not the nth calendar point. Indexing the input by return position to name a
  drawdown's peak picks the wrong day.
- **The *current* drawdown leads and the worst one is the footnote.** Showing only the max reads as a
  live warning long after the recovery.

### Target allocation and drift (`rebalance.ts`)

Targets live in **localStorage**, following `ForecastTab`'s precedent. Deliberately not a table plus
an endpoint: `/api/` is proxied publicly and every write is auth-gated, so a route that stores
portfolio intent is a larger surface than this earns. The cost — targets do not follow you to another
browser — is stated in the panel. `readTargets()` drops a stored value that is not a usable percent
rather than coercing it, because one NaN propagates into every drift on the page, and survives
corrupt JSON rather than taking the tab down.

Four rules, each a wrong number the other way:

- **A missing target means unmanaged, never 0%.** Reading absence as zero advises liquidating every
  holding whose target has not been set — all of them on first use. Clearing the input therefore
  *removes* the target; `0` means "hold none of this", and those are different instructions.
- **Targets are never renormalised to 100%.** The shortfall is reported instead. Scaling invents a
  target nobody chose, and the invented one moves whenever an *unrelated* target is edited.
- **An unpriced position has no weight rather than a zero weight** — and "unpriced" means **either** of
  the two ways the backend fails to value one. It values a holding with no cached price at 0.00, so naive
  drift advises buying its entire target when the position may be the largest one held: the SBI shape.

  **`market_price === null` alone does not catch it, which was a live gap until 2026-08-05.** When the
  price resolves but its **FX rate** does not, `get_positions_breakdown` sets `market_value_eur = 0.0`
  and leaves `market_price` populated — so the holding read as *priced*, took a 0% weight, and drift
  advised buying the whole target. Same bug, second route. A missing FX rate is not hypothetical:
  Frankfurter cannot serve TWD at all, which is the entire reason `WARM_CURRENCIES` exists.

  Both `rebalance.ts` and `currencyExposure.ts` therefore test `market_price === null ||
  market_value_eur <= 0`. The old narrow predicate was justified in a comment by "a *priced* holding
  genuinely worth zero is a real 0%, e.g. a fully-sold one" — **and that premise was false**:
  `get_positions_breakdown` selects `is_open == True`, so a sold-out security has no open lots and never
  reaches the client at all. Two tests asserted the wrong behaviour on the strength of it. What a zero
  value on a priced position actually means is the FX failure above, or a zero-quantity open lot, which
  is a data anomaly that should equally not drive advice.

  Note this is a *narrower* question than `summary.unpriced_holdings`, which is the backend's own count
  and the one to trust for the headline — the client predicate exists only to decide per-position
  weighting.
- **Targets key on `security_id`, not symbol**, because identity is `isin + exchange` and ASML is two
  securities.

Two further refusals. An empty plan is **not** `balanced` — vacuous truth renders *nothing to do* on
a portfolio nobody has configured. And `judgedCount` exists because counting only rows *outside* the
band cannot tell an all-clear from an empty comparison: zero judged rows must say so, which is also
wrong-with-a-healthy-backend when every target sits on an unpriced holding.

### Currency exposure (`currencyExposure.ts`)

`securities.currency` is the currency a listing **trades** in. For a direct holding that is also the
economic exposure; for a fund it need not be, and here often is not — a EUR-listed S&P 500 tracker is
quoted in EUR and carries USD risk. Folding it into the EUR bucket is confidently backwards on
exactly the positions that prompt the question.

**Nothing is re-attributed, and the ETF look-through table cannot fix it**: `app/etf_mappings.py` maps
*regions*, and regions do not determine currency — "Europe" spans EUR/GBP/CHF/SEK, "Asia Pacific"
spans JPY/AUD/HKD/TWD. Funds are counted where they trade, with their share of the book named on
screen and the reason given, so the rows cannot be mistaken for an FX position. The fund set comes
from the ETF bucket of the allocation response already on the page, since `Position` carries no asset
type; matching is by symbol, so a stock sharing a held fund's ticker would be flagged — accepted,
because the flag is a caveat rather than a figure.

Unpriced positions are excluded and counted, as above, and `foreignQuotedPct` returns `null` rather
than `0` when nothing is priced: no positions is an unknown exposure, not an unhedged-free one.

Tests: `src/lib/portfolioKpis.test.ts`, `src/lib/rebalance.test.ts`,
`src/lib/currencyExposure.test.ts`, plus jsdom tests beside each component and three checks in
`e2e/errors.mjs`.

---

## The mobile layout — one description, two renderings

The app is built to work at **390x844**, and the rule that keeps it that way is that a table and its
phone equivalent come from **one** `Column[]`, not two hand-written trees.

`ui/DataTable.tsx` renders a real `<table>` inside `ScrollableTable` at `>=sm` and a card list below
it, from the same descriptors. Thirteen tables times two renderings would be twenty-six places a
column can be added to one and not the other — the dominant failure mode above, in its worst form:
a diverging calculation eventually produces a number someone notices, whereas a column missing from
the phone produces *nothing at all*, on a device the author is not looking at. So `mobile` defaults
to `'detail'` (a new column reaches the phone unless someone explicitly hides it), `cell` takes the
view it is rendering into rather than being duplicated, and two test files hold the line:
`DataTable.test.tsx` drives both modes from one fixture, and `tableFamily.test.tsx` pins the
conventions across column sets plus a source scan for any raw `<table>` outside `ui/`.

**It never owns sort state.** The three sorting tables have genuinely bespoke comparators — nulls-last
over a string|number union, a rating consensus through a score table, a memoised portfolio total —
and pulling them in would need a `sortValue` per column, a fourth thing to keep in step with `cell`.
Rows arrive sorted. The contract every table already implemented and nothing stated: `onSort(active)`
flips direction, which is what makes the phone's single direction button correct.

Three tables deliberately stay tables, each argued in its own file: `CurrencyExposureCard` (the
comparison *between* rows is the point, and there is no identity column to promote), and the two
12-month x N-year matrices (`MonthlyReturnsHeatmap`, `DividendSummary`). They get a designed
`min-w-*` instead, because `ScrollableTable` applies none — which is why eighteen columns were being
*squeezed to min-content* rather than scrolled, and why its edge fades were describing a problem they
did not cause.

**Four things here were each a bug first:**

- **`justify-center` on a flex overflow container hides its leading items.** A centred row wider than
  its scroller overflows on *both* sides and `scrollLeft` cannot go negative, so the first tab sat
  ~150px off the left edge, permanently untappable. The tab strip is `justify-start` below `sm`.
- **A grid item defaults to `min-width: auto`** and so refuses to shrink below its content's
  min-content width. One wide table made a single-column track 392px inside a 358px page. The track
  yields, not the card: `[&>*]:min-w-0` on the paired grids.
- **A responsive base class loses to nothing at `>=640px`.** `p-4 sm:p-6` on `Card` would put
  `sm:p-6` inside a media query, where it beats a plain call-site `p-0` — and sixteen KPI cards pass
  `text-sm` to `CardTitle`, fourteen sites override card padding. Hence `--card-padding` and
  `--card-title-size` as custom properties, in the bracket form (`p-[var(...)]`) so tailwind-merge
  still classifies them and a call site still wins.
- **`useMediaQuery`'s no-`matchMedia` fallback is desktop, and that is an invariant.** jsdom
  implements neither `matchMedia` nor `ResizeObserver` nor `scrollIntoView`. Falling back to desktop
  is what lets every pre-existing component test keep seeing the `<table>` it was written against,
  needs no vitest setup file, and keeps `e2e/a11y.mjs`'s `aria-sort` count non-zero. Flip it and two
  component tests plus an e2e check fail on day one.

**`MAX_RANGE_DAYS` is the other cross-language constant**, and it drifts with no other symptom:
`dateRanges.ts` clamps the ALL button to `365 * 5` precisely so it never asks
`/api/portfolio/value-over-time` for a span the router's own `max_days = 365 * 5` returns 400 for. The
clamp lands *on* the boundary — the client sends exactly `max_days` and the server compares with `>` —
so an off-by-one on either side breaks ALL for anyone with enough history while both suites stay
green. `tests/test_range_limit_agreement.py` reads both files and pins them **equal**, not merely
compatible: a client that shrank to one year would satisfy "not larger" and silently truncate.

`lib/breakpoints.ts` holds the two boundaries as numbers because a Recharts axis width is a prop and
a card list is a different DOM tree — neither is expressible as a `sm:` utility. Everything that
*can* stay in CSS does. `breakpoints.test.ts` pins those constants against Tailwind's own scale and
fails if `tailwind.config.js` ever gains a `screens` override, since one boundary written down in two
languages is the same failure mode again.

**`e2e/mobile.mjs` is the only thing that can see this class of bug** — horizontal overflow is a
property of the assembled page at a real width, and jsdom loads no CSS. Read its docblock before
"fixing" an overflow: `body { overflow-x: hidden }` clips rather than fixes, kills `position: sticky`,
and makes the check pass vacuously. Two assertions are paired specifically to catch that.

Chart heights are CSS on a wrapper plus `height="100%"`, hoisted to a module constant per file so the
chart and its loading/error/empty states cannot drift. `PerformanceAttribution` is the exception and
keeps a numeric height: its height is *data*-driven, one row per security, so a CSS height would
squash thirty bars into 240px.

---

## Deployment

**Push to `main` → deployed automatically within 10 minutes.** `/root/auto-deploy.sh` on the VPS (root
crontab, `*/10 * * * *`) does: `flock` → `git fetch` → deploy **only if strictly behind** `origin/main`
(`merge-base --is-ancestor`; it will refuse and log if the VPS has diverged) → back up `portfolio.db` →
`deploy.sh` → health check → **roll back to the previous commit if health fails**. Log:
`/root/auto-deploy.log`.

`deploy.sh` is expensive (`docker compose down`, `build --no-cache`, `npm ci`), which is why the cron
guards on an actual change. Its own health check fires a few seconds after start and often reports
FAILED spuriously — check `/health` again after ~15s before believing it.

- SSH: `ssh -i ~/.ssh/id_ed25519_hostinger root@portfolio.srv1211053.hstgr.cloud`
- Secrets live only in `/root/IBKR_investment_tracker/backend/.env` (`IBKR_TOKEN`, `IBKR_QUERY_ID`)
- nginx proxies all `/api/` publicly with `proxy_read_timeout 300`; needs `listen [::]:443/80` (an AAAA
  record exists)
- Backups: `/root/ibkr-backups/<date>/`

**Changing `backend/.env` needs `docker compose up -d`, never `restart`.** Compose reads `env_file`
when it *creates* a container; `restart` reuses the existing one with its original environment, so a
new value is accepted, written, and silently ignored. `up -d` sees the changed config and recreates.
This bit us turning on `API_ADMIN_TOKEN` (2026-07-31): the token was in `.env`, every command
reported success, and `write_auth_enabled` stayed `false` — a site that looks locked down and isn't.
**Always confirm against `/health` rather than the command's exit status.**

**`deploy.sh` pulls the repo itself (line 13), so a deploy that changes `deploy.sh` runs the OLD
copy once.** Bash does not reload a running script. Any behaviour newly added to `deploy.sh` is
therefore absent from exactly the deploy that introduces it, and appears from the next one on. That
is why the build-identity deploy reported `commit: "unknown"` — the `GIT_COMMIT` export existed in
the pulled file but not in the executing one. Expect this for any future `deploy.sh` change; it is
not a failure, and it self-corrects. Anything asserting "the deploy landed" must therefore not key
solely on the commit sha (`ops/finish-deploy.*` also accept the `write_auth_enabled` marker).

A **cloud routine** `ibkr-sync-validator` (claude.ai/code/routines) runs daily at 07:45 UTC to validate
the morning sync via the public API + the IBKR MCP connector. It **cannot SSH**, so it opens PRs rather
than pushing to `main`.

---

## Local development

```bash
cd backend && venv\Scripts\activate && uvicorn app.main:app --reload --port 8000
cd frontend && npm run dev          # http://localhost:5173
```

**Set `SCHEDULER_ENABLED=false` in `backend/.env` for any local run.** Starting the backend is not a
neutral act: the lifespan handler arms every `ALL_SYNC_HOURS` Europe/Berlin job, which call
the live Flex API with the real token from `.env` and hit Yahoo — both rules at the top of this file,
from a dev machine. `settings.scheduler_enabled` defaults to **True** so production is unaffected, and
a disabled scheduler logs a warning because otherwise it looks exactly like a healthy site whose data
has quietly stopped moving. Pinned by `test_scheduler_is_enabled_by_default`.

Two traps when pointing a browser at a local stack:

- **Check which port Vite actually took.** If 5173 is occupied it silently moves to 5174 and prints it
  once. A second dev server on 5173 configured against production means you are looking at prod data
  and issuing requests to the live site — including `/api/dividends/summary`, which can enqueue a
  Yahoo dividend sync.
- **A real-data snapshot beats the stale local DB.** `sqlite3 .backup` on the VPS, copied down, and
  `DATABASE_URL` pointed at it (the local `backend/portfolio.db` predates trades, cash flows and
  the IBKR dividend era, so it exercises none of the interesting shapes). Delete the copy afterwards —
  `*.db` is gitignored, but it is real account data.

`tests/test_api_smoke.py` runs **every read endpoint through the real HTTP stack** against a fixture
carrying the shapes that actually break: a dual-listed ticker, a closed lot, a dividend row with a NULL
net, a pre-ownership zero row, a non-EUR security, CHF as base. Every other test calls services
directly, which is how a `Decimal + None` reached production behind a green suite. `yfinance` is a
raiser for that whole module, so an accidental network reach fails loudly; `/api/portfolio/benchmark`
is excluded because it lazy-fetches Yahoo on a cache miss, and POST routes are excluded because they
start real syncs. **Add a case here when an endpoint's response shape changes.**

Tests (933 backend + 431 frontend as of 2026-08-14, all offline — no IBKR, Yahoo or FX-provider
calls). Take the number the suite actually prints as your baseline, not this line — it has been stale
by 200+ on both halves before:
```bash
cd backend && ./venv/Scripts/python.exe -m pytest tests/ -q
cd frontend && npx tsc -b && npm run test && npm run build
```

Useful:
```bash
# masked token check (never echo the whole thing)
grep -o '^IBKR_TOKEN=.\{0,4\}' backend/.env

# data snapshot on the VPS
python3 -c "
import sqlite3; c=sqlite3.connect('/root/IBKR_investment_tracker/backend/portfolio.db')
c.execute('PRAGMA busy_timeout=30000')
for q in ['select count(*) from securities','select count(*) from taxlots where is_open=1',
          'select count(*) from trades','select source,count(*) from dividend_payments group by 1']:
    print(q, c.execute(q).fetchall())"
```

---

## Ticker mapping & currency

**Ticker mapping** (`market_data_service.py`) — three tiers: custom `ticker_mappings` row → exchange
suffix → try variations (`.DE`, `.F`, `.L`, bare), then auto-save what worked.
`EXCHANGE_SUFFIXES`: `XETRA`/`IBIS2`→`.DE`, `LSEETF`→`.L`, `AEB`→`.AS`, `KRX`→`.KS`, `TWSE`→`.TW`;
`TSE` is Tokyo (`.T`) **except** for CAD listings, where IBKR means Toronto (`.TO`). Add new exchanges
here when a security has no prices, and verify a ticker in a browser first.

**A wrong auto-discovered mapping is sticky and silent** — the failure that put `SBI@TSE` (Toronto, CAD)
on a US fund for months. The last variation tried is the **bare symbol**, which readily matches an
unrelated US listing of the same ticker; it fetched cleanly, so it was auto-saved — and because tier 1
(`ticker_mappings`) is consulted *first*, that row then shadowed the `.TO` suffix logic even after the
logic was fixed. `_get_currency_from_ticker()` compounded it: with no suffix to read it fell back to
"use the security's currency", stamping USD prices **CAD**, so nothing downstream could see the
mismatch. Result: the position was carried 61% high (7.70 vs 4.78 CAD).

Two guards now: prices carry **the currency Yahoo reports** (read from the history metadata already in
the response — no extra request), and a variation whose currency disagrees with the security's is
**rejected, not adopted, and not saved**. Tests: `tests/test_market_data_service.py`.

**A minor-unit quote defeats that second guard rather than tripping it, so the amount is scaled too.**
Yahoo reports London equities in `GBp` (pence), Johannesburg in `ZAc`, Tel Aviv in `ILA`. The code used
to `.upper()` those into the major code and store the number unchanged — a £5.12 close persisted as
512.4 GBP, **100× high**. And because the normalised code then *matches* the security's own currency,
the disagreement check passes and nothing downstream can see it: the SBI failure with its one safeguard
removed. `MINOR_UNIT_CURRENCIES` now maps each to `(major_code, divisor)`, the label and the amount move
together, and a `TICKER_CURRENCY_OVERRIDES` hit is **never** scaled — an override means someone read the
listing, so the reported code is not evidence.

Explicit map rather than inferring from letter case: `GBp`/`ZAc` are mixed-case but `ILA` is not, so
"not all-uppercase means minor unit" would silently miss Tel Aviv. Extend it like `EXCHANGE_SUFFIXES`.

Latent today, not live — this account holds no GBP security, and its one London line is `SMH@LSEETF`,
a USD ETF pinned `manual` to `SMH.L` (which is also why bare-ticker auto-discovery never grabbed the
NASDAQ SMH). The trap opens the day a UK equity or a GBP-line ETF is bought.

### Managing mappings — `app/cli/manage_mappings.py`

This table decides where every price comes from and was the last one still edited by hand-written SQL
over ssh, where a typo mis-prices a position and looks like a real price. Use the CLI instead:

```bash
docker exec backend-portfolio-backend-1 python -m app.cli.manage_mappings list
docker exec backend-portfolio-backend-1 python -m app.cli.manage_mappings set 2330 TWSE 2330.TW
docker exec backend-portfolio-backend-1 python -m app.cli.manage_mappings disable SBI TSE --purge-prices
```

- **`list`** prints the security's currency beside the one its Yahoo ticker implies. A disagreement
  between those columns *is* the SBI bug, so it's flagged explicitly rather than left to be noticed in
  the portfolio total.
- **`set`** stamps `source='manual'` and **refuses** a ticker whose suffix contradicts the security
  (`SBI.L` → GBP under a CAD security). It reuses `_get_currency_from_ticker()`, so the CLI and the
  fetch path can't drift apart. A *bare* ticker implies no currency and so can't be refused — for a
  foreign listing it warns loudly instead, since that's the exact SBI shape. Storing a mapping before
  the security exists is allowed on purpose: that's how you pin one ahead of the statement.
- **`disable`** sets `is_active=False` rather than deleting (`get_mapping()` already filters on it), so
  the row stops being consulted while the record of what was tried survives. **`--purge-prices`** is the
  whole recovery in one step: drop the prices **and the yfinance dividend estimates** the bad mapping
  produced, and let a scheduled job refill them — incremental caching fetches only missing dates, so
  it costs **no ad-hoc Yahoo call**. IBKR dividend rows are never touched. It purged prices only
  until 2026-07-30, which is how SBI's poisoned estimates outlived its mapping fix.
- `--dry-run` on both mutating commands; every edit records a `sync_runs` row (`manual_mapping`),
  because a mapping change being invisible is why SBI went unnoticed for months.

Tests: `tests/test_mapping_cli.py`.

**Currency** — Frankfurter at `https://api.frankfurter.dev/v1` (`.app` now 301-redirects here).
Batch-fetches date ranges (one call per ~30 days) and carries the last known rate forward across
weekends/holidays.

Frankfurter republishes the **ECB reference rates**, so its list is fixed at the ECB's 30 + EUR and will
never include TWD, RUB, QAR or SAR. That is not cosmetic: an unconvertible currency makes
`reconcile_taxlots()` skip the lot, so the holding vanishes from the portfolio *and* the tax report
(counted in `taxlots_skipped`, reported in `warnings[]`). Buying TSMC on TWSE hit exactly this.

So `CurrencyService` falls back to `https://open.er-api.com/v6/latest/EUR`, tagging rows
`source='er-api-latest'`. It is EUR-based (`rate = rates[to] / rates[from]`) and **latest-only — there
is no free historical endpoint**, so it is used only within `FALLBACK_MAX_AGE_DAYS` (7) of today and
refuses older dates rather than backdating a current rate onto an old tax lot. Never raises;
`get_exchange_rate()` owns that decision.

`get_exchange_rate()` resolves in this order, and the order is the design:
**cache → Frankfurter → carry-forward → fallback → carry-forward → raise.**

- Carry-forward comes *before* the fallback because Friday's real ECB rate beats today's
  approximation over a weekend. It preserves the source tag of the row it copies.
- The fallback now also covers currencies that *are* in `SUPPORTED_CURRENCIES` when Frankfurter
  answers with nothing. That demotes the hardcoded list from load-bearing to advisory: a provider
  outage or the ECB list drifting degrades the rate instead of erasing a position (a raise here makes
  `reconcile_taxlots()` skip the lot, so the holding disappears from the portfolio *and* the tax
  report). It is never reached while Frankfurter is answering — no silent provider switching.

**`WARM_CURRENCIES`** (`TWD, CNH, AED, SAR, QAR, KWD, RUB, CLP, COP`) is warmed daily by
`sync_exchange_rates` alongside the currencies actually held, via `warm_rates()`. Only currencies
Frankfurter *cannot* serve are listed, because those are the ones whose history can only accumulate
forward — an ECB rate is retrievable at any time, so warming it would be waste. The whole set costs
**one** request (`_fetch_fallback_table()` returns all ~166 at once), which is what makes daily
affordable. Without it, TWD only got a rate on days a lot happened to need one, so missing the 7-day
window once meant the lot was skipped forever. Extending the list is one edit and no extra request.

Tests: `tests/test_currency_fallback.py`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `has no attribute` / `is not a valid` in a sync | IBKR schema drift. `_sanitize_flex_xml` should absorb it — if not, extend it generically (never patch one field) + add a test |
| A sync warns about dropped attributes | Only fields the ingest **reads** warn now, and that means data may really be affected — read the names. The ~27 harmless ones IBKR sends every time are in the run's `details` as `flex_schema_notes`, not the banner |
| Yield on cost looks low after buying more, or after selling and rebuying | Fixed 2026-08-05. It is the *projected* annual rate over cost, so it tracks the shares held now. A rebuy at a higher price genuinely lowers it — more capital for the same income. If it still looks off, `forward_yield_pct ÷ yield_on_cost_pct` must equal market value ÷ cost for that row |
| `Code=1025` | Token lockout, usually self-inflicted. **Wait**, don't retry. The schedule recovers it |
| `Code=1001` | Not ready. Polled while retrieving; **fatal at the request step** — never re-request, a later job handles it. Since 2026-08-08 the common cause of a *run* of these is gone: the guard skips a slot once the ET day's generation is spent, so a `1001` now means a genuine refusal |
| The Sync button says *Already up to date* | Working as intended, and not an error. IBKR issues about one statement per US-Eastern day; today's has landed, so there is nothing to fetch. The panel says when the next one becomes available. *Sync anyway* is only worth pressing after editing the Flex Query in the portal — the one thing that resets the daily generation — because a forced refusal spends `Code=1025` budget |
| A scheduled run reads `skipped` in the history | Two different causes, told apart by `reason`. `already_generated_today` is the 00:00 Berlin slot correctly doing nothing because 18:00 succeeded — the normal daily state. `pipeline_busy` is a `single_flight` collision, where the next slot recovers freshness |
| Sync 200 but 0 trades | Flex Query section/period not covering them |
| `dividend_source` stuck on `yfinance_estimate` | No `<CashTransactions>` ingested — check the section + Withholding Tax option |
| Yahoo 404/429 | **Stop.** Wait 30-60 min. Check `yfinance >= 1.1.0` |
| A position is missing from the portfolio | Check `taxlots_skipped` + `warnings[]` on the sync run — usually a currency neither FX provider covers |
| The value chart declines toward zero over recent days | The chart says so itself now — a yellow notice above it names the day and holding counts. It means `unpriced_holdings > 0`: holdings whose price fell outside the 14-day lookback are counted at cost but not at value, and past 15 days every one drops out and it reads −100%. The cause is a stalled market-data sync, not a loss. The risk metrics already exclude those days |
| A Monthly Returns cell reads `–`, or a figure carries `†` | One holding could not be valued on those days, so the whole day is dropped rather than counted at a wrong value. Hover the cell: the tooltip names the days the figure *does* cover, which can be far shorter than its column. A spun-off line whose lots predate its listing is handled (see *A spun-off line is not held…*); anything else is a real gap — find the security in the market-data sync's `warnings[]` |
| Market Value looks low and a yellow notice sits above the cards | `summary.unpriced_holdings > 0`: a holding could not be valued, so the total omits it while Cost Basis keeps its cost. Not a loss — find the security via the market-data sync's `warnings[]` and check its `ticker_mappings` row |
| A position shows 0.00 / `market_price: null` | No cached price. The market-data sync's `warnings[]` now names it. Fix the `ticker_mappings` row, or fill it with `app/cli/import_prices.py` from IBKR bars |
| A price looks like an intraday value, not a close | Expected inside the session, and it self-corrects: a weekday within `PROVISIONAL_PRICE_DAYS` (3) is re-fetched at every slot, so the settled close lands after the market shuts. Still wrong **after** 3 days is a real freeze. **Don't diagnose it from `created_at`** — see below |
| A market-data run reports `rate_limited: true` | Yahoo returned 429 and the pass stopped deliberately, leaving the later securities on their previous prices. **Do not trigger a manual sync** — wait; the next slot resumes and re-fetches only what is missing |
| A benchmark's last point looks stale while the portfolio moves | The scheduled warm-up does not refresh provisional benchmark rows (Yahoo budget — eight tickers, seven slots). Opening the chart refreshes the one selected |
| The chart steps at a split date | Cached pre-split closes. A *new* split purges them automatically; for an older one delete that security's `market_prices` and let 08:00 refill |
| A new currency appears | Nothing to do if it's in `WARM_CURRENCIES` or the ECB set. Otherwise add it there — one edit, no extra request |
| "Money added" spikes in one month | A transfer booked as a deposit. `manage_cash_flows list`, then `reclassify <ib_key> --as TRANSFER_IN`. **Never** trust an Added figure without eyeballing that list first |
| "Money added" is blank or `—` | Expected before `deposits_from`: no IBKR deposit ledger exists for the pre-transfer years. Not a bug — Deployed covers that era |
| Realized gains look low + a `warnings[]` entry names a currency | No FX rate for that trade date, so the sale was **omitted** rather than mis-scaled. Check `WARM_CURRENCIES` covers it |
| Steuerwert reads `—` instead of a number | `holdings_snapshot_error`: the snapshot raised. Check the logs — this is deliberately *not* 0.00 |
| A dividend forecast looks invented | Check `forecast_samples` on the breakdown row — n≤2 is a guess. Then `manage_mappings list` for `DIVIDENDS PREDATE MAPPING`, and purge with `purge_dividend_estimates` |
| A trailing yield looks far too low | `trailing_yield_partial`: the position wasn't held a full year, so partial income is over a full position value. Not a bug, and deliberately not annualized |
| Dividend Yield reads a fraction of a percent | Expected on this book: most of it is growth equity plus seven accumulating ETFs, which correctly contribute nothing. Read the *N of M pay* footnote before suspecting the figure |
| Dividend Yield reads `—` / *No projected dividends* | Nothing projects among the **priced** holdings. Check the market-data sync's `warnings[]` for an unpriced payer — deliberately not 0.00%, which would claim the portfolio pays nothing |
| Dividend Yield says *couldn't load* | The `/api/dividends/breakdown` query failed. The other four cards on that row still render, by design — it is not gated on this one |
| Yield on Cost ≠ Dividend Yield × some factor you expect | The factor is exactly market value ÷ cost basis, nothing else: same numerator, same securities. If it isn't, one denominator has picked up a different holding set |
| A position's value is far off IBKR's | Suspect the `ticker_mappings` row before the price feed: run `manage_mappings list` and look for a currency disagreement, then compare `market_prices.close_price` against IBKR's `market_price` in the *same* currency |
| A UK/Johannesburg/Tel Aviv position is ~100× out | A minor-unit quote (`GBp`/`ZAc`/`ILA`) that wasn't scaled. `MINOR_UNIT_CURRENCIES` handles it; note the currency guard **cannot** catch this one, because the normalised label matches the security. Check the sync log for "quoted in GBp (minor units)" — its absence on a London equity is the tell |
| App total ≠ IBKR total | Compare against `gross_position_value`, **not** net liquidation (which adds cash); and intraday the app holds the last *close* while IBKR quotes live |
| Site "down" in the browser | Often TIM home DNS, not the server — verify with `Test-NetConnection`, not `nslookup` |
| Deploy says health FAILED | Usually the premature check; re-curl `/health` after ~15s |
| A write returns 401 | `API_ADMIN_TOKEN` is set and the browser has no key. Lock button in the header; the same value goes in `backend/.env` |
| A request returns 429 with `Retry-After` | Either the sync cooldown (`single_flight`) or the per-IP limit (`RATE_LIMIT_PER_MINUTE`). The response body says which |
| "Which build is live?" | `curl /health` — it reports `version`, `commit`, `scheduler_enabled`, `write_auth_enabled` and `scheduler_jobstore_persistent`. Same line in the app footer |
| A missed sync was **not** recovered after a restart | `curl /health` for `scheduler_jobstore_persistent`. `false` means the store fell back to memory, so misfire recovery is off — `/api/scheduler/status` still lists every job and cannot tell you this. Check the compose mount is the `scheduler-data` **directory** |
| A 500 with no detail | By design. Grep the container log for its `request_id`, which is in the body and the `X-Request-ID` header |
| The container log has no `app.*` lines | Fixed 2026-08-04. `settings.log_level` configured nothing, so Python's last-resort handler emitted WARNING+ only and every `logger.info` was discarded. If it recurs, check `_configure_logging()` still runs and still passes `force=True` — uvicorn installs handlers first, and `basicConfig` is a silent no-op when one exists |
| A log timestamp disagrees with a Berlin slot by 2h | Expected: `%(asctime)s` is the container's local time and the image sets no `TZ`, so logs are UTC while the schedule is Europe/Berlin |
| A missed sync ran late after a restart | Expected: the persistent job store honours a misfire for 30 min. Older than that is dropped, and the next slot recovers |
| A drift row reads `—` instead of advice | No target (unmanaged — blank is not 0%), or the position has no cached price so it has no weight to compare. Both deliberate; see *Client-side analytics* |
| Drift says "no target could be compared" | Nothing had both a target and a weight. **Not** an all-clear — that wording exists because "0 outside the band" was one |
| A drift or currency panel says it couldn't load positions | The positions query failed. The panel refuses to build a plan from absent data rather than reporting a portfolio of unheld rows |
| Currency exposure looks wrong for an ETF | It is quote currency, not economic exposure, and deliberately not re-attributed — a EUR-listed S&P tracker is EUR-quoted with USD risk. The fund share is named on screen |
| A recently bought holding sits in an *Unknown* sector or region | Expected, and correct rather than missing. `sync_helper` never writes `sector`/`country`, so an IBKR-ingested security has both NULL while `asset_type` has a `"Stock"` column default. Only `POST /api/allocation/sync` fills them and **nothing schedules it** (it needs Yahoo), so run it by hand. Before 2026-08-05 the holding was silently dropped from those two charts instead, which made them sum to under 100% under a "% of portfolio" label. A **mapped ETF** is the exception and needs no sync at all — `app/etf_mappings.py` supplies its sector, region *and* asset type live at read time |
| Look-through coverage is below 100% | Expected, and it cannot reach 100%. VWCE publishes no machine-readable basket at all and DBPG is excluded by design, so those two are a permanent floor under the gap; the rest is each decomposed fund's own residual. Read the `funds` table: every fund is named with the reason its constituents are unknown. **Every company row is an understatement by whatever those funds hold**, and nothing is rescaled to hide it — that is the yellow notice above the table, not a bug |
| The Coverage card is amber at a high percentage | It tones on whether any fund is *unresolved*, not on a threshold — green means every held fund is either decomposed or deliberately excluded. That is deliberate: a percentage threshold could not be green even with every obtainable basket loaded, so it would have been a warning that never clears. Amber names the count of funds still missing a basket |
| A basket is badged `†` stale but its issuer publishes slowly | `ADAPTER_STALE_DAYS` is per-source, so `†` means *the issuer has newer holdings we failed to fetch* rather than *this feed is slow*: 7 days for the six that republish daily (Xtrackers, iShares, Invesco, First Trust, Defiance, VanEck), 75 for Vanguard US (month-end, ~6-week lag by design), 45 for a hand import. A quarterly source needs its own entry — do not raise the global default to silence it |
| A company appears twice in the look-through table | The two rows share no identifier, and there are three causes in order of likelihood. (1) `key_type: ISIN` — no LEI and no shareClassFIGI on record, so run `python -m app.cli.resolve_identities`. (2) The row came out of **GRID or QTUM**, whose issuers publish a CINS or a SEDOL and no ISIN, so it has no identity at all until `resolve_identities --constituents` runs — and note a **basket re-import deliberately clears that resolution**, so it is the second half of every import for those two funds. Set `OPENFIGI_API_KEY` first. (3) Both rows are already resolved, which is a genuine gap no identifier closes — an ADR against its ordinary, or a dual-listed company with two legitimate LEIs — and needs an `ISSUER_OVERRIDES` entry with its evidence |
| A look-through row is named `台灣積體電路製造…` or similar | GLEIF's legal name is the company's real one in its own script, and it is preferred only when Latin. That row has no OpenFIGI name cached — re-run `resolve_identities`, which fills `figi_name` |
| Look-through total ≠ the Market Value card | It cannot be: both come from `get_positions_breakdown`, and `test_api_smoke.py` pins them equal. If they differ, one of them is not the deployed build — check `/health`'s commit |
| A look-through fund reads `Basket rejected` | Its stored basket accounts for less than `MIN_BASKET_COVERAGE_PCT` (80) of the fund, which is a half-parsed file rather than a fund holding mostly cash. Re-import it; the previous basket is kept on a refused import |
| Beta is blank with a benchmark selected | Fewer than the 20 flow-free days a regression needs; the count so far is in the footnote. Flow days are excluded by design |
| Sharpe, Volatility and Sortino all read `—` on a short range | Expected, and now consistent: all three need 5 daily returns. MTD in the first days of a month gives 2–3. Sharpe used to show `0.00` here instead, which looked like a measurement |
| Top 5 Weight reads `—` / *No priced positions* | Nothing held resolved a price, so there is no weight to concentrate. Deliberately not `0.0%`, which the tone ladder would have drawn **green** — a reassuring all-clear from no data |

---

## Correctness sweep (2026-07-29)

A three-part audit (valuation core / API surface / frontend) produced 16 fixes, all shipped and
deployed overnight; the test suite went 190 → 241. The ones that changed **stated behaviour** are
documented in place above — token redaction and the single-flight gate under *Sync schedule*, the
close-date convention, disposal inflows and the swept timeline under *Reconciliation*, the era splice
and forecasts under *Dividends*, the holiday rule under *split invalidation*. Also fixed and worth
knowing: the FX carry-forward is now bounded at **30 days** (it had no bound, so a months-stale rate
could be stamped onto a new lot's persisted `cost_basis_eur` — past the bound it raises and the lot is
skipped *with* a warning, which self-heals); the price-currency map picks the **newest** row
deterministically and warns on a mixed history instead of applying an arbitrary row to the whole series;
two composite indices were added (`taxlots(security_id, is_open)`,
`exchange_rates(from_currency, to_currency, date)`); and the UI now renders sync **warnings** (they ride
on *successful* runs and were structurally unreachable before — the SBI silent-failure class) and shows
an explicit error state instead of "No portfolio data — sync to get started" when the backend 500s.

## Current state (2026-07-28)

**40 securities, 36 open positions, 975 open tax lots, 67 trades, 62,178.99 CHF.** The 27 Jul
statement was ingested **offline** from a browser download (`ibkr_manual_xml`, 04:16 UTC) rather than
waiting on the Flex API, because three consecutive IBKR jobs had returned a plain `1001`. That is the
escape hatch working as designed: no token spend, no `1025` exposure, and `full_sync` re-ingesting the
same statement afterwards is idempotent.

`taxlots_skipped: 0`, `prices_invalidated: 0`, no "unsupported currencies" warning, and
`find_stale_priced_securities()` returns empty — every held security has a current price.

**The external cash ledger is live on production.** The Flex Query now carries
Deposits & Withdrawals (at **Detail**) and the **Transfers** section (at **Transfer** level, not Lot —
Lot would emit a row per transferred lot and bury the cash leg in the list that has to be audited).
The statement ingested **47 cash flows = 25 deposits + 22 in-kind transfers**, 0 skipped, and
**0 reclassified** — correct, because the transfer carried no cash (see the contributions section).
No manual reclassification was needed, so the automatic guard has never had to fire on this account.
`manage_cash_flows list` shows all 22 transfer rows as *not* counted, which is the audit to run before
trusting any money-added figure.

It reached production through the **offline path** (`ibkr_manual_xml`), not the Flex API: three
consecutive IBKR-only jobs had returned a plain `1001`, so a browser download was ingested instead —
no token spend, no `1025` exposure, and the next `full_sync` re-ingests the same statement idempotently.

`deposits_from` lands a few days *before* the transfer date, so the ledger genuinely starts at the
account's first funding. The set includes one real withdrawal (negative amount, sign preserved) and one
CHF deposit against an otherwise-EUR ledger, which is what exercises the FX path end to end. Note IBKR
sends the **legacy** `type="Deposits/Withdrawals"` spelling, not `"Deposits & Withdrawals"` — ibflex maps
both to `CashAction.DEPOSITWITHDRAW`, so nothing special is needed, but don't "fix" the enum comparison
if that string looks wrong.

`coverage_from` = the ledger's **first row**, in the second week of January — *not* the statement period
start, which is 1 Jan because the query is YTD. The account's first deposit and its first execution land
on the same day, so that is genuinely the date IBKR becomes the whole picture; the clamp described in the
contributions section is what stops the pre-account days of January being claimed as covered and their
purchases dropped. The incoming transfer arrives *later* than that date, which is why the boundary is the
ledger start and not the transfer. All-time and 12M come out `spliced`; 6M and 3M run on
**deposits alone** and are already rotation-proof.
Where both sources overlap they agree to within **~12%** — two independent derivations (lot cost basis
vs. the cash ledger) landing that close is the best available evidence that the pre-ledger lot-based
figures were sound. `Σ monthly[].net_eur` matches the open-lot cost basis **to the cent in EUR**; in CHF
it lands a few francs off, which is the per-date FX projection on four closed lots and not an error —
see the identity check in the contributions section.

**That statement carried a large IBKR schema drift and needed no code change.** 20+ new attributes
(`figi`, `issuerCountryCode`, `serialNumber`, `weight`, `subCategory`, `exDate`, `dividendType`,
`origTransactionID`, `initialInvestment`, …) plus a `Trade.notes` value `RI` that ibflex can't convert
to its enum tuple. `_sanitize_flex_xml()` dropped all of them generically. This is the case the
sanitizer was written for — don't start patching attribute names.

**The two new positions both landed cleanly.** `2330@TWSE` (TSMC, 12 sh, TWD) and `SOXQ@NASDAQ`
(7.5 sh, USD), both bought 2026-07-27. They worked because the FX rates their lots are valued at were
already cached for that exact date (`reconcile_taxlots` uses `open_date`) — without the TWD row TSMC
would have been silently skipped. `2330/TWSE → 2330.TW` is pinned `manual`; **SOXQ deliberately has no
mapping** and resolved through the bare ticker, which is correct: `NASDAQ` gives an empty suffix, so
that *is* what tier 2 produces, and with no suffix `_get_yahoo_ticker_variations()` returns a single
candidate — the bare-symbol auto-save that poisoned SBI is never reached.

**SBI is fully repaired: 4.79 CAD / 276.83 CHF, 501 cached prices.** Its poisoned rows were deleted
(backup: `/root/ibkr-backups/sbi-poisoned-2026-07-27.json`), 20 days were imported from Client Portal
bars (`source='ibkr'`, `2026-06-29..07-27`) and Yahoo later filled the other 481 via the `manual`
`SBI/TSE → SBI.TO` mapping. The two windows don't overlap — the imported dates were never re-fetched,
exactly as `get_missing_dates()` implies. Yahoo's 27 Jul close for `SBI.TO` came back at **4.79 CAD**,
identical to IBKR's own bar, which independently confirms both the mapping and the import.

Dividends: 57 cash-transaction rows → 26 IBKR dividend payments, all with real withholding, running
from mid-February. 2026 reports `dividend_source='ibkr'` and `realized_source='trades'`; realized is
a small net loss over 4 closed lots. The tax report's `holdings_snapshot_total` matches the portfolio
summary to the cent, which is the shared-code guarantee holding.

Figures are described rather than published, as elsewhere in this file — the repo is public, and a
pasted total also goes stale silently: the ones that used to sit here were superseded when the manual
XML re-ingest upserted corrected amounts, and read as a discrepancy months later. **Check the numbers
against the API or the DB, never against this file.** The reconciliations worth keeping are the
*relationships*: per-date FX means the IBKR EUR net and the tax report's base-currency net differ by a
percent or two rather than matching exactly, and the breakdown's year total is the IBKR era plus the
estimates that precede its boundary.

**Reconciled against IBKR to 0.12%** on 2026-07-27. Compare the app against `gross_position_value`,
never net liquidation (which adds cash and accrued dividends) — "buying power" is a margin metric
derived from that same cash, not a separate bucket.

Cross-checked against IBKR via the MCP connector: IBKR lists **282 YTD trades = 218 `CASH`** (FX
conversions, correctly filtered out) **+ 64 `STK`**, and the 64 match ours symbol-for-symbol. Same-day,
same-price pairs (e.g. NU 7 @ 17.205 twice on 2026-02-04) are **genuine separate fills**, not duplicates.

**Prior years remain estimates** — the rolling 30-day query can't reach them. Backfilling 2025 needs a one-off period
change in the Flex Query (see the tax section); until then 2025 correctly reports
`dividend_source='yfinance_estimate'`.
