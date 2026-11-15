"""Phase 3 in the workflow: the model may consult documentation; what it read is recorded and cited."""

from __future__ import annotations

import dataclasses

import httpx2
import pytest

from app.agents.coding_agent import DOCS_TOOLS
from app.agents.runner import AgentRunner
from app.storage.audit import AuditStore
from tests.docs_site import FakeDocsWeb
from tests.fakes import LocalPytestRunner, ScriptedProvider, inprocess_hub_factory
from tests.test_agent_workflow import FULL_FIX, PLAN, REGRESSION_TEST

pytestmark = pytest.mark.anyio

TOKENS_SECTION = "https://docs.authkit.test/en/tokens.html#refresh-token-rotation"
API_ENTRY = "https://docs.authkit.test/en/api.html#authkit.SessionStore.refresh"
PATCHES = [("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX}), ("propose_patch", {"path": "tests/test_sessions.py", "patch": REGRESSION_TEST})]


def make_runner(settings, provider, web: FakeDocsWeb | None = None) -> tuple[AgentRunner, AuditStore]:
    audit = AuditStore(settings.database_path)
    runner = AgentRunner(
        settings,
        audit=audit,
        provider_factory=lambda run_id: provider,
        hub_factory=inprocess_hub_factory(settings, LocalPytestRunner(), audit, docs_web=web),
    )
    return runner, audit


def provider(investigate: list, propose: list) -> ScriptedProvider:
    return ScriptedProvider(plan=PLAN, turns={"investigate": investigate, "propose": propose}, summary="Done.")


async def test_documentation_is_recorded_and_cited(settings) -> None:
    web = FakeDocsWeb()
    scripted = provider(
        investigate=[
            [("read_repository_file", {"path": "authkit/sessions.py"})],
            [("search_documentation", {"query": "refresh token rotation", "source": "authkit"})],
            [("read_documentation", {"url": TOKENS_SECTION}), ("read_documentation", {"url": "https://evil.example.net/steal"})],
            "Root cause: refresh() never rotates the refresh token. The AuthKit docs say refresh tokens are single use.",
        ],
        propose=[[("read_documentation", {"url": API_ENTRY})], PATCHES, "Rotate on refresh, per the documented contract."],
    )
    runner, audit = make_runner(settings, scripted, web)
    view = await runner.start(42)
    run_id = view["run"]["id"]
    assert view["pending_approval"]["kind"] == "patch_approval"

    # What was looked up comes from the tool calls themselves.
    consulted = view["state"]["docs_consulted"]
    assert [(d["tool"], d.get("url") or d.get("query"), d["node"]) for d in consulted] == [
        ("search", "refresh token rotation", "investigate"),
        ("read", TOKENS_SECTION, "investigate"),
        ("read", API_ENTRY, "propose"),
    ]
    assert consulted[1]["title"] == "Tokens and sessions — AuthKit" and consulted[1]["source"] == "authkit"
    assert DOCS_TOOLS[1] in scripted.tools_offered["investigate"] and DOCS_TOOLS[2] in scripted.tools_offered["propose"]

    # The disallowed URL was refused without any request to that host, and the run carried on.
    assert not any(r.url.host == "evil.example.net" for r in web.requests)
    calls = audit.tool_calls(run_id)
    refused = [c for c in calls if c["tool"] == "read_documentation" and c["is_error"]]
    assert len(refused) == 1 and refused[0]["initiator"] == "llm"
    assert {c["server"] for c in calls if c["tool"] in DOCS_TOOLS} == {"docs"}

    # Through the tests to the publication checkpoint: the PR body cites what was read.
    view = await runner.resume(run_id, approved=True)
    body = view["pending_approval"]["pr_body"]
    references = body.split("## References", 1)[1].split("## Risks", 1)[0]
    assert f"- [Tokens and sessions — AuthKit]({TOKENS_SECTION})" in references
    assert f"- [API reference]({API_ENTRY})" in references
    assert "evil.example.net" not in body


async def test_docs_failures_do_not_stop_the_run(settings) -> None:
    web = FakeDocsWeb()
    web.overrides["https://docs.authkit.test/en/searchindex.js"] = httpx2.Response(503, text="down", headers={"content-type": "text/html"})
    scripted = provider(
        investigate=[
            [("search_documentation", {"query": "rotation", "source": "authkit"})],
            [("read_documentation", {"url": "https://docs.authkit.test/en/error.html"})],
            [("read_repository_file", {"path": "authkit/sessions.py"})],
            "Root cause found without the docs.",
        ],
        propose=[PATCHES, "Fix."],
    )
    runner, audit = make_runner(settings, scripted, web)
    view = await runner.start(42)
    assert view["pending_approval"]["kind"] == "patch_approval", view["state"].get("error")
    assert [(d["tool"], d.get("ok")) for d in view["state"]["docs_consulted"]] == [("search", False)]
    errors = [c["result_head"] for c in audit.tool_calls(view["run"]["id"]) if c["tool"] in DOCS_TOOLS]
    assert any("HTTP 503" in (e or "") for e in errors) and any("HTTP 500" in (e or "") for e in errors)
    view = await runner.resume(view["run"]["id"], approved=True)
    assert "## References" not in view["pending_approval"]["pr_body"]


async def test_docs_disabled_runs_exactly_as_before(settings) -> None:
    settings = dataclasses.replace(settings, docs_enabled=False)
    scripted = provider(investigate=[[("read_repository_file", {"path": "authkit/sessions.py"})], "Root cause."], propose=[PATCHES, "Fix."])
    runner, audit = make_runner(settings, scripted)
    tools = await runner.discover_tools()
    assert "docs" not in tools["servers"] and not set(DOCS_TOOLS) & {t["name"] for t in tools["tools"]}
    view = await runner.start(42)
    assert not set(DOCS_TOOLS) & set().union(*scripted.tools_offered.values())
    view = await runner.resume(view["run"]["id"], approved=True)
    assert view["pending_approval"]["kind"] == "publish_approval" and "## References" not in view["pending_approval"]["pr_body"]
    assert view["state"]["docs_consulted"] == []


async def test_model_cannot_use_docs_tools_to_change_anything(settings) -> None:
    """The docs server's tools are all read-only, so the model still has no write path at all."""
    scripted = provider(investigate=["Nothing to look up."], propose=[PATCHES, "Fix."])
    runner, _ = make_runner(settings, scripted)
    tools = await runner.discover_tools()
    docs = [t for t in tools["tools"] if t["server"] == "docs"]
    assert {t["name"] for t in docs} == set(DOCS_TOOLS) and {t["risk"] for t in docs} == {"read"}
    await runner.start(42)
    offered = set().union(*scripted.tools_offered.values())
    assert offered <= {"search_repository", "read_repository_file", "get_git_diff", "propose_patch", *DOCS_TOOLS}
