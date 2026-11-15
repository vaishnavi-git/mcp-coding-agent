"""Phase 5: test timeout handling and failed-test recovery, end to end through the workflow.

Timeouts happen at two levels: the sandbox kills a test run at
SANDBOX_TIMEOUT_SECONDS (the run is reported as TIMED OUT and the model gets
a fix attempt), and the hub gives up on a sandbox server that does not answer
at all (the tests did not run, so a human decides at the recovery checkpoint;
a stuck run is never repeated automatically).
"""

from __future__ import annotations

import dataclasses
import json

import anyio
import pytest
from mcp.server import MCPServer

from app.mcp.client import ServerConfig, stdio_server_configs
from app.mcp.git_utils import run_git
from app.mcp.sandbox_server import SANDBOXED, ExecResult
from app.reliability.test_results import KIND_TEXT
from tests.fakes import FakeRunner, LocalPytestRunner, inprocess_servers
from tests.test_agent_workflow import FULL_FIX, PARTIAL_FIX, REGRESSION_TEST, REVOKE_USED_TOKEN
from tests.test_reliability_workflow import PASS, make_runner, scripted

pytestmark = pytest.mark.anyio

TIMED_OUT = ExecResult(["pytest", "-q"], -1, "tests/test_sessions.py ...", "", 120.0, True)

# FULL_FIX with a syntax error (missing parenthesis): pytest cannot even import the package.
BROKEN_FIX = FULL_FIX.replace("+        self._by_refresh.pop(refresh_token, None)\n", "+        self._by_refresh.pop(refresh_token, None\n")
REPAIR = """--- a/authkit/sessions.py
+++ b/authkit/sessions.py
@@ -61,3 +61,3 @@
         self._by_access.pop(session.access_token, None)
-        self._by_refresh.pop(refresh_token, None
+        self._by_refresh.pop(refresh_token, None)
         self._by_access[new_session.access_token] = new_session
"""


def propose(path: str, patch: str) -> list[tuple[str, dict]]:
    return [("propose_patch", {"path": path, "patch": patch})]


def fix_prompts(provider) -> list[str]:
    return [p for purpose, p in provider.prompts if purpose == "propose"][1:]  # the first is the initial proposal


# ---------------------------------------------------------------------- timeouts
async def test_a_test_run_killed_at_the_time_limit_is_reported_as_a_timeout(settings) -> None:
    provider = scripted(propose=[propose("authkit/sessions.py", FULL_FIX), "Fix.", propose("tests/test_sessions.py", REGRESSION_TEST), "Add the test."])
    sandbox = FakeRunner([TIMED_OUT, PASS])
    runner, _ = make_runner(settings, provider, sandbox)
    run_id = (await runner.start(42))["run"]["id"]

    view = await runner.resume(run_id, approved=True)  # apply -> tests time out -> fix attempt 1
    first = view["state"]["test_runs"][0]
    assert (first["status"], first["failure_kind"], first["exit_code"]) == ("TIMED OUT", "timeout", -1)
    assert "killed after 120s timeout" in first["summary"]
    prompt = fix_prompts(provider)[0]
    assert KIND_TEXT["timeout"] in prompt and "fix attempt 1 of 2" in prompt  # a time limit, not a failing assertion
    assert view["pending_approval"]["kind"] == "patch_approval"

    view = await runner.resume(run_id, approved=True)
    assert [r["status"] for r in view["state"]["test_runs"]] == ["TIMED OUT", "PASSED"]
    assert view["pending_approval"]["kind"] == "publish_approval"
    assert len(sandbox.commands) == 2


def hanging_sandbox(calls: list[str]) -> MCPServer:
    """A sandbox server that never answers (a stuck Docker daemon, say)."""
    server = MCPServer("sandbox")

    @server.tool(annotations=SANDBOXED)
    async def run_tests(test_command: str = "") -> str:
        calls.append(test_command)
        await anyio.sleep(60)
        return "status: PASSED"

    return server


async def test_a_sandbox_that_never_answers_hits_the_hub_deadline_and_is_not_repeated(settings) -> None:
    calls: list[str] = []

    def servers():
        configs = inprocess_servers(settings, FakeRunner())
        return [ServerConfig("sandbox", hanging_sandbox(calls), timeout_seconds=0.5, retry_on_timeout=False) if c.name == "sandbox" else c for c in configs]

    runner, audit = make_runner(settings, scripted(), servers=servers)
    run_id = (await runner.start(42))["run"]["id"]
    with anyio.fail_after(20):
        view = await runner.resume(run_id, approved=True)
    pending = view["pending_approval"]
    assert pending["kind"] == "recovery" and pending["reason"] == "tests_could_not_run"
    assert "run_tests timed out after 0.5s (the per-call limit for the sandbox server)" in pending["message"]
    assert view["state"]["test_runs"][-1]["failure_kind"] == "not_run"
    assert calls == [settings.test_command]  # not retried automatically
    assert not any(e["kind"] == "tool_retry" for e in audit.events(run_id))

    view = await runner.resume(run_id, approved=False)  # stop
    assert view["run"]["status"] == "failed" and view["failure"]["code"] == "tests_could_not_run"
    assert "Docker" in view["failure"]["hint"]


def test_configured_time_limits_reach_the_sandbox_and_the_hub(settings) -> None:
    settings = dataclasses.replace(settings, sandbox_timeout_seconds=45, sandbox_memory_mb=768, sandbox_pids=64)
    sandbox = next(c for c in stdio_server_configs(settings) if c.name == "sandbox")
    config = json.loads(sandbox.target.args[sandbox.target.args.index("--config-json") + 1])
    assert (config["timeout_seconds"], config["memory_mb"], config["pids"]) == (45, 768, 64)
    # The hub waits longer than the sandbox's own limit, so a killed run is still reported, and never retries it.
    assert sandbox.timeout_seconds == 45 + 120 and sandbox.retry_on_timeout is False
    for other in stdio_server_configs(settings):
        if other.name in ("github", "filesystem"):
            assert other.timeout_seconds == settings.tool_timeout_seconds


# ---------------------------------------------------------------------- failed test recovery
async def test_a_patch_that_breaks_collection_is_recognised_and_repaired(settings) -> None:
    provider = scripted(propose=[propose("authkit/sessions.py", BROKEN_FIX), "Fix.", propose("authkit/sessions.py", REPAIR), "Close the parenthesis."])
    runner, _ = make_runner(settings, provider, LocalPytestRunner())  # real pytest on a copy of the repository
    run_id = (await runner.start(42))["run"]["id"]

    view = await runner.resume(run_id, approved=True)
    broken = view["state"]["test_runs"][0]
    assert broken["status"] == "FAILED" and broken["failure_kind"] == "collection_error", broken["output"][-800:]
    prompt = fix_prompts(provider)[0]
    assert KIND_TEXT["collection_error"] in prompt and "SyntaxError" in prompt
    assert "authkit/sessions.py" in prompt.split("Changes applied so far:")[1]

    view = await runner.resume(run_id, approved=True)
    assert [r["status"] for r in view["state"]["test_runs"]] == ["FAILED", "PASSED"]
    assert [(c["path"], c["attempt"]) for c in view["state"]["applied_changes"]] == [("authkit/sessions.py", 0), ("authkit/sessions.py", 1)]
    source = (settings.repo_path / "authkit" / "sessions.py").read_text()
    assert source.count("self._by_refresh.pop(refresh_token, None)\n") == 1  # the repair applied on top of the first patch
    assert view["pending_approval"]["kind"] == "publish_approval"


async def test_a_rejected_fix_is_not_applied(settings) -> None:
    provider = scripted(propose=[propose("authkit/sessions.py", PARTIAL_FIX), "Partial fix.", propose("authkit/sessions.py", REVOKE_USED_TOKEN), "Revoke the used token."])
    runner, audit = make_runner(settings, provider, LocalPytestRunner())
    run_id = (await runner.start(42))["run"]["id"]
    view = await runner.resume(run_id, approved=True)  # partial fix applied, tests fail, fix proposed
    assert view["state"]["test_runs"][0]["failure_kind"] == "test_failures"
    assert view["pending_approval"]["kind"] == "patch_approval"

    view = await runner.resume(run_id, approved=False)  # the reviewer rejects the fix, no feedback
    assert view["run"]["status"] == "rejected"
    source = (settings.repo_path / "authkit" / "sessions.py").read_text()
    assert "self._by_refresh[new_session.refresh_token] = new_session" in source  # the approved first patch stays
    assert "self._by_refresh.pop(refresh_token, None)" not in source  # the rejected fix was never written
    assert sum(1 for c in audit.tool_calls(run_id) if c["tool"] == "apply_patch") == 1
    assert "authkit/sessions.py" in run_git(settings.repo_path, "status", "--porcelain")
