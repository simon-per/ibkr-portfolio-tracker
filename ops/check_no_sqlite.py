#!/usr/bin/env python3
"""Refuse a SQLite database in git, under any name.

This repository is public, and docs/local-development.md tells contributors to copy a
production snapshot into the checkout. `.gitignore` catches the names such a copy usually
gets, but a name pattern is always one name behind (issue #36: `portfolio.db.snapshot`,
`snap.sqlite3`, `prod.db.gz` all slipped past it). This checks *content* instead: every
blob in the index whose first bytes are a SQLite database, write-ahead log or rollback
journal header, bare or wrapped in gzip, bzip2 or xz.

The index, not the working tree, because it is exactly what the next commit contains, so
one scan serves both callers:

* the pre-commit hook (`ops/hooks/pre-commit`; install with
  `git config core.hooksPath ops/hooks`), which is the one that prevents the leak, and
* CI (`.github/workflows/ci.yml`), where the index equals the pushed commit; it runs
  after the push, so it can only say a file is already public.

Stdlib only and no `app` import, so it runs before `pip install` and from a hook.
Exit 0 when clean, 1 naming each offender, 2 when git itself fails.
"""
from __future__ import annotations

import bz2
import lzma
import subprocess
import sys
import zlib
from pathlib import Path

# Enough input to decompress a header out of any of the wrappers below: a gzip header
# carries the original file name, so the compressed payload does not start at a fixed offset.
HEAD_BYTES = 64 * 1024

_SQLITE_MAGICS = (
    b"SQLite format 3\x00",  # the database file itself
    b"\x37\x7f\x06\x82",  # -wal, big-endian checksums
    b"\x37\x7f\x06\x83",  # -wal, little-endian checksums
    b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7",  # -journal (rollback journal)
)
_PROBE = max(len(m) for m in _SQLITE_MAGICS)


def _gunzip(data: bytes) -> bytes:
    return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, _PROBE)


_WRAPPERS = (
    (b"\x1f\x8b", _gunzip),
    (b"BZh", lambda data: bz2.BZ2Decompressor().decompress(data, _PROBE)),
    (b"\xfd7zXZ\x00", lambda data: lzma.LZMADecompressor().decompress(data, _PROBE)),
)


def is_sqlite(head: bytes) -> bool:
    """True when `head` (a file's first bytes) is SQLite data, bare or compressed."""
    if head.startswith(_SQLITE_MAGICS):
        return True
    for magic, decompress in _WRAPPERS:
        if head.startswith(magic):
            try:
                return decompress(head).startswith(_SQLITE_MAGICS)
            except (zlib.error, OSError, EOFError, lzma.LZMAError, ValueError):
                return False
    return False


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repo, input=stdin, capture_output=True, check=True
    ).stdout


def find_sqlite_in_index(repo: Path) -> list[str]:
    """Paths in `repo`'s index whose staged content is SQLite data.

    Reads blobs by object id through one `git cat-file --batch`, so a path that is
    deleted, renamed or edited in the working tree is judged by what git would commit.
    Honours GIT_INDEX_FILE, which `git commit <paths>` sets for its hooks.
    """
    paths_by_oid: dict[str, list[str]] = {}
    for entry in _git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, oid, _stage = meta.split(b" ")
        if mode == b"160000":  # a submodule's commit, not a blob in this repository
            continue
        paths_by_oid.setdefault(oid.decode(), []).append(path.decode("utf-8", "replace"))
    if not paths_by_oid:
        return []

    out = _git(repo, "cat-file", "--batch", stdin="".join(f"{o}\n" for o in paths_by_oid).encode())
    offenders: list[str] = []
    pos = 0
    while pos < len(out):
        header_end = out.index(b"\n", pos)
        fields = out[pos:header_end].split(b" ")
        start = header_end + 1
        if len(fields) != 3:  # "<oid> missing": an intent-to-add entry has no content yet
            pos = start
            continue
        oid, _type, size = fields
        if is_sqlite(out[start : start + min(int(size), HEAD_BYTES)]):
            offenders.extend(paths_by_oid[oid.decode()])
        pos = start + int(size) + 1  # the content is followed by one newline
    return sorted(offenders)


def main() -> int:
    try:
        offenders = find_sqlite_in_index(Path.cwd())
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        print(f"check_no_sqlite: git failed: {exc} {detail.decode(errors='replace')}", file=sys.stderr)
        return 2
    if not offenders:
        return 0
    print(
        "Refusing: these files are SQLite databases (or their WAL/journal), and this "
        "repository is public:",
        file=sys.stderr,
    )
    for path in offenders:
        print(f"    {path}", file=sys.stderr)
    print(
        "Unstage with `git rm --cached <path>` and keep snapshots outside the checkout or "
        "under backend/data/ (ignored). See docs/local-development.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
