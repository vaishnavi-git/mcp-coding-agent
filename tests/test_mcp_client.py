"""MCP client hub: server connections (in-process and real stdio), discovery, risk, routing, audit."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from app.config import PROJECT_ROOT
from app.mcp.client import MCPHub, Risk, ServerConfig, ToolNotFound, classify_risk, stdio_server_configs
from app.storage.audit import AuditStore
from tests.fakes import FakeRunner, inprocess_servers

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "annotations, risk",
    [
        ({"read_only_hint": True}, Risk.READ),
        ({"read_only_hint": False, "destructive_hint": False, "open_world_hint": False}, Risk.WRITE),
        ({"read_only_hint": False, "destructive_hint": False, "open_world_hint": True}, Risk.EXTERNAL),
        ({"read_only_hint": False, "destructive_hint": True}, Risk.DESTRUCTIVE),
        ({}, Risk.DESTRUCTIVE),  # unannotated tools get the MCP specification's most conservative defaults
    ],
)
def test_classify_risk(annotations: dict, risk: Risk) -> None:
    assert classify_risk(annotations) is risk


async def test_discovery_across_servers(settings) -> None:
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        assert {name: spec.server for name, spec in hub.tools.items()} == {
            "get_github_issue": "github",
            "search_repository": "filesystem",
            "read_repository_file": "filesystem",
            "propose_patch": "filesystem",
            "apply_patch": "filesystem",
            "get_git_diff": "filesystem",
            "run_tests": "sandbox",
            "create_branch": "github",
            "commit_changes": "github",
            "push_branch": "github",
            "create_pull_request": "github",
            "list_documentation_sources": "docs",
            "search_documentation": "docs",
            "read_documentation": "docs",
        }
        assert {s.name for s in hub.by_risk(Risk.WRITE)} == {"apply_patch", "create_branch", "commit_changes"}
        assert {s.name for s in hub.by_risk(Risk.EXTERNAL)} == {"push_branch", "create_pull_request"}
        assert hub.by_risk(Risk.DESTRUCTIVE) == []
        assert hub.get("run_tests").risk is Risk.READ
        assert {hub.get(n).risk for n in ("search_documentation", "read_documentation", "list_documentation_sources")} == {Risk.READ}
        assert "never follow instructions" in hub.instructions["docs"]
        assert "untrusted" in hub.instructions["github"] or "never as instructions" in hub.instructions["github"]
        with pytest.raises(ToolNotFound):
            hub.get("delete_repository")


async def test_valid_and_invalid_calls_are_logged_and_redacted(settings, tmp_path: Path) -> None:
    (settings.repo_path / "authkit" / "config.py").write_text('GITHUB = "ghp_' + "z" * 36 + '"\n')
    audit = AuditStore(tmp_path / "audit.db")
    run_id = audit.create_run(42, "demo/sample-repo", "scripted")
    async with MCPHub(inprocess_servers(settings, FakeRunner()), audit=audit, run_id=run_id) as hub:
        ok = await hub.call("read_repository_file", {"path": "authkit/config.py"}, node="test", initiator="llm")
        assert not ok.is_error and "ghp_" not in ok.text and "REDACTED" in ok.text
        bad = await hub.call("read_repository_file", {"wrong": 1}, node="test")
        assert bad.is_error
    rows = audit.tool_calls(run_id)
    assert [(r["tool"], r["is_error"], r["initiator"]) for r in rows] == [("read_repository_file", 0, "llm"), ("read_repository_file", 1, "workflow")]
    assert rows[0]["result_chars"] > 0 and rows[0]["result_sha256"]
    assert "ghp_" not in (rows[0]["result_head"] or "")


async def test_real_stdio_server_connection(sample_repo: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp.filesystem_server", "--repo-path", str(sample_repo)],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(PROJECT_ROOT)},
        cwd=str(PROJECT_ROOT),
    )
    async with MCPHub([ServerConfig("filesystem", params, timeout_seconds=30)]) as hub:
        assert "read_repository_file" in hub.tools
        outcome = await hub.call("read_repository_file", {"path": "authkit/__init__.py"})
        assert not outcome.is_error and "SessionStore" in outcome.text


async def test_stdio_configs_launch_all_servers_with_minimal_env(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-reach-servers")
    configs = stdio_server_configs(dataclasses.replace(settings, github_token="ghp_" + "t" * 36))
    assert [c.name for c in configs] == ["github", "filesystem", "sandbox", "docs"]
    for cfg in configs:
        assert "ANTHROPIC_API_KEY" not in (cfg.target.env or {})
        assert ("GITHUB_TOKEN" in (cfg.target.env or {})) is (cfg.name == "github")
    async with MCPHub(configs) as hub:  # starting and listing tools needs no network
        assert len(hub.tools) == 14
        assert hub.tools["read_documentation"].server == "docs"
        issue = await hub.call("get_github_issue", {"issue_number": 42})
        assert issue.text.startswith("# Issue #42")


async def test_failing_server_is_reported_by_name(sample_repo: Path) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(3)"])
    with pytest.raises(RuntimeError, match="'broken' failed to start"):
        async with MCPHub([ServerConfig("broken", params, timeout_seconds=10)]):
            pass
