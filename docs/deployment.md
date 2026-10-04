# Deployment — the VPS, auto-deploy, deploy.sh

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Deployment

**Push to `main` → deployed automatically within 5 minutes** (once CI is green).
`/root/auto-deploy.sh` on the VPS (root crontab, `*/5 * * * *` — `*/10` until 2026-10-04) does: `flock` → `git fetch` → deploy **only if strictly behind** `origin/main`
(`merge-base --is-ancestor`; it will refuse and log if the VPS has diverged) → back up `portfolio.db` →
`deploy.sh` → health check → **roll back to the previous commit if health fails**. Log:
`/root/auto-deploy.log`.

`deploy.sh` is expensive (`npm ci`, `build --no-cache`), which is why the cron guards on an actual
change. **Since 2026-09-08 it builds before it stops anything**: the frontend goes to `dist.next`
and the image is built while the old containers keep serving, then `down`, swap `dist`, `up`. Before
that `down` came first and the site was offline for the whole build — minutes per push, and the
window that loses a scheduled sync. (A bind mount follows the directory inode, so the swap has to
sit between `down` and `up`, and `dist.old` is removed only after `up`.) Its own health check fires a
few seconds after start and often reports FAILED spuriously — check `/health` again after ~15s
before believing it.

- SSH: `ssh -i ~/.ssh/id_ed25519_hostinger root@portfolio.srv1211053.hstgr.cloud`
- Secrets live only in `/root/IBKR_investment_tracker/backend/.env` (`IBKR_TOKEN`, `IBKR_QUERY_ID`,
  `API_ADMIN_TOKEN`, `OPENFIGI_API_KEY`, and for the crypto book `COIN_STATS_API_KEY`,
  `COIN_STATS_SHARE_TOKEN`, `COIN_STATS_SHARE_PASSCODE` — see [crypto.md](crypto.md))
- nginx proxies all `/api/` publicly with `proxy_read_timeout 300`; needs `listen [::]:443/80` (an AAAA
  record exists)
- Backups: `/root/ibkr-backups/<date>/`

**`portfolio.db` was a FILE bind mount until 2026-10-04, and until 2026-09-08 every deploy
silently discarded the tail of the database.** SQLite in WAL mode writes its `-wal` and `-shm` sidecars *beside*
the database path — inside the container, that is `/app/portfolio.db-wal`, which is in the
container's writable layer, not on the host. `docker compose down` removes the container and
the sidecars with it, so every commit since the last auto-checkpoint (`wal_autocheckpoint`,
1000 pages, ~4 MB) was lost on every deploy, and every host-side backup was missing the same
tail. Measured: a `sync_runs` row written at 18:23 UTC was gone after the 18:30 deploy while
one from 18:07 survived (a market-data pass had crossed the auto-checkpoint threshold between
them), and the container held a 2.7 MB WAL against a 0-byte one on the host. Small writes were
the exposure — a settings change, a mapping edit, a basket import, the last `sync_runs` row —
because a big sync checkpoints itself. The scheduler's job store had this exact bug fixed on
2026-08-01 ("mount the parent directory, never the `.db` file"); the main database did not.

Three places checkpoint, each covering a case the others cannot, and
`tests/test_wal_checkpoint_on_shutdown.py` pins all three: the app on a clean shutdown
(`checkpoint_and_dispose_engine` in `main.py`), `deploy.sh` from outside the container between
`build` and `down`, and `ops/backup-db.sh` before the host-side copy. The two script copies ask
the running container for its own `settings.database_url` rather than naming a path, so they
are right on either side of the move below, and open it `mode=rw` so a wrong path fails instead
of creating an empty file. They were the mitigation; the fix is the directory mount.

### The database directory

**Since 2026-10-04 the database is `backend/data/portfolio.db`, and compose mounts the
directory** (`./data:/app/data`), so the `-wal`/`-shm` sidecars land on the host beside it and a
killed container loses nothing. `docker-compose.yml` sets `DATABASE_URL:
sqlite+aiosqlite:////app/data/portfolio.db` in `environment:`, which overrides `env_file:` — the
host `.env` may still say `./portfolio.db` and it no longer matters (tidy it anyway; STATUS.md
has the VPS steps). Local development is unchanged: `config.py`'s default is still
`./portfolio.db`.

The move happens inside an unattended deploy, so it is built around the one outcome that must
never happen — **the app starting on a new, empty database while the real one sits at the old
path.** That looks like total data loss, and the next IBKR sync would rebuild the account from a
bounded Flex window into the empty file. Three layers:

1. **`ops/db-layout.sh`**, run by `deploy.sh`. `preflight` runs before the build (old containers
   still serving) and is read-only; `switch` runs between `down` and `up`. It recognises three
   layouts — *migrated* (`data/portfolio.db` is a file; `portfolio.db` absent or a link to it),
   *legacy* (`portfolio.db` is a file; nothing at `data/portfolio.db` or its sidecars) and *fresh*
   (neither; only with `ALLOW_NEW_DATABASE=1`) — and **refuses everything else without moving
   anything**: two databases, a dangling link, the directory Docker auto-creates for a missing
   file mount, a stray `-wal` waiting at the destination. On *legacy* it moves the file into
   `data/`, then leaves **`backend/portfolio.db` as a symlink to `data/portfolio.db`**; if the
   link cannot be made it moves the file back and refuses. Idempotent: every later deploy finds
   *migrated* and does nothing. Host-side `-wal`/`-shm`/`-journal` beside the old file are
   **set aside** as `data/portfolio.db-*.pre-switchover-<stamp>`, not carried along: under the file
   mount the container never saw them, so the main file (checkpointed before `down`) is the state
   the app last served, and moving a host `-wal` beside it would make SQLite replay it.
2. **The symlink** keeps everything that still names the old path pointed at the real file: a
   rollback to a commit older than the move (its `deploy.sh` does `[ -f portfolio.db ] || touch
   portfolio.db` and its compose file mounts that path — through the link, the real database,
   back on the old file-mount semantics), the copy of `ops/backup-db.sh` at `/root/backup-db.sh`
   (which does not update itself), and anything typed by hand from an older document. SQLite
   resolves a symlinked database to its real name before naming the `-wal`, so a host-side reader
   through the link shares the container's WAL rather than seeing half the database. Remove the
   link only once `/root/backup-db.sh` is refreshed and a rollback past 2026-10-04 is unthinkable.
3. **`app/db_guard.py`, first in the container's CMD** (`python -m app.db_guard && alembic upgrade
   head && uvicorn ...`), refuses to start when the configured database file is missing, empty
   or not a regular file, unless `ALLOW_NEW_DATABASE=1` (compose passes it through, default `0`).
   It exists for the path that skips layer 1: an older `deploy.sh` (a human's `./deploy.sh` runs
   the pre-pull copy once — below) bringing up this compose file would otherwise boot on the empty
   `data/` Docker creates. With the guard it crash-loops instead, the deploy's health check fails,
   and auto-deploy's rollback restores the old layout; the next deploy then performs the move.

`ops/backup-db.sh` defaults `DB` to `backend/data/portfolio.db`, falling back to the legacy path
only while `data/` holds nothing, so a copy installed before the move does not fail every backup
(and with it every auto-deploy).

**A fresh install** (no database anywhere, on purpose): `ALLOW_NEW_DATABASE=1 ./deploy.sh` once.
Without the flag, `preflight` refuses and nothing is stopped. Restoring a backup instead: put the
snapshot at `backend/data/portfolio.db` (with the backend down, no `-wal`/`-shm` beside it) and
deploy normally.

**When `db-layout` refuses**, nothing was moved, and the message names the state. *Both files
exist* means someone created the second one — compare `sync_runs` and `max(trade_date)` in each to
decide which is live, set the other aside outside `backend/`, and redeploy. A refusal from
`switch` (after `down`) leaves the containers down: auto-deploy's rollback brings the previous
build back on the old path; by hand, fix what it names and re-run `./deploy.sh`. *A crash-looping
backend whose log says `REFUSING TO START`* is layer 3: the database is not where
`DATABASE_URL` points — run `bash ops/db-layout.sh preflight` from the repo on the VPS to see
which layout it finds.

One residual risk, accepted: a rollback after the move runs the old build on the old file-mount
semantics again, and if the *new* container was SIGKILLed rather than stopped (compose's 10 s
grace), its uncheckpointed `data/portfolio.db-wal` would be replayed over the old build's writes
on the next forward deploy. A clean `down` checkpoints on shutdown, so this needs a kill during a
deploy that is also being rolled back.

**Changing `backend/.env` needs `docker compose up -d`, never `restart`.** Compose reads `env_file`
when it *creates* a container; `restart` reuses the existing one with its original environment, so a
new value is accepted, written, and silently ignored. `up -d` sees the changed config and recreates.
This bit us turning on `API_ADMIN_TOKEN` (2026-07-31): the token was in `.env`, every command
reported success, and `write_auth_enabled` stayed `false` — a site that looks locked down and isn't.
**Always confirm against `/health` rather than the command's exit status.**

**And pass `GIT_COMMIT` when you do, or `/health` starts lying about which build is live.** The sha
is not baked into the image — `docker-compose.yml` sets `GIT_COMMIT: ${GIT_COMMIT:-unknown}` from the
environment and only `deploy.sh` exports it (line 59). So a bare `docker compose up -d` run by hand
recreates the container with `commit: "unknown"` while serving perfectly current code, and it does
**not** self-heal: nothing re-exports it until the next push. That breaks the one lookup this file
prescribes for "which build is live", including the check `ops/finish-deploy.*` and every deploy
verification make. Hit while putting `OPENFIGI_API_KEY` on production (2026-08-17). The fix needs no
rebuild:

```bash
cd /root/IBKR_investment_tracker/backend && \
  GIT_COMMIT=$(git -C /root/IBKR_investment_tracker rev-parse HEAD) docker compose up -d
```

**A `deploy.sh` that pulls the repo itself runs the OLD copy once.** Bash does not reload a
running script, so behaviour newly added to `deploy.sh` is absent from exactly the deploy that
introduces it. That is why the build-identity deploy reported `commit: "unknown"` — the
`GIT_COMMIT` export existed in the pulled file but not in the executing one. **Since 2026-08-19
this applies only to a human's `./deploy.sh`:** `ops/auto-deploy.sh` fast-forwards the checkout
*before* invoking `deploy.sh` (with `DEPLOY_NO_PULL=1`), so an automatic deploy runs the NEW copy —
provided `/root/auto-deploy.sh` is the current `ops/` version, worth a `sha256sum` before relying
on it. Where an old copy does run, it runs against the new compose file and image, which is why the
database move above has a container-side guard too. Anything asserting "the deploy landed" must
not key solely on the commit sha (`ops/finish-deploy.*` also accept the `write_auth_enabled`
marker).

A **cloud routine** `ibkr-sync-validator` (claude.ai/code/routines) runs daily at 07:45 UTC to validate
the morning sync via the public API + the IBKR MCP connector. It **cannot SSH**, so it opens PRs rather
than pushing to `main`.

---
