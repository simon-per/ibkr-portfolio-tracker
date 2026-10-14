# IBKR Portfolio Analyzer — Project Guide

Full-stack portfolio tracker for an Interactive Brokers account plus a Swiss pillar 3a account:
securities, tax lots, trades, corporate actions, dividends and cash; cost-basis vs. market-value
charts; a Swiss tax report; company-level look-through of the ETFs. A crypto book from CoinStats
lives beside it in its own **Crypto** mode and shares no figure with the stocks
([docs/crypto.md](docs/crypto.md)). All money is stored in EUR (the crypto book's USD is the one
exception) and projected into a base currency the user switches at will
(`app_settings.base_currency`, EUR/CHF/USD) — **read it from `/api/settings`, never from a
document**; every figure moves with it.

**Live:** https://portfolio.srv1211053.hstgr.cloud · **Repo is PUBLIC** (never commit account data)

**Read [STATUS.md](STATUS.md) too, and leave it accurate before you stop** (*Keeping STATUS.md
current* below). This file holds the two rules that must never break, the conventions that hold
everywhere, and an index. The reasoning behind each subsystem — every invariant that was a bug
first — lives in `docs/`, one file per area. **Those files are authoritative.** Read the one for
the area you are touching before changing it, and update it in the same change that moves the
code. A code comment citing a CLAUDE.md section by name (*Sync schedule*, *Reconciliation*, …)
means the `docs/` file the index maps.

## Where things are

| read this | before touching |
|---|---|
| [docs/flex-and-sync.md](docs/flex-and-sync.md) | the Flex Query, `ibkr_service`, `sync_helper` ingest, the sanitizer, the scheduler and its slots, `single_flight`, redaction, auth/rate limit, the job store, the unpriced-holdings family; the two rules' full reasoning |
| [docs/failure-modes.md](docs/failure-modes.md) | anything that exists in two places — the catalogue of every duplication that drifted here, and the lenses that find them |
| [docs/data-model-and-valuation.md](docs/data-model-and-valuation.md) | the schema, `reconcile_taxlots`, realized P&L, the close-date convention, the timeline walk, splits, holidays, provisional prices, the spinoff floor |
| [docs/tax.md](docs/tax.md) | `tax_service`, DA-1, Steuerwert, the honesty flags |
| [docs/dividends.md](docs/dividends.md) | `dividend_service`, the era splice, purging estimates, the forecast, forward yield, growth |
| [docs/cash-contributions-benchmark.md](docs/cash-contributions-benchmark.md) | `cash_service`, Total Value vs Money In, `get_contributions`, the benchmark and its anchors |
| [docs/pillar3a.md](docs/pillar3a.md) | the second account, `finpension_*`, account isolation, `price_source`, the NAV oracle |
| [docs/crypto.md](docs/crypto.md) | the crypto book: `coinstats_client`, `coingecko_client`, `crypto_book`, `crypto_service`, `/api/crypto`, the Crypto mode, CoinStats credits, the share token, the rebuild CLI, the USDC peg |
| [docs/activity-ledger.md](docs/activity-ledger.md) | `activity_service` |
| [docs/lookthrough.md](docs/lookthrough.md) | `lookthrough_service`, `etf_sources`, baskets and adapters, identities, the treemap, the evening basket refresh, researched future sources |
| [docs/pricing-and-fx.md](docs/pricing-and-fx.md) | ticker mapping, `manage_mappings`, minor-unit quotes, `currency_service`, Frankfurter and the fallback |
| [docs/frontend.md](docs/frontend.md) | `frontend/src/lib/` analytics (risk, rebalance, currency exposure, forecast), `DataTable`, the mobile layout |
| [docs/performance-analytics.md](docs/performance-analytics.md) | `performance_analytics_service`, `/api/performance/*`, `attribution_rows`, the price/FX split, the Analytics tab (hidden since 2026-10-04), `rollingRisk.ts` |
| [docs/tech-stack.md](docs/tech-stack.md) | versions and pins, the bundle boundaries, `e2e/` |
| [docs/deployment.md](docs/deployment.md) | `deploy.sh`, auto-deploy, the VPS, `.env`, `/health`, backups, the database directory (`ops/db-layout.sh`, `app/db_guard.py`) |
| [docs/local-development.md](docs/local-development.md) | running it locally, the smoke test, snapshots, test commands, **the local-dev traps** |
| [docs/troubleshooting.md](docs/troubleshooting.md) | a symptom you are looking at — symptom-indexed, plus **the accepted rough edges (not bugs)** |
| [docs/history.md](docs/history.md) | the 2026-07-29 correctness sweep and the 2026-07-28 state snapshot |
| [docs/shipped-log.md](docs/shipped-log.md) | what shipped when and what was verified on production, newest first |

---

## ⚠️ Two rules that must never be broken

Full reasoning, measurements and the lockout history are in
[docs/flex-and-sync.md](docs/flex-and-sync.md). The operative parts:

### 1. Never call Yahoo Finance without explicit user permission

`yfinance` powers prices, dividend estimates, fundamentals and benchmarks. Yahoo rate-limits hard
(~500–2,000 requests/hour, ~10–20 in a burst, IP-based) and a full market-data sync is 50–150+
requests. Symptoms: HTTP 429, HTTP 404 with `Expecting value: line 1 column 1 (char 0)`, empty
JSON, timeouts. **Recovery: stop immediately, wait 30–60 min, and do not trigger a manual sync.**

- Protections live in `market_data_service.py` (random delays, Chrome UA, incremental caching, a
  `rate_limited` latch that abandons the pass). **Every service that loops against Yahoo must
  abandon the pass on a 429** through `app/services/yahoo_rate_limit.is_rate_limit`;
  `tests/test_yahoo_rate_limit_family.py` walks the AST for any module importing `yfinance`
  without consulting it. Keep its marker list narrow — the same verdict decides whether to try
  ticker *variations*. `yfinance` stays `>= 1.1.0`.
- What can reach Yahoo: the scheduled jobs, every `POST .../sync`, `POST /api/watchlist`,
  `GET /api/dividends/summary` (can enqueue a sync) and `GET /api/portfolio/benchmark` (lazy
  fetch on a cache miss). Everything else is pure DB. The Flex sync (`POST /api/sync/ibkr`)
  touches **no** Yahoo. Alpha Vantage is a fallback *inside* a market-data sync, USD listings
  only, rows tagged `source='alpha_vantage'`.
- **Locally, set `SCHEDULER_ENABLED=false` in `backend/.env`** — starting uvicorn otherwise arms
  every job against the live Flex token and Yahoo.

### 2. Never loop the IBKR Flex sync

IBKR allows 1 request/second and 10/minute per token, and one statement generation is several
HTTP requests. Repeated **failed generations** trigger `Code=1025: Too many failed attempts`, an
undocumented token lockout lasting hours (~14h observed). Three lockouts so far, every one
self-inflicted. **When locked: do nothing; the schedule recovers it. Retrying can extend it.**

- Budgets in `ibkr_service.py`: `_FLEX_RETRY_DELAYS = [30]` (interactive),
  `FLEX_RETRY_DELAYS_PATIENT = [120, 300, 600]` (scheduled); `1025` fails fast, `1018` backs off
  ≥ 60s. Pinned by `tests/test_flex_retry_policy.py`.
- **`1001` means the opposite thing at each step.** While *retrieving* it is "not ready": keep
  polling the same `ReferenceCode` (`_RETRIEVE_PENDING_CODES`). From *SendRequest* it is a failed
  generation — exactly what `1025` counts — so it is deliberately **not** in
  `_REQUEST_RETRYABLE_CODES`: fail fast and let the next slot ask fresh. Transport errors
  (`requests.RequestException`) never reached IBKR and are retried.
- `IBKRService._download_statement()` drives the two-step protocol itself: SendRequest **once**,
  then poll GetStatement on the same reference until a deadline. Never `ibflex.client.download()`
  (raises on `1001`, so an outer retry starts a new generation) and never
  `client.request_statement()` (re-sends the GET up to three times, each a new generation).
  `send_flex_request` issues one GET; a `ReadTimeout` there fails fast like a `1001`.
  `tests/test_flex_retry_policy.py` pins both.
- IBKR generates about **one statement per ET calendar day**; `flex_generation` makes the second
  daily slot skip once one succeeded, and ET Sunday/Monday are skipped outright. Adding IBKR slots
  adds failed generations, not freshness.
- The Flex Query period is a portal setting. **Measure it** (`flex_window_days()` reads
  `data_to − data_from` off the last statement); never assert a number in a document.
- The escape hatch from a lockout is a Client Portal download ingested through
  `app/cli/ingest_flex_xml.py` — the same code path, no token spend.

---

## Keeping STATUS.md current

STATUS.md answers "where does this actually stand?" — needs a human, watching, unverified after
deploy, next. **Updating it is part of the work.** Leave it accurate before you stop whenever your
turn changed what it should say: code, config or a migration changed (and *Worth doing next* loses
what you finished); you shipped, reverted, or left something to watch; you found something broken,
flaky, or only a human can do. Discovering something is a change of status even when no code moved.

**It is a snapshot, not a log.** Bump `Last updated`, add what became true, **delete what stopped
being true**. A *Shipped* write-up goes to [docs/shipped-log.md](docs/shipped-log.md) (newest
first); an accepted-not-a-bug goes to docs/troubleshooting.md's *Accepted rough edges*; a new
local-dev trap to docs/local-development.md's *Traps*. *Recent sessions* is capped at five
one-liners. Never write **figures** there (public repo, switchable base currency) or **what git
already records**. When a subsystem's rules change, the durable half goes into its `docs/` file
in the same commit.

---

## The dominant failure mode: two implementations of one job, drifting

More bugs here have come from duplicated logic diverging than from any other cause, and quietly:
both copies keep working, they just stop agreeing. The catalogue is
[docs/failure-modes.md](docs/failure-modes.md); the rules:

- **When you find a copy, extract rather than sync it**, and test the *family* ("every service
  resolving a ticker agrees with the price path"), so the next copy is caught too. `ttm_growth.py`,
  `peg_ratio.py`, `safe_numbers.py`, `native_amounts.py`, `yahoo_rate_limit.py`,
  `positionValuation.ts`, `forecast.ts` are what that looks like.
- **A correct copy is still a copy.** The test to write is "is there a copy at all", not "do the
  copies agree".
- **Three lenses find them:** function names defined in more than one *service* module; paths that
  reach the same **upstream** (a route and a job that both loop Yahoo must agree when to stop); and,
  for every figure, **which other code reads the same rows or publishes the same name**.
- **The cheapest moment is while writing the third copy.**
- **The exception is a duplicate that writes its reasoning down on both sides** —
  `dividendGrowth.ts` mirrors the server's arithmetic across the language boundary and both ends
  name the rules they copy.

---

## Conventions that hold everywhere

Each of these was a bug first; the file for the area has the story.

- **Every stored timestamp is naive UTC and `app/clock.utcnow()` is the only way to make one.**
  Never `datetime.now()`; `tests/test_clock_convention.py` walks the tree.
- **An unknown is absent, never a stand-in.** `None`/`null`, not `0`, not `100`, not a reassuring
  sentence. A `0.00` Sharpe and a `0%` concentration look like answers, which is what makes them
  dangerous.
- **An unvaluable holding is excluded from both sides and counted**, never valued at 0.00.
  `unpriced_holdings` rides on every valuation response; the client reads `isUnpriced` from
  `positionValuation.ts` (price **or** FX can be missing) and never recomputes the backend's count.
- **The UI carries figures, not explanations** (owner, 2026-10-14). No footnotes, definitions,
  provenance or methodology notes, `†`/`*` markers or shaded "replayed" spans; hover texts a few
  words at most. What stays: a short tag on the figure (`est.`) and a conditional warning naming a
  problem present right now (unpriced holdings, a failed load, a missing price, a stale basket).
  The reasoning lives in `docs/` and code comments. Don't add it back.
- **Refuse whole; never half-apply.** Imports, parsers, `replace_basket`, the finpension ledger: a
  partial result is indistinguishable afterwards from a complete one. A refusal is that row's or
  fund's warning, never the job's status.
- **`warnings[]` on a successful run is the surface for anything a reader must see** — and a
  warning that is always present teaches the reader to skip it. Build the fix rather than leave the
  banner.
- **Never renormalise to 100; report the shortfall.** Percentages are shares of the whole book.
- **Whitelist, never blacklist** — flow types that count as money in, asset classes that count as
  equity.
- **Bound every retry and every re-ask** — `UPSTREAM_RETRY_COOLDOWN_SECONDS`, `*_checked_at`
  stamps, `HOLIDAY_GRACE_DAYS`. A loop that asks the same question forever spends an IP-based limit.
- **Secrets are redacted on write and on read** (`app/redact.py`): Flex puts the token in the URL
  and transport errors stringify it.
- **No upload endpoints.** `/api/` is public; the CLIs under `app/cli/` over ssh are the write path
  for anything not on the sync, and each records a `sync_runs` row. Every `POST/PUT/PATCH/DELETE`
  under `/api/` is gated by `app/auth.py` middleware (method-keyed, so a new route is covered the
  moment it exists); reads are open and rate-limited — **except under `/api/crypto`, whose reads
  need the key too and fail closed** when no key is configured.
- **A `response_model` is a filter.** Complete the model before attaching one, and pin the
  service's key set against it in both directions (`test_dividend_summary_contract.py`).
- **Pillar 3a blends everywhere except where it must not.** `reconcile_taxlots`, the wipe guard
  and `restamp_unsourced_closed_lots` are scoped to `account=IBKR`; the tax report excludes 3a;
  every other reader sees one portfolio. `tests/test_account_isolation.py` pins it.
- **Crypto blends nowhere** — its own tables, service, router and view, never an `account` label.
  `tests/test_crypto_isolation.py` pins the import graph both ways.
- **`price_source` is the Yahoo opt-out**, honoured at every Yahoo call site
  (`tests/test_yahoo_eligibility_family.py`).
- **Frontend formatters are pinned to `en-US`** in `lib/utils.ts`; a bare `toLocaleString()`
  rendered 850,000 as "850.000" under a German runtime.

---

## Stack, deploy, local — the essentials

**Stack:** FastAPI + SQLAlchemy 2.0/aiosqlite (WAL) + Alembic + APScheduler; `ibflex` **0.15**
pinned; React 19 + TypeScript + Vite, TanStack Query, Recharts, Tailwind/shadcn. Two deliberate
bundle boundaries — read [docs/tech-stack.md](docs/tech-stack.md) before "optimising" either.
`e2e/` is a separate Playwright package the deploy never installs.

**Deploy** ([docs/deployment.md](docs/deployment.md)):

- **Push to `main` → deployed within 5 minutes** once CI is green, by `/root/auto-deploy.sh`
  (backs up the DB, runs `deploy.sh`, health-checks, rolls back on failure). **Don't push within
  ~10 minutes of a Berlin sync slot** (`ALL_SYNC_HOURS` in `scheduler_service.py`;
  `ops/finish-deploy.*` checks). `/root/auto-deploy.sh` and `/root/backup-db.sh` are COPIES of
  `ops/` — refresh them by atomic rename when the repo scripts change.
- **The database is `backend/data/portfolio.db` behind a DIRECTORY bind mount**; the container
  refuses to start on a missing or empty one (`app/db_guard.py`). Anything that writes to
  production by hand and then stops the container must checkpoint the WAL first.
- **Changing `backend/.env` needs `docker compose up -d`, never `restart`**, with
  `GIT_COMMIT=$(git rev-parse HEAD)` exported. Confirm against `/health` (re-check after ~15 s; the
  first probe after start often fails spuriously).
- SSH: `ssh -i ~/.ssh/id_ed25519_hostinger root@portfolio.srv1211053.hstgr.cloud`. Secrets only in
  `/root/IBKR_investment_tracker/backend/.env`. A cloud routine `ibkr-sync-validator` checks the
  public API daily at 07:45 UTC and opens PRs.

**Local** ([docs/local-development.md](docs/local-development.md), including its *Traps*):

```bash
cd backend && ./venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000   # SCHEDULER_ENABLED=false in backend/.env first
cd frontend && npm run dev                                                            # check which port Vite actually took
cd backend && ./venv/Scripts/python.exe -m pytest tests/ -q -n auto --dist worksteal -m "not deploy_scripts"
    # offline, parallel, ~25 s; drop -m after touching deploy.sh or ops/; take the printed count as the baseline
cd frontend && npx tsc -b && npm run test && npm run build
```

- The local `backend/portfolio.db` exercises none of the interesting shapes. Use a `sqlite3
  .backup` snapshot from the VPS and **delete it afterwards** — it is real account data. The test
  suite writes into whatever `DATABASE_URL` names (STATUS.md *Worth doing next* 1).
- `tests/test_api_smoke.py` runs every read endpoint through the real HTTP stack. **Add a case
  there when a response shape changes.**
- Repo files are CRLF in the working tree (git autocrlf); an edit script must preserve line endings.

## When something looks wrong

Start at [docs/troubleshooting.md](docs/troubleshooting.md): indexed by symptom, and most entries
end in "expected, and here is why" — read those, and the accepted rough edges, before fixing what
is not broken.
