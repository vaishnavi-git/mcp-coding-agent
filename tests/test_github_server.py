"""GitHub MCP server: fixture mode and REST API mode against a mock transport (including failures)."""

from __future__ import annotations

import httpx2
import pytest
from mcp import Client

from app.config import PROJECT_ROOT
from app.mcp.github_server import GitHubSettings, build_server

pytestmark = pytest.mark.anyio

FIXTURE = PROJECT_ROOT / "examples" / "issues" / "sample-repo.json"


def text(result) -> str:
    return "\n".join(block.text for block in result.content)


def api_server(handler) -> object:
    return build_server(
        GitHubSettings(mode="api", repository="octo/demo", token="ghp_" + "x" * 36, transport=httpx2.MockTransport(handler))
    )


async def test_fixture_issue() -> None:
    async with Client(build_server(GitHubSettings(mode="fixture", issues_file=FIXTURE))) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"get_github_issue", "create_branch", "commit_changes", "push_branch", "create_pull_request"}
        issue_tool = tools["get_github_issue"]
        assert issue_tool.annotations.read_only_hint is True and issue_tool.annotations.open_world_hint is True
        body = text(await client.call_tool("get_github_issue", {"issue_number": 42}))
        assert body.startswith("# Issue #42:")
        assert "test_refresh_rotates_refresh_token" in body
        assert "## Comments (1)" in body
        missing = await client.call_tool("get_github_issue", {"issue_number": 999})
        assert missing.is_error and "not found" in text(missing)
        invalid = await client.call_tool("get_github_issue", {"issue_number": "abc"})
        assert invalid.is_error


async def test_api_success_sends_token_and_reads_comments() -> None:
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path.endswith("/issues/7"):
            return httpx2.Response(200, json={"number": 7, "title": "Crash", "state": "open", "body": "It crashes", "labels": [{"name": "bug"}], "comments": 1, "html_url": "https://github.com/octo/demo/issues/7"})
        if request.url.path.endswith("/issues/7/comments"):
            return httpx2.Response(200, json=[{"user": {"login": "maint"}, "body": "confirmed"}])
        return httpx2.Response(404, json={"message": "Not Found"})

    async with Client(api_server(handler)) as client:
        result = await client.call_tool("get_github_issue", {"issue_number": 7})
        body = text(result)
        assert not result.is_error
        assert "# Issue #7: Crash" in body and "maint: confirmed" in body
        assert "ghp_" not in body
    assert seen[0].headers["authorization"].startswith("Bearer ghp_")
    assert seen[0].url.path == "/repos/octo/demo/issues/7"


@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx2.Response(404, json={"message": "Not Found"}), "Not found on octo/demo"),
        (httpx2.Response(401, json={"message": "Bad credentials"}), "rejected the token"),
        (httpx2.Response(403, json={"message": "API rate limit exceeded"}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"}), "rate limit exceeded"),
        (httpx2.Response(403, json={"message": "Resource not accessible by integration"}), "denied access"),
        (httpx2.Response(502, text="bad gateway"), "server error"),
    ],
)
async def test_api_failures_have_clear_messages(response: httpx2.Response, expected: str) -> None:
    async with Client(api_server(lambda request: response)) as client:
        result = await client.call_tool("get_github_issue", {"issue_number": 1})
        assert result.is_error
        assert expected in text(result)
        assert "ghp_" not in text(result)


@pytest.mark.parametrize("exc, expected", [(httpx2.ConnectError("refused"), "Could not reach"), (httpx2.ReadTimeout("slow"), "timed out")])
async def test_api_network_failures(exc: Exception, expected: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc

    async with Client(api_server(handler)) as client:
        result = await client.call_tool("get_github_issue", {"issue_number": 1})
        assert result.is_error and expected in text(result)
