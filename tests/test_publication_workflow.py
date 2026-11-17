"""Publication workflow: tests pass -> publication checkpoint -> branch / commit / push / pull request."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.coding_agent import branch_name_for, compose_pr_body
from app.llm.base import LLMAPIError
from app.mcp.git_utils import run_git
from tests.fakes import DEFAULT_PUBLICATION, ScriptedProvider
from tests.test_agent_workflow import FULL_FIX, INVESTIGATE, PLAN, REGRESSION_TEST, git_status, make_runner

pytestmark = pytest.mark.anyio


def provider(**kwargs) -> ScriptedProvider:
    return ScriptedProvider(
        plan=PLAN,
        turns={
            "investigate": list(INVESTIGATE),
            "propose": [[("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX}), ("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})], "Fix rotation."],
        },
        summary="Published the fix.",
        **kwargs,
    )


async def to_publication(settings, scripted: ScriptedProvider):
    runner, audit = make_runner(settings, scripted)
    view = await runner.start(42)
    view = await runner.resume(view["run"]["id"], approved=True)  # approve the patch; tests pass
    assert view["pending_approval"]["kind"] == "publish_approval", view["state"].get("error")
    return runner, audit, view


def remote_branches(repo: Path) -> set[str]:
    return {line.split("refs/heads/")[1] for line in run_git(repo, "ls-remote", "--heads", "origin").splitlines()}


def pr_files(settings) -> list[Path]:
    return sorted((settings.data_dir / "pull_requests").glob("*.json"))


async def test_checkpoint_shows_every_action_and_publishes_on_approval(settings) -> None:
    runner, audit, view = await to_publication(settings, provider())
    pending = view["pending_approval"]
    assert pending["branch_name"] == "agent/issue-42-rotate-refresh-tokens"
    assert [a["tool"] for a in pending["actions"]] == ["create_branch", "commit_changes", "push_branch", "create_pull_request"]
    assert pending["publish_options"] == ["branch", "push", "pull_request"]
    assert pending["files"] == ["authkit/sessions.py", "tests/test_sessions.py"]
    assert pending["base"] == "main" and pending["draft"] is True
    body = pending["pr_body"]
    # Test results in the PR body come from the recorded run, not from the model.
    assert "## Tests" in body and "Attempt 0: PASSED (6 passed" in body
    assert "Fixes #42" in body and DEFAULT_PUBLICATION["risks"][0] in body
    assert "`authkit/sessions.py` (+2 / -0)" in body
    assert "#42" in pending["commit_message"]
    assert remote_branches(settings.repo_path) == {"main"}  # nothing published before approval

    view = await runner.resume(view["run"]["id"], approved=True)
    assert view["run"]["status"] == "published"
    publication = view["state"]["publication"]
    assert publication["status"] == "pr_created"
    assert [s["tool"] for s in publication["steps"]] == ["create_branch", "commit_changes", "push_branch", "create_pull_request"]
    assert publication["files"] == ["authkit/sessions.py", "tests/test_sessions.py"]

    repo = settings.repo_path
    assert remote_branches(repo) == {"main", "agent/issue-42-rotate-refresh-tokens"}
    initial_commit = run_git(repo, "rev-list", "--max-parents=0", "HEAD").strip()
    assert run_git(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == initial_commit  # default branch untouched
    assert run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "agent/issue-42-rotate-refresh-tokens"
    assert git_status(repo) == ""
    assert "Rotate refresh tokens on refresh" in run_git(repo, "log", "-1", "--format=%B")
    stored = json.loads(pr_files(settings)[0].read_text())
    assert stored["head"] == "agent/issue-42-rotate-refresh-tokens" and stored["base"] == "main" and stored["draft"] is True
    assert stored["body"] == body
    assert publication["pull_request"]["url"] == "fixture://demo/sample-repo/pull/1"


@pytest.mark.parametrize("level, status, pub_status, pushed", [("branch", "completed", "committed", False), ("push", "completed", "pushed", True)])
async def test_partial_publication(settings, level: str, status: str, pub_status: str, pushed: bool) -> None:
    runner, audit, view = await to_publication(settings, provider())
    view = await runner.resume(view["run"]["id"], approved=True, publish_up_to=level)
    assert view["run"]["status"] == status
    assert view["state"]["publication"]["status"] == pub_status
    assert ("agent/issue-42-rotate-refresh-tokens" in remote_branches(settings.repo_path)) is pushed
    assert pr_files(settings) == []
    tools = [c["tool"] for c in audit.tool_calls(view["run"]["id"])]
    assert "create_pull_request" not in tools
    assert ("push_branch" in tools) is pushed


async def test_reviewer_edits_are_used_and_recorded(settings) -> None:
    runner, audit, view = await to_publication(settings, provider())
    edits = {"branch_name": "agent/custom-branch", "pr_title": "Fix #42: single-use refresh tokens"}
    view = await runner.resume(view["run"]["id"], approved=True, edits=edits)
    assert view["run"]["status"] == "published"
    assert view["state"]["publication"]["pull_request"]["title"] == "Fix #42: single-use refresh tokens"
    assert "agent/custom-branch" in remote_branches(settings.repo_path)
    assert view["state"]["publication_approvals"][-1]["edited"] == ["branch_name", "pr_title"]


async def test_invalid_edited_branch_fails_before_anything_is_published(settings) -> None:
    runner, audit, view = await to_publication(settings, provider())
    view = await runner.resume(view["run"]["id"], approved=True, edits={"branch_name": "main"})
    assert view["run"]["status"] == "publish_failed"
    assert "must start with 'agent/'" in view["state"]["error"]
    assert [(s["tool"], s["ok"]) for s in view["state"]["publication"]["steps"]] == [("create_branch", False)]
    assert run_git(settings.repo_path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert remote_branches(settings.repo_path) == {"main"}


async def test_reject_with_feedback_redrafts_then_decline(settings) -> None:
    second = dict(DEFAULT_PUBLICATION, pr_title="Make refresh tokens single-use (security)")
    scripted = provider(json_by_purpose={"draft_publication": [DEFAULT_PUBLICATION, second]})
    runner, audit, view = await to_publication(settings, scripted)
    run_id = view["run"]["id"]

    view = await runner.resume(run_id, approved=False, feedback="Mention the security impact in the title.")
    assert view["pending_approval"]["kind"] == "publish_approval"
    assert view["pending_approval"]["pr_title"] == "Make refresh tokens single-use (security)"
    redraft_prompt = [p for purpose, p in scripted.prompts if purpose == "draft_publication"][-1]
    assert "Mention the security impact in the title." in redraft_prompt
    assert view["state"]["publication_revisions"] == 1

    view = await runner.resume(run_id, approved=False)
    assert view["run"]["status"] == "completed"
    assert view["state"]["publication"]["status"] == "declined"
    assert git_status(settings.repo_path) != ""  # tested changes stay in the working tree, uncommitted
    assert run_git(settings.repo_path, "branch", "--list", "agent/*").strip() == ""
    assert not {"create_branch", "commit_changes", "push_branch", "create_pull_request"} & {c["tool"] for c in audit.tool_calls(run_id)}


async def test_failure_midway_records_what_was_done(settings) -> None:
    runner, audit, view = await to_publication(settings, provider())
    run_git(settings.repo_path, "remote", "remove", "origin")
    view = await runner.resume(view["run"]["id"], approved=True)
    assert view["run"]["status"] == "publish_failed"
    error = view["state"]["error"]
    assert "push_branch failed" in error and "completed before the failure: create_branch, commit_changes" in error
    steps = view["state"]["publication"]["steps"]
    assert [(s["tool"], s["ok"]) for s in steps] == [("create_branch", True), ("commit_changes", True), ("push_branch", False)]
    assert view["state"]["publication"]["commit"]  # the local commit is recorded so a human can push it later


async def test_invalid_publish_level_fails_closed(settings) -> None:
    runner, audit, view = await to_publication(settings, provider())
    view = await runner.resume(view["run"]["id"], approved=True, publish_up_to="force_push")
    assert view["run"]["status"] == "publish_failed"
    assert "invalid publish_up_to" in view["state"]["error"]
    assert run_git(settings.repo_path, "branch", "--list", "agent/*").strip() == ""


async def test_draft_failure_reports_publish_failed(settings) -> None:
    scripted = provider()
    scripted.errors["draft_publication"] = LLMAPIError("Anthropic API error (500).", status_code=500, retryable=True)
    runner, _ = make_runner(settings, scripted)
    view = await runner.start(42)
    view = await runner.resume(view["run"]["id"], approved=True)
    assert view["run"]["status"] == "publish_failed"
    assert view["state"]["tests_passed"] is True
    assert "draft_publication" in view["state"]["error"]


def test_branch_names_and_pr_body_helpers() -> None:
    assert branch_name_for("agent/", 42, "Rotate Refresh Tokens!") == "agent/issue-42-rotate-refresh-tokens"
    assert branch_name_for("agent/", 42, "issue-42-fix-it") == "agent/issue-42-fix-it"
    assert branch_name_for("agent/", 7, "") == "agent/issue-7-fix"
    body = compose_pr_body("S", [], [("a.py", 1, 2)], [], 5)
    assert "- No test runs recorded." in body and "- None identified." in body and "Fixes #5" in body


# ---------------------------------------------------------------------- summary self-check

WRONG = "Logging out twice crashed with a KeyError, as the issue reports."
CHECKED = {
    "problems": [{"field": "summary", "claim": "the issue reports a KeyError crash", "correction": "the issue reports that a second refresh fails and a used refresh token still works"}],
    "pr_title": DEFAULT_PUBLICATION["pr_title"],
    "commit_message": "Rotate refresh tokens on refresh",  # the issue reference was dropped; the workflow adds it back
    "summary": "Refreshing never stored the new refresh token or revoked the used one; the fix does both.",
    "risks": DEFAULT_PUBLICATION["risks"],
}


async def test_the_summary_check_corrects_the_draft_and_keeps_the_original(settings) -> None:
    drafted = {**DEFAULT_PUBLICATION, "summary": WRONG}
    scripted = provider(json_by_purpose={"draft_publication": [drafted], "check_publication": [CHECKED]})
    _, _, view = await to_publication(settings, scripted)
    pending = view["pending_approval"]
    assert CHECKED["summary"] in pending["pr_body"] and WRONG not in pending["pr_body"]
    assert pending["commit_message"].startswith("Rotate refresh tokens on refresh") and "#42" in pending["commit_message"]
    check = pending["summary_check"]
    assert check["status"] == "corrected" and check["problems"] == CHECKED["problems"]
    assert check["original"]["summary"] == WRONG  # what the model first wrote stays on record
    assert view["state"]["publication_draft"]["summary_check"] == check
    prompt = next(p for purpose, p in scripted.prompts if purpose == "check_publication")
    assert "Logout should be idempotent" not in prompt and "Refreshing a session twice" in prompt  # the issue
    assert "+++ b/authkit/sessions.py" in prompt and "- attempt 0: PASSED (6 passed" in prompt  # the diff and the recorded tests
    assert WRONG in prompt


async def test_without_problems_the_draft_is_used_as_written(settings) -> None:
    rewritten = {**CHECKED, "problems": []}  # different text but no problems: nothing to correct
    _, _, view = await to_publication(settings, provider(json_by_purpose={"check_publication": [rewritten]}))
    pending = view["pending_approval"]
    assert DEFAULT_PUBLICATION["summary"] in pending["pr_body"] and CHECKED["summary"] not in pending["pr_body"]
    assert pending["summary_check"] == {"status": "ok", "problems": []}


async def test_a_failed_check_keeps_the_draft_and_says_so(settings) -> None:
    scripted = provider()
    scripted.errors["check_publication"] = LLMAPIError("Anthropic API error 400: bad request", status_code=400)
    _, _, view = await to_publication(settings, scripted)
    pending = view["pending_approval"]
    assert DEFAULT_PUBLICATION["summary"] in pending["pr_body"]
    assert pending["summary_check"]["status"] == "failed" and "400" in pending["summary_check"]["message"]


async def test_the_check_can_be_turned_off(settings) -> None:
    import dataclasses

    scripted = provider()
    _, _, view = await to_publication(dataclasses.replace(settings, check_pr_summary=False), scripted)
    assert "check_publication" not in scripted.calls
    assert view["pending_approval"]["summary_check"] is None
