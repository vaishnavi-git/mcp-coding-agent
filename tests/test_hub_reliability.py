"""MCP hub reliability: per-call timeouts, and retries only where repeating a call is safe."""

from __future__ import annotations

import anyio
import httpx2
import pytest
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from app.mcp.client import MCPHub, ServerConfig
from app.mcp.errors import parse_temporary, temporary_error
from app.mcp.github_server import GitHubSettings
from app.mcp.github_server import build_server as build_github
from app.reliability.retry import RetryPolicy
from app.storage.audit import AuditStore
from tests.docs_site import FakeDocsWeb, fake_docs_settings

pytestmark = pytest.mark.anyio

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
IDEMPOTENT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
POLICY = RetryPolicy(max_retries=2, base_delay=1, max_delay=30, jitter=0)


def flaky_server() -> tuple[MCPServer, dict[str, int]]:
    """Tools that fail a set number of times before succeeding (counts per tool)."""
    server = MCPServer("flaky")
    calls: dict[str, int] = {}
    plan = {"read_flaky": 2, "write_flaky": 2, "idempotent_flaky": 1, "read_permanent": 99, "read_slow_once": 1, "read_rate_limited": 99}

    def failing(name: str) -> bool:
        calls[name] = calls.get(name, 0) + 1
        return calls[name] <= plan[name]

    @server.tool(annotations=READ)
    def read_flaky() -> str:
        if failing("read_flaky"):
            raise temporary_error("upstream returned 503")
        return "read ok"

    @server.tool(annotations=WRITE)
    def write_flaky() -> str:
        if failing("write_flaky"):
            raise temporary_error("upstream returned 503")
        return "written"

    @server.tool(annotations=IDEMPOTENT)
    def idempotent_flaky() -> str:
        if failing("idempotent_flaky"):
            raise temporary_error("daemon busy")
        return "done"

    @server.tool(annotations=READ)
    def read_permanent() -> str:
        failing("read_permanent")
        raise ValueError("not found")

    @server.tool(annotations=READ)
    def read_rate_limited() -> str:
        failing("read_rate_limited")
        raise temporary_error("rate limited until the top of the hour", retry_after=1800)

    @server.tool(annotations=READ)
    async def read_slow_once() -> str:
        if failing("read_slow_once"):
            await anyio.sleep(5)
        return "fast now"

    @server.tool(annotations=READ)
    async def read_slow() -> str:
        await anyio.sleep(5)
        return "never"

    return server, calls


def make_hub(tmp_path, *, timeout: float = 10, retry_on_timeout: bool = True) -> tuple[MCPHub, AuditStore, list[float], dict[str, int]]:
    server, calls = flaky_server()
    audit = AuditStore(tmp_path / "agent.db")
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    hub = MCPHub([ServerConfig("flaky", server, timeout_seconds=timeout, retry_on_timeout=retry_on_timeout)], audit=audit, run_id="run-1", retry=POLICY, sleep=sleep)
    return hub, audit, sleeps, calls


def test_temporary_marker_parsing() -> None:
    assert parse_temporary("Error executing tool x: [temporary] upstream 503") == (True, None)
    assert parse_temporary("Error executing tool x: [temporary, retry after 7s] slow down") == (True, 7.0)
    assert parse_temporary("not found\n[temporary] on a later line does not count") == (False, None)


async def test_temporary_errors_on_read_only_tools_are_retried_with_backoff(tmp_path) -> None:
    hub, audit, sleeps, calls = make_hub(tmp_path)
    async with hub:
        outcome = await hub.call("read_flaky", {}, node="investigate", initiator="llm")
    assert not outcome.is_error and outcome.text == "read ok" and outcome.attempts == 3
    assert sleeps == [1, 2] and calls["read_flaky"] == 3
    rows = audit.tool_calls("run-1")
    assert [(r["attempt"], r["is_error"]) for r in rows] == [(1, 1), (2, 1), (3, 0)]
    retries = [e for e in audit.events("run-1") if e["kind"] == "tool_retry"]
    assert len(retries) == 2 and '"tool": "read_flaky"' in retries[0]["detail"]


async def test_idempotent_tools_are_retried_but_writes_never_are(tmp_path) -> None:
    hub, _, sleeps, calls = make_hub(tmp_path)
    async with hub:
        assert (await hub.call("idempotent_flaky", {})).text == "done"
        write = await hub.call("write_flaky", {})
    assert write.is_error and write.temporary and write.attempts == 1 and calls["write_flaky"] == 1
    assert "[temporary]" in write.text  # the reason stays visible
    assert sleeps == [1]


async def test_permanent_errors_and_long_waits_are_not_retried(tmp_path) -> None:
    hub, _, sleeps, calls = make_hub(tmp_path)
    async with hub:
        permanent = await hub.call("read_permanent", {})
        limited = await hub.call("read_rate_limited", {})
    assert permanent.is_error and not permanent.temporary and calls["read_permanent"] == 1
    assert limited.is_error and calls["read_rate_limited"] == 1 and sleeps == []  # 30 min is longer than RETRY_MAX_DELAY


async def test_retries_are_limited(tmp_path) -> None:
    hub, _, sleeps, calls = make_hub(tmp_path)
    hub.retry = RetryPolicy(max_retries=1, base_delay=1, jitter=0)
    async with hub:
        outcome = await hub.call("read_flaky", {})
    assert outcome.is_error and outcome.attempts == 2 and "(gave up after 2 attempts)" in outcome.text
    assert calls["read_flaky"] == 2 and sleeps == [1]


async def test_tool_timeout_is_clear_and_the_session_keeps_working(tmp_path) -> None:
    hub, audit, sleeps, _ = make_hub(tmp_path, timeout=0.3, retry_on_timeout=False)
    async with hub:
        outcome = await hub.call("read_slow", {}, node="investigate")
        assert outcome.is_error and outcome.timed_out and outcome.attempts == 1 and sleeps == []
        assert outcome.text.startswith("read_slow timed out after 0.3s (the per-call limit for the flaky server)")
        assert outcome.duration_ms < 2000
        assert (await hub.call("read_flaky", {})).text == "read ok"  # same session, still usable
    assert audit.tool_calls("run-1")[0]["is_error"] == 1


async def test_timeouts_are_retried_where_the_server_allows_it(tmp_path) -> None:
    hub, _, sleeps, calls = make_hub(tmp_path, timeout=0.3, retry_on_timeout=True)
    async with hub:
        outcome = await hub.call("read_slow_once", {})
    assert not outcome.is_error and outcome.text == "fast now" and outcome.attempts == 2 and sleeps == [1]


# ---------------------------------------------------------------------- servers mark temporary errors
def github_hub(handler, tmp_path) -> tuple[MCPHub, list[float]]:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    server = build_github(GitHubSettings(mode="api", repository="octo/demo", token="ghp_" + "x" * 36, transport=httpx2.MockTransport(handler)))
    return MCPHub([ServerConfig("github", server)], retry=POLICY, sleep=sleep), sleeps


async def test_github_server_errors_are_retried_only_when_temporary(tmp_path) -> None:
    responses = [
        httpx2.Response(502, json={"message": "Bad gateway"}),
        httpx2.Response(429, json={"message": "API rate limit exceeded"}, headers={"retry-after": "7"}),
        httpx2.Response(200, json={"number": 5, "title": "Bug", "state": "open", "body": "b", "labels": [], "comments": 0}),
    ]
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return responses.pop(0)

    hub, sleeps = github_hub(handler, tmp_path)
    async with hub:
        outcome = await hub.call("get_github_issue", {"issue_number": 5})
    assert not outcome.is_error and "Bug" in outcome.text and outcome.attempts == 3
    assert sleeps == [1, 7]  # backoff, then GitHub's Retry-After

    hub, sleeps = github_hub(lambda request: httpx2.Response(404, json={"message": "Not Found"}), tmp_path)
    async with hub:
        missing = await hub.call("get_github_issue", {"issue_number": 5})
    assert missing.is_error and not missing.temporary and "Not found on octo/demo" in missing.text and sleeps == []


async def test_github_write_tools_are_never_retry_candidates(tmp_path) -> None:
    hub, _ = github_hub(lambda request: httpx2.Response(503, json={"message": "unavailable"}), tmp_path)
    async with hub:
        unsafe = {name for name, spec in hub.tools.items() if not spec.safe_to_retry}
    # a retried timeout could create a second branch, commit or pull request
    assert unsafe == {"create_branch", "commit_changes", "push_branch", "create_pull_request"}


async def test_docs_server_transient_failures_are_retried(tmp_path) -> None:
    from app.mcp.docs_server import build_server as build_docs

    web = FakeDocsWeb()
    page = "https://docs.authkit.test/en/tokens.html"
    real = web.handle
    failures = {"left": 1}

    def handle(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == page and failures["left"]:
            failures["left"] -= 1
            web.requests.append(request)
            return httpx2.Response(503, text="busy", headers={"content-type": "text/html"})
        return real(request)

    web.handle = handle  # type: ignore[method-assign]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    hub = MCPHub([ServerConfig("docs", build_docs(fake_docs_settings(web, tmp_path / "cache")))], retry=POLICY, sleep=sleep)
    async with hub:
        read = await hub.call("read_documentation", {"url": page})
        missing = await hub.call("read_documentation", {"url": "https://docs.authkit.test/en/missing.html"})
    assert not read.is_error and "Refresh token rotation" in read.text and read.attempts == 2 and sleeps == [1]
    assert missing.is_error and not missing.temporary and missing.attempts == 1
