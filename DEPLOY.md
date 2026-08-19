# Deployment, Backup & Restore Runbook

Operational guide for running the IBKR Portfolio Analyzer on a server (Hostinger VPS)
and for wiping / rebuilding it without losing data.

- **Live URL:** https://portfolio.srv1211053.hstgr.cloud
- **Repo on server:** `/root/IBKR_investment_tracker`
- **SSH:** `ssh -i ~/.ssh/id_ed25519_hostinger root@portfolio.srv1211053.hstgr.cloud`
- **Stack:** host nginx (TLS, Certbot) → Docker Compose: FastAPI backend on
  `127.0.0.1:8000` + nginx serving the built frontend on `127.0.0.1:8080`.
  **There is no Traefik.** This document described one until 2026-08-19; it was
  replaced by the host nginx vhost long before, and the runbook you open when the VPS
  is gone was sending you to look for it.

## What is and isn't in git

**In git (restored by `git clone`):** all app code, `frontend/.env.production`
(empty `VITE_API_URL` → frontend calls `/api/*` same-origin), `backend/frontend-nginx.conf`,
`backend/docker-compose.yml`, `deploy.sh`, `backend/.env.example`.

**NOT in git — back these up before wiping:**

| Path on server | Contents | Notes |
|---|---|---|
| `backend/portfolio.db` | All portfolio data (securities, tax lots, prices, FX, dividends, fundamentals, etc.) | SQLite, **WAL mode** — never plain-`cp` it while the backend runs; use `/root/backup-db.sh` or stop the backend first |
| `backend/.env` | Secrets: `IBKR_TOKEN`, `IBKR_QUERY_ID`, etc. | Can also be rebuilt from `backend/.env.example` |
| `/etc/nginx/sites-available/portfolio` | The public vhost: TLS, the `/api/` proxy, `proxy_read_timeout 300` | Certbot re-issues the certificate on a fresh setup; the vhost itself is hand-written and **not** in git |
| `/root/auto-deploy.sh`, `/root/backup-db.sh` | The deploy and backup jobs | Both are in git under `ops/` — reinstall from there rather than restoring the copies |
| root's crontab | `auto-deploy` every 10 min, `backup-db daily` at 03:17 UTC | Two lines; recreate by hand |

---

## 0. The automated backup, and the last time a restore was proven

`/root/backup-db.sh` (in git as `ops/backup-db.sh`) runs **daily at 03:17 UTC** from
root's crontab and again before every unattended deploy. It takes a snapshot through
sqlite's own backup API, verifies the *copy* with `PRAGMA integrity_check`, and prunes
with both an age (30 days) and a floor (the 10 newest, whatever their age).

It replaced a plain `cp` of the live database on 2026-08-19. That copy was taken while
the backend was running and took neither `-wal` nor `-shm`, so in WAL mode it silently
omitted everything since the last checkpoint — measured: 500 rows written since a
checkpoint, `cp` recovers **0** of them and still answers `integrity_check` -> ok. The
Gotchas section below had stated the rule the whole time.

**Restore rehearsals** — this is the record, because a backup nobody has restored is a
belief:

| Date | Source | Result |
|---|---|---|
| 2026-08-19 | newest automatic snapshot | `integrity_check ok`, `foreign_key_check` 0 rows, all 21 tables' row counts identical to live |

Re-run it with the snippet in section 3; it copies the newest backup to `/tmp`, checks
it against the live database, and deletes itself.

**Why this matters more here than for most apps:** the data cannot be re-fetched. The
Flex Query window is 3 days and IBKR holds nothing before 2026 for this account, so
trades, cash flows, corporate actions and the IBKR dividend ledger exist *only* in this
file. `OpenPositions` is period-independent, so a sync would restore the lots and
nothing else.

---

## 1. Back up (run BEFORE wiping the VPS)

Run from your local machine (PowerShell). Stopping the backend first lets SQLite
checkpoint the WAL into the main `.db` file so the copy is consistent.

```powershell
# a) Stop the backend on the VPS (checkpoints WAL → portfolio.db)
ssh -i ~/.ssh/id_ed25519_hostinger root@portfolio.srv1211053.hstgr.cloud `
  "cd /root/IBKR_investment_tracker/backend && docker compose stop portfolio-backend"

# b) Pull the database and secrets into a local backup folder
mkdir -Force "$HOME\ibkr-backups"
scp -i ~/.ssh/id_ed25519_hostinger `
  root@portfolio.srv1211053.hstgr.cloud:/root/IBKR_investment_tracker/backend/portfolio.db `
  "$HOME\ibkr-backups\portfolio.db"
scp -i ~/.ssh/id_ed25519_hostinger `
  root@portfolio.srv1211053.hstgr.cloud:/root/IBKR_investment_tracker/backend/.env `
  "$HOME\ibkr-backups\backend.env"
```

Verify the DB copied intact (size > 0, and it opens):

```powershell
Get-Item "$HOME\ibkr-backups\portfolio.db" | Select-Object Length
```

> Optional, no shutdown needed: an online snapshot instead of stopping the backend —
> `ssh ... "cd /root/IBKR_investment_tracker/backend && docker compose exec -T portfolio-backend python -c \"import sqlite3; sqlite3.connect('portfolio.db').backup(sqlite3.connect('portfolio.backup.db'))\""` then scp `portfolio.backup.db`.

---

## 2. Fresh server setup (new / wiped VPS)

Prereqs: Docker + Docker Compose plugin, git, and **DNS A record**
`portfolio.srv1211053.hstgr.cloud` → the new server IP (required before Certbot can
issue a TLS cert). An **AAAA** record exists too, which is why the vhost must carry
`listen [::]:443` / `listen [::]:80` — a v4-only vhost looks fine until a client
prefers IPv6, which is the 2026-07-08 incident.

```bash
# Install Docker if missing:  curl -fsSL https://get.docker.com | sh
cd /root
# The repo was renamed to ibkr-portfolio-tracker, but the DIRECTORY must stay
# IBKR_investment_tracker: deploy.sh, ops/auto-deploy.sh (REPO_DIR), the compose
# file's relative mounts and /root/ibkr-backups all hardcode that path. So pass the
# target explicitly — a bare clone would create ibkr-portfolio-tracker/ and every
# one of those would silently miss.
# HTTPS, not SSH: a fresh VPS has no GitHub key, and adding one is the slower path.
git clone https://github.com/simon-per/ibkr-portfolio-tracker.git IBKR_investment_tracker
cd IBKR_investment_tracker

# Secrets: restore the backup, or create from template
cp backend/.env.example backend/.env   # then edit, OR scp your backup over it (step 3)
```

---

## 3. Restore data, then deploy

Push the backups from your machine up to the new server **before** the first `docker compose up`:

```powershell
# .env (secrets)
scp -i ~/.ssh/id_ed25519_hostinger "$HOME\ibkr-backups\backend.env" `
  root@portfolio.srv1211053.hstgr.cloud:/root/IBKR_investment_tracker/backend/.env

# database (must be a FILE at this exact path before containers start)
scp -i ~/.ssh/id_ed25519_hostinger "$HOME\ibkr-backups\portfolio.db" `
  root@portfolio.srv1211053.hstgr.cloud:/root/IBKR_investment_tracker/backend/portfolio.db
```

Then on the server:

```bash
cd /root/IBKR_investment_tracker
chmod +x deploy.sh
./deploy.sh
```

`deploy.sh` builds the frontend, ensures `portfolio.db`
exists as a file, builds the frontend, then builds and starts all containers and runs a
health check.

> Starting fresh with **no** backup? Skip the DB copy — `deploy.sh` creates an empty
> `portfolio.db` and the app auto-creates the schema on startup (`init_db` →
> `alembic upgrade head`, run by the container CMD). Then use **Sync IBKR Data** +
> market-data sync to repopulate.

---

### Rehearsing a restore without touching anything

Run this on the server. It restores the newest automatic snapshot to `/tmp`, checks it
against the live database, and removes itself. Nothing is stopped and nothing is
written outside `/tmp`, so it is safe at any hour.

```bash
ssh -i ~/.ssh/id_ed25519_hostinger root@portfolio.srv1211053.hstgr.cloud 'python3 - <<PY
import glob, os, shutil, sqlite3
live = "/root/IBKR_investment_tracker/backend/portfolio.db"
newest = max(glob.glob("/root/ibkr-backups/*/portfolio.db.*-*"), key=os.path.getmtime)
target = "/tmp/restore-rehearsal.db"; shutil.copyfile(newest, target)
r = sqlite3.connect(target)
l = sqlite3.connect("file:" + live + "?mode=ro", uri=True); l.execute("PRAGMA busy_timeout=15000")
print(newest)
print("integrity_check:", r.execute("PRAGMA integrity_check").fetchone()[0])
print("foreign_key_check rows:", len(r.execute("PRAGMA foreign_key_check").fetchall()))
tables = [t[0] for t in r.execute("select name from sqlite_master where type=\'table\' and name not like \'sqlite_%\'")]
bad = [(t, r.execute("select count(*) from "+t).fetchone()[0], l.execute("select count(*) from "+t).fetchone()[0])
       for t in tables]
print("mismatches:", [b for b in bad if b[1] != b[2]] or "none")
r.close(); l.close(); os.remove(target)
PY'
```

A row-count difference of a few in fast-moving tables (`market_prices`,
`exchange_rates`) is expected if a sync ran between the snapshot and the check — a
difference in `taxlots`, `trades`, `cash_flows` or `dividend_payments` is not, because
only an IBKR sync writes those and they are hours apart.

**Record the date and result in the table in section 0.** That table is the only thing
that distinguishes a backup that works from one nobody has opened.

## 4. Verify

```bash
cd /root/IBKR_investment_tracker/backend
docker compose ps
curl -s http://127.0.0.1:8000/health           # {"status":"healthy"}
curl -s http://127.0.0.1:8000/api/scheduler/status
```

In the browser at the live URL: the dashboard loads with your data, **Sync IBKR Data**
succeeds (retries transient 1001s), and the **Dividend Income** card populates.

---

## Gotchas

- **WAL mode:** never copy `portfolio.db` from a running backend without also copying
  `-wal`/`-shm`, or just stop the backend first (as above).
- **Bind mount needs a file:** if `backend/portfolio.db` is missing, Docker creates a
  *directory* and SQLite fails. `deploy.sh` now `touch`es it; for a restore, place the
  real file there first.
- **The host nginx owns ports 80/443**, not the compose stack — both containers bind
  to `127.0.0.1` only. The same nginx also serves an unrelated `n8n` vhost, so do not
  assume this host is single-purpose when editing its config or reloading it.
- **DNS before TLS:** the domain must resolve to the server before `./deploy.sh`, or the
  Let's Encrypt TLS challenge fails and HTTPS won't come up.
- **Migrations run on every boot:** the container CMD is
  `alembic upgrade head && uvicorn ...`, so a restored database is migrated forward
  automatically — and if the migration fails, the `&&` means uvicorn never starts and
  the container is simply down. Nothing calls `Base.metadata.create_all()` in
  production any more; `tests/test_migrations.py` is what keeps that safe.
