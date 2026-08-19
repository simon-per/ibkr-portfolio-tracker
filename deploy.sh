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
npm run build

# 4. Rebuild and restart Docker containers (backend + frontend nginx)
echo ""
echo "--- Rebuilding Docker containers ---"
cd "$REPO_DIR/backend"

# Ensure portfolio.db exists as a FILE before the bind mount — otherwise Docker
# creates a directory at ./portfolio.db and SQLite cannot open it. On a fresh install
# the app auto-creates the schema; to keep your data, restore the backup db here first.
[ -f portfolio.db ] || touch portfolio.db

# Same trap for the scheduler's job store, which is bind-mounted so persisted jobs
# survive the rebuild — the whole point of persisting them.
[ -f scheduler_jobs.db ] || touch scheduler_jobs.db

# Stamps the running build so /health and the app footer can identify it, which is how
# "did my deploy land" gets answered from a browser rather than over ssh.
GIT_COMMIT="$(cd "$REPO_DIR" && git rev-parse HEAD 2>/dev/null || echo unknown)"
export GIT_COMMIT
echo "Deploying commit: $GIT_COMMIT"

docker compose down
docker compose build --no-cache
docker compose up -d

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
