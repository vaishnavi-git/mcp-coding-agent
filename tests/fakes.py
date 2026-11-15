"""Test doubles: a scripted LLM provider and a local stand-in for the Docker runner."""

from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import Settings
from app.llm.base import LLMProvider, LLMResponse, Message, ToolCall, ToolDefinition, ToolResult
from app.mcp import docs_server, filesystem_server, github_server, sandbox_server
from app.mcp.client import MCPHub, ServerConfig, tool_retry_policy
from app.mcp.sandbox_server import ExecResult, copy_repository

Step = str | list[tuple[str, dict[str, Any]]] | Callable[[list[Message]], Any]

DEFAULT_PUBLICATION = {
    "branch_slug": "rotate-refresh-tokens",
    "commit_message": "Rotate refresh tokens on refresh\n\nStore the new refresh token and revoke the used one (#42).",
    "pr_title": "Rotate refresh tokens on refresh",
    "summary": "Refreshing a session never stored the new refresh token and never revoked the used one. The fix does both.",
    "risks": ["Clients that reuse an old refresh token will now be logged out."],
}


class ScriptedProvider(LLMProvider):
    """Replays scripted turns per workflow node (``purpose``).

    Like the real provider it reports every attempt to ``metrics_sink`` (set by
    the runner), with ``usage`` tokens per successful call, so budgets and cost
    accounting work in tests. ``errors[purpose]`` is an exception raised on
    every call, or a list of exceptions raised one per call before succeeding.
    """

    model = "scripted-model"

    def __init__(
        self,
        plan: dict | None = None,
        turns: dict[str, list[Step]] | None = None,
        summary: str = "Summary.",
        json_by_purpose: dict[str, list[dict]] | None = None,
        usage: tuple[int, int] = (1000, 200),
    ) -> None:
        self.plan = plan
        self.json_by_purpose = {k: list(v) for k, v in (json_by_purpose or {}).items()}
        self.turns = {k: list(v) for k, v in (turns or {}).items()}
        self.summary = summary
        self.usage = usage
        self.errors: dict[str, Exception | list[Exception]] = {}
        self.prompts: list[tuple[str, str]] = []  # (purpose, first user prompt)
        self.tools_offered: dict[str, set[str]] = {}
        self.calls: list[str] = []
        self._n = 0

    def _emit(self, purpose: str, error: Exception | None = None) -> None:
        if self.metrics_sink is None:
            return
        meta: dict[str, Any] = {"purpose": purpose, "model": self.model, "latency_ms": 1}
        if error is None:
            meta.update(input_tokens=self.usage[0], output_tokens=self.usage[1], cache_read_tokens=0, cache_write_tokens=0, stop_reason="end_turn")
        else:
            meta["error"] = f"{type(error).__name__}: {error}"
        self.metrics_sink(meta)

    def _maybe_fail(self, purpose: str) -> None:
        self.calls.append(purpose)
        planned = self.errors.get(purpose)
        if isinstance(planned, list):
            error = planned.pop(0) if planned else None
        else:
            error = planned
        if error is not None:
            self._emit(purpose, error)
            raise error

    def generate(self, system_prompt: str, user_prompt: str, *, purpose: str = "generate") -> str:
        self.prompts.append((purpose, user_prompt))
        self._maybe_fail(purpose)
        self._emit(purpose)
        return self.summary

    def generate_json(self, system_prompt: str, user_prompt: str, schema: dict, *, purpose: str = "generate_json") -> dict:
        self.prompts.append((purpose, user_prompt))
        self._maybe_fail(purpose)
        self._emit(purpose)
        queue = self.json_by_purpose.get(purpose)
        if queue:
            return copy.deepcopy(queue.pop(0) if len(queue) > 1 else queue[0])
        if purpose == "draft_publication":
            return copy.deepcopy(DEFAULT_PUBLICATION)
        return copy.deepcopy(self.plan or {})

    def chat(self, system_prompt: str, messages: list[Message], tools: list[ToolDefinition], *, purpose: str = "chat") -> LLMResponse:
        if len(messages) == 1:
            self.prompts.append((purpose, messages[0]["content"]))
        self.tools_offered.setdefault(purpose, set()).update(t.name for t in tools)
        self._maybe_fail(purpose)
        self._emit(purpose)
        queue = self.turns.get(purpose) or []
        step: Any = queue.pop(0) if queue else "done"
        if callable(step):
            step = step(messages)
        if isinstance(step, str):
            return LLMResponse(step, [], "end_turn", self.model, {"role": "assistant", "content": [{"type": "text", "text": step}]})
        calls = []
        for name, args in step:
            self._n += 1
            calls.append(ToolCall(f"call_{self._n}", name, args))
        content = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in calls]
        return LLMResponse("", calls, "tool_use", self.model, {"role": "assistant", "content": content})

    def tool_results_message(self, results: list[ToolResult]) -> Message:
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": r.tool_call_id, "content": r.content, "is_error": r.is_error} for r in results]}


class LocalPytestRunner:
    """Test-only stand-in for DockerRunner: runs pytest on a copy of the repo with this interpreter."""

    name = "local-test-double"

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def check_available(self) -> str | None:
        return None

    def run(self, repo_root: Path, argv: list[str]) -> ExecResult:
        self.commands.append(list(argv))
        cmd = [sys.executable, "-m", "pytest", *argv[1:]] if argv[0] == "pytest" else [sys.executable, *argv[1:]]
        start = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "repo"
            copy_repository(repo_root, copy)
            proc = subprocess.run(cmd, cwd=copy, capture_output=True, text=True, timeout=120, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        return ExecResult(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start, False)


class FakeRunner:
    """Returns canned results without running anything."""

    name = "fake"

    def __init__(self, results: list[ExecResult] | None = None, unavailable: str | None = None) -> None:
        self.results = list(results or [])
        self.unavailable = unavailable
        self.commands: list[list[str]] = []

    def check_available(self) -> str | None:
        return self.unavailable

    def run(self, repo_root: Path, argv: list[str]) -> ExecResult:
        self.commands.append(list(argv))
        if self.results:
            return self.results.pop(0)
        return ExecResult(argv, 0, "===== 1 passed in 0.01s =====", "", 0.01, False)


def inprocess_servers(settings: Settings, runner: Any, docs_web: Any = None) -> list[ServerConfig]:
    """All MCP servers in-process. The docs server reads the fake documentation web in tests/docs_site.py."""
    from tests.docs_site import FakeDocsWeb, fake_docs_settings

    servers = [
        ServerConfig(
            "github",
            github_server.build_server(
                github_server.GitHubSettings(
                    mode="fixture",
                    repository=settings.github_repository,
                    issues_file=settings.github_issues_file,
                    repo_path=settings.repo_path,
                    default_branch=settings.github_default_branch,
                    branch_prefix=settings.github_branch_prefix,
                    pr_draft=settings.github_pr_draft,
                    writable_paths=settings.writable_paths,
                    state_dir=settings.data_dir,
                    author_name=settings.git_author_name,
                    author_email=settings.git_author_email,
                )
            ),
        ),
        ServerConfig("filesystem", filesystem_server.build_server(settings.repo_path, settings.writable_paths, settings.allow_new_files)),
        ServerConfig(
            "sandbox",
            sandbox_server.build_server(
                sandbox_server.SandboxSettings(repo_path=settings.repo_path, default_command=settings.test_command, allowlist=settings.test_command_allowlist),
                runner=runner,
            ),
        ),
    ]
    if settings.docs_enabled:
        web = docs_web or FakeDocsWeb()
        servers.append(ServerConfig("docs", docs_server.build_server(fake_docs_settings(web, settings.docs_cache_dir))))
    return servers


def inprocess_hub_factory(settings: Settings, runner: Any, audit: Any, docs_web: Any = None, servers: Any = None) -> Callable[[str], MCPHub]:
    """Same retry policy as production; ``servers`` replaces the server list (for fault injection)."""
    build = servers or (lambda: inprocess_servers(settings, runner, docs_web))
    return lambda run_id: MCPHub(build(), audit=audit, run_id=run_id, retry=tool_retry_policy(settings))


def api_issue_payload(settings: Settings, number: int = 42) -> dict[str, Any]:
    """An issue from the fixture file in the shape the GitHub REST API returns it."""
    import json

    issue = next(i for i in json.loads(Path(settings.github_issues_file).read_text())["issues"] if i["number"] == number)
    return {
        "number": number,
        "title": issue["title"],
        "state": issue.get("state", "open"),
        "body": issue["body"],
        "labels": [{"name": name} for name in issue.get("labels", [])],
        "comments": 0,
        "user": {"login": issue.get("author", "someone")},
        "html_url": f"https://github.com/{settings.github_repository}/issues/{number}",
    }


def api_github_servers(settings: Settings, runner: Any, handler: Callable[[Any], Any], *, token: str | None, web_url: str = "https://github.com") -> list[ServerConfig]:
    """All servers in-process, with the GitHub server in API mode against a mock GitHub (``handler``).

    ``web_url`` can be a ``file://`` directory holding ``<owner>/<name>.git`` bare repositories,
    which stands in for github.com when pushing.
    """
    import httpx2

    github = github_server.build_server(
        github_server.GitHubSettings(
            mode="api",
            repository=settings.github_repository,
            token=token,
            web_url=web_url,
            transport=httpx2.MockTransport(handler),
            repo_path=settings.repo_path,
            default_branch=settings.github_default_branch,
            branch_prefix=settings.github_branch_prefix,
            pr_draft=settings.github_pr_draft,
            writable_paths=settings.writable_paths,
            state_dir=settings.data_dir,
            author_name=settings.git_author_name,
            author_email=settings.git_author_email,
        )
    )
    return [ServerConfig("github", github) if c.name == "github" else c for c in inprocess_servers(settings, runner)]
