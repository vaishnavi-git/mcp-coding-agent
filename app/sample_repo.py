"""Create a fresh git checkout of one of the bundled sample repositories.

A local bare repository next to it acts as ``origin``, so the
publishing flow (push + pull request in fixture mode) works without GitHub.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.config import PROJECT_ROOT
from app.languages import ALIASES, PROFILES, SAMPLES
from app.mcp.git_utils import run_git

SAMPLE_SOURCE = PROJECT_ROOT / "examples" / "sample-repo"


def sample_source(language: str = "python", sample: str | None = None) -> Path:
    """examples/<sample>, or the language's default sample (sample-repo, sample-repo-js, sample-repo-java)."""
    if sample is not None:
        if sample not in SAMPLES:
            raise ValueError(f"no sample repository named {sample!r} (choose from {', '.join(SAMPLES)})")
        return PROJECT_ROOT / "examples" / sample
    name = ALIASES.get(language, language)
    if name not in PROFILES:
        raise ValueError(f"no sample repository for {language!r} (choose from {', '.join(PROFILES)})")
    return PROJECT_ROOT / "examples" / PROFILES[name].sample


def origin_path(dest: Path) -> Path:
    return Path(dest).parent / f"{Path(dest).name}-origin.git"


def setup_sample_repo(dest: Path, *, force: bool = False, with_origin: bool = True, language: str = "python", sample: str | None = None) -> Path:
    dest = Path(dest)
    origin = origin_path(dest)
    if dest.exists():
        if not force:
            raise FileExistsError(f"{dest} already exists (use force=True to recreate it)")
        shutil.rmtree(dest)
    if force and origin.exists():
        shutil.rmtree(origin)
    shutil.copytree(sample_source(language, sample), dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "node_modules", "target"))
    run_git(dest, "init", "-q", "-b", "main")
    run_git(dest, "add", "-A")
    run_git(dest, "-c", "user.name=Sample Setup", "-c", "user.email=setup@example.invalid", "commit", "-q", "-m", "Initial commit")
    if with_origin:
        run_git(dest.parent, "init", "-q", "--bare", "-b", "main", str(origin))
        run_git(dest, "remote", "add", "origin", str(origin))
        run_git(dest, "push", "-q", "origin", "refs/heads/main:refs/heads/main")
    return dest
