# IBKR Portfolio Analyzer — Project Guide

Full-stack portfolio tracker for an Interactive Brokers account plus a Swiss pillar 3a account:
securities, tax lots, trades, corporate actions, dividends and cash; cost-basis vs. market-value
charts; a Swiss tax report; company-level look-through of the ETFs. All money is stored in EUR and
projected into a base currency the user switches at will (`app_settings.base_currency`,
EUR/CHF/USD) — **read it from `/api/settings`, never from a document**; it has been both CHF and
EUR, and every figure moves with it.

**Live:** https://portfolio.srv1211053.hstgr.cloud · **Repo is PUBLIC** (never commit account data)

**Read [STATUS.md](STATUS.md) too, and leave it accurate before you stop** — see *Keeping STATUS.md
current* below. This file is deliberately short: the two rules that must never break, the
conventions that hold everywhere, and an index. The reasoning behind each subsystem — every
invariant that was a bug first — lives in `docs/`, one file per area, moved there verbatim on
2026-09-08. **Those files are authoritative.** Read the one for the area you are touching before
changing it, and update it in the same change that moves the code it describes. A rule stated only
in prose is a rule the next reader repairs back. Code comments and STATUS.md entries that cite a
CLAUDE.md section by name (*Sync schedule*, *Reconciliation*, …) refer to the `docs/` file that now
holds it — the index below maps them.

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
| [docs/activity-ledger.md](docs/activity-ledger.md) | `activity_service` |
| [docs/lookthrough.md](docs/lookthrough.md) | `lookthrough_service`, `etf_sources`, baskets and adapters, identities, the treemap, the evening basket refresh |
| [docs/pricing-and-fx.md](docs/pricing-and-fx.md) | ticker mapping, `manage_mappings`, minor-unit quotes, `currency_service`, Frankfurter and the fallback |
| [docs/frontend.md](docs/frontend.md) | `frontend/src/lib/` analytics (risk, rebalance, currency exposure, forecast), `DataTable`, the mobile layout |
| [docs/tech-stack.md](docs/tech-stack.md) | versions, the bundle boundaries, `e2e/` |
| [docs/deployment.md](docs/deployment.md) | `deploy.sh`, auto-deploy, the VPS, `.env`, `/health` |
| [docs/local-development.md](docs/local-development.md) | running it locally, the smoke test, snapshots, test commands |
| [docs/troubleshooting.md](docs/troubleshooting.md) | a symptom you are looking at — symptom-indexed table |
| [docs/history.md](docs/history.md) | the 2026-07-29 correctness sweep and the 2026-07-28 state snapshot |
| [docs/shipped-log.md](docs/shipped-log.md) | what shipped when and what was verified on production — STATUS.md's former *Shipped* sections, newest first |

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
  — it raises on `1001`, so any outer retry starts a brand-new generation. **And never
  `client.request_statement()` for the SendRequest step** (found 2026-09-12): its
  `submit_request` re-sends the same GET up to three times on a 5 s timeout, each a new
  generation. `send_flex_request` issues one GET with a (connect, read) timeout pair, and a
  `ReadTimeout` there fails fast like a `1001` — the request may have been accepted.
  `tests/test_flex_retry_policy.py` pins that the name is never called.
- IBKR generates about **one statement per ET calendar day**; `flex_generation` makes the second
  daily slot skip once one succeeded. Adding IBKR slots adds failed generations, not freshness.
- The Flex Query period is a portal setting. **Measure it** (`flex_window_days()` reads
  `data_to − data_from` off the last statement); never assert a number in a document.
- The escape hatch from a lockout is a Client Portal download ingested through
  `app/cli/ingest_flex_xml.py` — the same code path, no token spend.

---

## Keeping STATUS.md current

STATUS.md answers "where does this actually stand?", and it is only worth reading if it is true.
**Updating it is part of the work, not a courtesy afterwards.**

Leave it accurate before you stop, on any turn where your work changed what it should say:

- code, config or a migration changed — and *Worth doing next* should lose whatever you just finished
- you shipped, reverted, or left something that needs watching after the next deploy
- you found something known-broken or flaky, or something only a human can do (rotate a token,
  change a Flex Query period, click through the IBKR portal)
- a *Known rough edge* stopped being true, or a new accepted-not-a-bug appeared
- you lost time to a *local-dev trap* that isn't in the list yet

A turn that only answers a question and finds nothing new needs no edit — but **discovering
something is a change of status even when no code moved**.

**It is a snapshot, not a log.** Bump `Last updated`, add what became true, and **delete what
stopped being true** instead of stacking corrections. A *Shipped* write-up goes to
[docs/shipped-log.md](docs/shipped-log.md) (newest first); STATUS.md's *Watch after the next
deploy* carries only what is still unverified. *Recent sessions* is the one append-only part,
capped at five one-liners — drop the oldest. Never accumulate **figures** (public repo,
user-switchable base currency, and a pasted total goes stale silently) or **what git already
records**. When a subsystem's rules change, the durable half of that change goes into its
`docs/` file in the same commit.

---

## The dominant failure mode: two implementations of one job, drifting

More bugs here have come from duplicated logic diverging than from any other cause, and the
divergences are quiet — both copies keep working, they just stop agreeing, and the number nobody
recomputes by hand is the one that is wrong. The catalogue of every instance, with how each
diverged, is [docs/failure-modes.md](docs/failure-modes.md); the rules are:

- **When you find a copy, extract rather than sync it**, and write the test against the *family*
  ("every service resolving a ticker agrees with the price path") rather than the instance, so the
  next copy is caught the same way. `ttm_growth.py`, `peg_ratio.py`, `safe_numbers.py`,
  `native_amounts.py`, `yahoo_rate_limit.py`, `positionValuation.ts`, `forecast.ts` are what that
  looks like.
- **A correct copy is still a copy.** Agreement is what a copy looks like right up to the moment it
  stops being one. The test to write is "is there a copy at all", not "do the copies agree".
- **Three lenses find them.** Walk the AST for function names defined in more than one module and
  read each cluster (router-to-service pairs and repository CRUD are noise; a *service* helper twice
  is not). Ask which paths reach the same **upstream** (a route and a job that both loop Yahoo must
  agree about when to stop). Ask, of every figure and every table, **which other code reads the same
  rows or publishes the same name** — and does it apply the same rules.
- **The cheapest moment to catch one is while writing the third copy.** Several extractions here
  happened exactly then.
- **The exception is a duplicate that writes its reasoning down on both sides** — `dividendGrowth.ts`
  reimplements the server's arithmetic across the language boundary and has not drifted, because
  both ends name the rules they copy.

---

## Conventions that hold everywhere

Each of these was a bug first; the file for the area has the story.

- **Every stored timestamp is naive UTC and `app/clock.utcnow()` is the only way to make one.**
  Never `datetime.now()`; `tests/test_clock_convention.py` walks the tree.
- **An unknown is absent, never a stand-in.** `None`/`null`, not `0`, not `100`, not a reassuring
  sentence. Ask what the stand-in would *claim*: a `0.00` Sharpe and a `0%` concentration look like
  answers, which is what makes them dangerous. Severity tracks plausibility, not magnitude.
- **An unvaluable holding is excluded from both sides and counted**, never valued at 0.00.
  `unpriced_holdings` rides on every valuation response; the client reads `isUnpriced` from
  `positionValuation.ts` (price **or** FX can be missing) and never recomputes the backend's count.
- **A caveat inside a collapsible or behind a hover does not exist.** Qualifiers sit on the surface
  next to the figure they qualify.
- **Refuse whole; never half-apply.** Imports, parsers, `replace_basket`, the finpension ledger: a
  partial result is indistinguishable afterwards from a complete one. A refusal is that row's or
  fund's warning, never the job's status.
- **`warnings[]` on a successful run is the surface for anything a reader must see** — and a
  warning that is always present teaches the reader to skip the banner. Build the fix rather than
  leave the banner (the stale baskets sat there for two weeks before the refresh existed).
- **Never renormalise to 100; report the shortfall.** Percentages are shares of the whole book.
- **Whitelist, never blacklist** — flow types that count as money in, asset classes that count as
  equity.
- **Bound every retry and every re-ask** — the holiday rule, `UPSTREAM_RETRY_COOLDOWN_SECONDS`,
  `*_checked_at` stamps, `HOLIDAY_GRACE_DAYS`. A loop that asks the same question forever is how
  an IP-based rate limit is spent.
- **Secrets are redacted on write and on read** (`app/redact.py`): Flex puts the token in the URL
  and transport errors stringify it.
- **No upload endpoints.** `/api/` is public; the CLIs under `app/cli/` over ssh are the write path
  for anything not on the sync, and each records a `sync_runs` row. Every `POST/PUT/PATCH/DELETE`
  under `/api/` is gated by `app/auth.py` middleware (method-keyed, so a new route is covered the
  moment it exists); reads are open and rate-limited.
- **A `response_model` is a filter.** Complete the model before attaching one, and pin the
  service's key set against it in both directions (`test_dividend_summary_contract.py`).
- **Pillar 3a blends everywhere except where it must not.** `reconcile_taxlots`, the wipe guard
  and `restamp_unsourced_closed_lots` are scoped to `account=IBKR`; the tax report excludes 3a;
  every other reader sees one portfolio. `tests/test_account_isolation.py` pins the destructive
  paths plus an AST rule.
- **`price_source` is the Yahoo opt-out**, honoured at every Yahoo call site
  (`tests/test_yahoo_eligibility_family.py`).
- **Frontend formatters are pinned to `en-US`** in `lib/utils.ts`; a bare `toLocaleString()`
  rendered 850,000 as "850.000" under a German runtime.

---

## Tech stack

**Backend** — FastAPI (async) on Starlette, SQLAlchemy 2.0 + aiosqlite (WAL), Alembic, APScheduler
with a persistent job store, `ibflex` **0.15** (pinned — it aborts on unknown attributes, hence
`_sanitize_flex_xml`), `yfinance >= 1.1.0`, Frankfurter for FX with `open.er-api.com` as the
bounded fallback. Pins are in `backend/requirements.txt` and were moved to current on 2026-09-08;
`pip-audit` or an OSV query against them is worth running when they age again.
**Frontend** — React 19 + TypeScript + Vite, TanStack Query, Recharts, Tailwind + shadcn/ui. Vitest
runs in `node` by default; component tests opt into jsdom per file. The bundle is code-split with
two deliberate boundaries (Recharts stays eager; lazy tabs sit behind `ui/LazyTabPanel.tsx`) — see
[docs/tech-stack.md](docs/tech-stack.md) before "optimising" either. **`e2e/`** is a separate
Playwright package that the deploy never installs; its scripts have per-script preconditions
(`e2e/README.md`).

---

## Deployment — the essentials

Full detail in [docs/deployment.md](docs/deployment.md).

- **Push to `main` → deployed automatically within 10 minutes.** `/root/auto-deploy.sh` (root
  crontab, `*/10`) resets the checkout, backs up the DB, runs `deploy.sh`, health-checks, and rolls
  back on failure. It defers a deploy that would land inside a sync slot; don't push within ~10
  minutes of one (`ALL_SYNC_HOURS` in `scheduler_service.py`; `ops/finish-deploy.*` checks).
- **`deploy.sh` builds before it stops anything** (frontend into `dist.next`, then the image), then
  checkpoints SQLite's WAL inside the running container, then `down`, swap `dist`, `up`. Measured
  after the reorder: zero failed health probes across a deploy.
- **`portfolio.db` is a FILE bind mount, so SQLite's `-wal`/`-shm` live in the container layer and
  die with `docker compose down`.** Until 2026-09-08 every deploy silently discarded the commits
  since the last auto-checkpoint (~4 MB) and every host backup lacked the same tail. Three
  checkpoints now cover it — app shutdown (`checkpoint_and_dispose_engine`), `deploy.sh` before
  `down`, `ops/backup-db.sh` before the copy — and `tests/test_wal_checkpoint_on_shutdown.py`
  pins all three. **The durable fix is mounting the directory** (STATUS.md, *Worth doing next*).
  Anything that writes to production by hand and then stops the container must checkpoint first.
- **Changing `backend/.env` needs `docker compose up -d`, never `restart`** (compose reads
  `env_file` only when it creates a container), **with `GIT_COMMIT=$(git rev-parse HEAD)`
  exported**, or `/health` reports `commit: "unknown"` until the next push. Confirm against
  `/health`, not the command's exit status.
- `/health` reports `commit`, `scheduler_enabled`, `write_auth_enabled` and
  `scheduler_jobstore_persistent` (`false` means misfire recovery is off, whatever
  `/api/scheduler/status` says). Its check right after start often reports FAILED spuriously;
  re-check after ~15 s.
- SSH: `ssh -i ~/.ssh/id_ed25519_hostinger root@portfolio.srv1211053.hstgr.cloud`. Secrets only in
  `/root/IBKR_investment_tracker/backend/.env`. Backups in `/root/ibkr-backups/<date>/`, 30 days,
  at least 10 kept. `/root/backup-db.sh` is a *copy* of `ops/backup-db.sh` that auto-deploy and the
  daily cron prefer — refresh it when the repo script changes.
- A cloud routine `ibkr-sync-validator` runs daily at 07:45 UTC against the public API; it cannot
  ssh, so it opens PRs rather than pushing.

---

## Local development — the essentials

Full detail and the traps in [docs/local-development.md](docs/local-development.md) and STATUS.md's
*Local development traps*.

```bash
cd backend && ./venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000   # SCHEDULER_ENABLED=false in backend/.env first
cd frontend && npm run dev                                                            # http://localhost:5173 — check which port Vite actually took
cd backend && ./venv/Scripts/python.exe -m pytest tests/ -q                           # all offline; take the count the suite prints as the baseline
cd frontend && npx tsc -b && npm run test && npm run build
```

- **`SCHEDULER_ENABLED=false` in `backend/.env` for any local run.** Defaults to `True` so
  production is unaffected; a disabled scheduler logs a warning on purpose.
- The local `backend/portfolio.db` predates trades, cash flows and the IBKR dividend era, so it
  exercises none of the interesting shapes. Use a `sqlite3 .backup` snapshot from the VPS, point
  `DATABASE_URL` at it, and **delete it afterwards** — it is real account data.
- `tests/test_api_smoke.py` runs every read endpoint through the real HTTP stack against a fixture
  carrying the shapes that break. **Add a case there when a response shape changes.**
- Repo files are CRLF in the working tree (git autocrlf); an edit script must preserve line endings
  or every later replace fails and the whole file reads as changed.

## When something looks wrong

Start at [docs/troubleshooting.md](docs/troubleshooting.md): it is indexed by symptom, and most
entries end in "expected, and here is why" — read those before fixing what is not broken.
