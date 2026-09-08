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

Tests (1427 backend as of 2026-09-07 + 555 frontend as of 2026-09-08, all offline — no IBKR, Yahoo or FX-provider
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
