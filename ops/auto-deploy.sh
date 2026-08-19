#!/bin/bash
# Unattended deploy for the IBKR portfolio tracker.
#
# Lives here rather than only on the VPS. It governs every deploy, and for months
# the only copy was /root/auto-deploy.sh — unversioned, unreviewed, and invisible
# to anyone reading the repo. Install with:
#
#     install -m 755 ops/auto-deploy.sh /root/auto-deploy.sh
#
# Runs from root's crontab every 10 minutes. deploy.sh is expensive (docker
# compose down + build --no-cache + npm ci), so this only invokes it when
# origin/main is genuinely AHEAD of the checkout — never on an unchanged tick,
# and never when the VPS is ahead (which happens legitimately after a local
# commit here; a bare inequality test would loop-deploy while `git pull` fails).
#
# Deploys unattended, so it also: refuses to start inside a sync window (below),
# backs up the DB first (the container CMD runs `alembic upgrade head`),
# health-checks afterwards, and ROLLS BACK to the previous commit if the app
# doesn't come up.

set -uo pipefail

# Overridable so the deploy/rollback logic can be rehearsed against a throwaway repo
# instead of production — see tests/test_deploy_rollback.py and the rehearsal it
# describes. Nothing on the VPS sets any of these, so the defaults are what runs.
# SYNC_HOURS and SLOT_MARGIN_MIN below are deliberately NOT parameterised: they are
# regex-parsed out of this file by tests/test_deploy_guard_hours.py.
REPO_DIR="${REPO_DIR:-/root/IBKR_investment_tracker}"
LOG="${LOG:-/root/auto-deploy.log}"
BACKUP_ROOT="${BACKUP_ROOT:-/root/ibkr-backups}"
LOCKFILE="${LOCKFILE:-/root/.auto-deploy.lock}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
# A real data endpoint, checked alongside /health — see health_ok.
SMOKE_URL="${SMOKE_URL:-http://127.0.0.1:8000/api/portfolio/summary}"
# Public repo, so the check-runs API needs no credential. See ci_verdict.
GITHUB_REPO="${GITHUB_REPO:-simon-per/ibkr-portfolio-tracker}"
# Records a commit whose deploy was rolled back — see the quarantine guard.
QUARANTINE="${QUARANTINE:-/root/.auto-deploy-quarantine}"

# Europe/Berlin hours at which APScheduler runs a sync. This list is a COPY of the
# CronTriggers in backend/app/services/scheduler_service.py, so it can drift — and it
# did: written on 2026-07-31, the same day 13:00 and 20:00 were retired in favour of
# 00:00 and 06:00, it kept the old pair. The effect was the wrong half of the day
# protected: two slots guarded that no longer run, and the two overnight IBKR slots
# guarded by nothing. `tests/test_deploy_guard_hours.py` now reads this file and fails
# the suite if the two ever disagree again.
#
# 18 and 0 are the IBKR slots; the rest are MARKET_DATA_HOURS, widened on 2026-08-04
# from 15/22 so the portfolio reprices through both sessions instead of only after each
# close. Eight guarded slots defer at most ~3h of the day in 21-minute bands, and
# auto-deploy ticks every 10 minutes, so a push still lands promptly.
#
# 06:00 left on 2026-08-08, when the IBKR slots became 18:00 (primary) and 00:00
# (recovery) — note 18 was already guarded as a market-data hour, so only 6 dropped.
SYNC_HOURS="0 8 11 13 15 18 20 22"
# Minutes either side of a slot to stay clear of. A rebuild takes ~2-5 minutes and
# APScheduler is in-process, so a deploy overlapping a slot loses that run. The
# persistent job store (2026-08-01) recovers a misfire up to 30 minutes late, so this
# is now a second line of defence rather than the only one — but a `--no-cache` rebuild
# can exceed that grace, so it still earns its keep. 10 covers the rebuild plus the job.
SLOT_MARGIN_MIN=10

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S') UTC] $*" >> "$LOG"; }

# Never let two deploys overlap.
exec 9>"$LOCKFILE"
flock -n 9 || exit 0

# --- Sync-slot guard ---------------------------------------------------------
# On 2026-07-30 a push landed at 06:00 UTC — exactly the 08:00 Berlin slot — and
# that day's full_sync never ran: no row in sync_runs, no 730-day price refresh,
# no dividend sync, and both IBKR retry slots happened to fail. A deploy is never
# urgent; the next tick is ten minutes away. So skip rather than race.
#
# Deliberately checked before `git fetch`: nothing here should touch the network
# on a tick that cannot deploy anyway.
in_sync_window() {
    local now_h now_m mins_now slot slot_mins delta
    now_h=$(TZ=Europe/Berlin date '+%-H')
    now_m=$(TZ=Europe/Berlin date '+%-M')
    mins_now=$(( now_h * 60 + now_m ))
    for slot in $SYNC_HOURS; do
        slot_mins=$(( slot * 60 ))
        delta=$(( mins_now - slot_mins ))
        [ "$delta" -lt 0 ] && delta=$(( -delta ))
        # Wrap around midnight so 23:55 is measured against 00:00 too.
        [ "$delta" -gt 720 ] && delta=$(( 1440 - delta ))
        if [ "$delta" -le "$SLOT_MARGIN_MIN" ]; then
            echo "$slot"
            return 0
        fi
    done
    return 1
}

if slot=$(in_sync_window); then
    # Only worth a log line when there is actually something waiting to deploy,
    # or this writes a skip every ten minutes forever.
    cd "$REPO_DIR" 2>/dev/null && \
        if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main 2>/dev/null)" ]; then
            log "SKIP: within ${SLOT_MARGIN_MIN}min of the ${slot}:00 Europe/Berlin sync slot; deferring to the next tick"
        fi
    exit 0
fi

cd "$REPO_DIR" || { log "ERROR: $REPO_DIR missing"; exit 1; }

git fetch origin main --quiet 2>>"$LOG" || { log "ERROR: git fetch failed"; exit 1; }

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

[ "$LOCAL" = "$REMOTE" ] && exit 0

# Only deploy when we are strictly BEHIND origin/main (HEAD is its ancestor).
if ! git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
    log "SKIP: local ${LOCAL:0:7} has diverged from origin/main ${REMOTE:0:7} — needs manual reconcile"
    exit 0
fi

# --- Quarantine guard --------------------------------------------------------
# A rollback that WORKS creates a problem the broken one never had: the checkout is
# back at the last good commit while origin/main still holds the bad one, so the very
# next tick sees "behind origin/main", passes the ancestor check, and redeploys the
# commit that just failed — every ten minutes, each time with a full --no-cache rebuild
# and an outage. (The old rollback avoided this only by accident: its `git pull` left
# HEAD at the broken commit, so the next tick saw nothing to do.)
#
# So a rolled-back sha is quarantined until origin/main moves past it. Pushing a fix
# clears it automatically, because the marker is compared against the sha itself.
#
# Re-logged at most hourly rather than every tick: a broken main must stay visible, and
# 144 identical lines a day is how a log stops being read.
if [ -f "$QUARANTINE" ] && [ "$(cat "$QUARANTINE" 2>/dev/null)" = "$REMOTE" ]; then
    if [ -z "$(find "$QUARANTINE" -mmin -60 -print -quit 2>/dev/null)" ]; then
        log "REFUSE: origin/main ${REMOTE:0:7} was rolled back and is quarantined; push a fix to clear it"
        touch "$QUARANTINE"
    fi
    exit 0
fi

# --- CI gate -----------------------------------------------------------------
# Until 2026-08-19 nothing ran the test suite before a commit deployed itself. The only
# gate was health_ok, and /health issues no database query and touches no router, so a
# commit that 500s every /api/portfolio/* route deployed, passed, and was logged
# SUCCESS. .github/workflows/ci.yml now runs both suites on every push; this asks
# GitHub what it concluded.
#
# The three answers are deliberately NOT symmetric, because the failure directions are
# not either. A red commit must never deploy. A pending one is just early — the suites
# take a few minutes and the next tick is ten away. But "no checks found" and "GitHub
# unreachable" must FAIL OPEN and deploy: a gate that can brick every future deploy
# because an unrelated service is down, or because the commit predates the workflow, is
# a worse failure than the one it prevents. Same reasoning as the sync-slot guard
# logging only when there is something waiting.
#
# Unauthenticated, which is fine on a public repo (60 requests/hour/IP) and means no
# credential on the VPS. Reached only when there is something to deploy.
# The check-runs decision table, kept as its own value so tests/test_deploy_rollback.py
# can extract and run it against real payload shapes rather than trusting it by eye. A
# gate whose verdict nobody has exercised is a gate nobody should rely on.
#
# `neutral` and `skipped` count as success: a job that deliberately did not run (a path
# filter, a manual skip) is not a failing test.
read -r -d '' CI_VERDICT_PY <<'PYEOF'
import json, sys
try:
    runs = json.load(sys.stdin).get("check_runs") or []
except Exception:
    print("unavailable"); raise SystemExit(0)
if not runs:
    print("none"); raise SystemExit(0)
if any(r.get("status") != "completed" for r in runs):
    print("pending"); raise SystemExit(0)
bad = [r.get("name", "?") for r in runs
       if r.get("conclusion") not in ("success", "neutral", "skipped")]
print("failed:" + ",".join(bad) if bad else "success")
PYEOF

ci_verdict() {
    local url="https://api.github.com/repos/${GITHUB_REPO}/commits/${REMOTE}/check-runs"
    curl -s --max-time 20 -H "Accept: application/vnd.github+json" "$url" 2>/dev/null \
        | python3 -c "$CI_VERDICT_PY" 2>/dev/null || echo unavailable
}

VERDICT=$(ci_verdict)
case "$VERDICT" in
    success)   : ;;
    pending)   log "WAIT: CI still running for ${REMOTE:0:7}; deferring to the next tick"; exit 0 ;;
    failed:*)  log "REFUSE: CI is red for ${REMOTE:0:7} (${VERDICT#failed:}) — not deploying. Fix it and push again."; exit 0 ;;
    none)
        # "No checks yet" and "no checks ever" look identical here, and they need
        # opposite answers: the first is a push from thirty seconds ago whose workflow
        # has not registered its jobs, the second is a commit that predates the workflow
        # or a repo where Actions is off. Deploying the first ungated would quietly
        # reopen the hole this gate exists to close.
        #
        # The commit's own timestamp separates them well enough. It is a proxy rather
        # than a proof — a commit authored yesterday and pushed now reads as old, so it
        # deploys ungated — but it costs one tick and closes the common case.
        NOW=$(date -u +%s)
        MADE=$(git show -s --format=%ct "$REMOTE" 2>/dev/null || echo 0)
        if [ "$MADE" -gt 0 ] && [ $((NOW - MADE)) -lt 180 ]; then
            log "WAIT: ${REMOTE:0:7} has no CI checks yet and is $((NOW - MADE))s old; deferring one tick"
            exit 0
        fi
        log "NOTE: no CI checks found for ${REMOTE:0:7}; deploying ungated"
        ;;
    *)         log "NOTE: could not reach the GitHub check API; deploying ungated" ;;
esac

log "change detected: ${LOCAL:0:7} -> ${REMOTE:0:7}, deploying"

# Back up the DB before any migration runs unattended.
#
# Delegated to backup-db.sh rather than done inline, because the inline version was a
# bare `cp` of a live WAL database and therefore did not work. Demonstrated rather than
# argued: with 500 rows written since the last checkpoint, `cp` of the main file alone
# recovers **0** of them and still answers `PRAGMA integrity_check` -> ok, while
# sqlite's backup API recovers all 500. DEPLOY.md's Gotchas already stated the rule.
#
# Prefer the installed copy: on the tick that first deploys this change the working
# tree is still at the previous commit, where ops/backup-db.sh does not exist yet.
BACKUP_SCRIPT="/root/backup-db.sh"
[ -x "$BACKUP_SCRIPT" ] || BACKUP_SCRIPT="$REPO_DIR/ops/backup-db.sh"

# A failed backup now ABORTS the deploy instead of warning and continuing. The next
# step runs `alembic upgrade head` unattended against the account's only copy of data
# that cannot be re-fetched from IBKR (the Flex window is 3 days), and a deploy is never
# urgent — the next tick is ten minutes away. Continuing was defensible while the
# backup was a `cp` nobody trusted; it is not defensible now that a failure means the
# snapshot genuinely could not be taken or did not verify.
if BACKUP_ROOT="$BACKUP_ROOT" REPO_DIR="$REPO_DIR" bash "$BACKUP_SCRIPT" autodeploy; then
    log "db snapshot taken and verified"
else
    log "ABORT: db snapshot failed or did not verify; refusing to deploy ${REMOTE:0:7} this tick"
    exit 1
fi

# Liveness AND one real query. /health deliberately touches no database and no router
# — it answers "is this process serving?" and nothing more — so on its own it accepts a
# build whose every portfolio route 500s. /api/portfolio/summary reads tax lots, prices
# and FX through the same stack the dashboard uses and answers in ~160ms, which makes it
# a smoke test rather than a second liveness check.
#
# Note what is deliberately NOT done here: /health is not made to fail on stale data.
# A 503 for "the last sync is old" would turn an IBKR outage into a deploy rollback,
# which is the opposite of useful. Freshness is reported in sync warnings; this gate
# only ever asks whether the build that was just deployed works.
health_ok() {
    local code
    for _ in $(seq 1 12); do   # up to ~60s: alembic + uvicorn startup
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL")
        if [ "$code" = "200" ]; then
            code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$SMOKE_URL")
            [ "$code" = "200" ] && return 0
        fi
        sleep 5
    done
    return 1
}

# Advance the checkout HERE rather than letting deploy.sh's `git pull` do it, and pass
# DEPLOY_NO_PULL=1 to both invocations below. That is what makes the rollback real: it
# does `git reset --hard "$LOCAL"`, and a deploy.sh that pulls would fast-forward
# straight back to the broken commit — guaranteed to succeed, because the ancestor check
# above has already proven LOCAL is an ancestor of REMOTE. So every "rollback" since this
# script was written rebuilt and redeployed the commit that had just failed, then
# reported that the rollback had failed too. It had never run once.
if ! git merge --ff-only "$REMOTE" >>"$LOG" 2>&1; then
    log "ERROR: fast-forward to ${REMOTE:0:7} failed — needs manual reconcile"
    exit 1
fi

if DEPLOY_NO_PULL=1 bash "$REPO_DIR/deploy.sh" >>"$LOG" 2>&1 && health_ok; then
    rm -f "$QUARANTINE"
    log "SUCCESS: deployed $(git rev-parse --short HEAD), health 200"
else
    log "FAILURE: deploy or health check failed — ROLLING BACK to ${LOCAL:0:7}"
    echo "$REMOTE" > "$QUARANTINE"
    git reset --hard "$LOCAL" >>"$LOG" 2>&1
    if DEPLOY_NO_PULL=1 bash "$REPO_DIR/deploy.sh" >>"$LOG" 2>&1 && health_ok; then
        log "ROLLED BACK to ${LOCAL:0:7}, health 200. origin/main ${REMOTE:0:7} is BROKEN — fix it before it redeploys."
    else
        log "CRITICAL: rollback also failed, app may be DOWN. Manual intervention required."
    fi
fi

# Pruning lives in backup-db.sh, which is also what the daily cron runs — one retention
# rule for both producers rather than two that can disagree about what survives.
