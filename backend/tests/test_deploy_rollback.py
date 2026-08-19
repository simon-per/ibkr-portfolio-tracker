"""
The unattended rollback must actually roll back.

`ops/auto-deploy.sh` deploys whatever lands on origin/main and, if the new build fails
its health check, is documented — in CLAUDE.md's Deployment section, as a plain
statement of fact — to "roll back to the previous commit". It could not, and never
could have:

    git reset --hard "$LOCAL"      # back to the last good commit
    bash deploy.sh                 # ← deploy.sh's first act was `git pull origin main`

The ancestor guard earlier in the script has already established that `$LOCAL` is an
*ancestor* of origin/main, so that pull fast-forwards straight back to the commit that
just failed — cleanly, with no conflict and no error. The rollback rebuilt the broken
build, health-checked it again, and logged `CRITICAL: rollback also failed`, which
misdescribes what happened: no rollback was ever attempted.

Rehearsed against a throwaway repository before the fix was written. With the checkout
parked on `good` and origin/main on `bad`, `git reset --hard good && git pull origin
main` lands on **bad** — that is the control. With `DEPLOY_NO_PULL=1` the same sequence
leaves deploy.sh building **good**.

These are source assertions rather than behavioural ones for the same reason
`test_deploy_guard_hours.py`'s are: the file under test is a shell script that runs as
root on another machine, on a path no test can reach. What a test *can* do is refuse to
let the two halves of the contract drift apart again.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_DEPLOY = REPO_ROOT / "ops" / "auto-deploy.sh"
DEPLOY = REPO_ROOT / "deploy.sh"

pytestmark = pytest.mark.skipif(
    not AUTO_DEPLOY.exists() or not DEPLOY.exists(),
    reason="ops/auto-deploy.sh or deploy.sh not present",
)


def _auto() -> str:
    return AUTO_DEPLOY.read_text(encoding="utf-8")


def _deploy() -> str:
    return DEPLOY.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """
    The script with comment lines removed.

    Not fussiness. The first version of `test_deploy_sh_can_be_told_not_to_pull` asked
    whether the string `DEPLOY_NO_PULL` appeared in deploy.sh, and it passed against a
    mutant that had replaced the guard with `if false; then` — because the paragraph
    *explaining* the guard still mentioned it by name. A test satisfied by its own
    documentation is this repository's most-repeated failure, and it is worth catching
    in a test written to catch that failure. Same technique as the `isUnpriced` family
    scan, which strips comments for the same reason.
    """
    keep = [l for l in source.splitlines() if not l.lstrip().startswith("#")]
    return chr(10).join(keep)


def test_deploy_sh_can_be_told_not_to_pull():
    """
    The half of the contract that lives in deploy.sh.

    Without this the rollback below is inert, and inert in the most misleading way: it
    succeeds, so the log says a rollback happened.
    """
    source = _code_only(_deploy())
    assert "${DEPLOY_NO_PULL" in source, (
        "deploy.sh must *read* DEPLOY_NO_PULL — a mention in a comment is not a guard. "
        "Without it ops/auto-deploy.sh's rollback fast-forwards straight back to the "
        "commit it is rolling back from"
    )
    pull = re.search(r"^\s*git pull origin main\s*$", source, re.M)
    assert pull, "expected a `git pull origin main` line to be guarded"
    # The pull must sit inside the guard, not merely after it.
    before = source[: pull.start()]
    guard = before.rfind("${DEPLOY_NO_PULL")
    assert guard != -1, "the `git pull` is not preceded by a DEPLOY_NO_PULL test"
    assert chr(10) + "fi" not in before[guard:], (
        "the DEPLOY_NO_PULL branch closes before the `git pull` — the pull is not guarded"
    )


def test_both_deploy_invocations_skip_the_pull():
    """
    Both, not just the rollback one.

    The forward deploy must skip it too, because auto-deploy now advances the checkout
    itself (below). If only the rollback passed the flag, the forward path would still
    work — and the next person to read it would reasonably conclude the flag is a
    rollback-only concern and drop it.
    """
    invocations = re.findall(r"^\s*(?:if\s+)?(.*?)bash \"\$REPO_DIR/deploy\.sh\"", _auto(), re.M)
    assert len(invocations) == 2, (
        f"expected exactly two deploy.sh invocations (forward + rollback), found "
        f"{len(invocations)}"
    )
    for prefix in invocations:
        assert "DEPLOY_NO_PULL=1" in prefix, (
            "every deploy.sh invocation from auto-deploy must set DEPLOY_NO_PULL=1; "
            f"this one does not: {prefix!r}"
        )


def test_auto_deploy_advances_the_checkout_itself():
    """
    The corollary of not pulling. With DEPLOY_NO_PULL set, nothing else moves HEAD, so
    the forward deploy would rebuild the commit already deployed and report success.
    `--ff-only` rather than a plain merge: the ancestor check has already proven a
    fast-forward is possible, so anything else is a state nobody has reasoned about.
    """
    assert re.search(r"git merge --ff-only \"\$REMOTE\"", _auto()), (
        "auto-deploy must fast-forward the checkout to $REMOTE itself now that "
        "deploy.sh no longer pulls"
    )


def test_a_rolled_back_commit_is_quarantined():
    """
    A rollback that works creates a hazard the broken one did not have.

    The old rollback left HEAD at the broken commit (that was the bug), so the next tick
    saw nothing to do. A rollback that genuinely returns to the last good commit leaves
    the checkout *behind* origin/main again — so ten minutes later the ancestor check
    passes and the same broken commit is rebuilt, with a --no-cache build and an outage,
    every ten minutes, indefinitely.

    So the failed sha is recorded and refused until origin/main moves past it.
    """
    source = _auto()
    assert 'echo "$REMOTE" > "$QUARANTINE"' in source, (
        "the rollback must record the sha it rolled back from, or the next tick "
        "redeploys it"
    )
    assert 'rm -f "$QUARANTINE"' in source, (
        "a successful deploy must clear the quarantine"
    )
    # The guard has to run before the deploy decision, not after it.
    guard = source.find('"$(cat "$QUARANTINE"')
    deploy = source.find('bash "$REPO_DIR/deploy.sh"')
    assert -1 < guard < deploy, "the quarantine check must precede the deploy"


def test_the_health_gate_asks_a_real_endpoint():
    """
    `/health` runs no query and touches no router — by design, so that a stale sync can
    never make it fail and turn an IBKR outage into a deploy rollback. The cost of that
    design is that it accepts a build whose every portfolio route 500s, which is most of
    what a bad deploy looks like. So the gate asks one real question too.
    """
    source = _auto()
    match = re.search(r'^SMOKE_URL="([^"]*)"', source, re.M)
    assert match, "SMOKE_URL not found in ops/auto-deploy.sh"
    assert "/api/" in match.group(1), (
        f"the smoke URL must exercise a real API route, got {match.group(1)!r}"
    )

    health_ok = re.search(r"health_ok\(\) \{(.*?)\n\}", source, re.S)
    assert health_ok, "health_ok() not found"
    body = health_ok.group(1)
    assert "$SMOKE_URL" in body and "$HEALTH_URL" in body, (
        "health_ok must check both liveness and one real endpoint"
    )


def test_a_failed_backup_stops_the_deploy():
    """
    The next thing a deploy does is run `alembic upgrade head` unattended against the
    only copy of data that cannot be re-fetched — the Flex window is 3 days and IBKR
    holds nothing before 2026 for this account. Continuing past a failed backup was
    defensible while the backup was an unverified `cp`; it is not now that a failure
    means the snapshot could not be taken or did not verify.
    """
    source = _auto()
    assert "ABORT: db snapshot failed" in source, (
        "a failed backup must abort the deploy rather than warn and continue"
    )
    assert "cp \"$REPO_DIR/backend/portfolio.db\"" not in source, (
        "the backup must not be a bare cp of a live WAL database — see ops/backup-db.sh"
    )


def _ci_verdict_source() -> str:
    """
    The decision table embedded in `ops/auto-deploy.sh`, lifted out so it can be run.

    Extracting rather than duplicating: a copy here would be a second implementation of
    the rule that decides whether a commit reaches production, which is the failure this
    repository documents more than any other.
    """
    match = re.search(
        r"read -r -d '' CI_VERDICT_PY <<'PYEOF'\n(.*?)\nPYEOF", _auto(), re.S
    )
    assert match, "CI_VERDICT_PY heredoc not found in ops/auto-deploy.sh"
    return match.group(1)


def _verdict(payload: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", _ci_verdict_source()],
        input=payload, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


@pytest.mark.parametrize(
    "label,payload,expected",
    [
        (
            "both suites green",
            '{"check_runs":[{"name":"backend","status":"completed","conclusion":"success"},'
            '{"name":"frontend","status":"completed","conclusion":"success"}]}',
            "success",
        ),
        (
            "one suite red — the case this gate exists for",
            '{"check_runs":[{"name":"backend","status":"completed","conclusion":"failure"},'
            '{"name":"frontend","status":"completed","conclusion":"success"}]}',
            "failed:backend",
        ),
        (
            "still running",
            '{"check_runs":[{"name":"backend","status":"in_progress","conclusion":null}]}',
            "pending",
        ),
        (
            "a cancelled job is not a pass",
            '{"check_runs":[{"name":"backend","status":"completed","conclusion":"cancelled"}]}',
            "failed:backend",
        ),
        (
            "timed out is not a pass either",
            '{"check_runs":[{"name":"backend","status":"completed","conclusion":"timed_out"}]}',
            "failed:backend",
        ),
        (
            "deliberately skipped jobs are not failures",
            '{"check_runs":[{"name":"b","status":"completed","conclusion":"skipped"},'
            '{"name":"f","status":"completed","conclusion":"neutral"}]}',
            "success",
        ),
        (
            "repo or commit unknown to GitHub",
            '{"message":"Not Found"}',
            "none",
        ),
        (
            "GitHub returned something that is not JSON",
            "<html>502 Bad Gateway</html>",
            "unavailable",
        ),
        (
            "empty body",
            "",
            "unavailable",
        ),
    ],
)
def test_the_ci_gate_decision_table(label, payload, expected):
    """
    Every branch of the gate, run rather than read.

    The three verdicts are deliberately asymmetric and the asymmetry is the design: a
    red commit must never deploy, a pending one is merely early, and `none` /
    `unavailable` must FAIL OPEN. A gate that blocks every deploy because GitHub is
    having an outage, or because a commit predates the workflow, is a worse failure than
    the one it prevents — so those two answers deploy, loudly, rather than refusing.
    """
    assert _verdict(payload) == expected, label


def test_a_red_commit_is_refused_and_a_pending_one_only_deferred():
    """
    The verdicts above only matter if the script acts on them differently. `pending`
    must not write anything or exit non-zero — it is a normal tick — while `failed:`
    must stop the deploy.
    """
    source = _code_only(_auto())
    case = re.search(r'case "\$VERDICT" in(.*?)esac', source, re.S)
    assert case, "the verdict case statement was not found"
    body = case.group(1)
    assert re.search(r"pending\)\s+log[^\n]*deferring", body), "pending must defer, with a log line"
    assert re.search(r"failed:\*\)\s+log[^\n]*REFUSE", body), "a red commit must be refused"
    assert "exit 0" in body, "refusing and deferring must both end the tick cleanly"
    # Fail-open is the property most likely to be "tidied" into fail-closed later, and
    # it has to be asserted about the `none)` arm SPECIFICALLY. The first version of
    # this check looked for "deploying ungated" anywhere in the case body and passed
    # against a mutant that made `none)` refuse — because the `*)` arm still carried the
    # phrase. Two arms, one search, and the test measured the wrong one.
    arms = dict(re.findall(r"^\s{4}([a-z:*]+\*?)\)(.*?)(?=^\s{4}[a-z:*]+\*?\)|\Z)",
                           body, re.S | re.M))
    assert "none" in arms, f"no `none)` arm found; arms were {sorted(arms)}"
    none_arm = arms["none"]
    assert "deploying ungated" in none_arm, (
        "an absent check set must fail OPEN and say so"
    )
    assert "REFUSE" not in none_arm, (
        "`none` must not refuse: a commit predating the workflow, or Actions being off, "
        "would then block every deploy forever"
    )
    # The young-commit defer is the one exit in this arm, and it is conditional.
    assert none_arm.count("exit 0") <= 1, (
        "the `none` arm must not unconditionally end the tick"
    )
