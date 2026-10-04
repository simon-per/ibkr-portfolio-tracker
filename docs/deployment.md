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

## Rollback and the database

**Since 2026-10-04 the rollback restores the pre-deploy snapshot when — and only when — the
failed deploy migrated the database.** Before that it never used the snapshot it had just taken.
The container's start command is `alembic upgrade head && uvicorn`, so a failed deploy that
got as far as starting its container has already moved `alembic_version` to a revision the
rolled-back tree does not have. The rollback's container then failed `alembic upgrade head`
("Can't locate revision"), `restart: unless-stopped` crash-looped it, and the script logged
`CRITICAL: rollback also failed` — on exactly the deploys a rollback exists for. The rehearsal
in `backend/tests/test_deploy_rollback.py` runs the real scripts against a throwaway repo; run
by hand against the pre-fix script it ends in exactly that `CRITICAL`.

**How the snapshot is found.** `ops/backup-db.sh` ends its stdout with two lines: the live
database it copied (its `DB=`, the one place that path is defined), then the verified snapshot
it wrote. `ops/auto-deploy.sh` captures both. The source path is the restore target on
purpose: the snapshot predates the deploy, so it came from wherever the rolled-back-to build
keeps its database, even if the failed commit moved it. A copy of `backup-db.sh` that prints
nothing (anything older than this change) means **no snapshot**: auto-deploy logs a `NOTE`
on the deploy and a `WARN` on the rollback, and behaves as it did before. It never guesses a
newest-file-in-the-directory and a target path, because a restore overwrites the live file.

**When it restores**, in the rollback branch after `git reset --hard` and before re-running
`deploy.sh`:

1. The range `LOCAL..REMOTE` must touch `backend/alembic/versions/`. If not, the plain
   redeploy suffices and nothing else happens — in particular the running app is not stopped
   early, so `deploy.sh` still builds while it serves.
2. The snapshot must be non-empty, start with the SQLite header, and pass
   `PRAGMA integrity_check` (`python3`, as `backup-db.sh` uses). If not: `WARN`, no restore.
3. `docker compose down` (in `backend/`) must succeed — nothing may hold the file while it is
   replaced. If not: `WARN`, no restore.
4. The live database's `alembic_version`, read through a **copy** (opening it in place would
   let SQLite checkpoint or discard its `-wal`), must differ from the snapshot's. Equal means
   the deploy failed before migrating (a broken build; a migration that raised and rolled
   back), and restoring would only throw away the writes since the snapshot. Unreadable
   counts as different.

Then: the snapshot is copied beside the live file, the live file and any `-wal`/`-shm`/`-journal`
are moved to `/root/ibkr-backups/failed-deploys/failed-<utc ts>-<sha>.db[-wal|-shm]`, and the copy
is renamed into place. Any step failing puts the failed state back together and skips the
restore. The sidecars must go: a `-wal` left beside the restored file would be replayed onto it.

**What a restore costs:** the writes between the snapshot and the failure — minutes, but a
scheduled sync can land in them. They are not lost: the failed-state file is kept, the log
names it (`RESTORED … kept as …`, then a closing `NOTE`), and `failed-deploys/` is outside the
checkout (public repo; `.gitignore` would not match it) and outside `backup-db.sh`'s prune glob,
so **nothing ever deletes it — clean it up by hand** once its rows are accounted for. The one
case that pays an outage it did not need is a range with a migration whose deploy failed in the
build: the `down` in step 3 comes before the rebuild.

**Both scripts run from copies on the VPS** (`/root/auto-deploy.sh` from cron, `/root/backup-db.sh`
from cron and from auto-deploy), so a change here takes effect only when they are refreshed —
by atomic rename, never `install`/`cp` onto the live file, which truncates it under a running
bash. Do it **after auto-deploy has deployed the commit carrying the change** (`/health`'s
`commit`), so the checkout holds the new files — never `git pull` by hand, which leaves
auto-deploy nothing to deploy:

```bash
cd /root/IBKR_investment_tracker
cp /root/backup-db.sh /root/backup-db.sh.bak-$(date -u +%F)
cp /root/auto-deploy.sh /root/auto-deploy.sh.bak-$(date -u +%F)
cp ops/backup-db.sh /root/backup-db.sh.new && chmod 755 /root/backup-db.sh.new \
  && mv -f /root/backup-db.sh.new /root/backup-db.sh
cp ops/auto-deploy.sh /root/auto-deploy.sh.new && chmod 755 /root/auto-deploy.sh.new \
  && mv -f /root/auto-deploy.sh.new /root/auto-deploy.sh
sha256sum ops/backup-db.sh /root/backup-db.sh ops/auto-deploy.sh /root/auto-deploy.sh
```

`backup-db.sh` now prints two lines on stdout, so the daily crontab line should end in
`>/dev/null` (`17 3 * * * /root/backup-db.sh daily >/dev/null`) or cron mails them. Until
both copies are refreshed, treat any `CRITICAL: rollback also failed` as "restore the newest
`/root/ibkr-backups/<date>/` snapshot by hand (containers down first, sidecars moved away),
then redeploy".

---
