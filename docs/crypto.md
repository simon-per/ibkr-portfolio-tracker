# The crypto book — CoinStats, kept apart from the stocks

> Written 2026-09-28 with the feature. **This file is authoritative for its subsystem**, the way
> the other `docs/` files are for theirs: update it in the same change that moves the code it
> describes.

The owner holds crypto on **Binance** and in a **Phantom** wallet, both connected to
**CoinStats**. The app shows that book in its own **Crypto** mode, switched from the header
(Stocks ⇄ Crypto, beside the light/dark toggle). It is deliberately not a third account: the
stock book and the crypto book never share a figure.

| where | what |
|---|---|
| `app/services/coinstats_client.py` | the only CoinStats caller: headers, pacing, credit costs, typed errors |
| `app/services/crypto_service.py` | `CryptoSyncService` (sync) and `CryptoService` (reads) |
| `app/models/crypto.py` | `crypto_snapshots`, `crypto_holdings`, `crypto_daily` (migration `w6f3b0c7d1e2`) |
| `app/routers/crypto.py`, `app/schemas/crypto.py` | `/api/crypto/{portfolio,history,status,sync}` |
| `app/cli/coinstats_probe.py` | prints the *shape* of every CoinStats answer, never a value |
| `frontend/src/components/Crypto*.tsx`, `lib/portfolioMode.tsx` | the Crypto mode |

## Separate by construction, not by filter

Pillar 3a **blends** everywhere except where it must not (`docs/pillar3a.md`). Crypto is the
opposite: it blends **nowhere**, and not because every stock reader filters it out — because
nothing on the stock side can see it. It has its own tables, service, router and view; no crypto
row is ever written to `securities`, `taxlots`, `trades`, `cash_flows`, `market_prices` or
`dividend_payments`, and crypto is **not** an `account` label (that would put it in every
valuation, XIRR, allocation, look-through, benchmark, tax and contribution reader at once).
Exclusion by not writing the row — the same move that keeps 3a fees out of the dividend ledger —
applied to a whole asset class.

`tests/test_crypto_isolation.py` pins both directions over the AST of `app/`, with allowlists:
which non-crypto modules may reference crypto code (the models registry, `main.py`, the scheduler
job, and `routers/scheduler.py`, which names the crypto run type only to *exclude* it), and which
shared modules crypto may import (settings, database, clock, redaction, gates, FX, app settings,
sync history — no stock model, service or repository). A new module on either side fails until
someone decides it belongs.

## The data source — a share token, not a portfolio id

CoinStats' public API (`https://api.coinstats.app/v1`, header `X-API-KEY`) reaches a portfolio
connected **in the CoinStats app** only through the token behind a **share link**
(coinstats.app/portfolio → Share → Generate Link → the segment after `/p/`), sent as the header
`sharetoken`, plus `passcode` if the link has one. The API's `portfolioId` covers only portfolios
connected *through the API* (`POST /portfolio/wallet|exchange`), which would mean handing Binance
keys to CoinStats a second time. **One combined share link** covers both connections (owner's
choice, 2026-09-27): CoinStats' combined P&L already accounts for coins moving between Binance and
Phantom.

Anyone holding the share token can read the portfolio, so it is a secret exactly like the key.
Both, and the passcode, live only in `backend/.env` (`COIN_STATS_API_KEY`,
`COIN_STATS_SHARE_TOKEN`, `COIN_STATS_SHARE_PASSCODE`), travel **in request headers, never the
URL** (httpx error strings carry the URL, and those strings reach `sync_runs.message`), and the
two long ones are masked by `app/redact.py` as a second line of defence. The six-digit passcode is
deliberately not a literal mask — replacing six digits would mangle any figure containing them —
and it never leaves a header. The account-level `/usage/credits` call is sent without the share
token at all.

## Credits are the budget

CoinStats charges credits per call against a monthly allowance; the key is on the **free plan,
20,000 credits/month** (read through the free `/usage/credits`, 2026-09-28). The documented rate
limit is 2 requests/second, and **the real one is tighter**: the probe's third call, 0.6 s after
the previous one had *started*, came back 429 "Rate limit exceeded". The client now waits
`MIN_REQUEST_INTERVAL_S` (2 s) after each response *completes*; a pass is three to five calls.
Costs, named once in `coinstats_client.CREDIT_COST` — and **measured exact**: the probe spent 53
by these constants and 53 by CoinStats' own balance:

| call | credits | when |
|---|---|---|
| `/usage/credits` | 0 | every run, first — so a run knows its balance before spending |
| `/portfolio/value` | 10 | every run |
| `/portfolio/coins` | 8 per page | every run (page size 100, cap `MAX_COIN_PAGES = 5`) |
| `/portfolio/chart?type=all` | 10 | the daily history pull |
| `/portfolio/pl/history?interval=daily&range=all` | 25 | the daily history pull |

Eight scheduled runs a day at one coin page plus one history pull is ~5.5k credits a month;
`tests/test_crypto_budget.py` multiplies the constants and pins both that case (≤ 35% of the
plan, leaving room for the button) and the worst scheduled month (every run at the page cap,
every history pull failing and retried) inside the allowance. **A spam-heavy wallet is the thing
that moves this**: every hundred airdropped tokens is another 8 credits on every sync, which is
why the run warns above one page and refuses above the cap rather than truncating.

Guards, all derived from those costs: a run with fewer credits than one snapshot costs records
`skipped/credits_exhausted` and asks nothing else; the history pull is skipped below
`HISTORY_CREDIT_FLOOR` so the month's last credits go to the snapshot; `warnings[]` says so under
a fifth of the plan.

## The sync — two refuse-whole units, all HTTP before any write

`CryptoSyncService.sync()`:

1. **Not configured** (no key or no share token) → return `None`. No request, and **no
   `sync_runs` row**: the view and `/api/crypto/status` already say `configured: false`, and eight
   skipped rows a day would only be noise.
2. **Snapshot unit** — `/portfolio/value` then every page of `/portfolio/coins`. Stored in **one
   short transaction or not at all**. Refused, keeping the previous snapshot, when any page fails,
   when there are more than `MAX_COIN_PAGES` full pages, or when the coin list is empty beside a
   positive `totalValue` (the wipe guard — storing that would draw every coin as sold).
3. **History unit** — `/portfolio/chart` + `/portfolio/pl/history`, only while no pull has
   succeeded yet today (Berlin) and fewer than `HISTORY_ATTEMPTS_PER_DAY` (2) have been attempted.
   The attempt is recorded on the snapshot row (`history_status`), which is what stops a failing
   pull being re-asked at every slot, 35 credits a time. Replaces `crypto_daily` wholesale
   (CoinStats recomputes history when a transaction syncs late, so a merge would keep numbers it
   has corrected), or is refused when empty or shorter than half of what is stored.
   **A history or FX failure is a warning on a successful run** — it never costs the snapshot the
   run already paid for.
4. **FX warm-up** — once the history is attempted: USD→EUR and EUR→CHF/USD over the crypto window
   through `CurrencyService.warm_rates`, one pair per session so no write lock spans the next
   pair's request; the whole window only while the cache does not reach within
   `FX_WARM_SLACK_DAYS` (7) of its start, a week otherwise. USD is warmed here because the stock
   side warms it only while some *security* is held in USD.

No write transaction is ever open during an HTTP call; each write retries once on SQLite's
"database is locked", since the job shares minute :00 with the stock jobs.

**Errors are typed and never retried inside a run** — the next slot is the retry:

| status | meaning | run records |
|---|---|---|
| 401/403 | key or share token rejected, or the plan lacks the endpoint | `error/auth` |
| 409 | "Transactions not synced": CoinStats is still syncing the portfolio | `skipped/not_synced` |
| 429 | rate limit **or** "Insufficient credits" — one status, no Retry-After | `error/quota`, pass abandoned |
| 5xx, transport, non-JSON | transient | `error/transient` |

429 is detected by status code; the Yahoo family test forbids marker tuples.

## Scheduling and gates

Eight jobs `crypto_sync_1…8` at `CRYPTO_SYNC_HOURS = ALL_SYNC_HOURS`, **derived, not copied**, so
the deploy guard (checked against `ALL_SYNC_HOURS` by `test_deploy_guard_hours.py`) already covers
every crypto run and no ops script learned a new hour. Registered through `_add_or_keep` in their
own **job group**. They run concurrently with the stock job at the same slot on purpose: the only
upstream is CoinStats and they hold none of the stock gates.

- The job's gate is `crypto-sync`; **never `SYNC_PIPELINE`**, which would put the stock Sync
  buttons on cooldown. The button (`POST /api/crypto/sync`) adds an outer `crypto-sync-manual`
  gate carrying the 600 s cooldown, so a *scheduled* run never starts the button's cooldown
  (the `manual-trigger` shape in `routers/scheduler.py`). It checks configuration and a run in
  flight before entering, so neither answer spends the cooldown.
- A crypto run **never sets `last_sync_result`**, the stock pipeline's "last sync".
- The public `/api/scheduler/status` lists the **stock group only** (the dashboard reads `jobs[0]`
  as its "Next:", and the daily `ibkr-sync-validator` counts the jobs) and its "last sync"
  fallback reads `SCHEDULED_JOB_TYPES` only — a whitelist. `/api/scheduler/history` leaves out
  `crypto_sync` rows. `tests/test_crypto_public_surfaces.py` pins all three.

## Privacy — the reads are private, and fail closed

The stock book's reads are public; the owner chose not to publish crypto balances the same way
(2026-09-27). `app/auth.py` gates `PRIVATE_PREFIXES = ("/api/crypto",)` for **every method except
OPTIONS** (a CORS preflight carries no credentials by design), and **refuses with 403 when no
`API_ADMIN_TOKEN` is configured** instead of opening up — a lock that opens when misconfigured is
not a lock. The view unlocks with the key the browser already sends from the lock button. Keyed on
the prefix, so a new crypto route is covered the moment it exists (`tests/test_crypto_auth.py`
walks the route table). Every crypto response is `Cache-Control: private, no-store`.

What is never stored or served: **wallet addresses and transaction hashes** (nothing here fetches
transactions at all yet), and **spam token symbols** — an airdropped scam token's "symbol" is often
a phishing URL, so spam is counted, never named. The `sync_runs` row carries type, status, reason
and a message only; counts, credits and warnings live on `crypto_snapshots`, served only by the
gated `/api/crypto/status`.

## Storage — USD, `Float`, one snapshot's holdings

- **USD, as CoinStats keeps its books.** Per-coin cost, average buy and P&L exist only in USD. The
  one exception to "all money is stored in EUR"; the read path projects it like everything else.
- **`Float`, not the stock tables' `Numeric(18, 6)`.** SQLite reads a Numeric back rounded to its
  scale, so six decimals would turn a 1.2e-8 token price into 0 and a valued coin into one that
  looks unpriced. These are a third party's display figures, not ledger amounts.
- `crypto_snapshots` — one row per successful sync (so it doubles as an intraday value history),
  with counts, credits and warnings. `crypto_holdings` — keyed by `snapshot_id`; only the newest
  snapshot keeps its holdings (per-coin history is drawn nowhere, and keeping it would be keeping
  more of a private portfolio than anything reads). `crypto_daily` — CoinStats' value history
  and its daily cash-flow-adjusted P&L.
- A coin listed twice is merged by id; an unknown part makes a sum unknown, never a quietly
  smaller known number. Items with no identifier or a non-positive count are skipped and counted.

## Reading it back — database only

A GET must not reach the network or take SQLite's write lock, and
`CurrencyService.get_exchange_rate` does both on a miss (Frankfurter, then a carried row it
*writes*) — for a daily series, every weekend. So `CryptoService` converts through the same
`NativeToBase` every reader uses, over `fx_preload.PreloadedRates`: one query for the window, then a
bounded (14-day) forward-fill. `BaseFx` is loaded from a fortnight before the window with
`backfill=False`, and when there is no EUR→base rate at all the figure is reported unconvertible
rather than served as euros under another currency's label (the edge `BaseFx` itself papers over).
`tests/test_crypto_read.py` seeds weekday-only rates, takes the snapshot on a Sunday and replaces
every network path with a raiser.

`fx_preload` and `base_fx` were extracted for this from the copies that already existed (the
benchmark and portfolio preloads, the `PortfolioService` loader), which now delegate —
`tests/test_exchange_rate_readers.py` pins which modules may select `ExchangeRate` rows.

What the figures mean:

- **Values convert at their own date**: the total, each holding, each history point.
- **Cost, average buy and P&L are CoinStats' USD figures at the snapshot's rate**, which leaves
  out every FX move since purchase. Whenever the base is not USD the response carries
  `fx_caveat`, and the view prints it beside those figures. (Owner's choice, 2026-09-28, over
  showing the crypto book in USD.) Percentages are ratios and need no conversion.
- **Weights are shares of CoinStats' `totalValue`, never renormalised.** The gap between the total
  and the itemised holdings is served as `unitemised_value` — anything CoinStats totals but does
  not list — and drawn as its own slice and row. **Measured 2026-09-28 it is zero**: `totalValue`
  is exactly Σ count × price of the listed coins. A *negative* gap is a warning, not clamped.
- **DeFi is beside the total, not inside it.** `defiValue` is reported separately and is **not**
  part of `totalValue`. The view prints
  it on the surface next to the total as *plus … in DeFi positions*; it is in no weight, slice or
  chart line, because CoinStats' holdings, cost and P&L do not cover it either.
- **Excluded and counted**: spam (`isFake`) and unpriced coins never enter a total; the counts
  (and the unpriced symbols) sit on the surface.
- **24h change** is Σ of the valued holdings' `profit.hour24`, as a share of the itemised value a
  day earlier. A missing FX rate is `None` plus `fx_unavailable`, never 0.
- **Summary and table come from one snapshot**: holdings are read by the snapshot's id, so a sync
  committing between the two SELECTs cannot pair one sync's total with another's table.
- The value history **ends at the newest snapshot**, so the chart is current between daily pulls.
  A snapshot older than 12 hours (`STALE_SNAPSHOT_HOURS`) warns: eight slots a day leave at most
  eight between runs.

**Colour identity.** Coins are coloured by `color_order` — valued coins by CoinStats' market-cap
rank — through the measured `--viz-series-*` palette (`seriesColor` in `lib/dividendColors.ts`).
Rank belongs to the coin, not to its size in this portfolio, so a sync that reorders the holdings
by value cannot repaint the chart (`docs/frontend.md`, *colour by identity*).

## The probe

`python -m app.cli.coinstats_probe` asks each question once (~53 credits) and prints structure
only — key names, types, counts, date spans, ratios. Read-only, records no `sync_runs` row,
ASCII-only output (a Windows console crashed on a Σ). **Never paste its output into a committed
file** beyond what it already prints; it prints no amounts, symbols or addresses by design.

What it established on the first real run (2026-09-28), each now built on:

- `totalValue` is exactly Σ count × price of the listed coins; `defiValue` is extra (above).
- One page of coins; no coin flagged `isFake`; one `isFiat`; every coin priced.
- A coin item carries `averageBuy`, `profit` and `profitPercent` keyed by period (`allTime`,
  `hour24`, `unrealized`, `realized`, `lastTrade`) and `price` in USD, EUR, BTC and ETH — but **no
  per-coin `totalCost`**, whatever the documentation lists. The table shows the average buy
  price and CoinStats' P&L; a per-coin cost stays absent rather than being derived.
- `/portfolio/chart?type=all` is one point **every three days** back to the first activity, so
  the short ranges draw few points. A finer `type=` for them is a cheap follow-up (10 credits).
- `/portfolio/pl/history` is **one day's P&L per point**, not a running total (consecutive
  points differ by about their own size). The view sums them over the visible range from 0 on
  its first day — `rangePnlSeries` in `lib/cryptoChart.ts` — the rule the stock chart's
  Profit/Loss line follows; an unknown day makes every later point unknown rather than 0.
- A share-link passcode can be password-style, not only six digits; `app/redact.py` masks it
  once it is long enough to substitute safely.

## Operating it

- **Set up**: the three keys in `backend/.env`, locally and on the VPS, then on the VPS
  `GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d`, never `restart`. Locally the fields
  must exist in `app/config.py` before the keys go in: pydantic-settings rejects an unknown key in
  the dotenv file it reads itself (in the container, compose passes the file as environment
  variables, which it does not police). The crypto view also needs `API_ADMIN_TOKEN`, which
  production has.
- **Check it**: `GET /api/crypto/status` with the admin key — last run, credits, next run. The
  public scheduler endpoints deliberately show nothing crypto.
- **Force a run**: `POST /api/crypto/sync` with the key; at most every 10 minutes.
