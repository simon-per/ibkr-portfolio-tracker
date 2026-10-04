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

The exception is the database restore at the end of this file, which is rehearsed for
real: both ops scripts run under bash against a throwaway repository, with only docker,
curl, flock, sleep and the clock stubbed.
"""

import os
import re
import shutil
import sqlite3
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


# --- The database restore, rehearsed --------------------------------------------------
#
# Everything above reads the script. What follows RUNS it: the real ops/auto-deploy.sh
# and the real ops/backup-db.sh, under bash, against a throwaway git repository with an
# `origin`, and a fake deploy.sh that does what the container does on start — run
# "alembic upgrade head" and refuse to start on a revision its tree does not know. Only
# the things that would leave the machine are stubbed: docker, curl, flock, sleep, and
# the clock the sync-slot guard reads.
#
# The control, run by hand before the fix (2026-10-04): the pre-fix auto-deploy against
# `test_a_migrating_failure_is_restored_and_the_rollback_comes_up` ends in
# `CRITICAL: rollback also failed`, which is the production failure STATUS.md described.


def _find_bash() -> str | None:
    """Git Bash on Windows, never System32's bash.exe (that one is WSL)."""
    if os.name != "nt":
        return shutil.which("bash")
    roots = []
    git = shutil.which("git")
    if git:
        roots.append(Path(git).resolve().parents[1])
    roots.append(Path(r"C:\Program Files\Git"))
    for root in roots:
        candidate = root / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    return None


BASH = _find_bash()
BACKUP_DB = REPO_ROOT / "ops" / "backup-db.sh"

rehearsal = pytest.mark.skipif(
    BASH is None or shutil.which("git") is None or not BACKUP_DB.exists(),
    reason="the rollback rehearsal needs bash, git and ops/backup-db.sh",
)

_STUBS = {
    # The guard reads `TZ=Europe/Berlin date '+%-H'` / `'+%-M'`; 03:30 is no sync slot.
    "date": """#!/bin/bash
case "$1" in '+%-H') echo 3; exit 0 ;; '+%-M') echo 30; exit 0 ;; esac
exec /usr/bin/date "$@"
""",
    "flock": "#!/bin/bash\nexit 0\n",
    # health_ok sleeps 5s twelve times on a failing build.
    "sleep": "#!/bin/bash\nexit 0\n",
    # The CI gate gets a green check run; the health gate reads the fake app's state.
    "curl": """#!/bin/bash
for a in "$@"; do
    case "$a" in *check-runs*)
        echo '{"check_runs":[{"name":"backend","status":"completed","conclusion":"success"}]}'
        exit 0 ;;
    esac
done
if [ -f "$STATE/healthy" ]; then printf 200; else printf 503; fi
""",
    "docker": """#!/bin/bash
echo "docker $*" >> "$STATE/docker.log"
exit 0
""",
}

# The container's start command, reduced to the part that matters here.
_FAKE_ALEMBIC = '''
import os, sqlite3, sys
db, versions = sys.argv[1], sys.argv[2]
revs = sorted(f.split("_")[0] for f in os.listdir(versions) if f.endswith(".py"))
con = sqlite3.connect(db)
row = con.execute("SELECT version_num FROM alembic_version").fetchone()
current = row[0] if row else None
if current is not None and current not in revs:
    sys.exit(f"Can't locate revision identified by {current!r}")
pending = revs[revs.index(current) + 1:] if current else revs
for rev in pending:
    con.execute(f"ALTER TABLE trades ADD COLUMN col_{rev} TEXT")
    con.execute("UPDATE alembic_version SET version_num = ?", (rev,))
    con.execute("INSERT INTO trades (note) VALUES (?)", (f"written after migrating to {rev}",))
con.commit()
con.close()
'''

# deploy.sh as the container behaves: a build that fails stops before anything touches
# the database; otherwise the container starts, migrates, and then either serves or —
# on the marked commit — does not. A container whose alembic refuses never serves.
_FAKE_DEPLOY = """#!/bin/bash
cd "$REPO_DIR"
echo "deploy $(git rev-parse --short HEAD)" >> "$STATE/deploys.log"
rm -f "$STATE/healthy"
[ -f BUILD_FAILS ] && exit 1
if [ -n "${SABOTAGE_SNAPSHOT:-}" ] && [ -f BROKEN ]; then
    for f in "$BACKUP_ROOT"/*/portfolio.db.autodeploy-*; do
        case "$SABOTAGE_SNAPSHOT" in
            missing)    rm -f "$f" ;;
            not-sqlite) printf 'this is not a database' > "$f" ;;
            malformed)  { printf 'SQLite format 3\\000'; head -c 8192 /dev/urandom; } > "$f" ;;
        esac
    done
fi
python3 "$STATE/fake_alembic.py" "$DB" backend/alembic/versions || exit 0
if [ -f BROKEN ]; then
    # What a live container leaves beside a FILE-mounted database once it is moved.
    printf 'stale wal' > "$DB-wal"
    printf 'stale shm' > "$DB-shm"
    exit 0
fi
touch "$STATE/healthy"
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    if executable:
        path.chmod(0o755)


def _revision(db: Path) -> str:
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        con.close()


def _notes(db: Path) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT note FROM trades ORDER BY id")]
    finally:
        con.close()


class _Rehearsal:
    """A production-shaped checkout one bad commit behind origin/main."""

    def __init__(self, tmp: Path, *, migration: bool, bad_marker: str):
        self.tmp = tmp
        self.state = tmp / "state"
        self.stubs = tmp / "bin"
        self.repo = tmp / "repo"
        self.backups = tmp / "backups"
        self.log = tmp / "auto-deploy.log"
        self.quarantine = tmp / "quarantine"
        self.state.mkdir()
        self.backups.mkdir()
        for name, body in _STUBS.items():
            _write(self.stubs / name, body, executable=True)
        python = Path(sys.executable).as_posix()
        _write(self.stubs / "python3", f'#!/bin/bash\nexec "{python}" "$@"\n', executable=True)
        _write(self.state / "fake_alembic.py", _FAKE_ALEMBIC)

        origin = tmp / "origin.git"
        _git(tmp, "init", "--bare", "-b", "main", origin.as_posix())
        _git(tmp, "init", "-b", "main", self.repo.as_posix())
        for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                           ("core.autocrlf", "false")):
            _git(self.repo, "config", key, value)
        _git(self.repo, "remote", "add", "origin", origin.as_posix())
        _write(self.repo / "deploy.sh", _FAKE_DEPLOY, executable=True)
        _write(self.repo / "backend" / "alembic" / "versions" / "a1_base.py", "# a1\n")
        _write(self.repo / ".gitignore", "backend/portfolio.db*\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "good")
        self.good = _git(self.repo, "rev-parse", "HEAD")

        if migration:
            _write(self.repo / "backend" / "alembic" / "versions" / "b2_new.py", "# b2\n")
        _write(self.repo / bad_marker, "x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "bad")
        self.bad = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "push", "-q", "origin", "main")
        _git(self.repo, "reset", "-q", "--hard", self.good)

        # The live database, at the good tree's revision, as production has it.
        self.db = self.repo / "backend" / "portfolio.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        con.execute("INSERT INTO alembic_version VALUES ('a1')")
        con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, note TEXT)")
        con.execute("INSERT INTO trades (note) VALUES ('before the deploy')")
        con.commit()
        con.close()

    def run(self, backup_script: Path = BACKUP_DB, **extra: str) -> str:
        if backup_script == BACKUP_DB:
            # auto-deploy uses BACKUP_SCRIPT only when it is executable (`[ -x ]`), as the
            # installed /root copy is, and git stores ops/backup-db.sh as 100644. Windows'
            # bash calls any *.sh executable, so passing the repo file worked there and on
            # Linux CI fell through to a REPO_DIR/ops/backup-db.sh the fake repo lacks.
            backup_script = self.tmp / "installed-backup-db.sh"
            _write(backup_script, BACKUP_DB.read_text(encoding="utf-8"), executable=True)
        env = {
            **os.environ,
            "STUBS": self.stubs.as_posix(),
            "AUTO": AUTO_DEPLOY.as_posix(),
            "STATE": self.state.as_posix(),
            "REPO_DIR": self.repo.as_posix(),
            "DB": self.db.as_posix(),
            "LOG": self.log.as_posix(),
            "BACKUP_LOG": (self.tmp / "backup-db.log").as_posix(),
            "BACKUP_ROOT": self.backups.as_posix(),
            "BACKUP_SCRIPT": backup_script.as_posix(),
            "LOCKFILE": (self.tmp / "lock").as_posix(),
            "QUARANTINE": self.quarantine.as_posix(),
            **extra,
        }
        # Prepend the stubs from INSIDE bash: Git Bash's launcher puts /usr/bin first,
        # which would shadow them, and PATH there is colon-separated POSIX paths.
        boot = ('s="$STUBS"; command -v cygpath >/dev/null && s=$(cygpath -u "$s"); '
                'export PATH="$s:$PATH"; exec bash "$AUTO"')
        subprocess.run([BASH, "-c", boot], env=env, check=False, timeout=180,
                       capture_output=True)
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def snapshots(self) -> list[Path]:
        return sorted(self.backups.glob("*/portfolio.db.autodeploy-*"))

    def aside(self) -> list[Path]:
        return sorted((self.backups / "failed-deploys").glob("failed-*.db"))

    def strays(self) -> list[Path]:
        """Anything the restore left beside the live database or in the backup root."""
        beside = [p for p in self.db.parent.iterdir()
                  if p.name.startswith("portfolio.db") and p.name != "portfolio.db"]
        hidden = [p for p in self.backups.iterdir() if p.name.startswith(".")]
        return beside + hidden


@rehearsal
def test_backup_db_reports_the_source_and_the_snapshot_last(tmp_path):
    """The two-line stdout contract the rollback parses, run rather than read."""
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    stubs = tmp_path / "bin"
    _write(stubs / "docker", "#!/bin/bash\nexit 0\n", executable=True)
    python = Path(sys.executable).as_posix()
    _write(stubs / "python3", f'#!/bin/bash\nexec "{python}" "$@"\n', executable=True)
    env = {**os.environ, "STUBS": stubs.as_posix(), "SCRIPT": BACKUP_DB.as_posix(),
           "DB": db.as_posix(), "BACKUP_ROOT": (tmp_path / "b").as_posix(),
           "BACKUP_LOG": (tmp_path / "log").as_posix()}
    boot = ('s="$STUBS"; command -v cygpath >/dev/null && s=$(cygpath -u "$s"); '
            'export PATH="$s:$PATH"; exec bash "$SCRIPT" autodeploy')
    out = subprocess.run([BASH, "-c", boot], env=env, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert len(lines) == 2, f"stdout must be exactly the two paths, got {lines!r}"
    assert lines[0] == db.as_posix()
    assert Path(lines[1]).is_file() and ".autodeploy-" in lines[1]


@rehearsal
def test_a_migrating_failure_is_restored_and_the_rollback_comes_up(tmp_path):
    """
    The case STATUS.md described: the bad commit carries a migration, its container
    ran it and then failed health. Without the restore the reverted tree's alembic
    cannot find revision b2 and the rollback ends in CRITICAL.
    """
    r = _Rehearsal(tmp_path, migration=True, bad_marker="BROKEN")
    log = r.run()

    assert "ROLLED BACK to" in log and "CRITICAL" not in log, log
    assert "RESTORED " in log, log
    # The live database is the snapshot again: old revision, none of the failed
    # deploy's writes.
    assert _revision(r.db) == "a1"
    assert _notes(r.db) == ["before the deploy"]
    # The failed state is kept, never deleted — with the write made after the snapshot.
    aside = r.aside()
    assert len(aside) == 1, aside
    # The sidecars went with it rather than staying to be replayed onto the snapshot —
    # and went untouched. Checked BEFORE anything opens the aside file: SQLite acts on a
    # -wal beside any database it opens, which is why auto-deploy probes a copy.
    assert not Path(f"{r.db}-wal").exists() and not Path(f"{r.db}-shm").exists()
    assert Path(f"{aside[0]}-wal").read_text() == "stale wal"
    assert Path(f"{aside[0]}-shm").read_text() == "stale shm"
    assert _revision(aside[0]) == "b2"
    assert "written after migrating to b2" in _notes(aside[0])
    # Nothing left in the checkout (a public repo, and the docker build context) or in
    # the backup root: no probe copy, no half-placed restore.
    assert r.strays() == [], r.strays()
    # And the backup's own pruning can never take the failed state for a snapshot.
    assert not aside[0].name.startswith("portfolio.db.")
    # Containers were down before the file was replaced, and the rollback redeployed.
    assert "compose down" in (r.state / "docker.log").read_text()
    assert (r.state / "deploys.log").read_text().count("deploy ") == 2
    assert r.quarantine.read_text().strip() == r.bad
    assert _git(r.repo, "rev-parse", "HEAD") == r.good


def _can_symlink(tmp: Path) -> bool:
    try:
        (tmp / "link-probe").symlink_to(tmp)
        return True
    except (OSError, NotImplementedError):
        return False


@rehearsal
def test_a_restore_follows_the_legacy_link_into_the_data_directory(tmp_path):
    """
    Since 2026-10-04 ops/db-layout.sh leaves backend/portfolio.db as a symlink to
    backend/data/portfolio.db. A backup that reported the link (the old /root copy, or a
    snapshot taken before the deploy that performed the move) must restore INTO the file
    it points at: replacing the link with a regular file leaves two databases, which the
    next preflight refuses, and restores into the copy the new layout never reads.
    """
    if not _can_symlink(tmp_path):
        pytest.skip("this machine cannot create symlinks (runs on CI)")
    r = _Rehearsal(tmp_path, migration=True, bad_marker="BROKEN")
    data = r.db.parent / "data"
    data.mkdir()
    real = data / "portfolio.db"
    r.db.rename(real)
    r.db.symlink_to(Path("data") / "portfolio.db")

    log = r.run()

    assert "ROLLED BACK to" in log and "CRITICAL" not in log, log
    assert "restoring into that file" in log, log
    assert r.db.is_symlink(), "the legacy path must stay a link"
    assert real.is_file() and not real.is_symlink()
    assert _revision(real) == "a1"
    assert _notes(real) == ["before the deploy"]
    aside = r.aside()
    assert len(aside) == 1 and _revision(aside[0]) == "b2", aside


@rehearsal
def test_a_failure_without_a_migration_is_not_restored(tmp_path):
    """No migration in the range: the plain redeploy suffices, and a restore would only
    throw away the writes made since the snapshot. Nor is the app stopped early."""
    r = _Rehearsal(tmp_path, migration=False, bad_marker="BROKEN")
    log = r.run()

    assert "ROLLED BACK to" in log and "CRITICAL" not in log, log
    assert "no migration in" in log, log
    assert "RESTORED " not in log
    assert r.aside() == []
    docker = r.state / "docker.log"
    assert not docker.exists() or "compose down" not in docker.read_text(), (
        "the rollback must not `down` the app early when no restore can be needed"
    )


@rehearsal
def test_a_migration_that_never_ran_is_not_restored(tmp_path):
    """A migration in the range is necessary, not sufficient: here the build failed
    before any container started, so the revision is unchanged and nothing is lost by
    keeping the live file."""
    r = _Rehearsal(tmp_path, migration=True, bad_marker="BUILD_FAILS")
    log = r.run()

    assert "ROLLED BACK to" in log and "CRITICAL" not in log, log
    assert "did not migrate; no restore" in log, log
    assert "RESTORED " not in log
    assert r.aside() == []
    assert r.strays() == [], "the probe's copy must be cleaned up"
    assert _notes(r.db) == ["before the deploy"]


@rehearsal
def test_an_old_backup_script_that_prints_nothing_means_no_restore(tmp_path):
    """/root/backup-db.sh predates the stdout contract until it is refreshed. The
    rollback must not guess a snapshot or a target; it says so, loudly, twice, and
    behaves exactly as it did before the fix — which for a migrating failure is
    CRITICAL, the state the NOTE tells a human how to fix."""
    old = tmp_path / "old-backup-db.sh"
    _write(old, f'#!/bin/bash\nbash "{BACKUP_DB.as_posix()}" "$@" >/dev/null\n',
           executable=True)
    r = _Rehearsal(tmp_path, migration=True, bad_marker="BROKEN")
    log = r.run(backup_script=old)

    assert "did not report its snapshot" in log, log
    assert "NO usable snapshot" in log, log
    assert "RESTORED " not in log
    assert r.aside() == []
    assert len(r.snapshots()) == 1, "the backup itself still ran"
    assert "CRITICAL" in log
    assert _revision(r.db) == "b2", "the live database must be left as it was"


@rehearsal
@pytest.mark.parametrize("sabotage", ["missing", "not-sqlite", "malformed"])
def test_an_unusable_snapshot_is_never_restored(tmp_path, sabotage):
    """A snapshot that vanished or no longer verifies between the backup and the
    rollback must not be written over the live database."""
    r = _Rehearsal(tmp_path, migration=True, bad_marker="BROKEN")
    log = r.run(SABOTAGE_SNAPSHOT=sabotage)

    assert "missing, empty or failed its integrity check" in log, log
    assert "RESTORED " not in log
    assert r.aside() == []
    assert _revision(r.db) == "b2", "the live database must be left as it was"
    assert "CRITICAL" in log
