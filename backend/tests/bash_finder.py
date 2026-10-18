"""
The bash every test that runs `deploy.sh` or `ops/*.sh` uses. One finder: three test
files used to carry their own, each slightly different.

On Windows that is Git Bash's `bin/bash.exe` launcher, found from wherever `git` is
installed. Never `System32\\bash.exe` (WSL's launcher, which cannot run these scripts
against Windows paths), and not `usr/bin/bash.exe` either: started from a Windows process
it does not put Git's coreutils on PATH, which the launcher does.
"""

import os
import shutil
from pathlib import Path


def find_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    roots = []
    git = shutil.which("git")
    if git:
        roots.append(Path(git).resolve().parents[1])
    roots += [Path(r"C:\Program Files\Git"), Path(r"C:\Program Files (x86)\Git")]
    for root in roots:
        candidate = root / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    return None
