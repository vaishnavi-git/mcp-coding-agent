"""Pull-request creation end to end in API mode.

test_publication_workflow.py publishes in fixture mode (a local bare remote and pull
requests stored as JSON). Here the GitHub server runs in API mode: the branch
is pushed with real git to a local bare repository standing in for
github.com, and the pull request goes to a mock GitHub API, so the exact
request GitHub would receive can be checked.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx2
import pytest

from app.mcp.client import MCPHub
from app.mcp.git_utils import run_git
from tests.fakes import DEFAULT_PUBLICATION, FakeRunner, api_github_servers, api_issue_payload, inprocess_servers
from tests.test_agent_workflow import FULL_FIX
from tests.test_reliability_workflow import make_runner, scripted

pytestmark = pytest.mark.anyio

TOKEN = "ghp_" + "p" * 36
BRANCH = "agent/issue-42-rotate-refresh-tokens"


class FakeGitHub:
    """Mock GitHub API: serves issue #42 and records pull-request requests."""

    def __init__(self, settings, pr_response: httpx2.Response | None = None) -> None:
        self.settings = settings
        self.requests: list[httpx2.Request] = []
        self.pr_response = pr_response

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        repo = f"/repos/{self.settings.github_repository}"
        if request.method == "GET" and request.url.path == f"{repo}/issues/42":
            return httpx2.Response(200, json=api_issue_payload(self.settings))
        if request.method == "POST" and request.url.path == f"{repo}/pulls":
            if self.pr_response is not None:
                return self.pr_response
            sent = json.loads(request.content)
            return httpx2.Response(201, json={"number": 7, "html_url": f"https://github.com/{self.settings.github_repository}/pull/7", "title": sent["title"], "draft": sent["draft"]})
        return httpx2.Response(404, json={"message": "Not Found"})

    @property
    def pull_requests(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


def bare_remote(settings, tmp_path: Path) -> tuple[str, Path]:
    """<web>/<owner>/<name>.git, which the API backend pushes to as if it were github.com."""
    web = tmp_path / "web"
    remote = web / f"{settings.github_repository}.git"
    remote.parent.mkdir(parents=True)
    run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    return f"file://{web}", remote


async def to_publication(settings, tmp_path: Path, pr_response: httpx2.Response | None = None):
    settings = dataclasses.replace(settings, github_mode="api", github_token=TOKEN)
    web_url, remote = bare_remote(settings, tmp_path)
    github = FakeGitHub(settings, pr_response)
    runner, audit = make_runner(settings, scripted(), servers=lambda: api_github_servers(settings, FakeRunner(), github, token=TOKEN, web_url=web_url))
    view = await runner.start(42)
    view = await runner.resume(view["run"]["id"], approved=True)  # patches approved, tests pass
    assert view["pending_approval"]["kind"] == "publish_approval", view["state"].get("error")
    return settings, runner, audit, view, github, remote


def stored_text(settings) -> str:
    """Everything the audit database holds, as text (including a write-ahead log, if any)."""
    db = Path(settings.database_path)
    return "".join(p.read_bytes().decode("utf-8", errors="ignore") for p in db.parent.glob(db.name + "*"))


async def test_api_mode_pull_request_end_to_end(settings, tmp_path: Path) -> None:
    settings, runner, audit, view, github, remote = await to_publication(settings, tmp_path)
    pending = view["pending_approval"]
    assert github.pull_requests == [] and run_git(remote, "for-each-ref", "refs/heads").strip() == ""  # nothing before approval

    view = await runner.resume(view["run"]["id"], approved=True)
    assert view["run"]["status"] == "published", view["state"].get("error")
    publication = view["state"]["publication"]
    assert publication["status"] == "pr_created" and publication["pull_request"]["url"].endswith("/pull/7")

    # Exactly one pull request, with what the reviewer approved.
    assert len(github.pull_requests) == 1
    pr = github.pull_requests[0]
    assert pr == {"title": DEFAULT_PUBLICATION["pr_title"], "body": pending["pr_body"], "head": BRANCH, "base": "main", "draft": True}
    for expected in ("Fixes #42", "## Tests", "PASSED", "`authkit/sessions.py`", "`tests/test_sessions.py`", DEFAULT_PUBLICATION["risks"][0]):
        assert expected in pr["body"], expected
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in github.requests)

    # The branch reached the remote with the reviewed commit; the default branch was never pushed.
    assert run_git(remote, "for-each-ref", "--format=%(refname)", "refs/heads").split() == [f"refs/heads/{BRANCH}"]
    assert "#42" in run_git(remote, "log", "-1", "--format=%B", BRANCH)
    assert sorted(run_git(remote, "show", "--name-only", "--format=", BRANCH).split()) == ["authkit/sessions.py", "tests/test_sessions.py"]
    order = [c["tool"] for c in audit.tool_calls(view["run"]["id"]) if c["tool"] in ("create_branch", "commit_changes", "push_branch", "create_pull_request")]
    assert order == ["create_branch", "commit_changes", "push_branch", "create_pull_request"]
    # The token never reaches git's config, the run view or the audit database.
    assert TOKEN not in (settings.repo_path / ".git" / "config").read_text()
    assert TOKEN not in json.dumps(view) and TOKEN not in stored_text(settings)


async def test_a_pull_request_rejected_after_the_push_reports_what_was_done(settings, tmp_path: Path) -> None:
    rejected = httpx2.Response(422, json={"message": "Validation Failed", "errors": [{"code": "custom", "message": "No commits between main and " + BRANCH}]})
    settings, runner, audit, view, github, remote = await to_publication(settings, tmp_path, pr_response=rejected)
    view = await runner.resume(view["run"]["id"], approved=True)
    assert view["run"]["status"] == "publish_failed"
    failure = view["failure"]
    assert failure["code"] == "publish_failed" and not failure["retryable"]
    assert "create_pull_request failed" in failure["message"] and "No commits between" in failure["message"]
    assert "completed before the failure: create_branch, commit_changes, push_branch" in failure["message"]
    assert [(s["tool"], s["ok"]) for s in view["state"]["publication"]["steps"]] == [
        ("create_branch", True), ("commit_changes", True), ("push_branch", True), ("create_pull_request", False)
    ]
    assert len(github.pull_requests) == 1  # not retried
    assert run_git(remote, "for-each-ref", "--format=%(refname)", "refs/heads").split() == [f"refs/heads/{BRANCH}"]  # the push stays


async def test_a_second_pull_request_for_the_same_branch_is_refused(settings) -> None:
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        assert not (await hub.call("apply_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX})).is_error
        for tool, args in [("create_branch", {"branch_name": "agent/once"}), ("commit_changes", {"message": "Fix #42"}), ("push_branch", {})]:
            assert not (await hub.call(tool, args)).is_error, tool
        first = await hub.call("create_pull_request", {"title": "Fix", "body": "Fixes #42"})
        second = await hub.call("create_pull_request", {"title": "Fix again", "body": "Fixes #42"})
    assert not first.is_error
    assert second.is_error and "A pull request already exists for 'agent/once'" in second.text
    assert len(list((settings.data_dir / "pull_requests").glob("*.json"))) == 1


async def test_no_pull_request_from_the_default_branch_or_an_unpushed_branch(settings) -> None:
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        on_main = await hub.call("create_pull_request", {"title": "Fix", "body": "b"})
        await hub.call("create_branch", {"branch_name": "agent/not-pushed"})
        unpushed = await hub.call("create_pull_request", {"title": "Fix", "body": "b"})
    assert on_main.is_error and "refusing to open a pull request on the default branch 'main'" in on_main.text
    assert unpushed.is_error and "is not on the remote yet; push it before opening a pull request" in unpushed.text
    assert not (settings.data_dir / "pull_requests").exists()
