#!/bin/bash
# Where the database lives on the VPS, and the one-time move that put it there.
#
#     bash ops/db-layout.sh preflight   # read-only; deploy.sh runs it before the build
#     bash ops/db-layout.sh switch      # deploy.sh runs it after `down`, before `up`
#
# Until this existed, docker-compose bind-mounted `./portfolio.db:/app/portfolio.db` — a
# FILE — so SQLite's `-wal`/`-shm` sidecars lived in the container's writable layer and
# died with every `docker compose down` (docs/deployment.md has the measurement). Three
# checkpoints papered over it, but a killed container never reaches one. The fix is the
# one the scheduler's job store got on 2026-08-01: mount the DIRECTORY, so the sidecars
# land on the host. The database therefore moves from backend/portfolio.db to
# backend/data/portfolio.db, and docker-compose sets DATABASE_URL to match.
#
# The move happens inside an unattended deploy (ops/auto-deploy.sh deploys every push
# within minutes), so this script is written around the one outcome that must never
# happen: the app starting on a NEW, EMPTY database while the real one sits at the old
# path. That would look like total data loss, and the next IBKR sync would rebuild the
# account from a bounded Flex window. So:
#
# * `preflight` runs before anything is built or stopped and refuses every state it does
#   not recognise, leaving the old containers serving.
# * `switch` is idempotent: a deploy that finds the move already done does nothing.
# * The old path becomes a SYMLINK to the new one, not a hole. Three things still look
#   there and each would otherwise act on an empty or missing file: a rollback to a commit
#   older than this one (its deploy.sh does `[ -f portfolio.db ] || touch portfolio.db`,
#   and its compose file mounts that path — through the link, the real file); the copy of
#   ops/backup-db.sh installed at /root/backup-db.sh, which does not update itself; and any
#   hand-typed command from the documentation of the time. SQLite resolves a symlinked
#   database to its real name before naming the -wal, so even a host-side backup through
#   the link reads the WAL that now lives beside data/portfolio.db.
# * The app's container refuses to start on a database file that does not exist or is
#   empty (app/db_guard.py, run before `alembic upgrade head`), so even a deploy that
#   skipped this script cannot quietly create one — it fails its health check instead.
#
# States, relative to backend/:
#   migrated  data/portfolio.db is a regular file; portfolio.db is absent or a symlink to it
#   legacy    portfolio.db is a regular file; nothing at data/portfolio.db (or its sidecars)
#   fresh     neither exists — accepted only with ALLOW_NEW_DATABASE=1
#   anything else is refused: two databases, a dangling link, a directory where a file
#   belongs (Docker's auto-create), a stray -wal waiting beside the destination.
#
# Overridable for tests/test_db_directory_mount.py, which runs both subcommands against
# throwaway directories. Nothing on the VPS sets BACKEND_DIR.

set -euo pipefail

BACKEND_DIR="${BACKEND_DIR:-${REPO_DIR:-/root/IBKR_investment_tracker}/backend}"
ALLOW_NEW_DATABASE="${ALLOW_NEW_DATABASE:-0}"

LEGACY="portfolio.db"
TARGET_DIR="data"
TARGET="$TARGET_DIR/portfolio.db"
SIDECARS="-wal -shm -journal"

say() { echo "db-layout: $*"; }
refuse() {
    echo "db-layout: REFUSE: $*" >&2
    echo "db-layout: see docs/deployment.md, \"The database directory\"." >&2
    exit 1
}

# -e follows links, so a dangling link is invisible to it; this is "anything is there".
present() { [ -e "$1" ] || [ -L "$1" ]; }
regular_file() { [ -f "$1" ] && [ ! -L "$1" ]; }

classify() {
    if present "$TARGET_DIR" && { [ -L "$TARGET_DIR" ] || [ ! -d "$TARGET_DIR" ]; }; then
        echo "conflict:$TARGET_DIR exists and is not a plain directory"
        return
    fi
    if regular_file "$TARGET"; then
        if ! present "$LEGACY"; then
            echo "migrated"
        elif [ -L "$LEGACY" ] && [ "$(readlink -f "$LEGACY")" = "$(readlink -f "$TARGET")" ]; then
            echo "migrated"
        else
            echo "conflict:both $LEGACY and $TARGET exist and are different files — which one is live is a question for a human"
        fi
        return
    fi
    if present "$TARGET"; then
        echo "conflict:$TARGET exists but is not a regular file"
        return
    fi
    if regular_file "$LEGACY"; then
        # A -wal waiting at the destination would be replayed into the database the moment
        # SQLite opens it there — pages from some other database, or an older one.
        local s
        for s in $SIDECARS; do
            if present "$TARGET$s"; then
                echo "conflict:$TARGET$s exists although $TARGET does not"
                return
            fi
        done
        echo "legacy"
        return
    fi
    if present "$LEGACY"; then
        echo "conflict:$LEGACY exists but is not a regular file (a dangling link, or the directory Docker creates for a missing bind-mount source)"
        return
    fi
    echo "fresh"
}

fresh_refusal() {
    refuse "no database at $BACKEND_DIR/$TARGET or $BACKEND_DIR/$LEGACY. Refusing to let the app create an empty one. Restore the newest /root/ibkr-backups/<date>/ snapshot to $BACKEND_DIR/$TARGET; only on a genuinely fresh install, re-run with ALLOW_NEW_DATABASE=1. Nothing was moved"
}

cd "$BACKEND_DIR" || refuse "cannot enter $BACKEND_DIR"
STATE="$(classify)"

case "${1:-}" in
    preflight)
        case "$STATE" in
            migrated)   say "database at $TARGET" ;;
            legacy)     say "database at $LEGACY; this deploy moves it to $TARGET after \`down\`" ;;
            fresh)      [ "$ALLOW_NEW_DATABASE" = "1" ] || fresh_refusal
                        say "fresh install (ALLOW_NEW_DATABASE=1): the app will create $TARGET" ;;
            conflict:*) refuse "${STATE#conflict:}. Nothing was moved" ;;
        esac
        ;;
    switch)
        case "$STATE" in
            migrated)
                say "database already at $TARGET; nothing to move"
                ;;
            fresh)
                [ "$ALLOW_NEW_DATABASE" = "1" ] || fresh_refusal
                mkdir -p "$TARGET_DIR"
                say "fresh install: created $TARGET_DIR/ for the app to create the database in"
                ;;
            legacy)
                mkdir -p "$TARGET_DIR"
                stamp="$(date -u +%Y%m%dT%H%M%SZ)"
                # Host-side sidecars are SET ASIDE, not carried along. Under the file mount
                # the container never saw them — its WAL was the one in its own layer, which
                # deploy.sh checkpointed before `down` — so the main file alone is the state
                # the app last served. Moving a host -wal next to it would make SQLite replay
                # it into that state. Kept (inside data/, which git ignores) rather than
                # deleted, because a file nobody has looked at is not a file to destroy.
                for s in $SIDECARS; do
                    if present "$LEGACY$s"; then
                        mv "$LEGACY$s" "$TARGET$s.pre-switchover-$stamp"
                        say "set aside host-side $LEGACY$s as $TARGET$s.pre-switchover-$stamp"
                    fi
                done
                mv "$LEGACY" "$TARGET"
                if ! ln -s "$TARGET" "$LEGACY"; then
                    mv "$TARGET" "$LEGACY"
                    refuse "could not create the $LEGACY -> $TARGET link; moved the database back"
                fi
                [ "$(classify)" = "migrated" ] || refuse "after the move the layout reads as '$(classify)'"
                say "moved $LEGACY to $TARGET; $LEGACY is now a link to it"
                ;;
            conflict:*)
                refuse "${STATE#conflict:}. Nothing was moved"
                ;;
        esac
        ;;
    *)
        echo "usage: $0 preflight|switch" >&2
        exit 2
        ;;
esac
