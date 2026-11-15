"""Hardened wrapper around the git CLI used by the MCP servers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment: no user/system git config, no prompts, no inherited secrets."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if extra:
        env.update(extra)
    return env


def run_git(repo: Path, *args: str, timeout: float = 60, check: bool = True, env_extra: dict[str, str] | None = None) -> str:
    # Hooks are disabled so the target repository cannot run code through our git calls.
    cmd = ["git", "-C", str(repo), "-c", f"core.hooksPath={os.devnull}", "-c", "color.ui=false", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=git_env(env_extra))
    except FileNotFoundError as exc:  # pragma: no cover - git missing
        raise GitError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        # Some details (e.g. "[rejected]" with --porcelain) are printed on stdout, so keep both streams.
        detail = "\n".join(part for part in (proc.stderr.strip(), proc.stdout.strip()) if part)
        raise GitError(f"git {args[0]} failed: {detail}")
    return proc.stdout


def is_git_repo(repo: Path) -> bool:
    try:
        return run_git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False
