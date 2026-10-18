"""
`deploy.sh` reuses the layer cache, prunes it, and skips `npm ci` when nothing changed
(issue #38).

Until 2026-10-18 every deploy ran `docker compose build --no-cache`, which re-resolved
floating dependencies against PyPI (shipping what PyPI served that minute, not what CI
tested) and recorded a build-cache entry per step that nothing pruned: 11.92 GB on the
VPS. The image now installs the hash-pinned requirements.lock, so the cache is safe to
reuse; `--pull` keeps the base image's patches arriving; a non-fatal prune after `up`
bounds the cache to about a week. And `npm ci`, which wipes and reinstalls node_modules,
runs only when `install_frontend_deps`'s stamp (Node version + lockfile hash) moved.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from bash_finder import find_bash

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy.sh"
BASH = find_bash()


def _code_only(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


CODE = _code_only(DEPLOY.read_text(encoding="utf-8"))


def test_the_build_pulls_and_reuses_the_cache():
    builds = [
        line.strip()
        for line in CODE.splitlines()
        if re.match(r"\s*docker compose build\b", line)
    ]
    assert builds == ["docker compose build --pull"], builds


def test_the_prune_runs_after_up_and_cannot_fail_the_deploy():
    """After `up`, because the new containers must already be serving; non-fatal, because
    under auto-deploy a non-zero exit reads as a broken build and triggers a rollback."""
    up = CODE.find("docker compose up -d")
    prune = re.search(
        r"^docker builder prune -f --filter until=168h \|\| .+$", CODE, re.M
    )
    assert up != -1 and prune, "deploy.sh must prune the build cache after `up -d`"
    assert up < prune.start()


def test_npm_ci_runs_only_through_the_stamp():
    calls = [
        line.strip() for line in CODE.splitlines() if re.match(r"\s*npm ci\b", line)
    ]
    assert calls == ["npm ci"], f"a bare npm ci outside install_frontend_deps: {calls}"
    body = re.search(r"^install_frontend_deps\(\) \{$.*?^\}$", CODE, re.M | re.S)
    assert body and "npm ci" in body.group(0)
    assert re.search(r"^install_frontend_deps$", CODE, re.M), "defined but never called"


# `node` and `npm` are functions, so nothing real is installed or run. The fake `npm ci`
# behaves like the real one where it matters: it deletes node_modules and recreates it,
# and it can fail.
_HARNESS = r"""
node() { echo "$NODE_VERSION"; }
npm() {
    [ "$1" = ci ] || return 2
    echo "npm ci" >> "$WORK/npm.log"
    rm -rf node_modules
    [ -f "$WORK/npm_fails" ] && return 1
    mkdir node_modules
}
eval "$(sed -n '/^install_frontend_deps() {$/,/^}$/p' "$DEPLOY")"
declare -F install_frontend_deps >/dev/null || { echo "NO FUNCTION"; exit 3; }
cd "$WORK"
step() {
    local before after
    before=$(wc -l < npm.log)
    if ( set -e; install_frontend_deps >/dev/null ); then status=ok; else status=failed; fi
    after=$(wc -l < npm.log)
    echo "$1 $([ "$after" -gt "$before" ] && echo ran || echo skipped) $status"
}
: > npm.log
echo '{"lockfileVersion": 3}' > package-lock.json
NODE_VERSION=v22.1.0
step fresh-host
step unchanged
echo '{"lockfileVersion": 3, "x": 1}' > package-lock.json
step lockfile-changed
step unchanged-again
NODE_VERSION=v22.2.0
step node-upgraded
DEPLOY_FORCE_NPM_CI=1 step forced
DEPLOY_FORCE_NPM_CI=0
rm -rf node_modules
step node-modules-deleted
echo '{"lockfileVersion": 3, "x": 2}' > package-lock.json
touch npm_fails
step install-fails
rm npm_fails
step after-failure
"""


@pytest.mark.deploy_scripts
@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_npm_ci_is_skipped_only_while_node_modules_matches_the_lockfile(tmp_path):
    env = {**os.environ, "WORK": tmp_path.as_posix(), "DEPLOY": DEPLOY.as_posix()}
    env.pop("DEPLOY_FORCE_NPM_CI", None)
    # Bytes, not text: text-mode stdin on Windows would turn the harness into CRLF.
    proc = subprocess.run(
        [BASH, "-s"], input=_HARNESS.encode(), env=env, capture_output=True, timeout=60
    )
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, out + proc.stderr.decode("utf-8", "replace")
    assert out.split("\n")[:-1] == [
        "fresh-host ran ok",
        "unchanged skipped ok",
        "lockfile-changed ran ok",
        "unchanged-again skipped ok",
        "node-upgraded ran ok",
        "forced ran ok",
        "node-modules-deleted ran ok",
        # A failed install stops the deploy and leaves no stamp, so the next one retries.
        "install-fails ran failed",
        "after-failure ran ok",
    ], out
