#!/bin/bash
set -e

REPO_DIR="/root/IBKR_investment_tracker"
DOMAIN="portfolio.srv1211053.hstgr.cloud"

echo "=== IBKR Portfolio Tracker - Deploy ==="

# 1. Pull latest code
#
# `DEPLOY_NO_PULL=1` skips this, and ops/auto-deploy.sh sets it on BOTH of its
# invocations. Without it the automated rollback could not roll back: it does
# `git reset --hard <last good>` and then calls this script, and because auto-deploy has
# already proven the last-good commit is an *ancestor* of origin/main, this pull
# fast-forwards straight back to the commit that just failed its health check. Cleanly,
# with no conflict and no error — so the "rollback" rebuilt and redeployed the broken
# build, then logged that the rollback had failed. It had never run.
#
# Default off, so a human typing ./deploy.sh still gets the pull they expect.
echo ""
cd "$REPO_DIR"
if [ "${DEPLOY_NO_PULL:-0}" = "1" ]; then
    echo "--- Skipping pull (DEPLOY_NO_PULL=1); deploying $(git rev-parse --short HEAD) as checked out ---"
else
    echo "--- Pulling latest code ---"
    git pull origin main
fi

# 2. Ensure backend/.env exists
if [ ! -f backend/.env ]; then
    echo ""
    echo "--- backend/.env not found, creating from root .env ---"
    if [ -f .env ]; then
        cp .env backend/.env
        echo "Copied .env → backend/.env"
    else
        echo "ERROR: No .env file found. Create backend/.env with IBKR_TOKEN, IBKR_QUERY_ID, CORS_ORIGINS, DATABASE_URL"
        exit 1
    fi
fi

# 3. Build frontend (before docker compose, since frontend container mounts dist/)
echo ""
echo "--- Rebuilding frontend ---"
cd "$REPO_DIR/frontend"

# Install Node.js if not present
if ! command -v node &> /dev/null; then
    echo "Node.js not found, installing via NodeSource..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi

npm ci
# Built beside `dist/`, not into it. The nginx container bind-mounts `dist/`, and
# `vite build` empties its output directory first — so building in place served 404s for
# every asset while the build ran, and then the backend image build below kept the site
# down for minutes more. The swap happens between `down` and `up` (a bind mount follows the
# directory inode, so a rename under a running container changes nothing it sees), and the
# old tree is removed only after `up`, for the same reason.
rm -rf dist.next
npm run build -- --outDir dist.next

# 4. Rebuild and restart Docker containers (backend + frontend nginx)
echo ""
echo "--- Rebuilding Docker containers ---"
cd "$REPO_DIR/backend"

# The database lives in backend/data/ (a DIRECTORY bind mount, so SQLite's -wal lands on
# the host); deploy.sh moves a legacy backend/portfolio.db there once, after `down` below.
# This read-only check runs first, while the old containers still serve, and refuses any
# layout it does not recognise — including no database at all, which used to be met with
# `touch portfolio.db` and an app that started on an empty file. A genuinely fresh
# install runs `ALLOW_NEW_DATABASE=1 ./deploy.sh`. See ops/db-layout.sh.
bash "$REPO_DIR/ops/db-layout.sh" preflight

# Same trap for the scheduler's job store, which is bind-mounted so persisted jobs
# survive the rebuild — the whole point of persisting them.
[ -f scheduler_jobs.db ] || touch scheduler_jobs.db

# Stamps the running build so /health and the app footer can identify it, which is how
# "did my deploy land" gets answered from a browser rather than over ssh.
GIT_COMMIT="$(cd "$REPO_DIR" && git rev-parse HEAD 2>/dev/null || echo unknown)"
export GIT_COMMIT
echo "Deploying commit: $GIT_COMMIT"

# Build first, while the old containers keep serving. `down` used to come first, which
# took the site offline for the whole `--no-cache` image build — minutes per push, and
# the window that loses a scheduled sync — when the swap itself takes seconds.
docker compose build --no-cache

# --- Sync-slot re-check, right before the restart --------------------------------------
# ops/auto-deploy.sh refuses to START a deploy within SLOT_MARGIN_MIN of a Berlin sync slot,
# but it checks once, before `git fetch`, and the build above then runs for minutes while
# the old containers serve. A build that ran past the margin restarted the app on top of
# the sync. Harmless if the slot had not fired yet (the persistent job store re-runs a
# misfire), but a sync killed mid-run is re-run from scratch, and at the 18:00/00:00 IBKR
# slots that is a second Flex generation — CLAUDE.md rule 2. So the restart itself waits:
# while within the margin of a slot, and then while the running backend reports a sync in
# flight (`sync_in_progress` on /api/scheduler/status, the in-process SYNC_PIPELINE gate).
# Waiting here is free: the old containers keep serving, and the cron ticks that fire
# meanwhile exit at auto-deploy's flock.
#
# The hours, the margin and in_sync_window() are READ from the checkout's
# ops/auto-deploy.sh, never copied: that is the file tests/test_deploy_guard_hours.py pins
# against the scheduler's ALL_SYNC_HOURS, and a copy of the hours nothing read is how they
# drifted before. The checkout is the commit being deployed, so the two always agree.
#
# Bounded by DEPLOY_SLOT_WAIT_MAX_MIN (default 45: the margin alone holds at most 21, the
# rest is for a long 18:00 full sync), after which it restarts ANYWAY rather than abort.
# Aborting is worse under auto-deploy: a non-zero exit reads as a broken build (rollback,
# and the good commit quarantined), while exiting 0 without restarting would be logged
# SUCCESS with the checkout already at the new commit, so nothing would ever deploy it.
# Unknowns fail open for the same reason: an unreachable status endpoint, or a backend
# that predates the field, counts as "no sync running" — the margin still applies.
# DEPLOY_IGNORE_SLOTS=1 skips the whole check, for a manual deploy that must go now.
wait_for_sync_slot_clear() {
    if [ "${DEPLOY_IGNORE_SLOTS:-0}" = "1" ]; then
        echo "--- Sync-slot re-check skipped (DEPLOY_IGNORE_SLOTS=1) ---"
        return 0
    fi
    local guard="$REPO_DIR/ops/auto-deploy.sh"
    local SYNC_HOURS="" SLOT_MARGIN_MIN=""
    eval "$(grep -E '^(SYNC_HOURS|SLOT_MARGIN_MIN)=' "$guard" 2>/dev/null)"
    eval "$(sed -n '/^in_sync_window() {$/,/^}$/p' "$guard" 2>/dev/null)"
    if ! [[ "$SYNC_HOURS" =~ ^[0-9\ ]+$ && "$SLOT_MARGIN_MIN" =~ ^[0-9]+$ ]] \
        || ! declare -F in_sync_window >/dev/null; then
        echo "WARNING: could not read the sync slots from $guard; restarting without the re-check"
        return 0
    fi
    local max_min="${DEPLOY_SLOT_WAIT_MAX_MIN:-45}"
    [[ "$max_min" =~ ^[0-9]+$ ]] || max_min=45
    local poll=30 waited=0 slot reason last_reason=""
    echo ""
    echo "--- Sync-slot re-check before the restart ---"
    while :; do
        reason=""
        if slot=$(in_sync_window); then
            reason="within ${SLOT_MARGIN_MIN} min of the ${slot}:00 Europe/Berlin sync slot"
        elif curl -s --max-time 5 "http://127.0.0.1:8000/api/scheduler/status" 2>/dev/null \
                | grep -Eq '"sync_in_progress"[[:space:]]*:[[:space:]]*true'; then
            reason="the running backend reports a sync in progress"
        fi
        if [ -z "$reason" ]; then
            break
        fi
        if [ "$waited" -ge $(( max_min * 60 )) ]; then
            echo "WARNING: still ${reason} after ${max_min} min (DEPLOY_SLOT_WAIT_MAX_MIN); restarting anyway"
            return 0
        fi
        if [ "$reason" != "$last_reason" ]; then
            echo "Holding the restart: ${reason} (Berlin $(TZ=Europe/Berlin date '+%H:%M')). The old containers keep serving; DEPLOY_IGNORE_SLOTS=1 skips this."
            last_reason="$reason"
        fi
        sleep "$poll"
        waited=$(( waited + poll ))
    done
    if [ "$waited" -gt 0 ]; then
        echo "Clear after $(( waited / 60 ))m$(( waited % 60 ))s of waiting (Berlin $(TZ=Europe/Berlin date '+%H:%M')); restarting"
    else
        echo "Clear of every sync slot (Berlin $(TZ=Europe/Berlin date '+%H:%M')); restarting"
    fi
}
wait_for_sync_slot_clear
# --- end of the sync-slot re-check -----------------------------------------------------

# Fold SQLite's write-ahead log into the database BEFORE the container goes. Under the old
# FILE bind mount (`./portfolio.db`, until 2026-10-04) the -wal/-shm sidecars lived in the
# container's writable layer and vanished with `down` — taking every commit since the last
# auto-checkpoint (up to ~4 MB) with them; the directory mount puts them on the host, and
# this stays because the deploy that performs that move stops a container still on the
# file mount, as does a rollback to an older commit. The path comes from the running
# container's own settings, so it is right on either side of the move; `mode=rw` so a
# wrong path fails instead of creating an empty file. Measured 2026-09-08: a sync_runs row
# written at 18:23 UTC was gone after the 18:30 deploy while one from 18:07 survived, and the
# container held a 2.7 MB WAL against a 0-byte one on the host. The app also checkpoints on a clean
# shutdown; this covers a stop that is not clean. A container that is not running has
# nothing to lose, hence the fallback message rather than a failure.
docker compose exec -T portfolio-backend python -c "import sqlite3; from sqlalchemy.engine import make_url; from app.config import settings; p = make_url(settings.database_url).database; c = sqlite3.connect('file:' + p + '?mode=rw', uri=True); c.execute('PRAGMA busy_timeout=30000'); print('WAL checkpoint of', p, '(busy, frames, done):', c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()); c.close()" \
    || echo "WAL checkpoint skipped (backend container not running)"
docker compose down

# --- Database directory switchover (ops/db-layout.sh) -------------------------
# One-time and idempotent: moves backend/portfolio.db into backend/data/ and leaves a
# symlink at the old path, or does nothing when that has already happened. Here, between
# `down` and `up`, because the file must not move under a running container. A refusal
# leaves the layout untouched and exits with the containers down: auto-deploy's rollback
# brings the previous build back up on the old path; by hand, fix what it names and re-run.
bash "$REPO_DIR/ops/db-layout.sh" switch
# --- end database directory switchover ---------------------------------------

cd "$REPO_DIR/frontend"
rm -rf dist.old
[ -d dist ] && mv dist dist.old
mv dist.next dist
cd "$REPO_DIR/backend"
docker compose up -d
rm -rf "$REPO_DIR/frontend/dist.old"

# 5. Status check
echo ""
echo "=== Deployment Complete ==="
echo ""
echo "--- Docker status ---"
docker compose ps

echo ""
echo "--- Health check ---"
sleep 3
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8000/health" || true)
if [ "$HEALTH" = "200" ]; then
    echo "Backend health check: OK (200)"
else
    echo "Backend health check: FAILED (HTTP $HEALTH)"
    echo "Check logs: cd $REPO_DIR/backend && docker compose logs -f"
fi

SCHEDULER=$(curl -s "http://127.0.0.1:8000/api/scheduler/status" 2>/dev/null || true)
if [ -n "$SCHEDULER" ]; then
    echo "Scheduler status: $SCHEDULER"
fi

echo ""
echo "Visit: https://$DOMAIN"
echo "Logs:  cd $REPO_DIR/backend && docker compose logs -f"
