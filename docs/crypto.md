# The crypto book — CoinStats holdings, CoinGecko prices, kept apart from the stocks

> Written 2026-09-28 with the feature; the history rebuilt on our side on 2026-10-02.
> **This file is authoritative for its subsystem**, the way the other `docs/` files are for
> theirs: update it in the same change that moves the code it describes.

The owner holds crypto on **Binance** and in a **Phantom** wallet, both connected to
**CoinStats**. The app shows that book in its own **Crypto** mode, switched from the header
(Stocks ⇄ Crypto, beside the light/dark toggle). It is deliberately not a third account: the
stock book and the crypto book never share a figure.

**CoinStats says what is held; CoinGecko says what it was worth.** Since 2026-10-02 the value
and P&L history is computed here — holdings × prices, from 1 January 2026 — instead of mirrored
from CoinStats' history, which counted coins moving from Kraken (not connected) to Binance as
profit and loss and was unreliable before 2026.

| where | what |
|---|---|
| `app/services/coinstats_client.py` | the only CoinStats caller: headers, pacing, credit costs, typed errors |
| `app/services/coingecko_client.py` | the only CoinGecko caller: the Demo key header, pacing, typed errors |
| `app/services/crypto_book.py` | the formula, pure: holdings timeline, prices, the peg table, closes |
| `app/services/crypto_service.py` | `CryptoSyncService` (sync, price fill) and `CryptoService` (reads) |
| `app/models/crypto.py` | `crypto_snapshots`, `crypto_holdings`, `crypto_daily` (migration `w6f3b0c7d1e2`); `crypto_daily_holdings`, `crypto_coin_prices`, `crypto_coin_ids` (migration `x7a3c9e1f5b2d`) |
| `app/routers/crypto.py`, `app/schemas/crypto.py` | `/api/crypto/{portfolio,history,status,sync}` |
| `app/cli/coinstats_probe.py` | prints the *shape* of every CoinStats answer, never a value |
| `app/cli/crypto_rebuild_holdings.py` | one-off: rebuilds the daily holdings backwards from CoinStats' transactions |
| `frontend/src/components/Crypto*.tsx`, `lib/cryptoChart.ts`, `lib/portfolioMode.tsx` | the Crypto mode |

## Separate by construction, not by filter

Pillar 3a **blends** everywhere except where it must not (`docs/pillar3a.md`). Crypto is the
opposite: it blends **nowhere**, and not because every stock reader filters it out — because
nothing on the stock side can see it. It has its own tables, service, router and view; no crypto
row is ever written to `securities`, `taxlots`, `trades`, `cash_flows`, `market_prices` or
`dividend_payments`, and crypto is **not** an `account` label (that would put it in every
valuation, XIRR, allocation, look-through, benchmark, tax and contribution reader at once).
CoinGecko's prices go to `crypto_coin_prices`, never to `market_prices`. Exclusion by not
writing the row — the same move that keeps 3a fees out of the dividend ledger — applied to a
whole asset class.

`tests/test_crypto_isolation.py` pins both directions over the AST of `app/`, with allowlists:
which non-crypto modules may reference crypto code (the models registry, `main.py`, the scheduler
job, and `routers/scheduler.py`, which names the crypto run types only to *exclude* them), and
which shared modules crypto may import (settings, database, clock, redaction, gates, FX, app
settings, sync history — no stock model, service or repository). A new module on either side
fails until someone decides it belongs; the CoinGecko client, `crypto_book` and the rebuild CLI
are on the list.

## The data sources

### CoinStats — a share token, not a portfolio id

CoinStats' public API (`https://api.coinstats.app/v1`, header `X-API-KEY`) reaches a portfolio
connected **in the CoinStats app** only through the token behind a **share link**
(coinstats.app/portfolio → Share → Generate Link → the segment after `/p/`), sent as the header
`sharetoken`, plus `passcode` if the link has one. The API's `portfolioId` covers only portfolios
connected *through the API* (`POST /portfolio/wallet|exchange`), which would mean handing Binance
keys to CoinStats a second time. **One combined share link** covers both connections (owner's
choice, 2026-09-27).

Anyone holding the share token can read the portfolio, so it is a secret exactly like the key.
Both, and the passcode, live only in `backend/.env` (`COIN_STATS_API_KEY`,
`COIN_STATS_SHARE_TOKEN`, `COIN_STATS_SHARE_PASSCODE`), travel **in request headers, never the
URL** (httpx error strings carry the URL, and those strings reach `sync_runs.message`), and the
two long ones are masked by `app/redact.py` as a second line of defence. The six-digit passcode is
deliberately not a literal mask — replacing six digits would mangle any figure containing them —
and it never leaves a header. The account-level `/usage/credits` call is sent without the share
token at all.

### CoinGecko — the Demo API, for prices

`https://api.coingecko.com/api/v3`, key `COINGECKO_API_KEY` sent **only** as the header
`x-cg-demo-api-key` and masked by `app/redact.py`. The free Demo plan allows about **30 calls a
minute and 10,000 a month**, and serves **365 days** of history (`HISTORY_LIMIT_DAYS`): 1 January
2026 is in reach until the end of 2026, and rows already stored persist after that. The client
waits `MIN_REQUEST_INTERVAL_S` (2.5 s) after each response *completes* — the CoinStats lesson.
Three endpoints:

| call | when |
|---|---|
| `/coins/list` | only when a held coin has no mapping yet, or is unmatched and was last looked up more than `MAPPING_RECHECK_DAYS` (7) ago |
| `/simple/price?ids=…&vs_currencies=usd` | every slot, one call per 50 coins held today: today's `spot` row, overwritten each slot |
| `/coins/{id}/market_chart/range` | per coin, only for finished days it still lacks a `daily` close for, and at most every `CLOSES_RECHECK_HOURS` (6) per coin |

**Errors are typed, never retried in a run:** 401/403 `auth`, 429 `quota` (detected by status
code — the Yahoo family test forbids marker lists), other 4xx `bad_request`, 5xx/transport/shape
`transient`. Any of them **abandons the price pass with a warning**; what was fetched before it
is kept (each price row is a fact on its own), and the snapshot is never lost to it.

**Which CoinGecko coin is it** (`crypto_coin_ids`): CoinStats identifiers are mostly CoinGecko
slugs (`bitcoin`, `ethereum`), so an exact id match resolves most; a **unique** symbol match is
the fallback. **A symbol several CoinGecko coins share is settled by price, never by guessing**:
CoinStats prices every coin it lists, so the candidates (up to `MAX_SYMBOL_CANDIDATES`) are quoted
in one `/simple/price` call and the one trading within `PRICE_MATCH_TOLERANCE` (10%) of CoinStats'
price is the coin — when exactly one does (`method = "symbol+price"`). **When several do, they are
one asset and its bridged copies**, which track its price exactly, so size decides: one
`/coins/markets` call, and the candidate whose market cap is at least `MARKET_CAP_DOMINANCE` (100×)
every other's wins (`symbol+price+cap`); comparable sizes stay unmatched. Found on BNB, 2026-10-03:
CoinStats' id is not CoinGecko's `binancecoin`, and the only other "BNB" is a bridged copy at the
identical 765.42 USD — 102 bn USD market cap against 289 k. **Every new
match, by id or symbol, is price-checked the same way**: more than `PRICE_SANITY_TOLERANCE` (25%)
from CoinStats' price and it is refused with a warning, so a coincidental id cannot value the
position as another token. The same call doubles as today's spot price, so a first sync still costs
one price call. Without CoinStats' prices (the rebuild CLI's past-only coins) neither check runs. An
unmatched coin keeps a row with `coingecko_id` NULL, is named in the run's warnings, is left out of
every figure (below), and is re-asked after `MAPPING_RECHECK_DAYS` — or at the next sync when its
row was stored under older rules: an unmatched row's `method` is `none:v<MAPPING_RULES_VERSION>`,
so **bump `MAPPING_RULES_VERSION` whenever the matching rules improve** and every unmatched coin is
retried at once, with no data migration. (`z9c5e1a3b7d4` was the one-off migration before this
existed.)

**Closes** (`crypto_book.daily_closes`): a CoinGecko point belongs to the UTC day that ends at
or after it, so the daily point at 00:00 UTC closes the day before; inside a day (hourly data for
spans under 90 days) the latest point wins; today is never a close. A day's `daily` row replaces
the last `spot` row of that day.

**Without `COINGECKO_API_KEY`** the sync still stores the snapshot and today's holdings and
records a warning that prices were skipped; every day without a stored price reads as unknown,
and the page renders with dashes.

## Credits are the budget

CoinStats charges credits per call against a monthly allowance; the key is on the **free plan,
20,000 credits/month** (read through the free `/usage/credits`, 2026-09-28). The documented rate
limit is 2 requests/second, and **the real one is tighter**: the probe's third call, 0.6 s after
the previous one had *started*, came back 429. The client waits `MIN_REQUEST_INTERVAL_S` (2 s)
after each response *completes*. Costs, named once in `coinstats_client.CREDIT_COST` — measured
exact (the probe spent 53 by these constants and 53 by CoinStats' own balance):

| call | credits | when |
|---|---|---|
| `/usage/credits` | 0 | every run, first — so a run knows its balance before spending |
| `/portfolio/value` | 10 | every run |
| `/portfolio/coins` | 8 per page | every run (page size 100, cap `MAX_COIN_PAGES = 5`) |
| `/portfolio/transactions` | 4 per page | the rebuild CLI only, once (page size 100, cap `MAX_TRANSACTION_PAGES = 100`) |
| `/portfolio/chart?type=all` | 10 | **no longer asked by the sync** (2026-10-02); the probe only |
| `/portfolio/pl/history?…` | 25 | **no longer asked by the sync** (2026-10-02); the probe only |

Dropping the history pull saves 35 credits a day. Eight scheduled runs a day at one coin page is
~4.5k credits a month; `tests/test_crypto_budget.py` multiplies the constants and pins that case
(≤ 25% of the plan), the worst scheduled month (every run at the page cap) inside the allowance,
the rebuild's worst case (≤ 5%), and a worst CoinGecko month (40 coins, every coin re-asked as
often as the re-check allows) inside the Demo plan. **A spam-heavy wallet is the thing that moves
the CoinStats figure**: every hundred airdropped tokens is another 8 credits on every sync, which
is why the run warns above one page and refuses above the cap rather than truncating.

A run with fewer credits than one snapshot costs records `skipped/credits_exhausted` and asks
nothing else; `warnings[]` says so under a fifth of the plan.

## The sync — all HTTP before any write

`CryptoSyncService.sync()`:

1. **Not configured** (no CoinStats key or no share token) → return `None`. No request, and **no
   `sync_runs` row**.
2. **Snapshot** — `/portfolio/value` then every page of `/portfolio/coins`. Refused, keeping the
   previous snapshot, when any page fails, when there are more than `MAX_COIN_PAGES` full pages,
   or when the coin list is empty beside a positive `totalValue` (the wipe guard).
3. **Prices** — the CoinGecko pass above, fetched *before* any write: which prices are needed
   is computed from the stored daily holdings plus today's set, still in memory.
4. **Writes**: the mapping and prices in one transaction (a failure is a warning); the FX warm-up
   (below); then, in **one** transaction, the snapshot, its holdings and **today's
   `crypto_daily_holdings` set**, replacing that UTC date's rows.
5. **FX warm-up** — once per Berlin day (on the day's first snapshot), USD→EUR and EUR→CHF/USD
   from `CRYPTO_HISTORY_START` minus the lookback, through the **existing** FX path
   (`CurrencyService.warm_rates`, one pair per session); the whole window only while the cache
   does not reach within `FX_WARM_SLACK_DAYS` (7) of its start, a week otherwise. No other FX
   source exists for crypto.

No write transaction is ever open during an HTTP call; each write retries once on SQLite's
"database is locked", since the job shares minute :00 with the stock jobs.

CoinStats errors are typed and never retried inside a run — the next slot is the retry:

| status | meaning | run records |
|---|---|---|
| 401/403 | key or share token rejected, or the plan lacks the endpoint | `error/auth` |
| 409 | "Transactions not synced": CoinStats is still syncing the portfolio | `skipped/not_synced` |
| 429 | rate limit **or** "Insufficient credits" — one status, no Retry-After | `error/quota`, pass abandoned |
| 5xx, transport, non-JSON | transient | `error/transient` |

## Scheduling and gates

Eight jobs `crypto_sync_1…8` at `CRYPTO_SYNC_HOURS = ALL_SYNC_HOURS`, **derived, not copied**, so
the deploy guard already covers every crypto run. Registered through `_add_or_keep` in their own
**job group**. They run concurrently with the stock job at the same slot on purpose: the
upstreams are CoinStats and CoinGecko, and they hold none of the stock gates.

- The job's gate is `crypto-sync`; **never `SYNC_PIPELINE`**. The button (`POST
  /api/crypto/sync`) adds an outer `crypto-sync-manual` gate carrying the 600 s cooldown, so a
  *scheduled* run never starts the button's cooldown.
- A crypto run **never sets `last_sync_result`**, the stock pipeline's "last sync".
- The public `/api/scheduler/status` lists the **stock group only** and its "last sync" fallback
  reads `SCHEDULED_JOB_TYPES` only — a whitelist. `/api/scheduler/history` leaves out every type
  in `crypto_service.PUBLIC_EXCLUDED_SYNC_TYPES` (`crypto_sync`, `crypto_rebuild`).
  `tests/test_crypto_public_surfaces.py` pins all three.

## Privacy — the reads are private, and fail closed

The stock book's reads are public; the owner chose not to publish crypto balances the same way
(2026-09-27). `app/auth.py` gates `PRIVATE_PREFIXES = ("/api/crypto",)` for **every method except
OPTIONS**, and **refuses with 403 when no `API_ADMIN_TOKEN` is configured** instead of opening up.
Keyed on the prefix, so a new crypto route is covered the moment it exists
(`tests/test_crypto_auth.py` walks the route table). Every crypto response is
`Cache-Control: private, no-store`.

What is never stored or served: **wallet addresses, transaction hashes and notes** — the rebuild
CLI reads the transaction list but keeps only (date, coin, signed count) in memory, writes only
daily quantities, and its probe prints key names and signs, never a value from those fields — and
**spam token symbols**: an airdropped scam token's "symbol" is often a phishing URL, so spam is
counted, never named. The `sync_runs` row carries type, status, reason and a message only;
counts, credits and warnings live on `crypto_snapshots`, served only by the gated
`/api/crypto/status`.

## Storage — USD, `Float`, and per-coin quantities per day

- **USD**, as CoinStats keeps its books and CoinGecko quotes them. The one exception to "all
  money is stored in EUR"; the read path projects it like everything else.
- **`Float`, not the stock tables' `Numeric(18, 6)`.** SQLite reads a Numeric back rounded to its
  scale, so six decimals would turn a 1.2e-8 token price into 0.
- `crypto_snapshots` — one row per successful sync, with counts, credits and warnings
  (`history_status` is no longer written).
- `crypto_holdings` — keyed by `snapshot_id`; **only the newest snapshot keeps its full rows**.
- `crypto_daily_holdings(date, coin_id, symbol, count, source)` — **the per-coin quantity per UTC
  day.** Kept because the book's history is computed from it (`value = qty × price`, `pnl =
  yesterday's qty × price move`), and that is the least of the portfolio the formula needs: a
  count per coin per day, no money. `source` is `snapshot` (each sync replaces its own date's
  set) or `transactions` (the rebuild CLI). Only coins CoinStats values and that are **not fiat**
  go in; spam, unpriced coins and exchange cash never do.
- `crypto_coin_prices(coingecko_id, date, price_usd, source, fetched_at)` — keyed by the
  **CoinGecko id** (owner-accepted deviation from the plan's `coin_id`): the price belongs to
  CoinGecko's coin, and two CoinStats identifiers that map to it share it. `source` is `daily`
  (a finished day's close) or `spot` (today, overwritten each slot).
- `crypto_coin_ids(coinstats_id, coingecko_id, symbol, method, checked_at, closes_checked_at)` —
  the mapping and the two re-ask stamps.
- `crypto_daily` — CoinStats' old value/P&L history. **Kept for one release, no longer written
  or read**, so the two can be compared on production; then drop it.

## The formula — one for the whole year

`crypto_book.py`, pure and shared by the reads, the sync (to know which prices it needs) and the
rebuild CLI:

    qty(d)    = the holdings set dated on or before d; before the earliest set, the earliest set
    value(d)  = Σ qty(d, coin) × price(d, coin)
    pnl(d)    = Σ qty(d−1, coin) × (price(d, coin) − price(d−1, coin))

Yesterday's coins times today's price move: **buying, a DCA, a transfer between exchanges or a
staking reward changes a quantity and never the P&L.** That is what removed the Kraken spike
without rewriting any history. Everything starts at `CRYPTO_HISTORY_START = 2026-01-01`; nothing
earlier is computed, fetched or drawn.

- **The reconstructed span.** Every day before the first `snapshot`-sourced holdings day carries
  `reconstructed: true`: before the earliest set, that basket at each day's price; between it and
  the first synced day, quantities the rebuild CLI wrote, if it has run. **As decided on 2026-10-03
  it has not**: the first synced day's basket (3 Oct 2026) stands in for every earlier day, and
  because every later day's set is stored, coins added later (Revolut X) never rewrite that span.
  The chart shades it and says so under it. Why the CLI was not run: STATUS.md, *Known rough
  edges*.
- **Left out and counted, never valued at 0.** A held coin with no price that day is left out of
  that day's value, and out of its P&L whenever either end of its move is unpriced — so a coin
  gaining or losing its price is never a gain or a loss. It is named on the surface: the Total
  value tile reads "Excludes BNB — no price", each history point carries `excluded`, the chart's
  tooltip and a line under it name them, and weights are shares of the priced total. A figure is
  null only when **no** coin is priced, and a sum through such a day is unknown. Never a price
  carried from a neighbouring day. This is the stock side's unpriced-holdings convention; until
  2026-10-03 the book made the whole day unknown instead, and one 0.64-BNB position blanked every
  total, tile and chart point.
- **The USDC peg — a deliberate exception** (owner's decision, 2026-10-02). On a day CoinGecko
  has no price for USDC (CoinStats id `usd-coin`, or symbol `USDC`), it is valued at exactly
  **1.00 USD**. The table is `STABLECOIN_PEGS` / `STABLECOIN_PEG_SYMBOLS` in `crypto_book.py`,
  holds USDC only, and grows only on the owner's word. The peg is **applied at read time**, never
  stored as a price row (owner-accepted), and is visible wherever it is used: `price_source: "peg"`
  on the holding (a *peg* badge beside its price) and a `peg_note` on both responses naming the
  days. Every other coin without a price stays unknown — `tests/test_crypto_book.py` pins both.

## Reading it back — database only

A GET must not reach the network or take SQLite's write lock. `CryptoService` converts through
the same `NativeToBase` every reader uses, over `fx_preload.PreloadedRates`: one query for the
window, then a bounded (14-day) forward-fill. `BaseFx` is loaded from a fortnight before the
window with `backfill=False`, and when there is no EUR→base rate at all the figure is reported
unconvertible rather than served as euros under another currency's label.
`tests/test_crypto_read.py` seeds weekday-only rates, takes the snapshot on a Sunday and replaces
every network path (FX, CoinStats, CoinGecko) with a raiser.

What the figures mean:

- **Every figure converts at its own date** — each history point, each day's P&L before it is
  summed. There is no USD-at-one-day's-rate figure any more, so the old `fx_caveat` is gone.
- **Total value** = Σ of the newest snapshot's coins × CoinGecko's price on the snapshot's UTC
  day. Unknown when any coin has none (`no_price` status, named in `no_price_symbols` and on the
  tile).
- **Today** (`change_today`, *owner-accepted deviation from "24h"*) = `pnl` of the snapshot's
  UTC day: yesterday's coins × the move since yesterday's close at **00:00 UTC**; the percentage
  is against yesterday's value. Per coin, `change_today_pct` is the price move over the same span.
- **P&L since 1 Jan 2026** (`pnl_since_start`) = Σ of `pnl(d)` after 1 January, each converted at
  its own date — exactly where the chart's ALL range ends.
- **Weights are shares of the total**; the total is the priced coins, so they add to 100 by
  construction, and unknown when the total is.
- **Beside the total, not in it** (*fiat: owner-accepted deviation*): exchange cash
  (`cash_value`, CoinStats' own figure for fiat balances) and DeFi (`defi_value`). Neither has a
  CoinGecko price or belongs in a crypto P&L.
- **Excluded and counted**: spam and coins CoinStats cannot price never enter a total.
- Dropped on 2026-10-02: CoinStats' cost basis, average buy, realized / unrealized / all-time P&L
  and the not-itemised gap — they belonged to CoinStats' books, not to this formula.

**Colour identity.** Coins are coloured by `color_order` — CoinStats' market-cap rank — through
the `--viz-series-*` palette, so a sync that reorders the holdings by value cannot repaint the
chart (`docs/frontend.md`, *colour by identity*).

**Chart ranges**: 1W · MTD · 1M · 3M · 6M · YTD · 1Y · ALL, all counted back from the latest point
(today's live valuation); MTD on the 1st is one point; **no range reaches before 1 Jan 2026** (ALL
and 1Y clamp to it). The P&L line is the daily `pnl` summed from 0 on the range's first point.

## The rebuild CLI — `app/cli/crypto_rebuild_holdings.py`

One-off, over ssh, **off-slot**. It rebuilds `crypto_daily_holdings` from the reference date
(`--reference`, default **2026-08-23**: the first day after the Kraken coins reached Binance)
through the day before the first synced day:

    qty(d) = the newest snapshot's holdings − Σ signed transaction legs dated after d
             (legs after the snapshot's own time are already outside it and are ignored)

- `--probe` — one page (4 credits), prints the page's keys, the item keys, the first item's
  *shape* (hash, note and address keys left out) and a count of patterns: transaction type, the
  sign of `coinData.count`, each transfer's type and sign, whether the transfer legs of the
  `coinData` coin add up to `coinData.count`, whether a fee is present. **No value.** Decide
  `--legs transfers|coindata` and `--fees ignore|subtract` from it. Writes nothing.
- `--dry-run` — fetches every page (cap `MAX_TRANSACTION_PAGES`), rebuilds, checks, and prints
  the reference basket and, for each day a sync already stored, whether the reconstruction agrees
  with it — **console only; never paste it into a committed file**. Writes nothing.
- The real run writes the rows in **one transaction** (replacing any earlier rebuild's
  `transactions` rows and that date range), then fills the CoinGecko closes from 2026-01-01 for
  every coin ever held through the sync's own price code, and records one `crypto_rebuild`
  `sync_runs` row (type, status, message — no figure).

**It refuses whole**, writing nothing, on: no snapshot or no synced day yet ("let one crypto
sync run first"); a page or item it does not recognise (no item list, no date, a leg without a
coin identifier or count); more pages than the cap; **a negative quantity** of a coin it would
write (a transaction is missing, or a leg is read with the wrong sign); and **a trusted window
that moved** — the reference day and the two after it must hold one basket within
`--tolerance-pct` (0.5%), or the run prints the per-coin diff. Coins CoinStats lists as spam,
unpriced or fiat, legs marked fiat, and `--exclude COIN_ID` are never written.

## The probe

`python -m app.cli.coinstats_probe` asks each CoinStats question once (~53 credits) and prints
structure only — key names, types, counts, date spans, ratios. Read-only, records no `sync_runs`
row, ASCII-only output. **Never paste its output into a committed file.**

What it established on the first real run (2026-09-28):

- `totalValue` is exactly Σ count × price of the listed coins; `defiValue` is extra.
- One page of coins; no coin flagged `isFake`; one `isFiat`; every coin priced.
- A coin item carries `averageBuy`, `profit` and `profitPercent` keyed by period and `price` in
  USD, EUR, BTC and ETH — but **no per-coin `totalCost`**.
- `/portfolio/chart?type=all` is one point **every three days**, and `/portfolio/pl/history` is
  one day's P&L per point. Both are history now: the book computes its own daily series.
- A share-link passcode can be password-style; `app/redact.py` masks it once it is long enough.
- Sold coins come back at quantity 0 and are skipped without a warning.

## Operating it

- **Set up**: the three CoinStats keys and `COINGECKO_API_KEY` in `backend/.env`, then on the VPS
  `GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d`, never `restart`. **A new key goes in
  only after the code that declares it is deployed**: pydantic-settings rejects an unknown key in
  the dotenv file it reads itself (in the container, compose passes the file as environment
  variables, which it does not police — but a local run would refuse to start). The crypto view
  also needs `API_ADMIN_TOKEN`.
- **Check it**: `GET /api/crypto/status` with the admin key — last run, credits, next run,
  `prices_configured`. The public scheduler endpoints deliberately show nothing crypto.
- **Force a run**: `POST /api/crypto/sync` with the key; at most every 10 minutes.
- **Rebuild the history before the first synced day** (once, off-slot):
  `docker exec backend-portfolio-backend-1 python -m app.cli.crypto_rebuild_holdings --probe`,
  then `--dry-run`, then without a flag.
