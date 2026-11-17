"""End-to-end LangGraph workflow: real MCP servers, scripted LLM, tests run on a local copy.

Covers: issue before plan, search before edit, plan shown, no write before
approval, diff after every change, failed-test recovery, rejection with
feedback, and failure paths.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from app.agents.coding_agent import DOCS_TOOLS, AgentDeps, WorkflowError, _llm_tools
from app.agents.runner import AgentRunner, RunStateError
from app.agents.tool_loop import run_tool_loop
from app.llm.base import LLMRateLimitError
from app.mcp.client import MCPHub, Risk
from app.mcp.git_utils import run_git
from app.storage.audit import AuditStore
from tests.fakes import FakeRunner, LocalPytestRunner, ScriptedProvider, inprocess_hub_factory, inprocess_servers

pytestmark = pytest.mark.anyio

PLAN = {
    "summary": "SessionStore.refresh does not rotate refresh tokens.",
    "steps": ["Read SessionStore.refresh", "Register the new refresh token and revoke the used one", "Add a regression test", "Run the test suite"],
    "search_queries": ["def refresh", "_by_refresh"],
    "acceptance_criteria": ["A used refresh token is rejected", "The new refresh token can be used"],
}

PARTIAL_FIX = """--- a/authkit/sessions.py
+++ b/authkit/sessions.py
@@ -60,4 +60,5 @@
         new_session = self._new_session(session.user_id)
         self._by_access.pop(session.access_token, None)
         self._by_access[new_session.access_token] = new_session
+        self._by_refresh[new_session.refresh_token] = new_session
         return new_session
"""

REVOKE_USED_TOKEN = """--- a/authkit/sessions.py
+++ b/authkit/sessions.py
@@ -61,3 +61,4 @@
         self._by_access.pop(session.access_token, None)
+        self._by_refresh.pop(refresh_token, None)
         self._by_access[new_session.access_token] = new_session
         self._by_refresh[new_session.refresh_token] = new_session
"""

FULL_FIX = """--- a/authkit/sessions.py
+++ b/authkit/sessions.py
@@ -60,4 +60,6 @@
         new_session = self._new_session(session.user_id)
         self._by_access.pop(session.access_token, None)
+        self._by_refresh.pop(refresh_token, None)
         self._by_access[new_session.access_token] = new_session
+        self._by_refresh[new_session.refresh_token] = new_session
         return new_session
"""

REGRESSION_TEST = """--- a/tests/test_sessions.py
+++ b/tests/test_sessions.py
@@ -55,2 +55,10 @@
     with pytest.raises(InvalidToken):
         store.refresh(session.refresh_token)
+
+
+def test_refresh_twice_in_a_row():
+    store = SessionStore()
+    first = store.login("alice")
+    second = store.refresh(first.refresh_token)
+    third = store.refresh(second.refresh_token)
+    assert store.authenticate(third.access_token) == "alice"
"""

INVESTIGATE = [
    [("read_repository_file", {"path": "authkit/sessions.py"})],
    [("read_repository_file", {"path": "tests/test_sessions.py"})],
    "Root cause: SessionStore.refresh (authkit/sessions.py) never stores the new refresh token and never revokes the used one.",
]


def make_runner(settings, provider, sandbox_runner=None) -> tuple[AgentRunner, AuditStore]:
    audit = AuditStore(settings.database_path)
    runner = AgentRunner(
        settings,
        audit=audit,
        provider_factory=lambda run_id: provider,
        hub_factory=inprocess_hub_factory(settings, sandbox_runner or LocalPytestRunner(), audit),
    )
    return runner, audit


def git_status(repo: Path) -> str:
    return run_git(repo, "status", "--porcelain").strip()


async def test_issue_to_passing_tests_with_failed_test_recovery(settings) -> None:
    provider = ScriptedProvider(
        plan=PLAN,
        turns={
            "investigate": list(INVESTIGATE),
            "propose": [
                # Attempt 0: incomplete fix plus a regression test.
                [("propose_patch", {"path": "authkit/sessions.py", "patch": PARTIAL_FIX}), ("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})],
                "Store the new refresh token on refresh; add a regression test for refreshing twice.",
                # Fix attempt 1, after the failing test run.
                [("read_repository_file", {"path": "authkit/sessions.py"})],
                [("propose_patch", {"path": "authkit/sessions.py", "patch": REVOKE_USED_TOKEN})],
                "Also revoke the used refresh token so it is single-use.",
            ],
        },
        summary="Fixed refresh-token rotation; all tests pass.",
    )
    runner, audit = make_runner(settings, provider)

    # 1. Start: runs until the first approval checkpoint.
    view = await runner.start(42)
    run_id = view["run"]["id"]
    assert view["run"]["status"] == "awaiting_approval"
    state = view["state"]
    assert state["issue"].startswith("# Issue #42")
    assert state["plan"]["summary"] == PLAN["summary"]  # the plan is visible before any change
    assert {s["query"] for s in state["searches"] if s["by"] == "workflow"} == {"def refresh", "_by_refresh"}
    assert state["files_examined"] == ["authkit/sessions.py", "tests/test_sessions.py"]
    pending = view["pending_approval"]
    assert [p["path"] for p in pending["proposals"]] == ["authkit/sessions.py", "tests/test_sessions.py"]
    assert "+        self._by_refresh[new_session.refresh_token] = new_session" in pending["proposals"][0]["diff"]
    assert git_status(settings.repo_path) == ""  # nothing written before approval

    # Ordering rules, from the audit log.
    calls = audit.tool_calls(run_id)
    tools = [c["tool"] for c in calls]
    assert tools[0] == "get_github_issue"
    assert tools.index("search_repository") < tools.index("propose_patch")
    assert "apply_patch" not in tools and "run_tests" not in tools
    # The model only ever had read-only tools (plus propose_patch, which writes nothing).
    offered = set().union(*provider.tools_offered.values())
    assert offered <= {"search_repository", "read_repository_file", "get_git_diff", "propose_patch", *DOCS_TOOLS}
    assert provider.calls.index("plan") < provider.calls.index("investigate")

    # 2. Approve: patches applied, diff captured, tests fail, agent proposes a fix.
    view = await runner.resume(run_id, approved=True)
    state = view["state"]
    assert view["run"]["status"] == "awaiting_approval"
    assert [c["path"] for c in state["applied_changes"]] == ["authkit/sessions.py", "tests/test_sessions.py"]
    assert "+def test_refresh_twice_in_a_row():" in state["repo_diff"]
    assert state["test_runs"][0]["status"] == "FAILED"
    assert "1 failed" in state["test_runs"][0]["summary"]
    assert state["fix_attempts"] == 1
    assert view["pending_approval"]["reason"] == "fix after failing tests"
    fix_prompt = [p for purpose, p in provider.prompts if purpose == "propose"][-1]
    assert "test_refresh_rotates_refresh_token" in fix_prompt  # failure output reached the model

    # 3. Approve the fix: tests pass, the run stops at the publication checkpoint.
    view = await runner.resume(run_id, approved=True)
    state = view["state"]
    assert view["run"]["status"] == "awaiting_approval"
    assert view["pending_approval"]["kind"] == "publish_approval"
    assert [r["status"] for r in state["test_runs"]] == ["FAILED", "PASSED"]
    source = (settings.repo_path / "authkit" / "sessions.py").read_text()
    assert "self._by_refresh.pop(refresh_token, None)" in source
    assert "self._by_refresh[new_session.refresh_token] = new_session" in source
    assert "self._by_refresh.pop(refresh_token, None)" in state["repo_diff"]
    assert run_git(settings.repo_path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"  # nothing published yet

    # 4. Approve publication: branch, commit, push, pull request.
    view = await runner.resume(run_id, approved=True)
    assert view["run"]["status"] == "published"
    assert view["state"]["summary"] == "Fixed refresh-token rotation; all tests pass."

    events = [e["kind"] for e in audit.events(run_id)]
    assert events.count("approval_requested") == 3 and events.count("approval_decided") == 2 and events.count("publication_decided") == 1
    apply_calls = [c for c in audit.tool_calls(run_id) if c["tool"] == "apply_patch"]
    assert len(apply_calls) == 3 and all(c["initiator"] == "workflow" for c in apply_calls)
    publish_calls = [c for c in audit.tool_calls(run_id) if c["tool"] in ("create_branch", "commit_changes", "push_branch", "create_pull_request")]
    assert [c["tool"] for c in publish_calls] == ["create_branch", "commit_changes", "push_branch", "create_pull_request"]
    assert all(c["initiator"] == "workflow" for c in publish_calls)
    offered = set().union(*provider.tools_offered.values())
    assert not offered & {"create_branch", "commit_changes", "push_branch", "create_pull_request", "apply_patch"}

    with pytest.raises(RunStateError):
        await runner.resume(run_id, approved=True)  # nothing left to approve


async def test_rejection_with_feedback_then_without(settings) -> None:
    provider = ScriptedProvider(
        plan=PLAN,
        turns={
            "investigate": list(INVESTIGATE),
            "propose": [
                [("propose_patch", {"path": "authkit/sessions.py", "patch": PARTIAL_FIX})],
                "Minimal fix.",
                [("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX})],
                "Revised: also revoke the used token.",
            ],
        },
    )
    runner, audit = make_runner(settings, provider)
    view = await runner.start(42)
    run_id = view["run"]["id"]

    view = await runner.resume(run_id, approved=False, feedback="Also revoke the used refresh token.")
    assert view["run"]["status"] == "awaiting_approval"
    assert view["state"]["revisions"] == 1
    revision_prompt = [p for purpose, p in provider.prompts if purpose == "propose"][-1]
    assert "Also revoke the used refresh token." in revision_prompt
    assert "self._by_refresh.pop(refresh_token, None)" in view["pending_approval"]["proposals"][0]["diff"]

    view = await runner.resume(run_id, approved=False)  # no feedback: stop
    assert view["run"]["status"] == "rejected"
    assert git_status(settings.repo_path) == ""
    assert "apply_patch" not in [c["tool"] for c in audit.tool_calls(run_id)]


async def test_single_approval_success(settings) -> None:
    provider = ScriptedProvider(
        plan=PLAN,
        turns={
            "investigate": list(INVESTIGATE),
            "propose": [[("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX}), ("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})], "Fix rotation."],
        },
    )
    runner, _ = make_runner(settings, provider)
    view = await runner.start(42)
    view = await runner.resume(view["run"]["id"], approved=True)
    assert view["run"]["status"] == "awaiting_approval"
    assert view["pending_approval"]["kind"] == "publish_approval"
    assert view["state"]["test_runs"][-1]["summary"].startswith("6 passed")


async def test_unknown_issue_fails_before_planning(settings) -> None:
    provider = ScriptedProvider(plan=PLAN)
    runner, _ = make_runner(settings, provider)
    view = await runner.start(999)
    assert view["run"]["status"] == "failed"
    assert "could not read issue #999" in view["state"]["error"]
    assert "plan" not in provider.calls


async def test_llm_errors_fail_the_run_clearly(settings) -> None:
    provider = ScriptedProvider(plan=PLAN)
    provider.errors["plan"] = LLMRateLimitError("Anthropic rate limit reached (429); retry after 30s.", retry_after=30)
    runner, _ = make_runner(settings, provider)
    view = await runner.start(42)
    assert view["run"]["status"] == "failed"
    assert "rate limit" in view["state"]["error"]
    assert "Run finished with status 'failed'" in view["state"]["summary"]


async def test_no_valid_patch_fails(settings) -> None:
    bad = PARTIAL_FIX.replace("         new_session = self._new_session(session.user_id)", "         this_line_does_not_exist()")
    provider = ScriptedProvider(plan=PLAN, turns={"investigate": list(INVESTIGATE), "propose": [[("propose_patch", {"path": "authkit/sessions.py", "patch": bad})], "I could not produce a patch."]})
    runner, audit = make_runner(settings, provider)
    view = await runner.start(42)
    assert view["run"]["status"] == "failed"
    assert "did not produce any valid patch" in view["state"]["error"]
    propose_calls = [c for c in audit.tool_calls(view["run"]["id"]) if c["tool"] == "propose_patch"]
    assert propose_calls and propose_calls[0]["is_error"] == 1


async def test_fix_attempts_are_limited(settings) -> None:
    limited = dataclasses.replace(settings, max_fix_attempts=0)
    provider = ScriptedProvider(plan=PLAN, turns={"investigate": list(INVESTIGATE), "propose": [[("propose_patch", {"path": "authkit/sessions.py", "patch": PARTIAL_FIX})], "Partial."]})
    runner, _ = make_runner(limited, provider)
    view = await runner.start(42)
    run_id = view["run"]["id"]
    view = await runner.resume(run_id, approved=True)
    # With no attempts left the run pauses at the recovery checkpoint instead of ending.
    assert view["run"]["status"] == "awaiting_approval"
    assert view["pending_approval"]["kind"] == "recovery" and view["pending_approval"]["reason"] == "fix_attempts_exhausted"
    assert len(view["state"]["test_runs"]) == 1
    view = await runner.resume(run_id, approved=False)  # stop
    assert view["run"]["status"] == "tests_failed"
    assert view["failure"]["code"] == "tests_failed" and "MAX_FIX_ATTEMPTS=0" in view["failure"]["message"]


async def test_docker_unavailable_fails_after_apply(settings) -> None:
    provider = ScriptedProvider(plan=PLAN, turns={"investigate": list(INVESTIGATE), "propose": [[("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX})], "Fix."]})
    runner, _ = make_runner(settings, provider, FakeRunner(unavailable="Docker is not installed on this host"))
    view = await runner.start(42)
    run_id = view["run"]["id"]
    view = await runner.resume(run_id, approved=True)
    # The run pauses so a human can start Docker and re-run the tests, or stop.
    assert view["pending_approval"]["kind"] == "recovery" and view["pending_approval"]["reason"] == "tests_could_not_run"
    assert "Docker is not installed" in view["pending_approval"]["message"]
    view = await runner.resume(run_id, approved=False)
    assert view["run"]["status"] == "failed"
    assert "Docker is not installed" in view["state"]["error"]
    assert view["failure"]["code"] == "tests_could_not_run" and "Docker" in view["failure"]["hint"]


async def test_tool_loop_rejects_unknown_tools_bad_arguments_and_enforces_budget(settings) -> None:
    provider = ScriptedProvider(
        turns={
            "loop": [
                [("apply_patch", {"path": "authkit/sessions.py", "patch": PARTIAL_FIX})],  # not offered
                [("read_repository_file", {"path": 123})],  # schema violation
                [("read_repository_file", {"path": "../../etc/passwd"})],  # server-side rejection
                [("search_repository", {"query": "refresh"})],
                [("search_repository", {"query": "again"})],  # over budget
                [("search_repository", {"query": "and again"})],  # still calling: loop stops
            ]
        }
    )
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        result = await run_tool_loop(
            provider=provider, hub=hub, system_prompt="s", user_prompt="u",
            tools=[hub.get("read_repository_file"), hub.get("search_repository")], max_tool_calls=4, node="loop",
        )
    assert result.hit_limit
    outcomes = [(c.tool, c.is_error) for c in result.calls]
    # Every requested call counts toward the per-step limit, rejected ones included.
    assert outcomes[0] == ("apply_patch", True) and "not available" in result.calls[0].text
    assert outcomes[1] == ("read_repository_file", True) and "INVALID_ARGUMENTS" in result.calls[1].text
    assert outcomes[2] == ("read_repository_file", True) and "outside" in result.calls[2].text
    assert outcomes[3] == ("search_repository", False)
    assert "limit for this step (4, MAX_TOOL_CALLS_PER_STEP) is used up" in result.calls[4].text
    assert git_status(settings.repo_path) == ""


async def test_write_tools_are_never_offered_to_the_model(settings) -> None:
    async with MCPHub(inprocess_servers(settings, FakeRunner()), risk_overrides={"read_repository_file": Risk.WRITE}) as hub:
        with pytest.raises(WorkflowError, match="refusing to give the model"):
            _llm_tools(hub, ("read_repository_file",))
        assert isinstance(AgentDeps, type)


async def test_the_plan_is_written_after_searching_the_code(settings) -> None:
    provider = ScriptedProvider(plan=PLAN, turns={"investigate": list(INVESTIGATE), "propose": [[("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX})], "Fix."]})
    runner, audit = make_runner(settings, provider)
    view = await runner.start(42)
    assert provider.calls[:3] == ["plan_searches", "plan", "investigate"]
    searches = [c for c in audit.tool_calls(view["run"]["id"]) if c["tool"] == "search_repository"]
    assert [c["node"] for c in searches] == ["plan", "plan"]  # run by the workflow while planning, before the plan exists
    choose, write = [p for purpose, p in provider.prompts if purpose in ("plan_searches", "plan")]
    assert "search_results" not in choose  # the first call only picks searches
    assert "$ search_repository('_by_refresh')" in write and "authkit/sessions.py:" in write  # the plan sees real paths
    assert view["state"]["plan"]["search_queries"] == PLAN["search_queries"]
    investigate = next(p for purpose, p in provider.prompts if purpose == "investigate")
    assert "$ search_repository('def refresh')" in investigate  # the results are passed on, not searched again


async def test_planning_falls_back_to_the_issue_title_and_stops_if_every_search_fails(settings) -> None:
    provider = ScriptedProvider(json_by_purpose={"plan_searches": [{"search_queries": ["   "]}], "plan": [PLAN]})
    runner, audit = make_runner(settings, provider)
    view = await runner.start(42)
    searched = [json.loads(c["arguments"])["query"] for c in audit.tool_calls(view["run"]["id"]) if c["tool"] == "search_repository"]
    assert searched == ["Refreshing a session twice logs the user out; old refresh tokens stay valid"]

    provider = ScriptedProvider(json_by_purpose={"plan_searches": [{"search_queries": ["x" * 300]}], "plan": [PLAN]})  # the tool refuses it
    runner, _ = make_runner(settings, provider)
    view = await runner.start(42)
    assert view["run"]["status"] == "failed" and view["failure"]["code"] == "search_failed" and view["failure"]["step"] == "plan"
    assert provider.calls == ["plan_searches"]  # no plan is written without search results
