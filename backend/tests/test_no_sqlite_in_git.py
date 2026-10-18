"""No SQLite database is ever tracked, under any name (issue #36).

The repository is public and `.gitignore` is a name list, always one name behind.
`ops/check_no_sqlite.py` checks content instead; CI runs it as its own step and the
pre-commit hook `ops/hooks/pre-commit` runs it before a commit exists. These tests pin
that it passes on this tree, that it catches what the name list misses, and that the hook
actually refuses the commit.
"""
from __future__ import annotations

import bz2
import gzip
import importlib.util
import lzma
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK = REPO_ROOT / "ops" / "check_no_sqlite.py"
HOOKS = REPO_ROOT / "ops" / "hooks"

_spec = importlib.util.spec_from_file_location("check_no_sqlite", CHECK)
check_no_sqlite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_no_sqlite)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An empty repository, isolated from any GIT_* variables of the calling process."""
    for var in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(var, raising=False)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _sqlite_bytes(tmp_path: Path) -> bytes:
    db = tmp_path.parent / f"{tmp_path.name}-source.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table t (x)")
    conn.close()
    return db.read_bytes()


def _stage(repo: Path, name: str, content: bytes) -> None:
    (repo / name).write_bytes(content)
    subprocess.run(["git", "add", "--", name], cwd=repo, check=True)


def test_this_tree_tracks_no_sqlite():
    if subprocess.run(["git", "rev-parse"], cwd=REPO_ROOT, capture_output=True, check=False).returncode:
        pytest.skip("not a git checkout")
    assert check_no_sqlite.find_sqlite_in_index(REPO_ROOT) == []


@pytest.mark.parametrize(
    "name, wrap",
    [
        ("notes.json", lambda b: b),  # a name no ignore pattern would ever catch
        ("prod.db.gz", gzip.compress),
        ("snap.bin", bz2.compress),
        ("snap", lzma.compress),
    ],
)
def test_a_staged_database_is_caught_under_any_name(repo, name, wrap):
    _stage(repo, name, wrap(_sqlite_bytes(repo)))
    _stage(repo, "README.md", b"hello\n")
    assert check_no_sqlite.find_sqlite_in_index(repo) == [name]


def test_wal_and_journal_headers_are_caught():
    assert check_no_sqlite.is_sqlite(b"\x37\x7f\x06\x82" + b"\0" * 28)
    assert check_no_sqlite.is_sqlite(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\0" * 20)


@pytest.mark.parametrize(
    "content",
    [b"", b"SQLite format 2", b"SQLite format 3 is a file format\n", gzip.compress(b"text"), b"\x1f\x8bjunk"],
)
def test_lookalikes_are_not_flagged(content):
    assert not check_no_sqlite.is_sqlite(content)


def test_the_index_is_judged_not_the_working_tree(repo):
    """The hook must judge what the commit will contain: a database staged and then
    overwritten on disk is still caught."""
    _stage(repo, "data.json", _sqlite_bytes(repo))
    (repo / "data.json").write_bytes(b"{}\n")
    assert check_no_sqlite.find_sqlite_in_index(repo) == ["data.json"]


def test_the_cli_exits_nonzero_and_names_the_file(repo):
    _stage(repo, "copy.txt", _sqlite_bytes(repo))
    result = subprocess.run([sys.executable, str(CHECK)], cwd=repo, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "copy.txt" in result.stderr


@pytest.mark.deploy_scripts  # runs an ops/ shell script; seconds under Git Bash
def test_the_pre_commit_hook_refuses_the_commit(repo):
    """End to end through git's own hook runner, with the repo's hook directory."""
    env = {**os.environ, "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]])}
    commit = [
        "git",
        "-c", f"core.hooksPath={HOOKS.as_posix()}",
        "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "x",
    ]
    _stage(repo, "README.md", b"hello\n")
    clean = subprocess.run(commit, cwd=repo, env=env, capture_output=True, text=True, check=False)
    assert clean.returncode == 0, clean.stderr

    _stage(repo, "portfolio.db.snapshot", _sqlite_bytes(repo))
    refused = subprocess.run(commit, cwd=repo, env=env, capture_output=True, text=True, check=False)
    assert refused.returncode != 0
    assert "portfolio.db.snapshot" in refused.stderr
