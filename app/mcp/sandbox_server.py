"""Sandbox MCP server: run allowlisted test commands inside Docker.

Each run works on a disposable copy of the repository, so tests can never
change the workspace. The container gets no network, a read-only root
filesystem, no Linux capabilities, no privilege escalation, a non-root user,
and memory/CPU/PID limits. It is killed when the timeout expires.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app.languages import runner_for
from app.reliability.test_results import parse_output

from .security import CommandRejected, PathGuard, has_other_names, is_protected, validate_test_command

# Runs code, but only in a throwaway container on a copy of the repo with no
# network: neither the workspace nor the outside world changes.
SANDBOXED = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)

COPY_IGNORE = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv", "node_modules"}

INSTRUCTIONS = """\
Runs the project's tests in an isolated Docker container (no network, resource
limits, timeout) on a disposable copy of the repository. Only allowlisted test
commands are accepted; shell syntax is rejected."""


@dataclass
class ExecResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool


class SandboxRunner(Protocol):
    name: str

    def check_available(self) -> str | None:
        """Return None when usable, otherwise a reason."""

    def run(self, repo_root: Path, argv: list[str]) -> ExecResult: ...


def copy_repository(src: Path, dest: Path) -> None:
    """Copy the repo, skipping VCS/caches, protected files and symlinks that point outside it.

    Protected files (.env, private keys, credentials; see security.is_protected)
    are left out so that code running in the container cannot read them and
    print them into the test output, which the model and the logs see.
    """
    src = src.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {n for n in names if n in COPY_IGNORE}
        if "pom.xml" in names and "target" in names:
            skipped.add("target")  # Maven build output
        for name in names:
            path = Path(directory) / name
            if is_protected(path.relative_to(src).as_posix()) or (not path.is_symlink() and has_other_names(path)):
                skipped.add(name)  # hard links may be other names for files outside the repository
                continue
            if path.is_symlink():
                target = path.resolve(strict=False)
                if target != src and src not in target.parents:
                    skipped.add(name)
        return skipped

    shutil.copytree(src, dest, symlinks=True, ignore=ignore)


class DockerRunner:
    name = "docker"

    def __init__(
        self,
        image: str,
        timeout_seconds: float = 120,
        memory_mb: int = 1024,
        cpus: float = 1.0,
        pids: int = 256,
        docker_bin: str = "docker",
        startup_grace_seconds: float = 15,
        work_dir: Path | None = None,
    ) -> None:
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb
        self.cpus = cpus
        self.pids = pids
        self.docker_bin = docker_bin
        self.startup_grace_seconds = startup_grace_seconds
        # The copy is bind-mounted into the test container, so the Docker daemon must see the same path.
        # When the API itself runs in a container (docker compose), SANDBOX_WORK_DIR is a directory
        # mounted at the same path on the host and in that container.
        self.work_dir = Path(work_dir) if work_dir else None
        self._available: str | None | bool = False  # False = not checked yet

    def check_available(self) -> str | None:
        if self._available is not False:
            return self._available  # type: ignore[return-value]
        if not shutil.which(self.docker_bin):
            reason: str | None = "Docker is not installed on this host"
        else:
            try:
                ok = subprocess.run([self.docker_bin, "info"], capture_output=True, timeout=15).returncode == 0
                reason = None if ok else "the Docker daemon is not running"
            except (OSError, subprocess.TimeoutExpired):
                reason = "the Docker daemon did not respond"
        self._available = reason
        return reason

    def isolation(self) -> list[str]:
        return [
            "disposable copy of the repository",
            "--network none",
            "--read-only root filesystem with tmpfs /tmp",
            "--cap-drop ALL, no-new-privileges, non-root user",
            f"{self.memory_mb}MB memory, {self.cpus} CPU, {self.pids} PIDs, {self.timeout_seconds:.0f}s timeout",
        ]

    def command(self, name: str, repo_copy: Path, argv: list[str]) -> list[str]:
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        return [
            self.docker_bin, "run", "--rm", "--name", name,
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", f"{uid}:{gid}",
            "--memory", f"{self.memory_mb}m",
            "--memory-swap", f"{self.memory_mb}m",
            "--cpus", str(self.cpus),
            "--pids-limit", str(self.pids),
            "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "CI=1",
            "--volume", f"{repo_copy}:/workspace:rw",
            "--workdir", "/workspace",
            self.image,
            *argv,
        ]

    def run(self, repo_root: Path, argv: list[str]) -> ExecResult:
        if self.work_dir is not None:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="mcp-sandbox-", dir=self.work_dir))
        name = f"mcp-sandbox-{uuid.uuid4().hex[:12]}"
        start = time.monotonic()
        try:
            repo_copy = workdir / "repo"
            copy_repository(repo_root, repo_copy)
            try:
                proc = subprocess.run(
                    self.command(name, repo_copy, argv),
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=self.timeout_seconds + self.startup_grace_seconds,
                )
                return ExecResult(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start, False)
            except subprocess.TimeoutExpired as exc:
                subprocess.run([self.docker_bin, "kill", name], capture_output=True, timeout=30)
                out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                return ExecResult(argv, -1, out, err, time.monotonic() - start, True)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def summarize(stdout: str, argv: list[str]) -> str | None:
    """One line about the run, read from the test runner's own output."""
    return parse_output(stdout, runner_for(argv))["summary"]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + f"\n... [{len(text) - limit} characters omitted] ...\n" + text[-(limit - head) :]


def format_result(result: ExecResult, runner_name: str, timeout_seconds: float, max_chars: int = 20_000) -> str:
    parsed = parse_output(result.stdout, runner_for(result.argv))
    if result.timed_out:
        status = "TIMED OUT"
    elif result.exit_code == 0 and not parsed["no_tests"]:
        status = "PASSED"
    else:
        # pytest exits with 5 when it finds no tests; node --test and Maven exit with 0. A run
        # where nothing was tested is not a pass for any of them.
        status = "FAILED"
    summary = parsed["summary"] or (f"killed after {int(timeout_seconds)}s timeout" if result.timed_out else "-")
    lines = [
        f"status: {status}",
        f"exit_code: {result.exit_code}",
        f"summary: {summary}",
        f"command: {' '.join(result.argv)}",
        f"duration: {result.duration_s:.2f}s  sandbox: {runner_name}",
        "--- output ---",
        _clip(result.stdout.rstrip(), max_chars) or "(no output)",
    ]
    if result.stderr.strip():
        lines += ["--- stderr ---", _clip(result.stderr.rstrip(), max_chars // 4)]
    return "\n".join(lines)


@dataclass
class SandboxSettings:
    repo_path: Path
    default_command: str = "pytest -q -p no:cacheprovider"
    allowlist: tuple[str, ...] = ("pytest", "python -m pytest")
    image: str = "mcp-agent-sandbox:latest"
    timeout_seconds: int = 120
    memory_mb: int = 1024
    cpus: float = 1.0
    pids: int = 256
    work_dir: Path | None = None


def build_server(settings: SandboxSettings, runner: SandboxRunner | None = None) -> MCPServer:
    guard = PathGuard(settings.repo_path)
    runner = runner or DockerRunner(settings.image, settings.timeout_seconds, settings.memory_mb, settings.cpus, settings.pids, work_dir=settings.work_dir)
    server = MCPServer("sandbox", instructions=INSTRUCTIONS, version="1.0.0")

    @server.tool(annotations=SANDBOXED)
    def run_tests(test_command: str = "") -> str:
        """Run tests in the Docker sandbox and return status, exit code, a summary line and the output.

        Args:
            test_command: An allowlisted test command, e.g. "pytest -q tests/test_tokens.py",
                          "node --test test/sessions.test.js" or "mvn -o -B test -Dtest=SessionStoreTest".
                          Empty runs the project's default test command. No shell syntax.
        """
        command = test_command.strip() or settings.default_command
        try:
            argv = validate_test_command(command, settings.allowlist, guard)
        except CommandRejected as exc:
            raise ToolError(f"command rejected: {exc}") from exc
        unavailable = runner.check_available()
        if unavailable:
            raise ToolError(f"cannot run tests: {unavailable}. Tests only run inside the Docker sandbox; start Docker and retry.")
        result = runner.run(guard.root, argv)
        return format_result(result, runner.name, settings.timeout_seconds)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sandbox MCP server (stdio)")
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--config-json", default="{}")
    args = parser.parse_args(argv)
    cfg = json.loads(args.config_json)
    settings = SandboxSettings(
        repo_path=Path(args.repo_path),
        default_command=cfg.get("default_command", "pytest -q -p no:cacheprovider"),
        allowlist=tuple(cfg.get("allowlist", ("pytest", "python -m pytest"))),
        image=cfg.get("image", "mcp-agent-sandbox:latest"),
        timeout_seconds=int(cfg.get("timeout_seconds", 120)),
        memory_mb=int(cfg.get("memory_mb", 1024)),
        cpus=float(cfg.get("cpus", 1.0)),
        pids=int(cfg.get("pids", 256)),
        work_dir=Path(cfg["work_dir"]) if cfg.get("work_dir") else None,
    )
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()
