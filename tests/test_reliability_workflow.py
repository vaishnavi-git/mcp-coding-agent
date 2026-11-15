"""Phase 4 end to end: budgets, retries, structured failures, recovery and approval checkpoints.

Real MCP servers in-process, a scripted model that reports token usage, and
(where the test needs exact test results) a fake sandbox runner.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from mcp import StdioServerParameters

from app.agents.runner import AgentRunner, RunStateError
from app.llm.base import LLMRateLimitError
from app.mcp.client import MCPHub, ServerConfig, tool_retry_policy
from app.mcp.sandbox_server import ExecResult
from app.storage.audit import AuditStore
from tests.fakes import FakeRunner, LocalPytestRunner, ScriptedProvider, inprocess_hub_factory, inprocess_servers
from tests.test_agent_workflow import FULL_FIX, INVESTIGATE, PARTIAL_FIX, PLAN, REGRESSION_TEST, REVOKE_USED_TOKEN, git_status

pytestmark = pytest.mark.anyio

FIX_BOTH = [("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX}), ("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})]
ROTATION_TEST = "tests/test_sessions.py::test_refresh_rotates_refresh_token"


def make_runner(settings, provider, sandbox=None, servers=None) -> tuple[AgentRunner, AuditStore]:
    audit = AuditStore(settings.database_path)
    runner = AgentRunner(
        settings,
        audit=audit,
        provider_factory=lambda run_id: provider,
        hub_factory=inprocess_hub_factory(settings, sandbox or LocalPytestRunner(), audit, servers=servers),
    )
    return runner, audit


def scripted(propose=None, investigate=None, **kwargs) -> ScriptedProvider:
    return ScriptedProvider(plan=PLAN, turns={"investigate": list(investigate or INVESTIGATE), "propose": list(propose or [FIX_BOTH, "Fix rotation."])}, **kwargs)


def tools_called(audit: AuditStore, run_id: str, name: str) -> int:
    return sum(1 for c in audit.tool_calls(run_id) if c["tool"] == name)


def pytest_output(*failing: str) -> ExecResult:
    lines = ["=========================== short test summary info ============================"]
    lines += [f"FAILED {t} - AssertionError" for t in failing]
    lines.append(f"{len(failing)} failed, 4 passed in 0.05s")
    return ExecResult(["pytest"], 1, "\n".join(lines), "", 0.1, False)


PASS = ExecResult(["pytest"], 0, "6 passed in 0.05s", "", 0.1, False)


# ---------------------------------------------------------------------- budgets
async def test_cost_limit_stops_the_run_before_the_next_model_call(settings) -> None:
    # Each scripted call: 1000 input + 200 output tokens at $2/$10 per MTok = $0.004.
    settings = dataclasses.replace(settings, max_task_cost_usd=0.02)
    provider = scripted()
    runner, audit = make_runner(settings, provider)
    view = await runner.start(42)
    run_id = view["run"]["id"]

    assert view["run"]["status"] == "failed"
    failure = view["failure"]
    assert failure["code"] == "budget_exceeded" and failure["step"] == "propose" and not failure["retryable"]
    assert "spent $0.0200 of its $0.0200 limit (MAX_TASK_COST_USD)" in failure["message"]
    assert "MAX_TASK_COST_USD" in view["run"]["error"] and "Raise the limit" in failure["hint"]
    # plan (1) + investigate (3) + propose (1): the sixth call was never sent
    assert len(provider.calls) == 5 and len(audit.llm_calls(run_id)) == 5
    usage = view["usage"]
    assert usage["cost_usd"] == pytest.approx(0.02) and usage["remaining"]["cost_usd"] == 0 and usage["limits"]["max_cost_usd"] == 0.02
    assert all(round(c["cost_usd"], 6) == 0.004 for c in audit.llm_calls(run_id))
    assert view["state"]["timeline"][-2]["step"] == "propose" and view["state"]["timeline"][-2]["outcome"] == "failed"
    assert git_status(settings.repo_path) == ""


async def test_run_tool_call_limit(settings) -> None:
    settings = dataclasses.replace(settings, max_tool_calls_per_run=4)
    runner, audit = make_runner(settings, scripted())
    view = await runner.start(42)
    run_id = view["run"]["id"]
    # get_github_issue + 2 planned searches + 1 file read; the second read was refused
    assert len(audit.tool_calls(run_id)) == 4
    failure = view["failure"]
    assert failure["code"] == "budget_exceeded" and failure["step"] == "investigate"
    assert "MAX_TOOL_CALLS_PER_RUN=4" in failure["message"]
    assert view["usage"]["remaining"]["tool_calls"] == 0


async def test_model_call_limit(settings) -> None:
    settings = dataclasses.replace(settings, max_llm_calls_per_run=2)
    provider = scripted()
    runner, _ = make_runner(settings, provider)
    view = await runner.start(42)
    assert view["failure"]["code"] == "budget_exceeded" and "MAX_LLM_CALLS_PER_RUN=2" in view["failure"]["message"]
    assert len(provider.calls) == 2


async def test_unknown_model_price_blocks_a_cost_limited_run(settings) -> None:
    priced = dataclasses.replace(settings, anthropic_model="claude-not-in-the-price-file")
    provider = scripted()
    runner, audit = make_runner(priced, provider)
    view = await runner.start(42)
    assert view["run"]["status"] == "failed" and view["failure"]["code"] == "pricing_unknown" and view["failure"]["step"] == "startup"
    assert "model_prices.json" in view["failure"]["hint"] and provider.calls == [] and audit.tool_calls(view["run"]["id"]) == []
    # Without a cost limit the same model runs; cost is simply not known.
    unlimited = dataclasses.replace(priced, max_task_cost_usd=0)
    runner, _ = make_runner(unlimited, scripted())
    view = await runner.start(42)
    assert view["pending_approval"]["kind"] == "patch_approval" and view["usage"]["pricing"] is None and view["usage"]["cost_usd"] == 0


# ---------------------------------------------------------------------- retries
async def test_rate_limited_model_calls_are_retried_automatically(settings) -> None:
    settings = dataclasses.replace(settings, anthropic_max_retries=3)
    provider = scripted()
    provider.errors["plan"] = [LLMRateLimitError("Anthropic rate limit reached (429)."), LLMRateLimitError("Anthropic rate limit reached (429).")]
    runner, audit = make_runner(settings, provider)
    view = await runner.start(42)
    run_id = view["run"]["id"]
    assert view["pending_approval"]["kind"] == "patch_approval"
    assert sum(1 for e in audit.events(run_id) if e["kind"] == "llm_retry") == 2
    plan_calls = [c for c in audit.llm_calls(run_id) if c["purpose"] == "plan"]
    assert [bool(c["error"]) for c in plan_calls] == [True, True, False]
    assert view["usage"]["failed_llm_calls"] == 2


async def test_failed_run_is_retried_from_the_failed_step(settings) -> None:
    settings = dataclasses.replace(settings, anthropic_max_retries=1)
    provider = scripted()
    provider.errors["propose"] = LLMRateLimitError("Anthropic rate limit reached (429).")
    runner, audit = make_runner(settings, provider)
    view = await runner.start(42)
    run_id = view["run"]["id"]
    failure = view["failure"]
    assert view["run"]["status"] == "failed"
    assert failure["code"] == "llm_rate_limited" and failure["step"] == "propose" and failure["retryable"]
    assert "Gave up after 2 attempts" in failure["message"] and "retry the run" in failure["hint"]
    assert view["retry"] == {"available": True, "from_step": "propose", "reason": None, "retries_used": 0, "max_retries": 3}

    del provider.errors["propose"]  # the rate limit has passed
    view = await runner.retry(run_id)
    assert view["run"]["status"] == "awaiting_approval" and view["pending_approval"]["kind"] == "patch_approval"
    assert view["failure"] is None and view["run"]["error"] is None
    # Completed steps were not repeated.
    assert tools_called(audit, run_id, "get_github_issue") == 1 and provider.calls.count("plan") == 1
    assert view["retry"]["retries_used"] == 1 and not view["retry"]["available"]
    view = await runner.resume(run_id, approved=True)
    assert view["pending_approval"]["kind"] == "publish_approval"
    assert [s["step"] for s in view["state"]["timeline"]].count("propose") == 1  # the failed branch is gone


async def test_retry_limit_and_states_that_cannot_be_retried(settings) -> None:
    settings = dataclasses.replace(settings, max_run_retries=1)
    provider = scripted()
    provider.errors["plan"] = LLMRateLimitError("Anthropic rate limit reached (429).")
    runner, _ = make_runner(settings, provider)
    view = await runner.start(42)
    run_id = view["run"]["id"]
    view = await runner.retry(run_id)  # still rate limited
    assert view["run"]["status"] == "failed" and view["retry"] == {
        "available": False, "from_step": None, "reason": "retry limit reached (MAX_RUN_RETRIES=1)", "retries_used": 1, "max_retries": 1,
    }
    with pytest.raises(RunStateError, match="MAX_RUN_RETRIES=1"):
        await runner.retry(run_id)

    ok_runner, _ = make_runner(settings, scripted())
    waiting = await ok_runner.start(42)
    with pytest.raises(RunStateError, match="only failed or interrupted runs"):
        await ok_runner.retry(waiting["run"]["id"])
    with pytest.raises(RunStateError, match="not found"):
        await ok_runner.retry("no-such-run")


async def test_a_failure_while_applying_is_not_retried_automatically(settings) -> None:
    runner, _ = make_runner(settings, scripted())
    view = await runner.start(42)
    run_id = view["run"]["id"]
    target = settings.repo_path / "authkit" / "sessions.py"
    target.write_text(target.read_text().replace("self._by_access.pop(session.access_token, None)", "self._by_access.clear()"))  # edited meanwhile
    view = await runner.resume(run_id, approved=True)
    failure = view["failure"]
    assert view["run"]["status"] == "failed" and failure["code"] == "apply_failed" and failure["step"] == "apply"
    assert "git status" in failure["hint"]
    assert view["retry"]["available"] is False and "stopped in 'apply'" in view["retry"]["reason"]
    with pytest.raises(RunStateError, match="stopped in 'apply', which changes files or GitHub"):
        await runner.retry(run_id)


async def test_unexpected_errors_are_recorded_and_the_run_continues_on_retry(settings) -> None:
    provider = scripted()
    provider.errors["investigate"] = [ValueError("unexpected bug")]
    runner, audit = make_runner(settings, provider)
    view = await runner.start(42)
    run_id = view["run"]["id"]
    assert view["run"]["status"] == "failed"
    assert view["failure"]["code"] == "internal_error" and view["failure"]["step"] == "investigate"
    assert "ValueError: unexpected bug" in view["failure"]["message"]
    view = await runner.retry(run_id)
    assert view["pending_approval"]["kind"] == "patch_approval" and provider.calls.count("plan") == 1


async def test_mcp_server_that_fails_to_start(settings) -> None:
    provider = scripted()
    audit = AuditStore(settings.database_path)
    broken = ServerConfig("github", StdioServerParameters(command="/nonexistent/mcp-server"))
    runner = AgentRunner(settings, audit=audit, provider_factory=lambda run_id: provider, hub_factory=lambda run_id: MCPHub([broken], audit=audit, run_id=run_id))
    view = await runner.start(42)
    run_id = view["run"]["id"]
    failure = view["failure"]
    assert failure["code"] == "mcp_server_unavailable" and failure["step"] == "startup" and failure["retryable"]
    assert "MCP server 'github' failed to start" in failure["message"]
    # Fix the configuration and retry: no checkpoint existed yet, so the run starts from the beginning.
    runner.hub_factory = lambda run_id: MCPHub(inprocess_servers(settings, LocalPytestRunner()), audit=audit, run_id=run_id, retry=tool_retry_policy(settings))
    view = await runner.retry(run_id)
    assert view["pending_approval"]["kind"] == "patch_approval"


async def test_interrupted_runs_are_marked_and_can_be_retried(settings) -> None:
    provider = scripted(investigate=INVESTIGATE * 2, propose=[FIX_BOTH, "Fix.", FIX_BOTH, "Fix."])  # two runs
    runner, audit = make_runner(settings, provider)
    # A run that never got going (the process died after accepting it).
    never_started = runner.create_run(42, "Keep the change minimal.")
    audit.update_run(never_started, "queued")
    # A run that died while an approval was being processed.
    waiting = (await runner.start(42))["run"]["id"]
    audit.update_run(waiting, "resuming")

    assert set(runner.mark_interrupted_runs()) == {never_started, waiting}
    view = await runner.view(never_started)
    assert view["run"]["status"] == "interrupted" and view["failure"]["code"] == "interrupted" and view["retry"]["available"]

    view = await runner.retry(never_started)
    assert view["pending_approval"]["kind"] == "patch_approval"
    assert "Keep the change minimal." in [p for purpose, p in provider.prompts if purpose == "plan"][-1]  # instructions kept

    view = await runner.retry(waiting)  # back to the pending checkpoint; nothing re-runs
    assert view["run"]["status"] == "awaiting_approval" and view["pending_approval"]["kind"] == "patch_approval"
    view = await runner.resume(waiting, approved=True)
    assert view["pending_approval"]["kind"] == "publish_approval"


# ---------------------------------------------------------------------- recovery after failed tests
async def test_failed_tests_are_analyzed_and_the_reviewer_can_grant_another_attempt(settings) -> None:
    settings = dataclasses.replace(settings, max_fix_attempts=1)
    provider = scripted(
        propose=[
            [("propose_patch", {"path": "authkit/sessions.py", "patch": PARTIAL_FIX})], "Partial fix.",
            [("propose_patch", {"path": "authkit/sessions.py", "patch": REVOKE_USED_TOKEN})], "Revoke the used token.",
            [("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})], "Add the regression test.",
        ]
    )
    sandbox = FakeRunner([pytest_output(ROTATION_TEST, "tests/test_sessions.py::test_logout_revokes_tokens"), pytest_output(ROTATION_TEST, "tests/test_sessions.py::test_logout_revokes_tokens"), PASS])
    runner, audit = make_runner(settings, provider, sandbox)
    run_id = (await runner.start(42))["run"]["id"]

    view = await runner.resume(run_id, approved=True)  # tests fail -> fix attempt 1
    first = view["state"]["test_runs"][0]
    assert first["failure_kind"] == "test_failures" and first["failing_tests"] == [ROTATION_TEST, "tests/test_sessions.py::test_logout_revokes_tokens"]
    fix_prompt = [p for purpose, p in provider.prompts if purpose == "propose"][-1]
    assert "fix attempt 1 of 1" in fix_prompt and f"Failing tests: {ROTATION_TEST}" in fix_prompt and "one or more tests failed" in fix_prompt

    view = await runner.resume(run_id, approved=True)  # same tests fail again, no attempts left
    pending = view["pending_approval"]
    assert pending["kind"] == "recovery" and pending["reason"] == "fix_attempts_exhausted"
    assert pending["last_test_run"]["failing_tests"][0] == ROTATION_TEST and pending["fix_attempts"] == 1 and pending["max_fix_attempts"] == 1
    assert [o["decision"] for o in pending["options"]] == ["retry", "stop"]
    assert view["run"]["status"] == "awaiting_approval"

    view = await runner.resume(run_id, approved=True, feedback="The regression test is missing; add it.")
    guided = [p for purpose, p in provider.prompts if purpose == "propose"][-1]
    assert "The regression test is missing; add it." in guided and "fix attempt 2 of 2" in guided
    assert "the same tests still fail, so your last change did not help" in guided
    assert view["pending_approval"]["kind"] == "patch_approval" and view["state"]["extra_fix_attempts"] == 1

    view = await runner.resume(run_id, approved=True)
    assert view["pending_approval"]["kind"] == "publish_approval"
    assert [r["status"] for r in view["state"]["test_runs"]] == ["FAILED", "FAILED", "PASSED"]
    decisions = view["state"]["recovery_decisions"]
    assert len(decisions) == 1 and decisions[0]["approved"] and decisions[0]["reason"] == "fix_attempts_exhausted"
    assert any(e["kind"] == "recovery_decided" for e in audit.events(run_id))


async def test_tests_can_be_rerun_once_docker_is_back(settings) -> None:
    sandbox = FakeRunner(unavailable="the Docker daemon is not running")
    runner, audit = make_runner(settings, scripted(), sandbox)
    run_id = (await runner.start(42))["run"]["id"]
    view = await runner.resume(run_id, approved=True)
    pending = view["pending_approval"]
    assert pending["kind"] == "recovery" and pending["reason"] == "tests_could_not_run"
    assert "Docker daemon is not running" in pending["message"] and pending["options"][0]["description"].startswith("Run the tests again")
    assert view["state"]["test_runs"][-1]["failure_kind"] == "not_run"

    sandbox.unavailable = None  # Docker started
    view = await runner.resume(run_id, approved=True)
    assert view["pending_approval"]["kind"] == "publish_approval"
    assert [r["status"] for r in view["state"]["test_runs"]] == ["ERROR", "PASSED"]
    assert tools_called(audit, run_id, "apply_patch") == 2  # the patches were applied once, not again


# ---------------------------------------------------------------------- plan checkpoint
async def test_plan_approval_checkpoint(settings) -> None:
    settings = dataclasses.replace(settings, require_plan_approval=True)
    revised = {**PLAN, "summary": "Revised: rotate and revoke refresh tokens."}
    provider = ScriptedProvider(json_by_purpose={"plan": [PLAN, revised]}, turns={"investigate": list(INVESTIGATE), "propose": [FIX_BOTH, "Fix."]})
    runner, audit = make_runner(settings, provider)
    view = await runner.start(42)
    run_id = view["run"]["id"]
    pending = view["pending_approval"]
    assert pending["kind"] == "plan_approval" and pending["reason"] == "initial plan" and pending["plan"]["summary"] == PLAN["summary"]
    assert [c["tool"] for c in audit.tool_calls(run_id)] == ["get_github_issue"]  # nothing investigated yet

    view = await runner.resume(run_id, approved=False, feedback="Also cover revoking the used token.")
    assert view["pending_approval"]["kind"] == "plan_approval" and view["pending_approval"]["reason"] == "revised plan"
    replan = [p for purpose, p in provider.prompts if purpose == "plan"][-1]
    assert "Also cover revoking the used token." in replan and PLAN["summary"] in replan

    view = await runner.resume(run_id, approved=True)
    assert view["pending_approval"]["kind"] == "patch_approval" and view["state"]["plan"]["summary"] == revised["summary"]
    assert [a["approved"] for a in view["state"]["plan_approvals"]] == [False, True]

    other = (await runner.start(42))["run"]["id"]
    view = await runner.resume(other, approved=False)
    assert view["run"]["status"] == "rejected" and view["failure"] is None


# ---------------------------------------------------------------------- structured state
async def test_timeline_and_usage_are_recorded_per_step(settings) -> None:
    runner, audit = make_runner(settings, scripted())
    run_id = (await runner.start(42))["run"]["id"]
    view = await runner.resume(run_id, approved=True)
    assert view["pending_approval"]["kind"] == "publish_approval"
    timeline = view["state"]["timeline"]
    assert [e["step"] for e in timeline] == ["fetch_issue", "plan", "investigate", "propose", "apply", "run_tests", "draft_publication"]
    assert all(e["outcome"] == "ok" and e["duration_ms"] >= 0 and e["started_at"] for e in timeline)
    by_step = {e["step"]: e for e in timeline}
    assert by_step["investigate"]["llm_calls"] == 3 and by_step["investigate"]["tool_calls"] == 4  # 2 searches + 2 reads
    assert by_step["apply"]["llm_calls"] == 0 and by_step["apply"]["tool_calls"] == 5  # 2 checks + 2 patches + git diff
    usage = view["usage"]
    assert sum(e["cost_usd"] for e in timeline) == pytest.approx(usage["cost_usd"])
    assert usage["llm_calls"] == len(audit.llm_calls(run_id)) and usage["tool_calls"] == len(audit.tool_calls(run_id))
    stored = json.loads([e for e in audit.events(run_id) if e["kind"] == "status"][-1]["detail"])
    assert stored == {"status": "awaiting_approval"}


async def test_a_run_without_a_configured_model_fails_clearly(settings) -> None:
    runner, audit = make_runner(dataclasses.replace(settings, anthropic_model=""), scripted())
    view = await runner.start(42)
    assert view["run"]["status"] == "failed"
    failure = view["failure"]
    assert failure["code"] == "model_not_configured" and failure["retryable"]
    assert "ANTHROPIC_MODEL is not set" in failure["message"] and ".env.example" in failure["hint"]
    assert audit.tool_calls(view["run"]["id"]) == []  # nothing ran
