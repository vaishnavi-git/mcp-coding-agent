"""Path validation, forbidden files, command allowlisting and secret redaction."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.mcp.security import CommandRejected, PathGuard, PathViolation, redact, validate_test_command

ALLOW = ("pytest", "python -m pytest")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / "tests" / "test_app.py").write_text("def test_x(): pass\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    return root


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "src/../../outside.txt", "/etc/passwd", "~/.ssh/id_rsa", "src/../../../etc/hosts"],
)
def test_path_traversal_is_rejected(repo: Path, path: str) -> None:
    with pytest.raises(PathViolation):
        PathGuard(repo).resolve(path)


def test_symlink_escape_is_rejected(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    os.symlink(outside, repo / "src" / "link.txt")
    with pytest.raises(PathViolation, match="outside"):
        PathGuard(repo).resolve("src/link.txt")


@pytest.mark.parametrize(
    "path",
    [".env", ".env.production", "config/.env", ".git/config", "keys/id_rsa", "keys/id_ed25519.pub", "certs/server.pem", "deploy/prod.key", ".ssh/known_hosts", "credentials.json"],
)
def test_forbidden_files_are_rejected(repo: Path, path: str) -> None:
    with pytest.raises(PathViolation, match="protected"):
        PathGuard(repo).resolve(path)


def test_normal_paths_resolve(repo: Path) -> None:
    guard = PathGuard(repo)
    assert guard.resolve("src/app.py", must_exist=True) == repo.resolve() / "src" / "app.py"
    assert guard.resolve("./src/../src/app.py") == repo.resolve() / "src" / "app.py"


def test_writable_paths_are_enforced(repo: Path) -> None:
    guard = PathGuard(repo, writable_paths=("tests",))
    assert guard.resolve("tests/test_new.py", for_write=True)
    with pytest.raises(PathViolation, match="writable"):
        guard.resolve("src/app.py", for_write=True)
    with pytest.raises(PathViolation):
        guard.resolve("testsuite/x.py", for_write=True)  # prefix match is per directory, not per string


def test_walk_skips_forbidden_files(repo: Path) -> None:
    guard = PathGuard(repo)
    names = {guard.relative(p) for p in guard.walk_files(repo)}
    assert names == {"src/app.py", "tests/test_app.py"}


@pytest.mark.parametrize(
    "command, reason",
    [
        ("rm -rf /", "deletion"),
        ("sudo pytest", "privilege"),
        ("git push --force origin main", "push"),
        ("pytest; rm -rf .", "deletion"),
        ("pytest && curl http://evil.example", "network"),
        ("pytest | sh", "shell syntax"),
        ("pytest $(cat .env)", "shell syntax"),
        ("python -c 'import os'", "allowlist"),
        ("bash -c pytest", "allowlist"),
        ("pytest --rootdir=/", "not allowed"),
        ("pytest -p evil_plugin", "not allowed"),
        ("pytest ../outside", "invalid test path"),
        ("pytest .env", "invalid test path"),
        ("pip install requests", "package installation"),
        ("", "empty"),
    ],
)
def test_dangerous_commands_are_rejected(repo: Path, command: str, reason: str) -> None:
    with pytest.raises(CommandRejected, match=reason):
        validate_test_command(command, ALLOW, PathGuard(repo))


@pytest.mark.parametrize(
    "command, argv",
    [
        ("pytest", ["pytest"]),
        ("pytest -q -p no:cacheprovider", ["pytest", "-q", "-p", "no:cacheprovider"]),
        ("python -m pytest tests/test_app.py::test_x -x --tb=short", ["python", "-m", "pytest", "tests/test_app.py::test_x", "-x", "--tb=short"]),
        ("pytest -k 'refresh and not slow' tests", ["pytest", "-k", "refresh and not slow", "tests"]),
    ],
)
def test_allowlisted_commands_are_accepted(repo: Path, command: str, argv: list[str]) -> None:
    assert validate_test_command(command, ALLOW, PathGuard(repo)) == argv


def test_redaction() -> None:
    data = {
        "api_key": "abc",
        "nested": {"Authorization": "Bearer xyz"},
        "text": "token ghp_" + "a" * 36 + " and sk-ant-api03-" + "b" * 20,
        "safe": "hello",
    }
    out = redact(data)
    assert out["api_key"] == "***REDACTED***"
    assert out["nested"]["Authorization"] == "***REDACTED***"
    assert "ghp_" not in out["text"] and "sk-ant-" not in out["text"]
    assert out["safe"] == "hello"
