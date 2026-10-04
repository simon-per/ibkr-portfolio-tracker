"""
The deploy guard's sync hours must match the scheduler's.

`ops/auto-deploy.sh` skips a deploy that would land within `SLOT_MARGIN_MIN` of a
Berlin sync slot, because APScheduler runs in-process and a rebuild overlapping a slot
loses that run. To do that it carries its own copy of the slot hours — a shell variable
that nothing connects to the `CronTrigger`s it is describing.

It drifted, and the drift was the worst possible shape. The guard was written on
2026-07-31, the same day the schedule moved 13:00/20:00 → 00:00/06:00, and it kept the
*old* pair. So from then until 2026-08-03 it guarded two slots that no longer ran and
left the two overnight IBKR slots guarded by nothing at all — protecting exactly the
half of the day that needed no protection.

Nothing could have caught that at runtime: a shell script on the VPS is invisible to
the application. So this test reads the file, the same way
`test_scheduler_jobstore_path.py` reads docker-compose.yml for the same reason.

The scheduler side used to be read by regex too — `CronTrigger\\(hour=(\\d+)` over the
source — and that had a silent hole in the shape this whole file is about. It saw only
*literal* hours, so registering slots from a constant or a loop, or at a half-hour,
would have contributed nothing to the comparison and every new slot would have looked
correctly guarded while being invisible to the guard. It now reads `ALL_SYNC_HOURS`,
which the scheduler builds its triggers from, and
`test_the_registered_slots_are_exactly_the_declared_ones` checks the triggers really
come from that constant. Importing it starts nothing.

Offline: filesystem plus one import, no scheduler started, no network.
"""

import os
import re
from pathlib import Path

import pytest

from app.services.scheduler_service import ALL_SYNC_HOURS

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_DEPLOY = REPO_ROOT / "ops" / "auto-deploy.sh"
FINISH_SH = REPO_ROOT / "ops" / "finish-deploy.sh"
FINISH_PS1 = REPO_ROOT / "ops" / "finish-deploy.ps1"


def _guard_hours() -> set[int]:
    """The hours `ops/auto-deploy.sh` believes it has to stay clear of."""
    match = re.search(r'^SYNC_HOURS="([^"]*)"', AUTO_DEPLOY.read_text(encoding="utf-8"), re.M)
    assert match, "SYNC_HOURS not found in ops/auto-deploy.sh"
    return {int(h) for h in match.group(1).split()}


def _finish_deploy_hours(path: Path, pattern: str) -> set[int]:
    """
    The hours a finish-deploy script warns about, from its minutes-past-midnight list.

    These two carry the same hours a third and fourth time, and both were stale for
    four days — written with 13:00/20:00 on the very day those were retired in favour
    of 00:00/06:00. Nothing read them, so the interactive guard a human runs before
    pushing was waving them straight into the two live overnight slots.
    """
    match = re.search(pattern, path.read_text(encoding="utf-8"))
    assert match, f"slot minute list not found in {path.name}"
    minutes = [int(m) for m in re.findall(r"\d+", match.group(1))]
    assert minutes, f"empty slot list in {path.name}"
    for minute in minutes:
        assert minute % 60 == 0, (
            f"{path.name} guards {minute} minutes past midnight, which is not on the "
            f"hour — the scheduler only registers whole hours, so this is drift."
        )
    return {m // 60 for m in minutes}


def _scheduled_hours() -> set[int]:
    """The hours the scheduler declares it runs at."""
    assert ALL_SYNC_HOURS, "ALL_SYNC_HOURS is empty"
    return set(ALL_SYNC_HOURS)


@pytest.mark.skipif(not AUTO_DEPLOY.exists(), reason="ops/auto-deploy.sh not present")
def test_deploy_guard_covers_exactly_the_scheduled_slots():
    guard, scheduled = _guard_hours(), _scheduled_hours()

    unguarded = scheduled - guard
    stale = guard - scheduled

    assert not unguarded, (
        f"ops/auto-deploy.sh does not guard {sorted(unguarded)}:00 Europe/Berlin, but the "
        f"scheduler runs a sync then. A deploy landing there loses that run — the "
        f"persistent job store recovers a misfire only within 30 minutes, and a "
        f"--no-cache rebuild can exceed that. Update SYNC_HOURS."
    )
    assert not stale, (
        f"ops/auto-deploy.sh guards {sorted(stale)}:00 Europe/Berlin, but no sync runs "
        f"then. Harmless, but it defers deploys for nothing and hides real drift. "
        f"Update SYNC_HOURS."
    )


@pytest.mark.parametrize("path,pattern", [
    (FINISH_SH, r"for slot in ([\d ]+); do"),
    (FINISH_PS1, r"\$near = @\(([\d, ]+)\) \| Where-Object"),
])
def test_both_finish_deploy_twins_warn_about_exactly_the_scheduled_slots(path, pattern):
    """
    The interactive guard a human runs before pushing, in its two equivalent forms.

    CLAUDE.md already says to keep the twins in step with each other; what it could not
    say is that they must also stay in step with the *scheduler*, since nothing checked
    it. Both drifted the same day `ops/auto-deploy.sh` did and stayed wrong for four
    days, warning about 13:00 and 20:00 while the live 00:00 and 06:00 slots were
    unmentioned — so the script would cheerfully report "clear of every sync slot" at
    05:58 Berlin.
    """
    if not path.exists():
        pytest.skip(f"{path.name} not present")
    warned = _finish_deploy_hours(path, pattern)
    scheduled = _scheduled_hours()

    assert warned == scheduled, (
        f"{path.name} warns about {sorted(warned)}:00 Europe/Berlin but the scheduler "
        f"runs at {sorted(scheduled)}:00. Missing slots let a push land on a sync; "
        f"extra ones train the operator to click through the warning."
    )


DEPLOY_SH = REPO_ROOT / "deploy.sh"

# Every script that knows when the syncs run, and how it is held to ALL_SYNC_HOURS. A new
# one must be added here, with a test, or `test_every_script_that_tells_berlin_time_is_pinned`
# fails — the family test, so the next copy is caught the way these four were not.
_PINNED_SLOT_SCRIPTS = {
    "ops/auto-deploy.sh": "declares SYNC_HOURS (test_deploy_guard_covers_exactly_the_scheduled_slots)",
    "ops/finish-deploy.sh": "its own minute list (test_both_finish_deploy_twins_...)",
    "ops/finish-deploy.ps1": "its own minute list (test_both_finish_deploy_twins_...)",
    "deploy.sh": "reads auto-deploy.sh's (test_deploy_sh_reads_the_slots_rather_than_copying_them, "
                 "and test_deploy_restart_slot_wait.py runs it)",
}
_SKIP_DIRS = {".git", ".claude", "node_modules", "venv", ".venv", "__pycache__", "dist",
              "dist.next", "dist.old"}


def _code_lines(path: Path) -> str:
    """Comment lines dropped (`#` in both bash and PowerShell): a script that only
    *mentions* Berlin in prose computes nothing from it."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _scripts():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.endswith((".sh", ".ps1", ".bash")):
                yield Path(root) / name


def test_every_script_that_tells_berlin_time_is_pinned():
    """
    A shell script has no way to know the sync hours except to carry them or read them,
    and the only reason to ask for Europe/Berlin time is to compare it with them. So any
    script that does is a member of this family, and must be one the tests above hold to
    the scheduler. `deploy.sh` joined it on 2026-10-04, when it gained the re-check before
    the restart — and did so by reading auto-deploy.sh's list rather than adding a copy.
    """
    found = set()
    for path in _scripts():
        code = _code_lines(path)
        if re.search(r"Europe/Berlin|SYNC_HOURS|SLOT_MARGIN_MIN|in_sync_window", code):
            found.add(path.relative_to(REPO_ROOT).as_posix())
    unpinned = found - set(_PINNED_SLOT_SCRIPTS)
    assert not unpinned, (
        f"{sorted(unpinned)} reason about Berlin sync slots but nothing holds them to "
        f"ALL_SYNC_HOURS. Read the hours from ops/auto-deploy.sh as deploy.sh does, and "
        f"add the script to _PINNED_SLOT_SCRIPTS with a test."
    )
    gone = {p for p in _PINNED_SLOT_SCRIPTS if not (REPO_ROOT / p).exists()}
    assert found >= set(_PINNED_SLOT_SCRIPTS) - gone, (
        f"{sorted(set(_PINNED_SLOT_SCRIPTS) - gone - found)} no longer look like slot "
        f"guards — the pattern above has gone stale, so it may be missing new ones too."
    )


@pytest.mark.skipif(not DEPLOY_SH.exists(), reason="deploy.sh not present")
def test_deploy_sh_reads_the_slots_rather_than_copying_them():
    """
    deploy.sh's re-check before the restart takes SYNC_HOURS, SLOT_MARGIN_MIN and
    in_sync_window() from the checkout's ops/auto-deploy.sh. A literal list of its own
    would be the fifth copy of the hours, and the first one written after the lesson.
    """
    code = _code_lines(DEPLOY_SH)
    assert not re.search(r"SYNC_HOURS=\"?[\d ]", code), "deploy.sh carries its own SYNC_HOURS"
    assert not re.search(r"SLOT_MARGIN_MIN=\d", code), "deploy.sh carries its own margin"
    assert not re.search(r"for \w+ in [\d ]{3,}", code), "deploy.sh loops over literal hours"
    assert not re.search(r"^in_sync_window\(\)", code, re.M), (
        "deploy.sh defines its own in_sync_window instead of reading auto-deploy.sh's"
    )
    assert "ops/auto-deploy.sh" in code and "in_sync_window" in code


@pytest.mark.skipif(not AUTO_DEPLOY.exists(), reason="ops/auto-deploy.sh not present")
def test_the_margin_is_wide_enough_for_a_rebuild():
    """
    `deploy.sh` does `docker compose down`, `build --no-cache` and `npm ci`. Anything
    under a few minutes either side of a slot would let a rebuild straddle it, which is
    the whole failure this guard exists to prevent.
    """
    match = re.search(r"^SLOT_MARGIN_MIN=(\d+)", AUTO_DEPLOY.read_text(encoding="utf-8"), re.M)
    assert match, "SLOT_MARGIN_MIN not found in ops/auto-deploy.sh"
    assert int(match.group(1)) >= 5
