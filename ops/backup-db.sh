#!/bin/bash
# Snapshot backend/portfolio.db, verify the snapshot, and prune old ones.
#
# Lives here rather than only on the VPS, for the reason ops/auto-deploy.sh's own
# header gives: a file that governs the data's only safety net should be readable by
# anyone reading the repo. Install with:
#
#     install -m 755 ops/backup-db.sh /root/backup-db.sh
#
# and run it daily from root's crontab at an hour clear of every Europe/Berlin sync
# slot (cron on the VPS runs in UTC; 03:17 UTC = 05:17 Berlin, which no slot uses):
#
#     17 3 * * * /root/backup-db.sh daily
#
# Two reasons this exists as its own script rather than three lines inside
# auto-deploy.sh:
#
# 1. **`cp` is the wrong tool and was the tool.** Until 2026-08-19 auto-deploy did a
#    bare `cp` of portfolio.db *while the backend was running* — the copy happened
#    before deploy.sh's `docker compose down`, and neither `-wal` nor `-shm` came with
#    it. DEPLOY.md's own Gotchas section states that exact prohibition. In WAL mode the
#    main file holds only what has been checkpointed, so every transaction since the
#    last checkpoint was missing from the backup, and a copy taken *during* a checkpoint
#    can be torn. The failure is silent in the worst way: the file opens, has a
#    plausible size, and is only revealed by a restore. sqlite's own backup API takes a
#    consistent snapshot of a live database, which is what it is for.
#
# 2. **Backups only happened when somebody pushed.** The whole crontab was auto-deploy,
#    and the backup sat inside the deploy path — so on 2026-08-19 the newest backup was
#    two days old purely because nobody had committed. A daily run makes the backup
#    interval a property of time rather than of development activity.
#
# What matters about this data: it cannot be re-fetched. The Flex Query window is 3
# days and IBKR holds nothing before 2026 for this account, so trades, cash flows,
# corporate actions and the IBKR dividend ledger exist only here. OpenPositions is
# period-independent, so lots would come back from a sync; nothing else would.

set -uo pipefail

REPO_DIR="${REPO_DIR:-/root/IBKR_investment_tracker}"
DB="${DB:-$REPO_DIR/backend/portfolio.db}"
BACKUP_ROOT="${BACKUP_ROOT:-/root/ibkr-backups}"
LOG="${BACKUP_LOG:-/root/backup-db.log}"

# Retention is bounded from BOTH ends, because each rule alone fails in a way this
# repository has already seen. Age-only would delete everything if backups ever stopped
# being taken (the newest is then older than the cutoff too). Count-only was the
# previous rule and evaporated the history on a busy afternoon: ten pushes and the
# oldest surviving copy was hours old, so corruption noticed the next morning had no
# clean restore point.
RETAIN_DAYS="${RETAIN_DAYS:-30}"
MIN_KEEP="${MIN_KEEP:-10}"

# Overridable so the script can be exercised off the VPS, where Git Bash has
# `python` but no `python3`. The VPS has python3 and never sets this.
PYTHON="${PYTHON:-python3}"

TAG="${1:-manual}"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S') UTC] $*" >> "$LOG"; }

[ -f "$DB" ] || { log "ERROR: $DB not found"; exit 1; }

DEST_DIR="$BACKUP_ROOT/$(date -u +%F)"
mkdir -p "$DEST_DIR" || { log "ERROR: cannot create $DEST_DIR"; exit 1; }
DEST="$DEST_DIR/portfolio.db.$TAG-$(date -u +%H%M%S)"

# The source is opened read-WRITE on purpose. A read-only connection to a WAL database
# cannot create the -shm it may need, so `mode=ro` fails outright whenever the app is
# not already holding the file open — i.e. precisely during a restore rehearsal or after
# `docker compose down`. sqlite's backup API takes only a read lock either way, and
# `timeout=30` matches the app's own busy_timeout so a checkpoint in flight is waited
# out rather than raising.
"$PYTHON" - "$DB" "$DEST" <<'PY' >>"$LOG" 2>&1
import sqlite3
import sys

src_path, dest_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path, timeout=30)
dest = sqlite3.connect(dest_path)
try:
    src.backup(dest)
finally:
    dest.close()
    src.close()

# Verify the COPY, not the original. A backup nobody has read is a belief, and this is
# the one check that turns "the file exists and is about the right size" into evidence.
# Loud beats silent here by a wide margin: an unusable backup discovered during a
# restore is discovered at the exact moment it cannot be fixed.
check = sqlite3.connect(dest_path)
try:
    verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    check.close()
if verdict != "ok":
    sys.exit(f"integrity_check on the snapshot returned {verdict!r}")
PY

if [ $? -ne 0 ]; then
    log "ERROR: snapshot or integrity check FAILED for $DEST"
    rm -f "$DEST"
    exit 1
fi

log "snapshot ok -> $DEST ($(stat -c %s "$DEST" 2>/dev/null || echo '?') bytes)"

# Prune: delete snapshots older than RETAIN_DAYS, but never drop below MIN_KEEP of the
# most recent, whatever their age.
mapfile -t all_backups < <(ls -1t "$BACKUP_ROOT"/*/portfolio.db.*-* 2>/dev/null)
if [ "${#all_backups[@]}" -gt "$MIN_KEEP" ]; then
    for old in "${all_backups[@]:$MIN_KEEP}"; do
        if [ -n "$(find "$old" -mtime "+$RETAIN_DAYS" -print -quit 2>/dev/null)" ]; then
            rm -f "$old" && log "pruned $old"
        fi
    done
fi

# Empty dated directories left behind by pruning; harmless but they accumulate forever.
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null

exit 0
