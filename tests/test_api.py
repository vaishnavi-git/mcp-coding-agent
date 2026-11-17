"""FastAPI endpoints: start a run, review the proposal, approve, read the audit trail."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.runner import AgentRunner
from app.api import main as api
from app.llm.base import LLMRateLimitError
from app.storage.audit import AuditStore
from tests.fakes import LocalPytestRunner, ScriptedProvider, as_configured, inprocess_hub_factory
from tests.test_agent_workflow import FULL_FIX, INVESTIGATE, PLAN, REGRESSION_TEST


@pytest.fixture
def client(settings):
    provider = ScriptedProvider(
        plan=PLAN,
        turns={
            "investigate": list(INVESTIGATE),
            "propose": [[("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX}), ("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})], "Fix rotation."],
        },
    )
    audit = AuditStore(settings.database_path)
    runner = AgentRunner(settings, audit=audit, provider_factory=lambda run_id: as_configured(provider, settings), hub_factory=inprocess_hub_factory(settings, LocalPytestRunner(), audit))
    api.app.dependency_overrides[api.get_runner] = lambda: runner
    api._runner = runner  # used by background tasks
    with TestClient(api.app) as test_client:
        yield test_client
    api.app.dependency_overrides.clear()
    api._runner = None


def test_full_run_through_the_api(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}

    tools = client.get("/tools").json()
    by_name = {t["name"]: t for t in tools["tools"]}
    assert by_name["apply_patch"]["risk"] == "write" and by_name["read_repository_file"]["risk"] == "read"
    assert set(tools["servers"]) == {"github", "filesystem", "sandbox", "docs"}
    assert {by_name[n]["risk"] for n in ("search_documentation", "read_documentation", "list_documentation_sources")} == {"read"}

    started = client.post("/runs", json={"issue_number": 42})
    assert started.status_code == 202
    run_id = started.json()["run_id"]

    view = client.get(f"/runs/{run_id}").json()  # background task completed within the request in TestClient
    assert view["run"]["status"] == "awaiting_approval"
    assert view["state"]["plan"]["steps"]
    assert {p["path"] for p in view["pending_approval"]["proposals"]} == {"authkit/sessions.py", "tests/test_sessions.py"}

    decided = client.post(f"/runs/{run_id}/approval", json={"approved": True})
    assert decided.status_code == 202

    view = client.get(f"/runs/{run_id}").json()
    assert view["run"]["status"] == "awaiting_approval"
    assert view["state"]["test_runs"][-1]["status"] == "PASSED"
    assert "+        self._by_refresh.pop(refresh_token, None)" in view["state"]["repo_diff"]
    publication = view["pending_approval"]
    assert publication["kind"] == "publish_approval"
    assert publication["branch_name"] == "agent/issue-42-rotate-refresh-tokens"
    assert publication["publish_options"] == ["branch", "push", "pull_request"]

    # Invalid level is rejected by validation before anything runs.
    assert client.post(f"/runs/{run_id}/approval", json={"approved": True, "publish_up_to": "force_push"}).status_code == 422

    decided = client.post(
        f"/runs/{run_id}/approval",
        json={"approved": True, "publish_up_to": "pull_request", "edits": {"pr_title": "Fix #42: rotate refresh tokens"}},
    )
    assert decided.status_code == 202 and decided.json()["publish_up_to"] == "pull_request"

    view = client.get(f"/runs/{run_id}").json()
    assert view["run"]["status"] == "published"
    pr = view["state"]["publication"]["pull_request"]
    assert pr["title"] == "Fix #42: rotate refresh tokens" and pr["url"].startswith("fixture://")

    trail = client.get(f"/runs/{run_id}/audit").json()
    assert trail["tool_calls"][0]["tool"] == "get_github_issue"
    assert any(e["kind"] == "approval_decided" for e in trail["events"])
    assert any(e["kind"] == "publication_decided" for e in trail["events"])

    assert client.post(f"/runs/{run_id}/approval", json={"approved": True}).status_code == 409
    assert [r["id"] for r in client.get("/runs").json()] == [run_id]


def test_validation_and_unknown_runs(client: TestClient) -> None:
    assert client.post("/runs", json={"issue_number": 0}).status_code == 422
    assert client.post("/runs", json={}).status_code == 422
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/runs/nope/audit").status_code == 404
    assert client.post("/runs/nope/approval", json={"approved": True}).status_code == 404


def test_retry_endpoint_and_reliability_fields(client: TestClient) -> None:
    runner = api._runner
    provider = runner.provider_factory("x")
    provider.errors["plan"] = LLMRateLimitError("Anthropic rate limit reached (429).")

    run_id = client.post("/runs", json={"issue_number": 42, "instructions": "Be brief."}).json()["run_id"]
    view = client.get(f"/runs/{run_id}").json()
    assert view["run"]["status"] == "failed" and view["run"]["instructions"] == "Be brief."
    assert view["failure"]["code"] == "llm_rate_limited" and view["failure"]["step"] == "plan" and view["failure"]["hint"]
    assert view["retry"]["available"] and view["retry"]["from_step"] == "plan"
    assert view["usage"]["limits"]["max_cost_usd"] == 5.0 and view["usage"]["failed_llm_calls"] == 1

    assert client.post(f"/runs/{run_id}/approval", json={"approved": True}).status_code == 409  # nothing to approve
    del provider.errors["plan"]
    retried = client.post(f"/runs/{run_id}/retry")
    assert retried.status_code == 202 and retried.json()["from_step"] == "plan" and retried.json()["retries_left"] == 2
    view = client.get(f"/runs/{run_id}").json()
    assert view["run"]["status"] == "awaiting_approval" and view["failure"] is None
    assert [e["step"] for e in view["state"]["timeline"]] == ["fetch_issue", "plan", "investigate", "propose"]

    assert client.post(f"/runs/{run_id}/retry").status_code == 409  # waiting for approval, not failed
    assert client.post("/runs/nope/retry").status_code == 404
