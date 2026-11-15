"""Create a fresh git checkout of the bundled sample repository.

A local bare repository next to it acts as ``origin``, so the Phase 2
publishing flow (push + pull request in fixture mode) works without GitHub.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.config import PROJECT_ROOT
from app.mcp.git_utils import run_git

SAMPLE_SOURCE = PROJECT_ROOT / "examples" / "sample-repo"


def origin_path(dest: Path) -> Path:
    return Path(dest).parent / f"{Path(dest).name}-origin.git"


def setup_sample_repo(dest: Path, *, force: bool = False, with_origin: bool = True) -> Path:
    dest = Path(dest)
    origin = origin_path(dest)
    if dest.exists():
        if not force:
            raise FileExistsError(f"{dest} already exists (use force=True to recreate it)")
        shutil.rmtree(dest)
    if force and origin.exists():
        shutil.rmtree(origin)
    shutil.copytree(SAMPLE_SOURCE, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    run_git(dest, "init", "-q", "-b", "main")
    run_git(dest, "add", "-A")
    run_git(dest, "-c", "user.name=Sample Setup", "-c", "user.email=setup@example.invalid", "commit", "-q", "-m", "Initial commit")
    if with_origin:
        run_git(dest.parent, "init", "-q", "--bare", "-b", "main", str(origin))
        run_git(dest, "remote", "add", "origin", str(origin))
        run_git(dest, "push", "-q", "origin", "refs/heads/main:refs/heads/main")
    return dest
