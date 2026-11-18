"""Prepare a sample repository for one evaluation trial (docs/EVALUATION.md, protocol).

Every trial starts from the same state:

- a fresh copy of the bundled sample (Python, JavaScript or Java) with a fresh local ``origin``;
- fixture-mode pull requests from earlier runs moved out of ``DATA_DIR/pull_requests``
  (they live outside the repository, and an old one with the same branch name makes
  ``create_pull_request`` refuse, correctly, to open a duplicate);
- for issue #43, issue #42 already fixed and committed. The samples ship with #42's bug,
  which fails one existing test; without this, an issue-43 run cannot pass the tests
  without also fixing an unrelated issue.

Found while running the first experiment (2026-11-15): the first two trials ran without
these steps and were set aside.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.mcp.git_utils import run_git
from app.sample_repo import setup_sample_repo

_ROTATION_BUG = (
    "        self._by_access.pop(session.access_token, None)\n"
    "        self._by_access[new_session.access_token] = new_session\n"
    "        return new_session\n"
)
_ROTATION_FIX = (
    "        self._by_refresh.pop(refresh_token, None)\n"
    "        self._by_access.pop(session.access_token, None)\n"
    "        self._by_access[new_session.access_token] = new_session\n"
    "        self._by_refresh[new_session.refresh_token] = new_session\n"
    "        return new_session\n"
)

_JS_ROTATION_BUG = (
    "    this._byAccess.delete(session.accessToken);\n"
    "    this._byAccess.set(newSession.accessToken, newSession);\n"
    "    return newSession;\n"
)
_JS_ROTATION_FIX = (
    "    this._byRefresh.delete(refreshToken);\n"
    "    this._byAccess.delete(session.accessToken);\n"
    "    this._byAccess.set(newSession.accessToken, newSession);\n"
    "    this._byRefresh.set(newSession.refreshToken, newSession);\n"
    "    return newSession;\n"
)
_JAVA_ROTATION_BUG = (
    "        byAccess.remove(session.accessToken());\n"
    "        byAccess.put(newSession.accessToken(), newSession);\n"
    "        return newSession;\n"
)
_JAVA_ROTATION_FIX = (
    "        byRefresh.remove(refreshToken);\n"
    "        byAccess.remove(session.accessToken());\n"
    "        byAccess.put(newSession.accessToken(), newSession);\n"
    "        byRefresh.put(newSession.refreshToken(), newSession);\n"
    "        return newSession;\n"
)
_MESSAGE = "Fix refresh token rotation (issue 42): starting point for issue 43 trials"

# (language, issue) -> changes committed before the trial starts: (file, old text, new text, commit message).
STARTING_POINTS: dict[tuple[str, int], list[tuple[str, str, str, str]]] = {
    ("python", 43): [("authkit/sessions.py", _ROTATION_BUG, _ROTATION_FIX, _MESSAGE)],
    ("javascript", 43): [("src/sessions.js", _JS_ROTATION_BUG, _JS_ROTATION_FIX, _MESSAGE)],
    ("java", 43): [("src/main/java/authkit/SessionStore.java", _JAVA_ROTATION_BUG, _JAVA_ROTATION_FIX, _MESSAGE)],
}


def archive_pull_requests(data_dir: Path) -> Path | None:
    """Move DATA_DIR/pull_requests aside (never deleted); returns where it went, if anywhere."""
    folder = Path(data_dir) / "pull_requests"
    if not folder.is_dir() or not any(folder.iterdir()):
        return None
    target = Path(data_dir) / "pull_requests-archive" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target.parent.mkdir(parents=True, exist_ok=True)
    folder.rename(target)
    return target


def prepare_trial(issue: int, repo: Path, data_dir: Path, language: str = "python", sample: str | None = None) -> dict[str, object]:
    """Reset ``repo`` and ``data_dir`` for a trial of ``issue``; returns what was done.

    ``sample`` picks a bundled sample other than the language's default; starting points
    only exist for the default samples.
    """
    repo = setup_sample_repo(Path(repo), force=True, language=language, sample=sample)
    commits = []
    for rel, old, new, message in STARTING_POINTS.get((language, issue), []) if sample is None else []:
        path = repo / rel
        text = path.read_text()
        if text.count(old) != 1:
            raise ValueError(f"{rel} does not have the expected content; is this the bundled sample repository?")
        path.write_text(text.replace(old, new))
        run_git(repo, "-c", "user.name=Experiment setup", "-c", "user.email=setup@example.invalid", "commit", "-q", "-am", message)
        commits.append(message)
    if commits:
        run_git(repo, "push", "-q", "origin", "main")
    archived = archive_pull_requests(Path(data_dir))
    return {"repo": str(repo), "starting_commits": commits, "archived_pull_requests": str(archived) if archived else None}
