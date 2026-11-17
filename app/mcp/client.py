"""MCP client hub: connect to servers, discover tools, route and log calls.

The hub learns each tool's name, description, JSON schema and annotations
from ``tools/list`` at start-up. Nothing about GitHub, files or tests is
hardcoded here; the risk level of each tool is derived from the standard MCP
annotations (with the MCP specification's conservative defaults) and can be overridden.

Reliability:
- every call has a deadline (the server's ``timeout_seconds``); a timeout
  becomes a tool error that names the tool and the limit;
- errors a server marks ``[temporary]`` (and timeouts, where the server allows
  it) are retried with exponential backoff, but only for tools whose
  annotations say they are read-only or idempotent. Writes are never retried;
- every attempt is a row in the audit log, and every retry is an event.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment
from mcp.server import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import TextContent

from app.config import PROJECT_ROOT, Settings
from app.mcp.errors import parse_temporary
from app.mcp.security import redact
from app.reliability.retry import NO_RETRY, RetryPolicy

REQUEST_TIMEOUT = -32001  # MCP client error code when read_timeout_seconds expires

if TYPE_CHECKING:
    from app.storage.audit import AuditStore


class Risk(str, Enum):
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"
    DESTRUCTIVE = "destructive"


def classify_risk(annotations: dict[str, Any] | None) -> Risk:
    a = annotations or {}
    if a.get("read_only_hint") is True:
        return Risk.READ
    # MCP defaults for tools that are not read-only: destructive and open-world.
    destructive = True if a.get("destructive_hint") is None else a["destructive_hint"]
    open_world = True if a.get("open_world_hint") is None else a["open_world_hint"]
    if destructive:
        return Risk.DESTRUCTIVE
    if open_world:
        return Risk.EXTERNAL
    return Risk.WRITE


@dataclass(frozen=True)
class ToolSpec:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)
    risk: Risk = Risk.DESTRUCTIVE

    @property
    def safe_to_retry(self) -> bool:
        """Repeating the call cannot change anything twice (read-only or idempotent)."""
        return self.risk is Risk.READ or self.annotations.get("idempotent_hint") is True

    def describe(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "name": self.name,
            "risk": self.risk.value,
            "description": self.description,
            "annotations": self.annotations,
            "input_schema": self.input_schema,
        }


@dataclass
class ServerConfig:
    name: str
    target: StdioServerParameters | MCPServer | str  # stdio command, in-process server (tests) or URL
    timeout_seconds: float = 300.0  # deadline for each tool call
    retry_on_timeout: bool = True  # False where the server enforces its own, longer limit (sandbox)


@dataclass
class ToolOutcome:
    text: str
    is_error: bool
    duration_ms: int
    temporary: bool = False  # the failure may go away on its own
    timed_out: bool = False
    attempts: int = 1


def _result_text(result: Any) -> str:
    parts: list[str] = []
    for block in result.content or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{getattr(block, 'type', 'non-text')} content omitted]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, indent=2, default=str))
    return "\n".join(parts)


def _contains(group: BaseExceptionGroup, target: BaseException) -> bool:
    return any(e is target or (isinstance(e, BaseExceptionGroup) and _contains(e, target)) for e in group.exceptions)


class ToolNotFound(KeyError):
    pass


class MCPHub:
    def __init__(
        self,
        servers: list[ServerConfig],
        *,
        risk_overrides: dict[str, Risk] | None = None,
        audit: AuditStore | None = None,
        run_id: str | None = None,
        retry: RetryPolicy = NO_RETRY,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.server_configs = servers
        self.configs = {cfg.name: cfg for cfg in servers}
        self.risk_overrides = risk_overrides or {}
        self.audit = audit
        self.run_id = run_id
        self.retry = retry
        self.sleep = sleep
        self.tools: dict[str, ToolSpec] = {}
        self.instructions: dict[str, str] = {}
        self._clients: dict[str, Client] = {}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> MCPHub:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for cfg in self.server_configs:
                try:
                    await self._connect(cfg)
                except Exception as exc:  # noqa: BLE001 - name the failing server
                    raise RuntimeError(f"MCP server '{cfg.name}' failed to start: {exc}") from exc
        except BaseException as exc:
            stack, self._stack = self._stack, None
            try:
                await stack.__aexit__(type(exc), exc, exc.__traceback__)
            except BaseExceptionGroup as group:
                # In-process servers re-raise the startup error inside their task group;
                # callers get the clear error instead of the group.
                if _contains(group, exc):
                    raise exc from exc.__cause__
                raise
            raise
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(*exc)
            self._stack = None

    async def _connect(self, cfg: ServerConfig) -> None:
        client = await self._stack.enter_async_context(Client(cfg.target, read_timeout_seconds=cfg.timeout_seconds))  # type: ignore[union-attr]
        self._clients[cfg.name] = client
        self.instructions[cfg.name] = client.instructions or ""
        cursor: str | None = None
        while True:
            listing = await client.list_tools(cursor=cursor)
            for tool in listing.tools:
                annotations = tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
                spec = ToolSpec(cfg.name, tool.name, tool.description or "", tool.input_schema, annotations, classify_risk(annotations))
                if tool.name in self.risk_overrides:
                    spec = replace(spec, risk=self.risk_overrides[tool.name])
                if tool.name in self.tools:
                    raise RuntimeError(f"tool name {tool.name!r} is exposed by both {self.tools[tool.name].server} and {cfg.name}")
                self.tools[tool.name] = spec
            cursor = listing.next_cursor
            if not cursor:
                break

    def get(self, name: str) -> ToolSpec:
        try:
            return self.tools[name]
        except KeyError:
            raise ToolNotFound(f"no connected MCP server exposes a tool named {name!r}") from None

    def by_risk(self, *risks: Risk) -> list[ToolSpec]:
        return [spec for spec in self.tools.values() if spec.risk in risks]

    async def call(self, name: str, arguments: dict[str, Any], *, node: str | None = None, initiator: str = "workflow") -> ToolOutcome:
        spec = self.get(name)
        attempt = 1
        while True:
            outcome = await self._call_once(spec, arguments, node=node, initiator=initiator, attempt=attempt)
            if not (outcome.is_error and outcome.temporary and spec.safe_to_retry):
                return outcome
            _, retry_after = parse_temporary(outcome.text)
            delay = self.retry.next_delay(attempt, retry_after)
            if delay is None:
                if attempt > 1:
                    outcome.text += f"\n(gave up after {attempt} attempts)"
                return outcome
            if self.audit is not None and self.run_id is not None:
                self.audit.log_event(
                    self.run_id,
                    "tool_retry",
                    {"tool": spec.name, "node": node, "retry": attempt, "delay_s": round(delay, 3), "error": outcome.text.split("\n", 1)[0][:300]},
                )
            await self.sleep(delay)
            attempt += 1

    async def _call_once(self, spec: ToolSpec, arguments: dict[str, Any], *, node: str | None, initiator: str, attempt: int) -> ToolOutcome:
        client = self._clients[spec.server]
        cfg = self.configs[spec.server]
        start = time.monotonic()
        temporary = timed_out = False
        try:
            result = await client.call_tool(spec.name, arguments, read_timeout_seconds=cfg.timeout_seconds)
            text, is_error = _result_text(result), bool(result.is_error)
            if is_error:
                temporary, _ = parse_temporary(text)
        except MCPError as exc:
            if exc.error.code == REQUEST_TIMEOUT:
                timed_out, temporary = True, cfg.retry_on_timeout
                text = f"{spec.name} timed out after {cfg.timeout_seconds:g}s (the per-call limit for the {spec.server} server); no result was received."
            else:
                text = f"MCP error calling {spec.server}.{spec.name}: {exc}"
            is_error = True
        except Exception as exc:  # noqa: BLE001 - transport/protocol failures become tool errors
            text, is_error = f"MCP error calling {spec.server}.{spec.name}: {type(exc).__name__}: {exc}", True
        outcome = ToolOutcome(redact(text), is_error, int((time.monotonic() - start) * 1000), temporary, timed_out, attempt)
        if self.audit is not None and self.run_id is not None:
            self.audit.log_tool_call(
                self.run_id,
                server=spec.server,
                tool=spec.name,
                arguments=arguments,
                result_text=outcome.text,
                is_error=outcome.is_error,
                duration_ms=outcome.duration_ms,
                node=node,
                initiator=initiator,
                risk=spec.risk.value,
                attempt=attempt,
            )
        return outcome


def tool_retry_policy(settings: Settings) -> RetryPolicy:
    return RetryPolicy(max_retries=settings.tool_max_retries, base_delay=settings.retry_base_delay_seconds, max_delay=settings.retry_max_delay_seconds)


def stdio_server_configs(settings: Settings) -> list[ServerConfig]:
    """Launch each server as its own process with a minimal environment.

    Only the GitHub server receives GITHUB_TOKEN; no server receives the
    Anthropic API key. The docs server gets no credentials at all
    and ignores proxy and .netrc settings.
    """
    base_env = {**get_default_environment(), "PYTHONPATH": str(PROJECT_ROOT), "PYTHONUNBUFFERED": "1"}
    python = sys.executable
    repo = str(settings.repo_path)

    github_args = [
        "-m", "app.mcp.github_server",
        "--mode", settings.github_mode,
        "--repository", settings.github_repository,
        "--api-url", settings.github_api_url,
        "--web-url", settings.github_web_url,
        "--repo-path", repo,
        "--default-branch", settings.github_default_branch,
        "--branch-prefix", settings.github_branch_prefix,
        "--writable", ",".join(settings.writable_paths),
        "--state-dir", str(settings.data_dir),
        "--author-name", settings.git_author_name,
        "--author-email", settings.git_author_email,
    ]
    if settings.github_issues_file:
        github_args += ["--issues-file", str(settings.github_issues_file)]
    if not settings.github_pr_draft:
        github_args.append("--no-draft")
    github_env = dict(base_env)
    if settings.github_token:
        github_env["GITHUB_TOKEN"] = settings.github_token

    fs_args = ["-m", "app.mcp.filesystem_server", "--repo-path", repo, "--writable", ",".join(settings.writable_paths)]
    if not settings.allow_new_files:
        fs_args.append("--no-new-files")

    sandbox_cfg = {
        "default_command": settings.test_command,
        "allowlist": list(settings.test_command_allowlist),
        "image": settings.sandbox_image,
        "timeout_seconds": settings.sandbox_timeout_seconds,
        "memory_mb": settings.sandbox_memory_mb,
        "cpus": settings.sandbox_cpus,
        "pids": settings.sandbox_pids,
        "work_dir": str(settings.sandbox_work_dir) if settings.sandbox_work_dir else None,
    }
    sandbox_env = dict(base_env)
    for var in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
        if os.environ.get(var):
            sandbox_env[var] = os.environ[var]

    def params(args: list[str], env: dict[str, str]) -> StdioServerParameters:
        return StdioServerParameters(command=python, args=args, env=env, cwd=str(PROJECT_ROOT))

    configs = [
        ServerConfig("github", params(github_args, github_env), timeout_seconds=settings.tool_timeout_seconds),
        ServerConfig("filesystem", params(fs_args, base_env), timeout_seconds=settings.tool_timeout_seconds),
        # The sandbox kills the test container at SANDBOX_TIMEOUT_SECONDS itself; the hub deadline
        # only catches a stuck server, and a stuck test run is not repeated automatically.
        ServerConfig("sandbox", params(["-m", "app.mcp.sandbox_server", "--repo-path", repo, "--config-json", json.dumps(sandbox_cfg)], sandbox_env),
                     timeout_seconds=settings.sandbox_timeout_seconds + 120, retry_on_timeout=False),
    ]
    if settings.docs_enabled:
        docs_cfg = {
            "timeout_seconds": settings.docs_timeout_seconds,
            "max_page_bytes": settings.docs_max_page_bytes,
            "max_fetches": settings.docs_max_fetches,
            "respect_robots": settings.docs_respect_robots,
            "cache_dir": str(settings.docs_cache_dir),
            "cache_ttl_seconds": settings.docs_cache_ttl_seconds,
        }
        docs_args = ["-m", "app.mcp.docs_server", "--sources-file", str(settings.docs_sources_file), "--config-json", json.dumps(docs_cfg)]
        # a few requests per call at most (robots.txt, redirects, an index download)
        configs.append(ServerConfig("docs", params(docs_args, base_env), timeout_seconds=settings.docs_timeout_seconds * 6 + 30))
    return configs
