#!/usr/bin/env bash
#
# Ship the pending work and turn on write auth, in the one order that is safe.
#
# The three remaining steps have a hard dependency that is easy to get wrong and
# unpleasant to undo:
#
#   1. push            -> the VPS auto-deploys within 5 minutes of a green CI
#   2. API_ADMIN_TOKEN -> ONLY once the new frontend is actually live
#   3. auto-deploy.sh  -> any time after
#
# Setting the token first locks you out of your own site: the currently-deployed
# frontend has no lock button, so its sync and base-currency controls start getting
# 401s with no way to supply the key. Recovering means another ssh trip to unset it.
#
# So this script refuses to reach step 2 until /health reports the commit it pushed.
# Every step asks first and can be skipped; nothing here is silent.
#
# Usage:  bash ops/finish-deploy.sh
set -euo pipefail

HOST="${DEPLOY_HOST:-portfolio.srv1211053.hstgr.cloud}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_ed25519_hostinger}"
REMOTE="root@${HOST}"
ENV_PATH="/root/IBKR_investment_tracker/backend/.env"
HEALTH="https://${HOST}/health"
SSH=(ssh -i "$SSH_KEY" "$REMOTE")

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mXX %s\033[0m\n' "$*" >&2; exit 1; }

ask() {  # ask "question" -> 0 if yes
  local reply
  read -r -p "$1 [y/N] " reply </dev/tty
  [[ "$reply" =~ ^[Yy]$ ]]
}

# --- preflight -------------------------------------------------------------
say "Preflight"

cd "$(dirname "$0")/.."
[ -n "$(git status --porcelain)" ] && die "Working tree is dirty. Commit or stash first."

branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || die "On '$branch', not main."

ahead="$(git rev-list --count origin/main..main)"
echo "Branch main, tree clean, $ahead commit(s) to push."
[ "$ahead" -eq 0 ] && warn "Nothing to push — skipping to the verification step."

# A deploy rebuilding during a Berlin sync slot used to lose that sync outright. The
# persistent job store now recovers a miss under 30 min, but it is not live until this
# very deploy lands, so the first push still has to dodge the slots by hand.
#
# Berlin time comes from Python's zoneinfo, NOT from `TZ=Europe/Berlin date`.
# Git Bash on Windows silently ignores TZ and returns UTC — verified: both
# `TZ=Europe/Berlin` and `TZ=America/New_York` print the UTC time. In summer that is a
# two-hour error in the unsafe direction: at 07:55 Berlin the shell would report 05:55,
# measure two hours to the 08:00 slot, and wave the push through into exactly the
# collision this guard exists to prevent.
#
berlin_hhmm() {
  local py
  for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
      "$py" -c 'from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo("Europe/Berlin")).strftime("%H:%M"))' && return 0
    fi
  done
  return 1
}

slot_guard() {
  local now_min berlin
  if ! berlin="$(berlin_hhmm)"; then
    warn "Could not determine Berlin time (no usable python), so the sync-slot check"
    warn "was skipped. Slots are on the hour at 00/06/08/11/13/15/18/20/22 Berlin."
    ask "Continue anyway?" || die "Stopped."
    return
  fi
  now_min=$(( 10#${berlin%:*} * 60 + 10#${berlin#*:} ))
  # Minutes past midnight for each slot. A THIRD copy of the scheduler's hours, after
  # ops/auto-deploy.sh and this file's PowerShell twin — and the two here were stale
  # for four days: written with 13:00/20:00 on the day those were retired for
  # 00:00/06:00, so the guard both waved pushes through the two live overnight slots
  # and blocked them at two that no longer ran. Only auto-deploy.sh was under test.
  # tests/test_deploy_guard_hours.py now reads all three against ALL_SYNC_HOURS.
  # 00:00  08:00  11:00  13:00  15:00  18:00  20:00  22:00
  for slot in 0 480 660 780 900 1080 1200 1320; do
    local delta=$(( now_min - slot ))
    [ "$delta" -lt 0 ] && delta=$(( -delta ))
    # Wrap around midnight, or 23:55 is measured as 1435 minutes from the 00:00 slot
    # instead of 5 — which is the whole reason 00:00 needed adding here.
    [ "$delta" -gt 720 ] && delta=$(( 1440 - delta ))
    if [ "$delta" -le 12 ]; then
      warn "It is $berlin Berlin — within 12 minutes of a sync slot."
      warn "Auto-deploy rebuilds in ~90s and would land on top of it."
      ask "Push anyway?" || die "Stopped. Try again in ~15 minutes."
      return
    fi
  done
  echo "Berlin time $berlin — clear of every sync slot."
}
slot_guard

# --- step 1: push ----------------------------------------------------------
target="$(git rev-parse HEAD)"
if [ "$ahead" -gt 0 ]; then
  say "Step 1/3 — push to origin/main"
  git --no-pager log --oneline origin/main..main | sed 's/^/    /'
  ask "Push these?" || die "Stopped before pushing."
  git push origin main
  echo "Pushed. The VPS cron picks it up within 5 minutes of a green CI."
fi

# --- step 2 gate: the deploy must actually be live -------------------------
say "Waiting for ${target:0:7} to go live at ${HEALTH}"
echo "Auto-deploy runs every 5 min once CI is green, and the rebuild takes a few minutes, so allow ~10 min."
echo "(Ctrl-C is safe — the deploy continues; re-run this script to pick up here.)"

# Two accepted signals, because an exact sha match is not always available:
#
#   * commit == target                     -> unambiguous, the normal case.
#   * commit == "unknown" AND the response carries the new-build marker fields.
#     `deploy.sh` pulls the repo *itself*, so the copy already executing is the one from
#     before the pull — bash does not reload a running script. Any deploy that changes
#     deploy.sh therefore runs the OLD logic once, and the GIT_COMMIT export it gained
#     is missing for exactly that run. Matching only on sha hangs the full 15 minutes
#     over a cosmetic stamp. Hit on 2026-07-31.
#
# `write_auth_enabled` is the marker: it does not exist in any build predating this
# work, so a rolled-back old build cannot satisfy it.
deadline=$(( $(date +%s) + 900 ))
landed=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  body="$(curl -fsS --max-time 10 "$HEALTH" 2>/dev/null || true)"
  live="$(printf '%s' "$body" | sed -n 's/.*"commit"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"

  if [ -n "$live" ] && [ "${live:0:7}" = "${target:0:7}" ]; then
    echo "Live commit is ${live:0:7} — the deploy landed."
    landed=1; break
  fi
  if [ "$live" = "unknown" ] && printf '%s' "$body" | grep -q '"write_auth_enabled"'; then
    echo "New build is live (commit reads 'unknown' — expected once, when the deploy"
    echo "changed deploy.sh itself; the next deploy stamps the real sha)."
    landed=1; break
  fi

  printf '  still %s, waiting...\n' "${live:-old build}"
  sleep 20
done

if [ -z "$landed" ]; then
  warn "The new build never appeared at $HEALTH."
  warn "Check /root/auto-deploy.log — a failed health check rolls back automatically,"
  warn "which looks identical to 'still waiting' from out here."
  die "NOT setting the API token: the frontend carrying the lock button is not live yet."
fi

# --- step 2: write auth ----------------------------------------------------
say "Step 2/3 — turn on write auth"
if "${SSH[@]}" "grep -qs '^API_ADMIN_TOKEN=.\\+' '$ENV_PATH'"; then
  echo "API_ADMIN_TOKEN is already set on the VPS. Nothing to do."
else
  echo "Right now anyone who can reach the host can change the base currency, edit the"
  echo "watchlist, and start syncs that spend the IBKR and Yahoo budgets."
  if ask "Generate a token and install it?"; then
    token="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    # Written over ssh, never echoed into a file in this repo (which is public).
    #
    # `up -d`, NOT `restart`: compose reads `env_file` when it *creates* a container, so
    # `restart` reuses the existing one with its original environment and the new token
    # is silently ignored — the script then reports success over a site whose write API
    # is still open. `up -d` notices the changed config and recreates. Cost us a
    # confused ten minutes on 2026-07-31.
    "${SSH[@]}" "printf '\nAPI_ADMIN_TOKEN=%s\n' '$token' >> '$ENV_PATH' && cd /root/IBKR_investment_tracker/backend && docker compose up -d" >/dev/null

    # Confirm rather than assume: /health reports the flag the middleware actually read.
    sleep 15
    if curl -fsS --max-time 10 "$HEALTH" | grep -q '"write_auth_enabled"[[:space:]]*:[[:space:]]*true'; then
      echo "Confirmed: /health reports write_auth_enabled=true."
    else
      warn "/health still reports write_auth_enabled=false — the container did not pick"
      warn "up the token. Check: ssh <host> \"cd /root/IBKR_investment_tracker/backend && docker compose up -d\""
    fi
    echo
    echo "  Token (shown once — paste it into the lock button beside Sync):"
    echo "      $token"
    echo
    echo "Verify: a write without the header should now return 401."
  else
    warn "Skipped. The footer will keep reporting 'write API unauthenticated'."
  fi
fi

# --- step 3: the guarded deploy script -------------------------------------
say "Step 3/3 — install the sync-slot-guarded auto-deploy script"
if ask "Install ops/auto-deploy.sh to /root/auto-deploy.sh?"; then
  scp -i "$SSH_KEY" ops/auto-deploy.sh "${REMOTE}:/tmp/auto-deploy.sh"
  "${SSH[@]}" 'install -m 755 /tmp/auto-deploy.sh /root/auto-deploy.sh && rm -f /tmp/auto-deploy.sh'
  echo "Installed. Watch /root/auto-deploy.log for a 'SKIP: within 10min' line near a slot."
else
  warn "Skipped. Deploys can still land inside a sync slot; the job store recovers a"
  warn "miss under 30 minutes, so this is belt-and-braces rather than the only defence."
fi

say "Done"
curl -fsS --max-time 10 "$HEALTH" || true
echo
