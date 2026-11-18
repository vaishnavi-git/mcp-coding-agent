"""Security primitives shared by the MCP servers and the client.

- PathGuard: confines every path to the approved repository (symlinks resolved
  before the containment check), blocks secret-bearing files, and restricts
  writes to configured directories.
- validate_test_command: turns a test command string (pytest, node --test or
  Maven) into a safe argv, or rejects it with a specific reason.
- redact: masks credentials in anything that gets logged or returned.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from app.languages import runner_for

# --------------------------------------------------------------------------- paths

FORBIDDEN_DIR_NAMES = frozenset({".git", ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker"})
FORBIDDEN_FILE_GLOBS = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "authorized_keys",
    "known_hosts",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "secrets.*",
)
SKIP_WALK_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build"})


class PathViolation(Exception):
    """A path is outside the repository, forbidden, or not writable."""


def is_protected(rel: str) -> bool:
    """True for secret-bearing paths (relative, POSIX separators), whatever their letter case.

    Names are compared in lower case because macOS and Windows file systems are
    usually case-insensitive: there ``.ENV`` opens ``.env`` and ``.GIT/config``
    opens ``.git/config``.
    """
    if rel in ("", "."):
        return False
    parts = [part.lower() for part in PurePosixPath(rel).parts]
    if any(part in FORBIDDEN_DIR_NAMES for part in parts):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], glob) for glob in FORBIDDEN_FILE_GLOBS)


def has_other_names(path: Path) -> bool:
    """True for a regular file with more than one hard link.

    Every hard link is an equal name for the same file, so a link planted in the repository can
    be a second name for a file anywhere else on the same disk (``ln ~/.ssh/id_rsa notes.txt``).
    Path checks can't see where the other names are, so such files are treated as protected.
    Git never creates hard links in a checkout.
    """
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink > 1


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


class PathGuard:
    def __init__(self, root: str | Path, writable_paths: tuple[str, ...] = (), allow_new_files: bool = True) -> None:
        resolved = Path(root).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"repository root is not a directory: {resolved}")
        self.root = resolved
        self.writable = tuple(p.strip("/").strip() for p in writable_paths if p.strip("/").strip())
        self.allow_new_files = allow_new_files

    def resolve(self, path: str | None, *, must_exist: bool = False, for_write: bool = False) -> Path:
        raw = (path or ".").strip() or "."
        if "\x00" in raw:
            raise PathViolation("path contains a NUL byte")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)  # follows symlinks in existing components
        if resolved != self.root and self.root not in resolved.parents:
            raise PathViolation(f"'{raw}' is outside the approved repository")
        rel = self.relative(resolved)
        if self.is_forbidden(rel):
            raise PathViolation(f"'{rel}' is a protected file and cannot be accessed")
        if must_exist and not resolved.exists():
            raise PathViolation(f"'{rel}' does not exist")
        if has_other_names(resolved):
            raise PathViolation(f"'{rel}' is protected: it has more than one hard link, so it may be another name for a file outside the repository")
        if for_write:
            if rel == ".":
                raise PathViolation("cannot write to the repository root")
            if self.writable and not any(rel == w or rel.startswith(w + "/") for w in self.writable):
                raise PathViolation(f"'{rel}' is outside the writable paths ({', '.join(self.writable)})")
        return resolved

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def is_forbidden(self, rel: str) -> bool:
        return is_protected(rel)

    def walk_files(self, start: Path):
        """Yield readable files below ``start`` (already validated)."""
        if start.is_file():
            yield start
            return
        stack = [start]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name, reverse=True)
            except OSError:
                continue
            for entry in entries:
                rel = self.relative(entry)
                if self.is_forbidden(rel):
                    continue
                if entry.is_symlink():
                    # Follow a link only if its target is inside the repository and not protected
                    # (authkit/env_link -> ../.env must not expose .env).
                    target = entry.resolve(strict=False)
                    if target != self.root and self.root not in target.parents:
                        continue
                    if self.is_forbidden(self.relative(target)):
                        continue
                if entry.is_dir():
                    if entry.name not in SKIP_WALK_DIRS:
                        stack.append(entry)
                elif entry.is_file() and not has_other_names(entry):
                    yield entry


# --------------------------------------------------------------------------- commands

class CommandRejected(Exception):
    """A command is not allowed in the sandbox."""


# Explicit, human-readable reasons for well-known dangerous commands.
DANGEROUS_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\b"), "file deletion (rm) is not allowed"),
    (re.compile(r"\bsudo\b|\bsu\b|\bdoas\b"), "privilege escalation (sudo/su) is not allowed"),
    (re.compile(r"\bgit\b.*\bpush\b"), "git push is not allowed in the sandbox (and force push is never allowed)"),
    (re.compile(r"--force\b|\s-f\s*$"), "force flags are not allowed"),
    (re.compile(r"\b(curl|wget|nc|ncat|netcat|ssh|scp|rsync|ftp|telnet)\b"), "network tools are not allowed"),
    (re.compile(r"\b(chmod|chown|mkfs|dd|mount|kill|pkill|shutdown|reboot)\b"), "system administration commands are not allowed"),
    (re.compile(r"\b(pip|pip3|npm|yarn|apt|apt-get|brew)\s+(install|add|uninstall|remove)\b"), "package installation is not allowed"),
)
SHELL_METACHARACTERS = set(";|&`$<>\n\r\\")

SAFE_PYTEST_FLAGS = frozenset(
    {"-q", "-qq", "-v", "-vv", "-x", "-s", "-l", "-ra", "-rA", "--quiet", "--verbose", "--exitfirst", "--lf", "--ff", "--no-header", "--showlocals"}
)
_PYTEST_VALUE_FLAG = re.compile(r"^(--tb=(short|long|line|no|native|auto)|--maxfail=\d{1,3}|--durations=\d{1,3}|-p=?no:cacheprovider)$")
_KEYWORD_RE = re.compile(r"^[\w\s.\-\[\]:()]{1,200}$")

# node --test: only the test runner, never options that load or evaluate other code
# (--require, --import, --eval, loaders, inspectors, --env-file, ...).
_NODE_REPORTER = re.compile(r"^--test-reporter=(tap|spec|dot)$")
_NODE_NAME_PATTERN = re.compile(r"^[\w\s.\-]{1,200}$")

# Maven: offline, quiet or batch output, an optional test selector, and the test goal only.
_MAVEN_FLAGS = frozenset({"-o", "--offline", "-q", "--quiet", "-B", "--batch-mode", "-e", "--errors", "-ntp", "--no-transfer-progress"})
_MAVEN_PROPERTIES = frozenset({"-DfailIfNoTests=false", "-Dsurefire.failIfNoSpecifiedTests=false"})
_MAVEN_TEST_SELECTOR = re.compile(r"^-Dtest=[A-Za-z_][\w.*]*(#[\w*+]+)?(,[A-Za-z_][\w.*]*(#[\w*+]+)?){0,19}$")


def validate_test_command(command: str, allowlist: tuple[str, ...], guard: PathGuard) -> list[str]:
    """Return a safe argv for ``command`` or raise CommandRejected with the reason."""
    if not command or not command.strip():
        raise CommandRejected("empty command")
    if len(command) > 500:
        raise CommandRejected("command is too long")
    for pattern, reason in DANGEROUS_COMMANDS:
        if pattern.search(command):
            raise CommandRejected(reason)
    bad = sorted(SHELL_METACHARACTERS & set(command))
    if bad:
        raise CommandRejected(f"shell syntax is not allowed (found {' '.join(repr(c) for c in bad)})")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise CommandRejected(f"could not parse command: {exc}") from exc

    prefix: list[str] | None = None
    for allowed in allowlist:
        candidate = shlex.split(allowed)
        if argv[: len(candidate)] == candidate:
            prefix = candidate
            break
    if prefix is None:
        raise CommandRejected(f"command is not on the allowlist (allowed: {', '.join(allowlist)})")

    runner = runner_for(argv)
    if runner == "node":
        _check_node_arguments(argv, guard)
    elif runner == "maven":
        _check_maven_arguments(argv[1:])
    else:
        _check_pytest_arguments(argv[len(prefix) :], guard)
    return argv


def _check_test_path(arg: str, guard: PathGuard) -> None:
    target = arg.split("::", 1)[0]
    try:
        guard.resolve(target, must_exist=True)
    except PathViolation as exc:
        raise CommandRejected(f"invalid test path: {exc}") from exc


def _check_pytest_arguments(rest: list[str], guard: PathGuard) -> None:
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in SAFE_PYTEST_FLAGS or _PYTEST_VALUE_FLAG.match(arg):
            i += 1
            continue
        if arg == "-p" and i + 1 < len(rest) and rest[i + 1] == "no:cacheprovider":
            i += 2
            continue
        if arg == "-k":
            if i + 1 >= len(rest) or not _KEYWORD_RE.match(rest[i + 1]):
                raise CommandRejected("-k needs a simple keyword expression")
            i += 2
            continue
        if arg.startswith("-"):
            raise CommandRejected(f"option {arg!r} is not allowed")
        if arg.startswith("@"):
            # pytest reads "@file" as a file of extra arguments, which would bypass this check
            raise CommandRejected(f"argument files ({arg!r}) are not allowed")
        _check_test_path(arg, guard)
        i += 1


def _check_node_arguments(argv: list[str], guard: PathGuard) -> None:
    # "--test" has to come first: anything before it, or a file before it, makes node run that
    # file as a script instead of running the test runner.
    if len(argv) < 2 or argv[1] != "--test":
        raise CommandRejected("node may only run its test runner: the command must start with 'node --test'")
    rest = argv[2:]
    seen_path = False
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg.startswith("-"):
            if seen_path:
                raise CommandRejected("options must come before the test files")
            if _NODE_REPORTER.match(arg):
                i += 1
                continue
            if arg == "--test-name-pattern" or arg.startswith("--test-name-pattern="):
                inline = arg.startswith("--test-name-pattern=")
                value = arg.partition("=")[2] if inline else (rest[i + 1] if i + 1 < len(rest) else "")
                if not _NODE_NAME_PATTERN.match(value):
                    raise CommandRejected("--test-name-pattern needs a simple pattern (letters, digits, spaces, dots and dashes)")
                i += 1 if inline else 2
                continue
            raise CommandRejected(f"option {arg!r} is not allowed")
        _check_test_path(arg, guard)
        seen_path = True
        i += 1


def _check_maven_arguments(rest: list[str]) -> None:
    goals: list[str] = []
    for arg in rest:
        if arg in _MAVEN_FLAGS or arg in _MAVEN_PROPERTIES or _MAVEN_TEST_SELECTOR.match(arg):
            continue
        if arg.startswith("-Dtest="):
            raise CommandRejected("-Dtest needs simple class names, optionally with #method (for example -Dtest=SessionStoreTest#refresh)")
        if arg.startswith("-"):
            raise CommandRejected(f"option {arg!r} is not allowed")
        goals.append(arg)
    if goals != ["test"]:
        raise CommandRejected(f"the only Maven goal allowed is 'test' (got {' '.join(goals) or 'none'})")
    if not {"-o", "--offline"} & set(rest):
        raise CommandRejected("Maven must run offline (-o): the sandbox has no network")


# --------------------------------------------------------------------------- redaction

_SECRET_KEYS = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential|private[_-]?key)", re.I)
_SECRET_VALUES = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]{16,})"
)
REDACTED = "***REDACTED***"


def redact(value: Any) -> Any:
    """Mask credentials in strings, and values of secret-looking keys in dicts."""
    if isinstance(value, dict):
        return {k: (REDACTED if _SECRET_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUES.sub(REDACTED, value)
    return value
