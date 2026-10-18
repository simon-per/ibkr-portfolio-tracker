# Local development

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Local development

```bash
cd backend && venv\Scripts\activate && uvicorn app.main:app --reload --port 8000
cd frontend && npm run dev          # http://localhost:5173
```

**Set `SCHEDULER_ENABLED=false` in `backend/.env` for any local run.** Starting the backend is not a
neutral act: the lifespan handler arms every `ALL_SYNC_HOURS` Europe/Berlin job, which call
the live Flex API with the real token from `.env` and hit Yahoo — both rules at the top of CLAUDE.md,
from a dev machine.

**Python 3.11** is what the image (`backend/Dockerfile`) and CI (`.github/workflows/ci.yml`) run;
a fresh venv: `uv venv --python 3.11 backend/venv`, then
`uv pip install --python backend/venv/Scripts/python.exe -r backend/requirements.txt -r backend/requirements-dev.txt`
(the inputs: the locks are resolved for the Linux image and do not install on Windows, see
docs/tech-stack.md *Dependencies*). 3.12 has
also worked locally; the global interpreter has not (its FastAPI is too old for the suite). `settings.scheduler_enabled` defaults to **True** so production is unaffected, and
a disabled scheduler logs a warning because otherwise it looks exactly like a healthy site whose data
has quietly stopped moving. Pinned by `test_scheduler_is_enabled_by_default`.

Traps when pointing a browser at a local stack:

- **Check which port Vite actually took.** If 5173 is occupied it silently moves to 5174 and prints it
  once. A second dev server on 5173 configured against production means you are looking at prod data
  and issuing requests to the live site — including `/api/dividends/summary`, which can enqueue a
  Yahoo dividend sync.
- **A real-data snapshot beats the stale local DB.** `sqlite3 .backup` on the VPS, copied down, and
  `DATABASE_URL` pointed at it (the local `backend/portfolio.db` predates trades, cash flows and
  the IBKR dividend era, so it exercises none of the interesting shapes). Delete the copy afterwards —
  it is real account data and **this repo is public**. Keep it **outside the checkout**, or under
  `backend/data/` (ignored whole). `.gitignore` also catches the usual copy names (`*.db.*`,
  `*.db-*`, `*.sqlite*`, `*.snapshot`, `*.gz`, `.env.*`, e2e screenshots/videos/traces), but a name
  list is always one name behind (issue #36), so the backstop checks content:
  `ops/check_no_sqlite.py` refuses any staged file that is a SQLite database, WAL or journal, bare
  or gzip/bzip2/xz-wrapped, whatever it is called. CI runs it, but only after the push, when the
  file is already public. **Install the pre-commit hook once per clone** so it runs before a commit
  exists (all worktrees then use it; Git for Windows runs it through its own `sh`, and it fails
  closed without a Python 3.8+ on `PATH`):

  ```bash
  git config core.hooksPath ops/hooks     # relative: each worktree runs its own ops/hooks/pre-commit
  ```

  That replaces any existing `core.hooksPath`; this clone's was a stale path to the pre-rename
  folder, so no hook ran at all. Pinned by `tests/test_no_sqlite_in_git.py` (the hook end to end
  is `deploy_scripts`).
- **The test suite runs on its own database** (since 2026-10-17), so a snapshot named in
  `backend/.env` is safe while you run it. `tests/conftest.py` sets `DATABASE_URL` to a temporary file
  per process — the xdist controller and each worker — before `settings` is built, and removes it on
  exit; `tests/test_suite_database_isolation.py` pins that the app's engine is bound to it. Before
  that, the lifespan tests and the module `AsyncSessionLocal` wrote into whatever `.env` named.
- **Locally the database is still `backend/portfolio.db`** (`config.py`'s default). Only the
  container uses `backend/data/portfolio.db`, which `docker-compose.yml` sets; running the compose
  stack locally therefore needs the file there (or `ALLOW_NEW_DATABASE=1` for an empty one), because
  the container refuses to start on a missing database (`app/db_guard.py`).

`tests/test_api_smoke.py` runs **every read endpoint through the real HTTP stack** against a fixture
carrying the shapes that actually break: a dual-listed ticker, a closed lot, a dividend row with a NULL
net, a pre-ownership zero row, a non-EUR security, CHF as base. Every other test calls services
directly, which is how a `Decimal + None` reached production behind a green suite. `yfinance` is a
raiser for that whole module, so an accidental network reach fails loudly; `/api/portfolio/benchmark`
is excluded because it lazy-fetches Yahoo on a cache miss, and POST routes are excluded because they
start real syncs. **Add a case here when an endpoint's response shape changes.** The fixture is seeded
once per module into a template and each test is served from its own SQLite-backup copy (issue #50:
the seed was 93% of the module's runtime). Keep the copy: some read endpoints write, so one shared
database would make the module order-dependent.

A service test that wants an empty in-memory schema takes `db` from `tests/conftest.py`. A module
whose `db` adds rows or patches defines its own, which overrides it; a plain copy is refused by
`tests/test_fixture_copies.py`. Anything else that needs an in-memory database — the engine itself
(a session factory, a patched `AsyncSessionLocal`), a subset of the tables (`tables=`), or a schema
created inside a `TestClient` fixture's own event loop — builds it with `tests/memory_db.py`
(`memory_engine`, `new_memory_engine`, `create_schema`), which is also what `db` is made of. The
same test fails `create_all` or an import of `create_async_engine` anywhere else under `tests/`
(issue #72: the block had been copied into ~90 modules, one behind an import alias); its `ALLOWED`
map names the two modules that need a database of another kind, each with its reason. Scope
predicates shared by the AST family tests (`imports_yfinance`) live in `tests/ast_scope.py` and
may not be redefined.

`tests/test_migrations.py` runs `alembic check` against a temporary database built by
`upgrade head`, so a model that no longer describes what the migrations created — down to an
index name or a unique constraint versus a unique index — fails the suite. Align the model:
production's schema is what the migrations made.

Tests (1427 backend as of 2026-09-07 + 555 frontend as of 2026-09-08, all offline — no IBKR, Yahoo or FX-provider
calls). Take the number the suite actually prints as your baseline, not this line — it has been stale
by 200+ on both halves before:
```bash
cd backend && ./venv/Scripts/python.exe -m pytest tests/ -q -n auto --dist worksteal -m "not deploy_scripts"
cd frontend && npx tsc -b && npm run test && npm run build
cd frontend && npm run lint         # ESLint; CI runs it
cd backend && ruff check .          # correctness rules only (backend/ruff.toml); CI pins ruff 0.16.0
```
The backend runs in parallel under `pytest-xdist`: ~25 s on Windows, against 5½ min serial.
`-m "not deploy_scripts"` deselects the 20 tests that run `deploy.sh` and `ops/auto-deploy.sh`
under bash (the slot-wait harness and the rollback rehearsal). Git Bash forks slowly, so they are
10–30 s each and would double the run; on Linux they are cheap. **Drop the `-m` when you touched
`deploy.sh` or `ops/`** (~1 min). CI never deselects them, and `ops/auto-deploy.sh` refuses a
red commit, so a deploy-script regression is caught before production either way, only later.
Under xdist the summary line does not say "deselected"; the count is simply lower than CI's.

`worksteal` matters for that full run: the default `load` scheduler piles the deploy tests onto
one worker (93 s against 55 s). Drop the flags, or pass `-n 0`, to run a single test serially with
readable output and `pdb`. Contention slows the deploy tests (~25 s → ~35 s each on a busy
machine), closer to their 60 s subprocess guards; if one fails only in a parallel run, rerun that
file with `-n 0` before calling it a regression.

Useful:
```bash
# masked token check (never echo the whole thing)
grep -o '^IBKR_TOKEN=.\{0,4\}' backend/.env

# data snapshot on the VPS
python3 -c "
import sqlite3; c=sqlite3.connect('/root/IBKR_investment_tracker/backend/data/portfolio.db')
c.execute('PRAGMA busy_timeout=30000')
for q in ['select count(*) from securities','select count(*) from taxlots where is_open=1',
          'select count(*) from trades','select source,count(*) from dividend_payments group by 1']:
    print(q, c.execute(q).fetchall())"
```

## Traps

Moved from STATUS.md on 2026-10-14. Each of these cost real time at least once; add the one you
lose time to.

- **Git Bash's `TZ=Europe/Berlin date` prints UTC.** It does not know the zone name and falls
  back silently (labelled `GMT`), two hours behind Berlin in summer. The deploy rule (never push
  within ~10 minutes of an `ALL_SYNC_HOURS` slot) needs Berlin time: `python -c "from datetime
  import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('Europe/Berlin')))"`.
  A push made 3 minutes before 11:00 on 2026-10-18 this way was held back by the auto-deploy guard.
- **Playwright: the LAST registered `page.route` wins.** A catch-all that proxies a local build's
  `/api/` to production, added after the two `abort()` routes for `/api/dividends/summary` and
  `/api/portfolio/benchmark`, forwards both to the live site — rule 1. Refuse those two paths
  inside the catch-all itself. And a `vite build` calls relative `/api/…` (`.env.production`), so
  `vite preview` answers 500 from its own proxy unless the route matches the preview origin.

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
  as does `npx -y node@22 node_modules/vitest/vitest.mjs run`. CI uses Node 22 and is unaffected.
  Read the failing files before trusting a red count.
- **A fresh checkout or worktree has no `backend/venv`, no `backend/.env` and no
  `frontend/node_modules`.** Use Python 3.11, which the image and CI run (docs/local-development.md
  has the `uv` commands; 3.12 has also worked); the global interpreter's FastAPI is too old to
  collect `test_api_hardening.py`. Start `backend/.env` from `backend/.env.example` with
  placeholder IBKR values, `SCHEDULER_ENABLED=false` and an `API_ADMIN_TOKEN` — Crypto mode
  refuses to open without one. `npm ci` in `frontend/`.
- **Vite's dev proxy forwards `/api` only.** Running with `VITE_API_URL=` (same-origin, the only way
  a 401 reaches the page locally — see STATUS.md *Worth doing next*, crypto follow-ups) leaves `/health`
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
- **The base currency is whatever the user last picked** (`/api/settings`, EUR/CHF/USD). Every money
  figure moves with it, so never compare a number across sessions without checking it — hold it
  fixed by comparing payloads built from one snapshot, or ratios inside one payload.
- **Don't push within ~10 minutes of a Berlin sync slot** — ten of them, on the hour at
  00/04/05/08/11/13/15/18/20/22 (`ALL_SYNC_HOURS`; 04:00 is the watchlist's and 05:00 the
  fundamentals', Tuesday to Saturday).
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
- **On Linux (a cloud session), `pkill -f <pattern>` can kill the shell that runs it.** The
  agent's shell command line contains the pattern too, so `pkill -f run_demo_backend.py` ended
  its own shell (exit 144) and left the server running. Find the PID with
  `ps -eo pid,args | grep '[r]un_demo_backend'` and `kill <pid>`.
- **In a cloud container the preinstalled Chromium (`/opt/pw-browsers/chromium`) may not be the
  build `e2e/`'s Playwright expects**, and `playwright install` is off-limits there. The e2e
  scripts take no browser option, so preload a module that wraps `chromium.launch` to pass
  `executablePath: '/opt/pw-browsers/chromium'`, and run `node --import ./preload.mjs mobile.mjs`.
- **A dev server against a prod snapshot must run on port 5173.** `frontend/.env` points
  `VITE_API_URL` at `localhost:8000`, so the browser calls the backend cross-origin and only the
  ports in `CORS_ORIGINS` work. Any other port fails every request with a CORS error and looks like
  a backend outage.
- **Every position in the local `backend/portfolio.db` has `market_price: null` and a market value of
  0.00.** So the currency-exposure card reports *no priced positions* and the rebalance panel shows
  every row unpriced — both correct, and both easy to mistake for a broken feature. Anything that
  depends on a valued portfolio can only be browser-verified against a production snapshot.
- **A component test that renders anything using `useBaseCurrency`/`useCurrencySymbol` needs a
  `QueryClientProvider` *above* `CurrencyProvider`**, or it throws `No QueryClient set` — not a
  component defect; `AllocationTab.test.tsx` has the pattern. The `ResizeObserver` that
  `ScrollableTable` (anything through `DataTable`) and Recharts need is stubbed once in
  `src/testSetup.ts` for every jsdom file; don't stub it again in a test (`testSetup.test.tsx`
  fails one that does, #70).
- **uvicorn can die mid-Playwright-run with `OSError: [WinError 64] The specified network name is no
  longer available`** — a Windows asyncio-proactor reaction to an abruptly closed connection, not a code
  fault. The e2e script then reports `ERR_CONNECTION_REFUSED` and a shrunken panel, which reads exactly
  like a regression in whatever you just wrote. **Confirm `/health` still answers before believing an
  e2e failure.**
- **Testing Library's async queries wait 4 s here, not the library's 1 s** (`frontend/src/testSetup.ts`,
  2026-10-17). One component test failed once in nine runs that overlapped the backend suite, and the
  slowest measured 3 s under that load; a `findBy*` racing the scheduler is a flake, not a finding.
  A genuinely missing element still fails, inside vitest's 5 s test timeout. Put shared jsdom setup
  in that file rather than in each test.
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
  `yfinance`, `lxml` and `pyxirr` (only the Linux-only lock pins them), so reinstalling from it
  quietly drifts the local env. Then
  `python -m venv venv --clear` and install the freeze. A plain `python -m venv venv` regenerates
  `activate` and leaves the shims broken, which is the worst of the three states.

---
