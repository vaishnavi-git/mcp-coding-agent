"""GitHub API failures, from odd HTTP responses up to a GitHub outage in the middle of a run.

test_github_server.py and test_github_publish.py cover the common status codes
(401, 403, 404, 422, 429, 5xx, network errors). These add the responses that
used to surface as Python exceptions (a non-JSON body, a redirect, an
unexpected payload), check which failures are marked temporary, and run the
workflow against a GitHub that is down and then comes back.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx2
import pytest

from app.mcp.client import MCPHub, ServerConfig
from app.mcp.errors import parse_temporary
from app.mcp.github_server import GitHubSettings, build_server
from app.reliability.retry import RetryPolicy
from tests.fakes import FakeRunner, api_github_servers, api_issue_payload
from tests.test_reliability_workflow import make_runner, scripted

pytestmark = pytest.mark.anyio

TOKEN = "ghp_" + "s" * 36
POLICY = RetryPolicy(max_retries=2, base_delay=1, max_delay=30, jitter=0)
ISSUE = {"number": 1, "title": "Bug", "state": "open", "body": "b", "labels": [], "comments": 0}


def hub_for(handler, *, repo: Path | None = None, retry: RetryPolicy = POLICY) -> tuple[MCPHub, list[float]]:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    server = build_server(GitHubSettings(mode="api", repository="octo/demo", token=TOKEN, transport=httpx2.MockTransport(handler), repo_path=repo))
    return MCPHub([ServerConfig("github", server)], retry=retry, sleep=sleep), sleeps


@pytest.mark.parametrize(
    "response, expected, temporary",
    [
        (httpx2.Response(200, text="<html>Sign in to the proxy</html>", headers={"content-type": "text/html; charset=utf-8"}), "not JSON (200, text/html)", False),
        (httpx2.Response(301, json={"message": "Moved Permanently"}, headers={"location": "https://api.github.com/repositories/99/issues/1"}), "probably renamed or transferred", False),
        (httpx2.Response(307, text=""), "Update GITHUB_REPOSITORY", False),
        (httpx2.Response(200, json=[]), "unexpected response", False),
        (httpx2.Response(200, json={"message": "ok"}), "unexpected response", False),
        (httpx2.Response(410, json={"message": "Issues are disabled for this repository"}), "Issues are disabled", False),
        (httpx2.Response(422, json={"message": "Validation Failed", "errors": [{"code": "invalid"}]}), "(422)", False),
        (httpx2.Response(403, json={"message": "You have exceeded a secondary rate limit. Please wait a few minutes."}, headers={"retry-after": "60"}), "rate limit exceeded; GitHub asks to wait 60s", True),
        (httpx2.Response(429, json={"message": "Too many requests"}), "rate limit exceeded; try again in a few minutes", True),
        (httpx2.Response(500, text="boom"), "server error (500)", True),
        (httpx2.Response(503, json={"message": "unavailable"}), "server error (503)", True),
    ],
)
async def test_unusual_responses_become_clear_errors(response: httpx2.Response, expected: str, temporary: bool) -> None:
    hub, _ = hub_for(lambda request: response, retry=RetryPolicy(max_retries=0))
    async with hub:
        outcome = await hub.call("get_github_issue", {"issue_number": 1})
    assert outcome.is_error and expected in outcome.text, outcome.text
    assert "Traceback" not in outcome.text and "JSONDecodeError" not in outcome.text and "KeyError" not in outcome.text
    assert parse_temporary(outcome.text)[0] is temporary
    assert TOKEN not in outcome.text


async def test_secondary_rate_limits_are_waited_out(tmp_path: Path) -> None:
    responses = [
        httpx2.Response(403, json={"message": "You have exceeded a secondary rate limit."}, headers={"retry-after": "4"}),
        httpx2.Response(200, json=ISSUE),
    ]
    hub, sleeps = hub_for(lambda request: responses.pop(0))
    async with hub:
        outcome = await hub.call("get_github_issue", {"issue_number": 1})
    assert not outcome.is_error and outcome.attempts == 2 and sleeps == [4]


@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx2.Response(201, text="created", headers={"content-type": "text/plain"}), "not JSON (201, text/plain)"),
        (httpx2.Response(201, json={}), "check the repository's pull requests before retrying"),
        (httpx2.Response(502, text="bad gateway"), "server error (502)"),
        (httpx2.Response(301, headers={"location": "https://api.github.com/repositories/99/pulls"}), "renamed or transferred"),
    ],
)
async def test_pull_request_failures_are_never_retried(sample_repo: Path, response: httpx2.Response, expected: str) -> None:
    posts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        posts.append(request)
        return response

    hub, sleeps = hub_for(handler, repo=sample_repo)
    async with hub:
        await hub.call("create_branch", {"branch_name": "agent/pr-failure"})
        outcome = await hub.call("create_pull_request", {"title": "Fix", "body": "Fixes #1"})
    assert outcome.is_error and expected in outcome.text, outcome.text
    # A retried POST could open a second pull request: whatever the error, exactly one request is sent.
    assert len(posts) == 1 and sleeps == [] and outcome.attempts == 1
    assert TOKEN not in outcome.text


async def test_github_outage_fails_the_run_cleanly_and_a_retry_continues(settings) -> None:
    settings = dataclasses.replace(settings, github_mode="api", github_token=TOKEN)
    github = {"down": True, "requests": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        github["requests"] += 1
        if github["down"]:
            return httpx2.Response(502, text="<html>502 Bad Gateway</html>", headers={"content-type": "text/html"})
        assert request.url.path == f"/repos/{settings.github_repository}/issues/42"
        return httpx2.Response(200, json=api_issue_payload(settings))

    provider = scripted()
    runner, audit = make_runner(settings, provider, servers=lambda: api_github_servers(settings, FakeRunner(), handler, token=TOKEN))
    view = await runner.start(42)
    run_id = view["run"]["id"]
    assert view["run"]["status"] == "failed"
    failure = view["failure"]
    assert failure["code"] == "issue_unavailable" and failure["step"] == "fetch_issue" and failure["retryable"]
    assert "server error (502)" in failure["message"] and "gave up after 3 attempts" in failure["message"]
    assert github["requests"] == 3  # TOOL_MAX_RETRIES=2
    assert provider.calls == []  # no plan without the issue: nothing was sent to the model
    assert view["retry"]["available"] and view["retry"]["from_step"] == "fetch_issue"
    assert TOKEN not in json.dumps(view)

    github["down"] = False
    view = await runner.retry(run_id)
    assert view["run"]["status"] == "awaiting_approval" and view["pending_approval"]["kind"] == "patch_approval"
    assert view["state"]["issue"].startswith("# Issue #42: Refreshing a session twice") and provider.calls.count("plan") == 1
    assert github["requests"] == 4
