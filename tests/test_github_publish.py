"""Phase 2 GitHub tools: branch, commit, push (never forced), pull request; fixture and API modes."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import httpx2
import pytest
from mcp import Client

from app.config import PROJECT_ROOT
from app.mcp import git_utils
from app.mcp.git_utils import run_git
from app.mcp.github_server import GitHubSettings, build_server, push_error_message
from app.sample_repo import origin_path

pytestmark = pytest.mark.anyio

FIXTURE = PROJECT_ROOT / "examples" / "issues" / "sample-repo.json"
TOKEN = "ghp_" + "t" * 36


def text(result) -> str:
    return "\n".join(block.text for block in result.content)


def fixture_server(repo: Path, tmp_path: Path, **overrides) -> object:
    settings = GitHubSettings(
        mode="fixture",
        issues_file=FIXTURE,
        repo_path=repo,
        writable_paths=("authkit", "tests"),
        state_dir=tmp_path / "data",
        author_email="agent@example.invalid",
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return build_server(settings)


def edit_source(repo: Path) -> None:
    path = repo / "authkit" / "sessions.py"
    path.write_text(path.read_text().replace("        return new_session\n", "        self._by_refresh[new_session.refresh_token] = new_session\n        return new_session\n", 1))


def remote_heads(repo: Path) -> dict[str, str]:
    out = run_git(repo, "ls-remote", "--heads", "origin")
    return {line.split("\t")[1].removeprefix("refs/heads/"): line.split("\t")[0] for line in out.splitlines()}


async def test_tool_annotations(sample_repo: Path, tmp_path: Path) -> None:
    async with Client(fixture_server(sample_repo, tmp_path)) as client:
        tools = {t.name: t.annotations for t in (await client.list_tools()).tools}
    for name in ("create_branch", "commit_changes"):
        assert tools[name].read_only_hint is False and tools[name].destructive_hint is False and tools[name].open_world_hint is False
    for name in ("push_branch", "create_pull_request"):
        assert tools[name].read_only_hint is False and tools[name].destructive_hint is False and tools[name].open_world_hint is True


@pytest.mark.parametrize(
    "name, reason",
    [
        ("fix/issue-42", "must start with 'agent/'"),
        ("main", "must start with 'agent/'"),
        ("agent/../escape", "invalid branch name"),
        ("agent/has space", "invalid branch name"),
        ("agent/x.lock", "invalid branch name"),
        ("agent/trailing/", "invalid branch name"),
        ("agent/a~1", "invalid branch name"),
        ("agent/a:b", "invalid branch name"),
        ("agent/@{x}", "invalid branch name"),
    ],
)
async def test_invalid_branch_names(sample_repo: Path, tmp_path: Path, name: str, reason: str) -> None:
    async with Client(fixture_server(sample_repo, tmp_path)) as client:
        result = await client.call_tool("create_branch", {"branch_name": name})
        assert result.is_error and reason in text(result)
    assert run_git(sample_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


async def test_create_branch_and_duplicates(sample_repo: Path, tmp_path: Path) -> None:
    async with Client(fixture_server(sample_repo, tmp_path)) as client:
        created = await client.call_tool("create_branch", {"branch_name": "agent/issue-42-fix"})
        assert not created.is_error and json.loads(text(created)) == {"created": "agent/issue-42-fix", "from": "main"}
        run_git(sample_repo, "switch", "main")
        again = await client.call_tool("create_branch", {"branch_name": "agent/issue-42-fix"})
        assert again.is_error and "already exists" in text(again)
    async with Client(fixture_server(sample_repo, tmp_path, branch_prefix="")) as client:
        default = await client.call_tool("create_branch", {"branch_name": "main"})
        assert default.is_error and "default branch" in text(default)


async def test_commit_rules_and_file_filtering(sample_repo: Path, tmp_path: Path) -> None:
    async with Client(fixture_server(sample_repo, tmp_path)) as client:
        edit_source(sample_repo)
        on_main = await client.call_tool("commit_changes", {"message": "Fix"})
        assert on_main.is_error and "default branch" in text(on_main)

        await client.call_tool("create_branch", {"branch_name": "agent/commit-test"})
        (sample_repo / ".env").write_text("SECRET=1\n")  # forbidden, and not gitignored
        (sample_repo / "README.md").write_text("changed outside the writable paths\n")
        committed = await client.call_tool("commit_changes", {"message": "Rotate refresh tokens (#42)"})
        assert not committed.is_error, text(committed)
        data = json.loads(text(committed))
        assert data["files"] == ["authkit/sessions.py"]
        assert {s["path"] for s in data["skipped"]} == {".env", "README.md"}
        assert run_git(sample_repo, "show", "--name-only", "--format=", "HEAD").split() == ["authkit/sessions.py"]
        assert run_git(sample_repo, "log", "-1", "--format=%an <%ae>").strip() == "MCP Coding Agent <agent@example.invalid>"

        nothing = await client.call_tool("commit_changes", {"message": "Again"})
        assert nothing.is_error and "nothing to commit" in text(nothing)
        empty = await client.call_tool("commit_changes", {"message": "   "})
        assert empty.is_error


async def test_push_and_pull_request_in_fixture_mode(sample_repo: Path, tmp_path: Path) -> None:
    async with Client(fixture_server(sample_repo, tmp_path)) as client:
        on_main = await client.call_tool("push_branch", {})
        assert on_main.is_error and "default branch" in text(on_main)

        await client.call_tool("create_branch", {"branch_name": "agent/issue-42-fix"})
        edit_source(sample_repo)
        await client.call_tool("commit_changes", {"message": "Fix #42"})

        early = await client.call_tool("create_pull_request", {"title": "Fix", "body": "Body"})
        assert early.is_error and "push it before" in text(early)

        pushed = await client.call_tool("push_branch", {})
        assert not pushed.is_error, text(pushed)
        assert remote_heads(sample_repo)["agent/issue-42-fix"] == run_git(sample_repo, "rev-parse", "HEAD").strip()
        assert remote_heads(sample_repo)["main"] == run_git(sample_repo, "rev-parse", "main").strip()  # default branch untouched

        pr = await client.call_tool("create_pull_request", {"title": "Rotate refresh tokens", "body": "Fixes #42"})
        assert not pr.is_error, text(pr)
        data = json.loads(text(pr))
        assert data == {"number": 1, "url": "fixture://demo/sample-repo/pull/1", "title": "Rotate refresh tokens", "head": "agent/issue-42-fix", "base": "main", "draft": True}
        stored = json.loads((tmp_path / "data" / "pull_requests" / "1.json").read_text())
        assert stored["body"] == "Fixes #42"

        duplicate = await client.call_tool("create_pull_request", {"title": "Again", "body": "x"})
        assert duplicate.is_error and "already exists" in text(duplicate)


async def test_push_never_forces_a_diverged_branch(sample_repo: Path, tmp_path: Path) -> None:
    async with Client(fixture_server(sample_repo, tmp_path)) as client:
        await client.call_tool("create_branch", {"branch_name": "agent/diverge"})
        edit_source(sample_repo)
        await client.call_tool("commit_changes", {"message": "First"})
        assert not (await client.call_tool("push_branch", {})).is_error
        first = remote_heads(sample_repo)["agent/diverge"]

        # Rewrite local history so the remote branch is no longer an ancestor.
        run_git(sample_repo, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "--amend", "-q", "-m", "Rewritten")
        rejected = await client.call_tool("push_branch", {})
        assert rejected.is_error and "Force pushing is not allowed" in text(rejected)
        assert remote_heads(sample_repo)["agent/diverge"] == first


async def test_missing_origin_is_a_clear_error(sample_repo: Path, tmp_path: Path) -> None:
    run_git(sample_repo, "remote", "remove", "origin")
    async with Client(fixture_server(sample_repo, tmp_path)) as client:
        await client.call_tool("create_branch", {"branch_name": "agent/no-origin"})
        result = await client.call_tool("push_branch", {})
        assert result.is_error and "'origin' remote" in text(result)


# --------------------------------------------------------------------------- API mode


def api_server(repo: Path, handler, **overrides) -> object:
    settings = GitHubSettings(
        mode="api", repository="octo/demo", token=TOKEN, repo_path=repo, writable_paths=("authkit", "tests"), transport=httpx2.MockTransport(handler)
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return build_server(settings)


async def test_api_pull_request_request_shape(sample_repo: Path) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(201, json={"number": 7, "html_url": "https://github.com/octo/demo/pull/7", "title": "Fix", "draft": True})

    async with Client(api_server(sample_repo, handler)) as client:
        await client.call_tool("create_branch", {"branch_name": "agent/api-pr"})
        result = await client.call_tool("create_pull_request", {"title": "Fix", "body": "Fixes #42"})
        assert not result.is_error, text(result)
        assert json.loads(text(result))["url"] == "https://github.com/octo/demo/pull/7"
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/repos/octo/demo/pulls"
    assert json.loads(request.content) == {"title": "Fix", "body": "Fixes #42", "head": "agent/api-pr", "base": "main", "draft": True}
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx2.Response(422, json={"message": "Validation Failed", "errors": [{"message": "A pull request already exists for octo:agent/api-pr."}]}), "already exists"),
        (httpx2.Response(422, json={"message": "Validation Failed", "errors": [{"resource": "PullRequest", "code": "custom", "message": "No commits between main and agent/api-pr"}]}), "No commits between"),
        (httpx2.Response(403, json={"message": "Resource not accessible by personal access token"}), "Pull requests: read and write"),
        (httpx2.Response(401, json={"message": "Bad credentials"}), "rejected the token"),
        (httpx2.Response(404, json={"message": "Not Found"}), "Not found on octo/demo"),
        (httpx2.Response(403, json={"message": "API rate limit exceeded"}, headers={"x-ratelimit-remaining": "0"}), "rate limit exceeded"),
        (httpx2.Response(503, text="unavailable"), "server error (503)"),
    ],
)
async def test_api_pull_request_failures(sample_repo: Path, response: httpx2.Response, expected: str) -> None:
    async with Client(api_server(sample_repo, lambda request: response)) as client:
        await client.call_tool("create_branch", {"branch_name": "agent/api-pr"})
        result = await client.call_tool("create_pull_request", {"title": "Fix", "body": "Body"})
        assert result.is_error and expected in text(result)
        assert TOKEN not in text(result)


async def test_api_pull_request_network_failure(sample_repo: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    async with Client(api_server(sample_repo, handler)) as client:
        await client.call_tool("create_branch", {"branch_name": "agent/api-pr"})
        result = await client.call_tool("create_pull_request", {"title": "Fix", "body": "Body"})
        assert result.is_error and "Could not reach the GitHub API" in text(result)


async def test_api_push_keeps_token_out_of_argv_and_git_config(sample_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Stand in for github.com with a local bare repository at <web_url>/octo/demo.git.
    remote = tmp_path / "web" / "octo" / "demo.git"
    remote.parent.mkdir(parents=True)
    run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))

    calls: list[tuple[list[str], dict[str, str]]] = []
    real_run = subprocess.run

    def recording_run(cmd, *args, **kwargs):
        calls.append((list(cmd), dict(kwargs.get("env") or {})))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(git_utils.subprocess, "run", recording_run)
    async with Client(api_server(sample_repo, lambda r: httpx2.Response(500), web_url=f"file://{tmp_path / 'web'}")) as client:
        await client.call_tool("create_branch", {"branch_name": "agent/api-push"})
        edit_source(sample_repo)
        await client.call_tool("commit_changes", {"message": "Fix #42"})
        pushed = await client.call_tool("push_branch", {})
        assert not pushed.is_error, text(pushed)

    assert "agent/api-push" in run_git(remote, "branch", "--list", "agent/api-push")
    push_calls = [(cmd, env) for cmd, env in calls if "push" in cmd]
    assert len(push_calls) == 1
    cmd, env = push_calls[0]
    assert all(TOKEN not in part for part in cmd)
    assert "--force" not in cmd and not any(part.startswith("+") for part in cmd)
    assert base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode() in env["GIT_CONFIG_VALUE_0"]
    assert TOKEN not in (sample_repo / ".git" / "config").read_text()


async def test_api_push_requires_token(sample_repo: Path) -> None:
    async with Client(api_server(sample_repo, lambda r: httpx2.Response(500), token=None)) as client:
        await client.call_tool("create_branch", {"branch_name": "agent/no-token"})
        result = await client.call_tool("push_branch", {})
        assert result.is_error and "Contents: read and write" in text(result)


@pytest.mark.parametrize(
    "stderr, expected",
    [
        (" ! [rejected]        agent/x -> agent/x (non-fast-forward)", "Force pushing is not allowed"),
        ("hint: Updates were rejected because the tip of your current branch is behind", "Force pushing is not allowed"),
        ("remote: error: GH006: Protected branch update failed", "branch protection"),
        ("remote: Permission to octo/demo.git denied to bot.\nfatal: unable to access: The requested URL returned error: 403", "Contents: read and write"),
        ("fatal: Authentication failed for 'https://github.com/octo/demo.git/'", "Check GITHUB_TOKEN"),
        ("fatal: unable to access 'https://github.com/': Could not resolve host: github.com", "Check network access"),
    ],
)
def test_push_error_messages(stderr: str, expected: str) -> None:
    assert expected in push_error_message(stderr, "octo/demo")


def test_sample_repo_has_local_origin(sample_repo: Path) -> None:
    assert origin_path(sample_repo).is_dir()
    assert "main" in remote_heads(sample_repo)
