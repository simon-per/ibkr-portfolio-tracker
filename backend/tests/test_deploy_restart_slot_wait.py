"""
`deploy.sh` holds the restart while a sync slot is near or a sync is running.

`ops/auto-deploy.sh` checks the sync-slot guard once, before `git fetch`. `deploy.sh` then
builds for minutes while the old containers serve, and only then checkpoints the WAL and
runs `down`/`up`. A build that ran past the margin restarted the app on top of the sync —
and a sync killed mid-run re-runs from scratch, which at the 18:00/00:00 IBKR slots is a
second Flex generation (CLAUDE.md rule 2). So `deploy.sh` re-checks right before the
restart, in `wait_for_sync_slot_clear`.

Unlike the source assertions in `test_deploy_rollback.py`, these RUN the function under
bash. The clock, `sleep` and `curl` are replaced by shell functions, so a 20-minute wait
takes milliseconds and the "sync running" answer is scripted. Nothing in deploy.sh exists
for the test's sake: the fakes shadow the real commands by name.

The fake `date` answers in Berlin time only when called with `TZ=Europe/Berlin` and in UTC
(two hours off, as in summer) otherwise — so a re-check that read the host clock, which
is UTC on the VPS, fails here instead of guarding the wrong hours.

Offline: one bash subprocess per case, no network, no Docker.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.scheduler_service import ALL_SYNC_HOURS

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy.sh"
AUTO_DEPLOY = REPO_ROOT / "ops" / "auto-deploy.sh"


def _bash():
    """A real bash. On Windows that is Git Bash; `System32\\bash.exe` is WSL's launcher."""
    found = shutil.which("bash")
    if found and not found.lower().endswith("system32\\bash.exe"):
        return found
    if sys.platform == "win32":
        for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                          r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).exists():
                return candidate
    return None


BASH = _bash()

pytestmark = pytest.mark.skipif(
    BASH is None or not DEPLOY.exists() or not AUTO_DEPLOY.exists(),
    reason="needs bash, deploy.sh and ops/auto-deploy.sh",
)

# The fakes. `FAKE_S` is seconds since Berlin midnight on the deploy's first day; it may
# run past 86400, which is how the midnight wrap is exercised. `START_S` may list several
# starting times, each run in turn in the same shell (one bash spawn is ~1s on Windows).
_HARNESS = r"""
set -e
date() {
    local s=$(( FAKE_S % 86400 ))
    if [ "${TZ:-}" != "Europe/Berlin" ]; then
        s=$(( (s - 7200 + 86400) % 86400 ))
    fi
    case "$1" in
        '+%-H')   echo $(( s / 3600 )) ;;
        '+%-M')   echo $(( s % 3600 / 60 )) ;;
        '+%H:%M') printf '%02d:%02d\n' $(( s / 3600 )) $(( s % 3600 / 60 )) ;;
        *)        echo "unexpected date call: $*" >&2; return 1 ;;
    esac
}
sleep() { FAKE_S=$(( FAKE_S + $1 )); SLEPT=$(( SLEPT + $1 )); }
curl() {
    if [ -n "${RUNNING_UNTIL_S:-}" ] && [ "$FAKE_S" -lt "$RUNNING_UNTIL_S" ]; then
        printf '%s' "$STATUS_BUSY"
    else
        printf '%s' "$STATUS_IDLE"
    fi
}
eval "$(sed -n '/^wait_for_sync_slot_clear() {$/,/^}$/p' deploy.sh)"
declare -F wait_for_sync_slot_clear >/dev/null || { echo "NO FUNCTION"; exit 3; }
for START in $START_S; do
    FAKE_S=$START
    SLEPT=0
    wait_for_sync_slot_clear
    echo "RESULT slept=$SLEPT now=$FAKE_S"
done
"""

# What the running backend serves, rendered the way FastAPI renders it (compact JSON).
_STATUS_BUSY = json.dumps({"status": "running", "jobs": [], "sync_in_progress": True},
                          separators=(",", ":"))
_STATUS_IDLE = json.dumps({"status": "running", "jobs": [], "sync_in_progress": False},
                          separators=(",", ":"))


def _at(hh: int, mm: int) -> int:
    return hh * 3600 + mm * 60


def _run_many(starts, *, running_until_s=None, repo_dir=".", **env_extra):
    """[(seconds slept, fake clock at the restart)] per start, plus the whole output."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("DEPLOY_") and k not in ("TZ", "RUNNING_UNTIL_S")}
    env.update(START_S=" ".join(str(s) for s in starts), REPO_DIR=repo_dir,
               STATUS_BUSY=_STATUS_BUSY, STATUS_IDLE=_STATUS_IDLE)
    if running_until_s is not None:
        env["RUNNING_UNTIL_S"] = str(running_until_s)
    env.update({k: str(v) for k, v in env_extra.items()})
    # Bytes, not text: text-mode stdin on Windows would turn the harness into CRLF.
    proc = subprocess.run([BASH, "-s"], input=_HARNESS.encode(), cwd=REPO_ROOT, env=env,
                          capture_output=True, timeout=60)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, f"exit {proc.returncode}\n{out}\n{err}"
    results = [(int(a), int(b)) for a, b in re.findall(r"RESULT slept=(\d+) now=(\d+)", out)]
    assert len(results) == len(starts), (
        f"the function did not return under set -e:\n{out}\n{err}"
    )
    return results, out


def _run(start_s: int, **kwargs):
    [(slept, now)], out = _run_many([start_s], **kwargs)
    return slept, now, out


def test_outside_the_margin_it_restarts_at_once():
    slept, _, out = _run(_at(14, 0))
    assert slept == 0
    assert "Clear of every sync slot" in out


def test_before_a_slot_it_waits_until_the_margin_after_it_has_passed():
    """17:55 is five minutes before the 18:00 full sync. The guard's window is inclusive
    (`delta <= SLOT_MARGIN_MIN`), so 18:10 still holds and 18:11 is the first clear minute."""
    slept, now, out = _run(_at(17, 55))
    assert now == _at(18, 11)
    assert slept == 16 * 60
    assert "18:00 Europe/Berlin sync slot" in out
    assert "restarting" in out and "anyway" not in out


def test_the_margin_edges_match_the_guard():
    results, _ = _run_many([_at(17, 49), _at(17, 50), _at(18, 10), _at(18, 11)])
    slept = [s for s, _ in results]
    assert slept[0] == 0       # 11 minutes before: clear
    assert slept[1] > 0        # 10 minutes before: held
    assert slept[2] > 0        # 10 minutes after: held
    assert slept[3] == 0       # 11 minutes after: clear


def test_the_midnight_slot_wraps():
    """23:55 is five minutes from the 00:00 IBKR recovery slot, not 1435."""
    slept, now, out = _run(_at(23, 55))
    assert slept == 16 * 60
    assert now % 86400 == _at(0, 11)
    assert "0:00 Europe/Berlin sync slot" in out


def test_after_the_margin_it_waits_for_a_running_sync():
    """The 18:00 full sync is still running when the margin ends; the restart waits for it."""
    slept, now, out = _run(_at(18, 5), running_until_s=_at(18, 25))
    assert now == _at(18, 25)
    assert slept == 20 * 60
    assert "sync slot" in out and "sync in progress" in out


def test_a_sync_running_away_from_any_slot_is_waited_for_too():
    """A manual POST /api/sync/... at 14:00 holds the same gate."""
    slept, now, _ = _run(_at(14, 0), running_until_s=_at(14, 3))
    assert (slept, now) == (3 * 60, _at(14, 3))


def test_the_wait_is_bounded_and_then_restarts_anyway():
    """
    Proceeding, not aborting, is the chosen behaviour at the bound. Under auto-deploy a
    non-zero exit is read as a broken build (rollback, quarantine of a good commit), and a
    zero exit without restarting is logged SUCCESS with the checkout already advanced, so
    the commit would never deploy. The function must therefore return 0 at the bound.
    """
    forever = _at(48, 0)
    slept, _, out = _run(_at(14, 0), running_until_s=forever, DEPLOY_SLOT_WAIT_MAX_MIN=5)
    assert slept == 5 * 60
    assert "restarting anyway" in out


def test_the_default_bound_is_45_minutes_and_a_malformed_one_falls_back_to_it():
    """One run covers both (each poll forks, which is slow under Git Bash): a malformed
    value takes the same path as an unset one."""
    slept, _, _ = _run(_at(14, 0), running_until_s=_at(48, 0), DEPLOY_SLOT_WAIT_MAX_MIN="5min")
    assert slept == 45 * 60


def test_the_override_skips_the_check():
    slept, _, out = _run(_at(18, 0), running_until_s=_at(48, 0), DEPLOY_IGNORE_SLOTS=1)
    assert slept == 0
    assert "skipped (DEPLOY_IGNORE_SLOTS=1)" in out


def test_an_unreadable_guard_fails_open_and_says_so():
    """No ops/auto-deploy.sh beside it: warn and restart, never hang or abort a deploy."""
    slept, _, out = _run(_at(18, 0), repo_dir="./does-not-exist")
    assert slept == 0
    assert "WARNING: could not read the sync slots" in out


def test_the_hours_it_holds_for_are_exactly_the_scheduled_ones():
    """
    The behavioural half of `test_deploy_guard_hours.py`: whatever deploy.sh reads the
    hours from, the hours it actually holds the restart for are ALL_SYNC_HOURS.
    """
    # A 1-minute bound keeps each held hour to two polls; held or not is all this asks.
    results, _ = _run_many([_at(hour, 0) for hour in range(24)], DEPLOY_SLOT_WAIT_MAX_MIN=1)
    held = {hour for hour, (slept, _) in enumerate(results) if slept > 0}
    assert held == set(ALL_SYNC_HOURS), (
        f"deploy.sh holds the restart at {sorted(held)}:00 Berlin, but the scheduler runs "
        f"at {sorted(ALL_SYNC_HOURS)}:00. A missing hour restarts on top of a sync."
    )


def _code_only(source: str) -> str:
    return "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("#"))


def test_the_wait_runs_after_the_build_and_before_the_checkpoint_and_down():
    """The block is only worth anything in that position: after the slow part, before the
    first step that stops the old containers."""
    code = _code_only(DEPLOY.read_text(encoding="utf-8"))
    call = re.search(r"^wait_for_sync_slot_clear\s*$", code, re.M)
    assert call, "deploy.sh defines the wait but never calls it"
    build = code.find("docker compose build")
    checkpoint = code.find("wal_checkpoint")
    down = code.find("docker compose down")
    assert -1 not in (build, checkpoint, down)
    assert build < call.start() < checkpoint < down


def _sync_in_progress_pattern() -> str:
    """The regex deploy.sh greps the status body with, in Python syntax."""
    code = _code_only(DEPLOY.read_text(encoding="utf-8"))
    match = re.search(r"grep -Eq '([^']*sync_in_progress[^']*)'", code)
    assert match, "deploy.sh no longer greps /api/scheduler/status for sync_in_progress"
    return match.group(1).replace("[[:space:]]", r"\s")


@pytest.mark.parametrize("armed", [False, True])
def test_the_status_endpoint_publishes_what_deploy_sh_reads(monkeypatch, armed):
    """
    Two ends of one contract across a language boundary: the router publishes the field,
    the shell script greps for it. Rename either and deploy.sh silently stops waiting for
    running syncs (an unknown fails open), so the pairing is pinned here, through the same
    JSON rendering FastAPI serves.
    """
    from fastapi.responses import JSONResponse

    from app.routers import scheduler as scheduler_router
    from app.single_flight import SYNC_PIPELINE, single_flight

    fake = SimpleNamespace(
        scheduler=object() if armed else None,
        jobs_in_group=lambda group: [],
        last_sync_result={"type": "market_data_only", "status": "success"},
    )
    monkeypatch.setattr(scheduler_router, "get_scheduler", lambda: fake)
    pattern = re.compile(_sync_in_progress_pattern())

    def body() -> str:
        result = asyncio.run(scheduler_router.get_scheduler_status(db=None))
        return JSONResponse(result).body.decode()

    assert not pattern.search(body()), "an idle pipeline must not read as busy"
    with single_flight(SYNC_PIPELINE):
        assert pattern.search(body()), "a held pipeline gate must read as a running sync"
    assert not pattern.search(body())
