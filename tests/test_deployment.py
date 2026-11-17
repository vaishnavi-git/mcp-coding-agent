"""Static checks of the Docker files, and the sandbox work directory they rely on.

Docker is not available where these tests were written, so the images are
never built here. These checks catch the mistakes that matter most without
building them: secrets in the build context or the compose file, ports open
beyond localhost, root users, unpinned base images, and missing paths.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.mcp.sandbox_server import DockerRunner
from tests.test_sandbox_server import fake_docker

yaml = pytest.importorskip("yaml")


def compose() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())


def dockerfile_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.strip().startswith("#")]


def test_compose_services_ports_and_build_contexts() -> None:
    config = compose()
    assert set(config["services"]) == {"api", "ui", "sandbox"}
    for name, service in config["services"].items():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), f"{name} publishes {port} beyond localhost"
        build = service.get("build")
        if build:
            assert (PROJECT_ROOT / build["context"]).is_dir(), name
    assert config["services"]["api"]["env_file"] == ".env" and config["services"]["ui"]["environment"]["AGENT_API_URL"] == "http://api:8000"
    assert config["services"]["sandbox"]["network_mode"] == "none"


def test_compose_holds_no_secrets() -> None:
    text = (PROJECT_ROOT / "docker-compose.yml").read_text()
    assert not re.search(r"(ANTHROPIC_API_KEY|GITHUB_TOKEN)\s*[:=]", text)
    assert not re.search(r"sk-ant-|ghp_|github_pat_", text)


def test_sandbox_work_dir_has_the_same_path_on_both_sides() -> None:
    api = compose()["services"]["api"]
    work_dir = api["environment"]["SANDBOX_WORK_DIR"]
    assert f"{work_dir}:{work_dir}" in api["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in api["volumes"]


def test_images_run_as_non_root_with_pinned_bases() -> None:
    app_image = dockerfile_lines(PROJECT_ROOT / "Dockerfile")
    sandbox_image = dockerfile_lines(PROJECT_ROOT / "sandbox" / "Dockerfile")
    for lines, user in ((app_image, "agent"), (sandbox_image, "runner")):
        users = [line for line in lines if line.startswith("USER ")]
        assert users and users[-1] == f"USER {user}"
        for line in lines:
            if line.startswith("FROM "):
                image = line.split()[1]
                assert ":" in image and not image.endswith(":latest"), line


def test_app_image_copies_only_existing_sources_and_no_secrets() -> None:
    lines = dockerfile_lines(PROJECT_ROOT / "Dockerfile")
    copies = [line for line in lines if line.startswith(("COPY ", "ADD ")) and "--from=" not in line]
    sources = [part for line in copies for part in line.split()[1:-1] if not part.startswith("--")]
    assert sources and all((PROJECT_ROOT / s).exists() for s in sources), sources
    assert not any(".env" in s or s in ("data", "workspaces", ".") for s in sources)
    ignored = (PROJECT_ROOT / ".dockerignore").read_text().split()
    assert {".env", "data/", "workspaces/", ".venv/", ".git/"} <= set(ignored)


def test_docker_runner_puts_repository_copies_in_the_work_dir(sample_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker"))
    work_dir = tmp_path / "shared-with-host"
    runner = DockerRunner("img", timeout_seconds=10, docker_bin=str(fake_docker(tmp_path)), startup_grace_seconds=0, work_dir=work_dir)
    result = runner.run(sample_repo, ["pytest", "-q"])
    assert result.exit_code == 0
    mount = next(a for a in (tmp_path / "docker.args").read_text().splitlines() if a.endswith(":/workspace:rw"))
    assert mount.startswith(str(work_dir.resolve()) + "/") or mount.startswith(str(work_dir) + "/")
    assert list(work_dir.iterdir()) == []  # the copy is removed after the run


def test_ci_workflow_lints_and_runs_every_test_with_docker() -> None:
    workflow = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "tests.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}  # CI never needs to write to the repository
    steps = {job: [s.get("run") or s.get("uses") for s in spec["steps"]] for job, spec in workflow["jobs"].items()}
    assert any("ruff check app scripts tests" in str(s) for s in steps["lint"])
    test_steps = " ".join(str(s) for s in steps["test"])
    assert "docker build -t mcp-agent-sandbox:latest sandbox/" in test_steps  # so the real-Docker test runs, not skips
    pytest_step = next(str(s) for s in steps["test"] if str(s).startswith("pytest"))
    assert "--cov=app" in pytest_step and " -m " not in pytest_step and " -k " not in pytest_step  # the whole suite
    for spec in workflow["jobs"].values():
        for step in spec["steps"]:
            if "uses" in step:
                assert "@" in step["uses"], step  # actions are pinned to a version


def test_pyproject_metadata_is_valid() -> None:
    import tomllib

    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]
    # A section header in the wrong place silently moves keys into the wrong table (this broke CI once).
    assert isinstance(project["dependencies"], list) and any(d.startswith("anthropic") for d in project["dependencies"])
    assert all(isinstance(url, str) and url.startswith("https://") for url in project["urls"].values())
    assert set(project["optional-dependencies"]) == {"ui", "dev"}
    assert project["license"] == {"text": "MIT"} and (PROJECT_ROOT / "LICENSE").is_file()
