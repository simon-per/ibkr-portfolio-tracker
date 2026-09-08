# Deployment — the VPS, auto-deploy, deploy.sh

> Moved verbatim from `CLAUDE.md` on 2026-09-08, when that file became a short index so a
> session no longer loads ~92k tokens of documentation to start. **This file is
> authoritative for its subsystem**: every invariant here was a bug first. Keep it accurate
> the way CLAUDE.md was kept — update it in the same change that moves the code it describes.

## Deployment

**Push to `main` → deployed automatically within 10 minutes.** `/root/auto-deploy.sh` on the VPS (root
crontab, `*/10 * * * *`) does: `flock` → `git fetch` → deploy **only if strictly behind** `origin/main`
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
- Secrets live only in `/root/IBKR_investment_tracker/backend/.env` (`IBKR_TOKEN`, `IBKR_QUERY_ID`)
- nginx proxies all `/api/` publicly with `proxy_read_timeout 300`; needs `listen [::]:443/80` (an AAAA
  record exists)
- Backups: `/root/ibkr-backups/<date>/`

**`portfolio.db` is a FILE bind mount, and until 2026-09-08 every deploy silently discarded
the tail of the database.** SQLite in WAL mode writes its `-wal` and `-shm` sidecars *beside*
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

Three places now checkpoint, each covering a case the others cannot, and
`tests/test_wal_checkpoint_on_shutdown.py` pins all three: the app on a clean shutdown
(`checkpoint_and_dispose_engine` in `main.py`), `deploy.sh` from outside the container between
`build` and `down`, and `ops/backup-db.sh` before the host-side copy. **The durable fix is
still to mount the directory** so the sidecars land on the host — STATUS.md, *Worth doing
next*, item 0 — because a killed container never runs its shutdown and a deploy script that
changes runs its old copy once.

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
