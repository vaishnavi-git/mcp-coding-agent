"""The coding-agent workflow as a LangGraph state machine.

    START -> fetch_issue -> plan -> await_plan_approval -> investigate -> propose -> await_approval
              (optional checkpoint; rejected + feedback -> plan)    ^             |  approved -> apply -> run_tests
                                                                    |             |  rejected + feedback -> propose
                                                                    +-- tests failed (attempts left) <------+
    fix attempts used up, or tests could not run -> await_recovery -> propose | run_tests | finalize
    tests passed -> draft_publication -> await_publication -> publish
                          ^                   | rejected + feedback
                          +-------------------+
    any failure / final outcome -> finalize -> END

The ordering rules are enforced by the graph and by guards in
the nodes, not just by prompts:
- the plan is produced only after the issue has been read;
- patches are proposed only after at least one repository search succeeded;
- the model only ever gets read-only tools (plus propose_patch, which writes
  nothing); apply_patch is called by the workflow, after a human approved the
  exact patch text (checked by SHA-256);
- the full diff is captured after every applied change;
- GitHub write tools (branch, commit, push, pull request) are never offered to
  the model: the publish node runs only the steps the human approved, with the
  exact values shown at the approval checkpoint;
- documentation tools are read-only and optional: when the docs
  server is connected the model may use them while investigating and
  proposing, and every page it reads is recorded and cited in the PR.

Reliability: every step is wrapped so that it checks the run budget
before it starts (model calls and cost for steps that use the model, tool
calls for steps that call tools), records a timeline entry, and turns an
expected failure into a structured ``failure`` record instead of crashing.
Approved actions (apply, run_tests, publish) are not budget-checked, so they
never stop halfway. Failed tests lead to bounded fix attempts with targeted
information about what still fails; when the attempts are used up, or the
tests cannot run at all, a human decides at the recovery checkpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.config import Settings
from app.languages import runner_for
from app.llm.base import LLMError, LLMProvider
from app.mcp.client import MCPHub, Risk, ToolNotFound, ToolOutcome, ToolSpec
from app.reliability import test_results
from app.reliability.budget import BudgetExceeded, RunBudget
from app.reliability.failures import CodedError, failure_from_exception, make_failure
from app.storage.audit import AuditStore

from . import prompts
from .state import AgentState, Proposal, SandboxRunRecord
from .tool_loop import run_tool_loop

INVESTIGATE_TOOLS = ("search_repository", "read_repository_file")
PROPOSE_TOOLS = ("search_repository", "read_repository_file", "get_git_diff", "propose_patch")
DOCS_TOOLS = ("list_documentation_sources", "search_documentation", "read_documentation")  # optional (docs server)
FINAL_STATUSES = {"completed", "published", "tests_failed", "rejected", "failed", "publish_failed"}
FAILED_STATUSES = {"failed", "publish_failed"}
FAILURE_CODES_BY_STATUS = {"failed": "workflow_error", "publish_failed": "publish_failed", "tests_failed": "tests_failed"}

# What each step needs from the run budget before it starts. Steps not listed
# (apply, run_tests, publish, finalize) carry out approved work and always finish.
BUDGET_CHECKS: dict[str, tuple[str, ...]] = {
    "fetch_issue": ("tools",),
    "plan": ("llm",),
    "investigate": ("llm", "tools"),
    "propose": ("llm", "tools"),
    "draft_publication": ("llm",),
}
# Steps that may run again when a failed run is retried. apply and publish change
# files or GitHub, so a failure inside them is never repeated automatically.
RESUMABLE_STEPS = {
    "fetch_issue", "plan", "await_plan_approval", "investigate", "propose", "await_approval",
    "run_tests", "await_recovery", "draft_publication", "await_publication", "finalize",
}

# How far a publication may go; each level includes the ones before it.
PUBLISH_LEVELS = ("branch", "push", "pull_request")
PUBLISH_STEPS = {
    "branch": ("create_branch", "commit_changes"),
    "push": ("create_branch", "commit_changes", "push_branch"),
    "pull_request": ("create_branch", "commit_changes", "push_branch", "create_pull_request"),
}
EDITABLE_PUBLICATION_FIELDS = ("branch_name", "commit_message", "pr_title", "pr_body")
CHECKED_FIELDS = ("pr_title", "commit_message", "summary", "risks")  # what the self-check may correct


class WorkflowError(CodedError):
    """A workflow rule was violated or a required step failed (``code``: see failures.HINTS)."""


@dataclass
class AgentDeps:
    hub: MCPHub
    provider: LLMProvider
    settings: Settings
    audit: AuditStore
    run_id: str
    budget: RunBudget | None = None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [{len(text) - limit} characters truncated]"


def _per_file_stats(diff: str) -> list[tuple[str, int, int]]:
    """(path, added, removed) for each file in a unified diff."""
    stats: dict[str, list[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            current = path[2:] if path.startswith("b/") else path
            stats.setdefault(current, [0, 0])
        elif current and line.startswith("+") and not line.startswith("+++"):
            stats[current][0] += 1
        elif current and line.startswith("-") and not line.startswith("---"):
            stats[current][1] += 1
    return [(path, a, r) for path, (a, r) in stats.items()]


def branch_name_for(prefix: str, issue_number: int, slug: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", (slug or "").lower()).strip("-")[:48].strip("-") or "fix"
    clean = re.sub(rf"^(issue-)?{issue_number}-", "", clean) or "fix"
    return f"{prefix}issue-{issue_number}-{clean}"


def _link_text(title: str, url: str) -> str:
    """Page titles come from third-party sites: keep them from adding Markdown, HTML or @mentions."""
    clean = re.sub(r"[\[\]()<>`*_|\\]", "", " ".join(title.split())).replace("@", "(at)")[:100].strip()
    return clean or url


def compose_pr_body(
    summary: str,
    risks: list[str],
    files: list[tuple[str, int, int]],
    test_runs: list[dict[str, Any]],
    issue_number: int,
    references: list[dict[str, Any]] | None = None,
) -> str:
    """PR description. Changes, Tests and References come from recorded results, never from the model."""
    lines = ["## Summary", summary.strip() or "-", "", "## Changes"]
    lines += [f"- `{path}` (+{added} / -{removed})" for path, added, removed in files] or ["- (none)"]
    lines += ["", "## Tests"]
    if test_runs:
        lines.append(f"Run in the Docker sandbox: `{test_runs[-1]['command']}`")
        lines += [f"- Attempt {r['attempt']}: {r['status']} ({r['summary']})" for r in test_runs]
    else:
        lines.append("- No test runs recorded.")
    if references:
        lines += ["", "## References", "Documentation the agent read while preparing this change:"]
        lines += [f"- [{_link_text(r.get('title') or '', r['url'])}]({r['url']})" for r in references]
    lines += ["", "## Risks"]
    lines += [f"- {r.strip()}" for r in risks if r.strip()] or ["- None identified."]
    lines += [
        "",
        f"Fixes #{issue_number}",
        "",
        "---",
        "_Generated by MCP Coding Agent. A human reviewer approved every code change before it was applied, and approved this pull request._",
    ]
    return "\n".join(lines)


def _llm_tools(hub: MCPHub, names: tuple[str, ...], optional: tuple[str, ...] = ()) -> list[ToolSpec]:
    """Tools offered to the model must be read-only according to discovery.

    ``optional`` tools are offered only if a connected server exposes them
    (the docs server can be disabled).
    """
    specs = []
    for name in (*names, *(n for n in optional if n in hub.tools)):
        spec = hub.get(name)
        if spec.risk is not Risk.READ:
            raise WorkflowError(f"refusing to give the model tool {name!r}: discovered risk is {spec.risk.value}, not read")
        specs.append(spec)
    return specs


def _system_prompt(base: str, tools: list[ToolSpec]) -> str:
    return base + (prompts.DOCS_GUIDANCE if any(s.name in DOCS_TOOLS for s in tools) else "")


def _doc_record(spec: ToolSpec, args: dict[str, Any], outcome: ToolOutcome, node: str) -> dict[str, Any] | None:
    """What the model looked up, taken from the tool call itself (not from what the model says)."""
    if spec.name == "read_documentation" and not outcome.is_error:
        header = dict(re.findall(r"^(url|title|source): (.*)$", outcome.text.split("\n--- begin", 1)[0], re.MULTILINE))
        return {"tool": "read", "url": header.get("url") or str(args.get("url")), "title": header.get("title", ""), "source": header.get("source", ""), "node": node}
    if spec.name == "search_documentation":
        return {"tool": "search", "query": str(args.get("query", "")), "source": str(args.get("source", "")), "ok": not outcome.is_error, "node": node}
    return None


def _references(state: AgentState, limit: int = 10) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for record in state.get("docs_consulted", []):
        if record.get("tool") == "read" and record.get("url") and record["url"] not in seen:
            seen[record["url"]] = {"url": record["url"], "title": record.get("title") or ""}
    return list(seen.values())[:limit]


async def check_publication(provider: LLMProvider, state: dict[str, Any], draft: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fact-check the drafted text against the issue, the approved plan, the diff and the test runs.

    Returns the draft to use and a record for the reviewer: what was wrong, and the original
    text when something was corrected. If the check itself fails, the draft is used as it was.
    """
    original = {k: draft.get(k) for k in CHECKED_FIELDS}
    tests = "\n".join(f"- attempt {r['attempt']}: {r['status']} ({r['summary']})" for r in state.get("test_runs", [])) or "(none)"
    plan = {k: v for k, v in (state.get("plan") or {}).items() if k in ("summary", "steps", "acceptance_criteria", "files_to_change")}
    user = prompts.PUBLISH_CHECK_USER.format(
        issue=state["issue"],
        plan=_clip(json.dumps(plan, indent=2), 4000) if plan else "(no plan recorded)",
        diff=_clip(state.get("repo_diff") or "", 15000),
        tests=tests,
        draft=json.dumps(original, indent=2),
    )
    try:
        checked = await asyncio.to_thread(provider.generate_json, prompts.PUBLISH_CHECK_SYSTEM, user, prompts.PUBLISH_CHECK_SCHEMA, purpose="check_publication")
    except LLMError as exc:
        return draft, {"status": "failed", "message": str(exc)[:500], "problems": []}
    problems = [
        {"field": str(p.get("field") or ""), "claim": str(p.get("claim") or "").strip(), "correction": str(p.get("correction") or "").strip()}
        for p in checked.get("problems") or []
        if isinstance(p, dict) and (p.get("claim") or p.get("correction"))
    ][:20]
    if not problems:
        return draft, {"status": "ok", "problems": []}
    corrected = dict(draft)
    for key in CHECKED_FIELDS:
        value = checked.get(key)
        if key == "risks" and isinstance(value, list):
            corrected[key] = [str(r) for r in value if str(r).strip()]
        elif isinstance(value, str) and value.strip():
            corrected[key] = value
    return corrected, {"status": "corrected", "problems": problems, "original": original}


def build_graph(deps: AgentDeps | None) -> StateGraph:
    """Build the workflow. ``deps`` may be None when the graph is only used to read state."""

    def d() -> AgentDeps:
        if deps is None:
            raise WorkflowError("workflow dependencies are not configured", code="internal_error")
        return deps

    def now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def step(name: str, fn: Callable[[AgentState], Awaitable[dict[str, Any]]], failure_status: str = "failed"):
        """Budget check before, timeline entry after, expected failures -> failure record."""

        async def node(state: AgentState) -> dict[str, Any]:
            budget = deps.budget if deps is not None else None
            before = budget.usage() if budget is not None else None
            started, clock = now(), time.monotonic()
            try:
                if budget is not None:
                    checks = BUDGET_CHECKS.get(name, ())
                    if "tools" in checks:
                        budget.check_tools(name)
                    if "llm" in checks:
                        budget.check_llm(name)
                update = await fn(state)
            except (LLMError, CodedError, BudgetExceeded, ToolNotFound) as exc:
                update = {"status": failure_status, "error": f"{name}: {exc}", "failure": failure_from_exception(exc, name)}
            update.setdefault("phase", name)
            failed = update.get("status") in FAILED_STATUSES and state.get("status") not in FAILED_STATUSES  # this step failed
            if failed and not update.get("failure"):
                message = str(update.get("error") or "").removeprefix(f"{name}: ")
                update["failure"] = make_failure(FAILURE_CODES_BY_STATUS.get(update["status"], "workflow_error"), name, message)
            entry: dict[str, Any] = {"step": name, "outcome": "failed" if failed else "ok", "started_at": started, "duration_ms": int((time.monotonic() - clock) * 1000)}
            if budget is not None and before is not None:
                entry.update(budget.usage().minus(before))
            update["timeline"] = [entry]
            return update

        node.__name__ = name
        return node

    # ------------------------------------------------------------------ nodes
    async def fetch_issue(state: AgentState) -> dict[str, Any]:
        number = state["issue_number"]
        outcome = await d().hub.call("get_github_issue", {"issue_number": number}, node="fetch_issue")
        if outcome.is_error:
            raise WorkflowError(f"could not read issue #{number}: {outcome.text}", code="issue_unavailable", retryable=outcome.temporary)
        return {"issue": outcome.text, "status": "running"}

    async def plan(state: AgentState) -> dict[str, Any]:
        """Plan in three parts: the model picks searches, the workflow runs them, the model plans with the results."""
        if not state.get("issue"):
            raise WorkflowError("cannot plan before the issue has been read", code="invalid_state")
        deps_ = d()
        extra = f"\nAdditional instructions from the user:\n{state['instructions']}\n" if state.get("instructions") else ""
        feedback = ""
        if state.get("plan_feedback"):
            feedback = prompts.PLAN_FEEDBACK.format(feedback=state["plan_feedback"], previous=_clip(json.dumps(state.get("plan") or {}, indent=2), 6000))
        context = {"repository": deps_.settings.github_repository, "issue": state["issue"], "instructions": extra, "feedback": feedback}

        asked = await asyncio.to_thread(
            deps_.provider.generate_json, prompts.PLAN_SEARCH_SYSTEM, prompts.PLAN_SEARCH_USER.format(**context), prompts.PLAN_SEARCH_SCHEMA, purpose="plan_searches"
        )
        queries = [q.strip() for q in asked.get("search_queries", []) if isinstance(q, str) and q.strip()][:5]
        if not queries:
            title = state["issue"].splitlines()[0].split(":", 1)[-1].strip()
            queries = [title[:80]] if title else []
        if not queries:
            raise WorkflowError("the model did not suggest any searches", code="plan_invalid")

        earlier = [s for s in state.get("searches", []) if s.get("by") == "workflow"]  # from an earlier version of the plan
        searches: list[dict[str, Any]] = []
        for query in queries:
            if any(s["query"] == query for s in earlier):
                continue
            outcome = await deps_.hub.call("search_repository", {"query": query}, node="plan")
            searches.append({"query": query, "ok": not outcome.is_error, "by": "workflow", "result": _clip(outcome.text, 3000)})
        if not any(s["ok"] for s in [*earlier, *searches]):
            raise WorkflowError("every repository search failed: " + "; ".join(s["result"][:200] for s in searches), code="search_failed")
        search_text = "\n\n".join(f"$ search_repository({s['query']!r})\n{s['result']}" for s in [*earlier, *searches])

        result = await asyncio.to_thread(
            deps_.provider.generate_json, prompts.PLAN_SYSTEM, prompts.PLAN_USER.format(**context, searches=search_text), prompts.PLAN_SCHEMA, purpose="plan"
        )
        result["search_queries"] = queries
        files = result.get("files_to_change")
        result["files_to_change"] = list(dict.fromkeys(f.strip().removeprefix("./") for f in files if isinstance(f, str) and f.strip()))[:30] if isinstance(files, list) else []
        return {"plan": result, "plan_feedback": "", "searches": searches}

    async def await_plan_approval(state: AgentState) -> dict[str, Any]:
        """Optional checkpoint (REQUIRE_PLAN_APPROVAL): approve the plan before any investigation."""
        if deps is None or not deps.settings.require_plan_approval:
            return {}
        plan_ = state["plan"]
        decision = interrupt(
            {
                "kind": "plan_approval",
                "reason": "revised plan" if state.get("plan_revisions", 0) else "initial plan",
                "plan": plan_,
                "issue_title": (state.get("issue") or "").splitlines()[0] if state.get("issue") else "",
            }
        )
        decision = decision if isinstance(decision, dict) else {"approved": bool(decision)}
        approved = bool(decision.get("approved"))
        feedback = str(decision.get("feedback") or "").strip()
        record = {
            "approved": approved,
            "feedback": feedback,
            "decided_by": str(decision.get("decided_by") or "human"),
            "decided_at": now(),
            "plan_sha256": _sha(json.dumps(plan_, sort_keys=True)),
        }
        deps.audit.log_event(deps.run_id, "plan_decided", record)
        if approved:
            return {"plan_approvals": [record]}
        revisions = state.get("plan_revisions", 0)
        if feedback and revisions < deps.settings.max_revisions:
            return {"plan_approvals": [record], "plan_feedback": feedback, "plan_revisions": revisions + 1}
        return {"plan_approvals": [record], "status": "rejected"}

    async def investigate(state: AgentState) -> dict[str, Any]:
        deps_ = d()
        planned = [s for s in state.get("searches", []) if s.get("by") == "workflow"]  # run while planning
        if not any(s["ok"] for s in planned):
            raise WorkflowError("cannot investigate before the repository has been searched", code="invalid_state")
        searches: list[dict[str, Any]] = []  # the model's own searches

        files: list[str] = []
        docs: list[dict[str, Any]] = []

        def on_call(spec: ToolSpec, args: dict[str, Any], outcome: ToolOutcome) -> None:
            if (record := _doc_record(spec, args, outcome, "investigate")) is not None:
                docs.append(record)
            if outcome.is_error:
                return
            if spec.name == "read_repository_file":
                files.append(str(args.get("path")))
            elif spec.name == "search_repository":
                searches.append({"query": args.get("query"), "ok": True, "by": "llm", "result": _clip(outcome.text, 1000)})

        tools = _llm_tools(deps_.hub, INVESTIGATE_TOOLS, optional=DOCS_TOOLS)
        search_text = "\n\n".join(f"$ search_repository({s['query']!r})\n{s['result']}" for s in planned)
        loop = await run_tool_loop(
            provider=deps_.provider,
            hub=deps_.hub,
            system_prompt=_system_prompt(prompts.INVESTIGATE_SYSTEM, tools),
            user_prompt=prompts.INVESTIGATE_USER.format(issue=state["issue"], plan=json.dumps(state["plan"], indent=2), searches=search_text),
            tools=tools,
            max_tool_calls=deps_.settings.max_tool_calls_per_step,
            node="investigate",
            on_call=on_call,
            budget=deps_.budget,
        )
        if loop.stop_reason == "refusal":
            raise WorkflowError("the model declined to continue during investigation", code="model_refused")
        if loop.budget_exhausted:
            raise BudgetExceeded("tool_calls", f"the run's tool-call limit (MAX_TOOL_CALLS_PER_RUN={deps_.settings.max_tool_calls_per_run}) was reached during investigation")
        return {"searches": searches, "files_examined": files, "docs_consulted": docs, "findings": loop.final_text.strip() or "(no findings reported)"}

    async def propose(state: AgentState) -> dict[str, Any]:
        if not any(s.get("ok") for s in state.get("searches", [])):
            raise WorkflowError("cannot propose changes before searching the repository", code="invalid_state")
        deps_ = d()
        context = ""
        if state.get("feedback"):
            previous = "\n".join(f"--- {p['path']} ---\n{p['diff']}" for p in state.get("proposals", [])) or "(none)"
            context += prompts.PROPOSE_REVISION_CONTEXT.format(feedback=state["feedback"], previous=_clip(previous, 8000))
        runs = state.get("test_runs", [])
        if runs and runs[-1]["status"] != "PASSED" and state.get("fix_attempts", 0) > 0:
            last = runs[-1]
            applied = "\n".join(f"- {c['path']} (+{c['added']}/-{c['removed']})" for c in state.get("applied_changes", [])) or "(none)"
            kind = last.get("failure_kind", "unknown")
            failing = ", ".join(last.get("failing_tests", [])[:20]) or "(none listed; see the output)"
            guidance = prompts.RECOVERY_GUIDANCE.format(guidance=state["recovery_guidance"]) if state.get("recovery_guidance") else ""
            context += prompts.PROPOSE_FIX_CONTEXT.format(
                attempt=state["fix_attempts"],
                max_attempts=deps_.settings.max_fix_attempts + state.get("extra_fix_attempts", 0),
                kind=test_results.KIND_TEXT.get(kind, kind),
                failing=failing,
                progress=test_results.progress_note(runs[-2] if len(runs) > 1 else None, last),
                applied=applied,
                guidance=guidance,
                output=_clip(last["output"], 12000),
            )

        batch = f"r{state.get('revisions', 0)}f{state.get('fix_attempts', 0)}"
        proposals: dict[str, Proposal] = {}
        files: list[str] = []
        docs: list[dict[str, Any]] = []

        def on_call(spec: ToolSpec, args: dict[str, Any], outcome: ToolOutcome) -> None:
            if (record := _doc_record(spec, args, outcome, "propose")) is not None:
                docs.append(record)
            if outcome.is_error:
                return
            if spec.name == "read_repository_file":
                files.append(str(args.get("path")))
            elif spec.name == "propose_patch":
                data = json.loads(outcome.text)
                proposals[data["path"]] = Proposal(  # a later proposal for the same file replaces the earlier one
                    id=f"{batch}-{len(proposals) + 1}",
                    path=data["path"],
                    patch=args["patch"],
                    diff=data["diff"],
                    added=data["added"],
                    removed=data["removed"],
                    is_new_file=data["is_new_file"],
                    patch_sha256=_sha(args["patch"]),
                )

        tools = _llm_tools(deps_.hub, PROPOSE_TOOLS, optional=DOCS_TOOLS)
        loop = await run_tool_loop(
            provider=deps_.provider,
            hub=deps_.hub,
            system_prompt=_system_prompt(prompts.PROPOSE_SYSTEM, tools),
            user_prompt=prompts.PROPOSE_USER.format(
                issue=state["issue"], plan=json.dumps(state["plan"], indent=2), findings=state.get("findings", ""), context=context
            ),
            tools=tools,
            max_tool_calls=deps_.settings.max_tool_calls_per_step,
            node="propose",
            on_call=on_call,
            budget=deps_.budget,
        )
        if loop.stop_reason == "refusal":
            raise WorkflowError("the model declined to propose a change", code="model_refused")
        if not proposals:
            if loop.budget_exhausted:
                raise BudgetExceeded("tool_calls", f"the run's tool-call limit (MAX_TOOL_CALLS_PER_RUN={deps_.settings.max_tool_calls_per_run}) was reached before a valid patch was proposed")
            why = f" within the step's limit of {deps_.settings.max_tool_calls_per_step} tool calls (MAX_TOOL_CALLS_PER_STEP)" if loop.hit_limit else ""
            raise WorkflowError(f"the model did not produce any valid patch{why}", code="no_valid_patch")
        return {
            "proposals": list(proposals.values()),
            "proposal_rationale": loop.final_text.strip(),
            "files_examined": files,
            "docs_consulted": docs,
            "feedback": "",
            "recovery_guidance": "",
        }

    async def await_approval(state: AgentState) -> dict[str, Any]:
        # Code before interrupt() re-runs on resume, so nothing with side effects goes here.
        proposals = state["proposals"]
        planned = (state.get("plan") or {}).get("files_to_change") or []
        decision = interrupt(
            {
                "kind": "patch_approval",
                # Files the plan did not list: worth a closer look before approving.
                "outside_plan": [p["path"] for p in proposals if planned and p["path"] not in planned],
                "reason": "fix after failing tests" if state.get("fix_attempts", 0) else ("revision" if state.get("revisions", 0) else "initial proposal"),
                "plan": state.get("plan"),
                "rationale": state.get("proposal_rationale", ""),
                "proposals": [{k: p[k] for k in ("id", "path", "diff", "added", "removed", "is_new_file", "patch_sha256")} for p in proposals],
            }
        )
        decision = decision if isinstance(decision, dict) else {"approved": bool(decision)}
        approved = bool(decision.get("approved"))
        feedback = str(decision.get("feedback") or "").strip()
        record = {
            "approved": approved,
            "feedback": feedback,
            "decided_by": str(decision.get("decided_by") or "human"),
            "decided_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "patches": {p["path"]: p["patch_sha256"] for p in proposals},
        }
        if deps is not None:
            deps.audit.log_event(deps.run_id, "approval_decided", record)
        if approved:
            return {"approvals": [record]}
        revisions = state.get("revisions", 0)
        if feedback and revisions < (deps.settings.max_revisions if deps else 0):
            return {"approvals": [record], "feedback": feedback, "revisions": revisions + 1}
        return {"approvals": [record], "status": "rejected"}

    async def apply(state: AgentState) -> dict[str, Any]:
        deps_ = d()
        approvals = state.get("approvals", [])
        if not approvals or not approvals[-1]["approved"]:
            raise WorkflowError("refusing to write files without an approval", code="invalid_state")
        approved = approvals[-1]["patches"]
        for proposal in state["proposals"]:
            if approved.get(proposal["path"]) != _sha(proposal["patch"]):
                raise WorkflowError(f"the patch for {proposal['path']} does not match what was approved", code="approval_mismatch")
        # Check every patch against the files as they are now before writing any, so a file
        # changed since the proposal stops the whole set instead of leaving it half applied.
        for proposal in state["proposals"]:
            check = await deps_.hub.call("propose_patch", {"path": proposal["path"], "patch": proposal["patch"]}, node="apply")
            if check.is_error:
                raise WorkflowError(
                    f"the approved patch for {proposal['path']} no longer applies ({check.text}); nothing was written",
                    code="apply_failed",
                )
        changes = []
        for proposal in state["proposals"]:
            outcome = await deps_.hub.call("apply_patch", {"path": proposal["path"], "patch": proposal["patch"]}, node="apply")
            if outcome.is_error:
                done = ", ".join(c["path"] for c in changes) or "none"
                raise WorkflowError(f"applying the approved patch to {proposal['path']} failed ({outcome.text}); already applied: {done}", code="apply_failed")
            data = json.loads(outcome.text)
            changes.append({"path": data["path"], "diff": data["diff"], "added": data["added"], "removed": data["removed"], "attempt": state.get("fix_attempts", 0)})
        diff = await deps_.hub.call("get_git_diff", {}, node="apply")
        return {"applied_changes": changes, "repo_diff": diff.text}

    async def run_tests(state: AgentState) -> dict[str, Any]:
        deps_ = d()
        attempt = state.get("fix_attempts", 0)
        outcome = await deps_.hub.call("run_tests", {"test_command": deps_.settings.test_command}, node="run_tests")
        if outcome.is_error:
            # The tests did not run at all (for example Docker is not running): a human decides.
            record = SandboxRunRecord(
                attempt=attempt, command=deps_.settings.test_command, status="ERROR", exit_code=None, summary="-",
                output=_clip(outcome.text, 4000), failure_kind="not_run", failing_tests=[],
            )
            return {"test_runs": [record], "recovery": {"reason": "tests_could_not_run", "message": f"The tests could not run: {outcome.text}"}}
        fields = dict(re.findall(r"^(status|exit_code|summary|command): (.*)$", outcome.text, re.MULTILINE))
        code = fields.get("exit_code", "")
        exit_code = int(code) if code.lstrip("-").isdigit() else None
        status = fields.get("status", "FAILED")
        record = SandboxRunRecord(
            attempt=attempt,
            command=fields.get("command", deps_.settings.test_command),
            status=status,
            exit_code=exit_code,
            summary=fields.get("summary", "-"),
            output=_clip(outcome.text, 20000),
            **test_results.analyze(status, outcome.text, exit_code, runner_for(fields.get("command", deps_.settings.test_command).split())),  # type: ignore[typeddict-item]
        )
        if record["status"] == "PASSED":
            return {"test_runs": [record], "tests_passed": True, "recovery": {}}
        allowed = deps_.settings.max_fix_attempts + state.get("extra_fix_attempts", 0)
        if attempt < allowed:
            return {"test_runs": [record], "fix_attempts": attempt + 1, "recovery": {}}
        message = f"The tests still fail after {attempt} fix attempt(s) (MAX_FIX_ATTEMPTS={deps_.settings.max_fix_attempts}): {record['summary']}"
        return {"test_runs": [record], "recovery": {"reason": "fix_attempts_exhausted", "message": message}}

    async def await_recovery(state: AgentState) -> dict[str, Any]:
        """Recovery checkpoint: fix attempts are used up, or the tests could not run."""
        recovery = state.get("recovery") or {}
        reason = recovery.get("reason", "fix_attempts_exhausted")
        runs = state.get("test_runs", [])
        last = runs[-1] if runs else {}
        extra = state.get("extra_fix_attempts", 0)
        retry_text = (
            "Run the tests again (for example after starting Docker)"
            if reason == "tests_could_not_run"
            else "Let the agent make one more fix attempt; your feedback is passed to it as guidance"
        )
        decision = interrupt(
            {
                "kind": "recovery",
                "reason": reason,
                "message": recovery.get("message", ""),
                "last_test_run": {k: last.get(k) for k in ("attempt", "status", "summary", "failure_kind", "failing_tests")},
                "fix_attempts": state.get("fix_attempts", 0),
                "max_fix_attempts": (deps.settings.max_fix_attempts if deps else 0) + extra,
                "applied_files": sorted({c["path"] for c in state.get("applied_changes", [])}),
                "options": [
                    {"decision": "retry", "approved": True, "description": retry_text},
                    {"decision": "stop", "approved": False, "description": "End the run; the applied changes stay in the working tree"},
                ],
            }
        )
        decision = decision if isinstance(decision, dict) else {"approved": bool(decision)}
        approved = bool(decision.get("approved"))
        guidance = str(decision.get("feedback") or "").strip()
        record = {"reason": reason, "approved": approved, "guidance": guidance, "decided_by": str(decision.get("decided_by") or "human"), "decided_at": now()}
        if deps is not None:
            deps.audit.log_event(deps.run_id, "recovery_decided", record)
        if approved:
            if reason == "tests_could_not_run":
                return {"recovery_decisions": [record], "recovery": {}}
            return {
                "recovery_decisions": [record],
                "recovery": {},
                "extra_fix_attempts": extra + 1,
                "fix_attempts": state.get("fix_attempts", 0) + 1,
                "recovery_guidance": guidance,
            }
        message = recovery.get("message") or "stopped at the recovery checkpoint"
        if reason == "tests_could_not_run":
            failure = make_failure("tests_could_not_run", "run_tests", message)
            return {"recovery_decisions": [record], "recovery": {}, "status": "failed", "error": f"run_tests: {message}", "failure": failure}
        failure = make_failure("tests_failed", "run_tests", message)
        return {"recovery_decisions": [record], "recovery": {}, "status": "tests_failed", "error": f"run_tests: {message}", "failure": failure}

    # ------------------------------------------------------------------ publication
    async def draft_publication(state: AgentState) -> dict[str, Any]:
        deps_ = d()
        if not state.get("tests_passed"):
            raise WorkflowError("refusing to publish before the tests pass", code="invalid_state")
        number = state["issue_number"]
        files = _per_file_stats(state.get("repo_diff") or "")
        feedback = ""
        if state.get("publication_feedback"):
            previous = json.dumps({k: state.get("publication_draft", {}).get(k) for k in EDITABLE_PUBLICATION_FIELDS}, indent=2)
            feedback = prompts.PUBLISH_FEEDBACK.format(feedback=state["publication_feedback"], previous=_clip(previous, 6000))
        user = prompts.PUBLISH_USER.format(
            issue=state["issue"],
            plan_summary=(state.get("plan") or {}).get("summary", "-"),
            files=", ".join(p for p, _, _ in files) or "none",
            diff=_clip(state.get("repo_diff") or "", 15000),
            feedback=feedback,
            issue_number=number,
        )
        draft = await asyncio.to_thread(deps_.provider.generate_json, prompts.PUBLISH_SYSTEM, user, prompts.PUBLISH_SCHEMA, purpose="draft_publication")
        check: dict[str, Any] | None = None
        if deps_.settings.check_pr_summary:
            draft, check = await check_publication(deps_.provider, state, draft)
        title_line = (state.get("issue") or "").splitlines()[0].split(":", 1)[-1].strip() if state.get("issue") else ""
        pr_title = " ".join(str(draft.get("pr_title") or "").split())[:120] or f"Fix #{number}: {title_line}"[:120]
        commit_message = str(draft.get("commit_message") or "").strip() or pr_title
        if f"#{number}" not in commit_message:
            commit_message += f"\n\nRefs #{number}"
        body = compose_pr_body(
            str(draft.get("summary") or ""), [str(r) for r in draft.get("risks") or []], files, state.get("test_runs", []), number, _references(state)
        )
        settings = deps_.settings
        return {
            "publication_draft": {
                "branch_name": branch_name_for(settings.github_branch_prefix, number, str(draft.get("branch_slug") or "")),
                "commit_message": commit_message,
                "pr_title": pr_title,
                "pr_body": body,
                "base": settings.github_default_branch,
                "draft": settings.github_pr_draft,
                "files": [p for p, _, _ in files],
                "push_target": f"github.com/{settings.github_repository}" if settings.github_mode == "api" else "origin (local fixture remote)",
                "summary_check": check,
            },
            "publication_feedback": "",
        }

    async def await_publication(state: AgentState) -> dict[str, Any]:
        draft = state["publication_draft"]
        decision = interrupt(
            {
                "kind": "publish_approval",
                **draft,
                "actions": [
                    {"tool": "create_branch", "description": f"create local branch {draft['branch_name']} from the current HEAD"},
                    {"tool": "commit_changes", "description": f"commit {len(draft['files'])} file(s) on that branch"},
                    {"tool": "push_branch", "description": f"push the branch to {draft['push_target']} (never forced)"},
                    {"tool": "create_pull_request", "description": f"open a {'draft ' if draft['draft'] else ''}pull request into {draft['base']}"},
                ],
                "publish_options": list(PUBLISH_LEVELS),
            }
        )
        decision = decision if isinstance(decision, dict) else {"approved": bool(decision)}
        approved = bool(decision.get("approved"))
        feedback = str(decision.get("feedback") or "").strip()
        level = decision.get("publish_up_to") or "pull_request"
        edits = decision.get("edits") or {}
        values = {k: draft[k] for k in EDITABLE_PUBLICATION_FIELDS}
        for key in EDITABLE_PUBLICATION_FIELDS:
            if isinstance(edits.get(key), str) and edits[key].strip():
                values[key] = edits[key].strip() if key != "pr_body" else edits[key]
        record = {
            "approved": approved,
            "publish_up_to": level,
            "feedback": feedback,
            "edited": sorted(k for k in EDITABLE_PUBLICATION_FIELDS if values[k] != draft[k]),
            "values": values,
            "values_sha256": _sha(json.dumps(values, sort_keys=True)),
            "decided_by": str(decision.get("decided_by") or "human"),
            "decided_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        if deps is not None:
            deps.audit.log_event(deps.run_id, "publication_decided", {k: v for k, v in record.items() if k != "values"} | {"branch_name": values["branch_name"]})
        if approved:
            if level not in PUBLISH_LEVELS:
                return {"publication_approvals": [record], "status": "publish_failed", "error": f"invalid publish_up_to {level!r}; expected one of {', '.join(PUBLISH_LEVELS)}"}
            return {"publication_approvals": [record]}
        revisions = state.get("publication_revisions", 0)
        if feedback and revisions < (deps.settings.max_revisions if deps else 0):
            return {"publication_approvals": [record], "publication_feedback": feedback, "publication_revisions": revisions + 1}
        # Declined: the tested changes stay in the working tree, uncommitted.
        return {"publication_approvals": [record], "status": "completed", "publication": {"status": "declined", "steps": []}}

    async def publish(state: AgentState) -> dict[str, Any]:
        deps_ = d()
        approvals = state.get("publication_approvals", [])
        if not approvals or not approvals[-1]["approved"]:
            raise WorkflowError("refusing to publish without an approval", code="invalid_state")
        approval = approvals[-1]
        values = approval["values"]
        if approval["values_sha256"] != _sha(json.dumps(values, sort_keys=True)):
            raise WorkflowError("publication values changed after approval", code="approval_mismatch")
        arguments = {
            "create_branch": {"branch_name": values["branch_name"]},
            "commit_changes": {"message": values["commit_message"]},
            "push_branch": {},
            "create_pull_request": {"title": values["pr_title"], "body": values["pr_body"]},
        }
        result: dict[str, Any] = {"status": "in_progress", "branch": values["branch_name"], "publish_up_to": approval["publish_up_to"], "steps": []}
        for tool in PUBLISH_STEPS[approval["publish_up_to"]]:
            outcome = await deps_.hub.call(tool, arguments[tool], node="publish")
            result["steps"].append({"tool": tool, "ok": not outcome.is_error, "result": _clip(outcome.text, 2000)})
            if outcome.is_error:
                done = [s["tool"] for s in result["steps"] if s["ok"]]
                result["status"] = "failed"
                message = f"{tool} failed: {outcome.text} (completed before the failure: {', '.join(done) or 'nothing'})"
                return {
                    "publication": result,
                    "status": "publish_failed",
                    "error": f"publish: {message}",
                    "failure": make_failure("publish_failed", "publish", message, retryable=False),
                }
            try:
                data = json.loads(outcome.text)
            except json.JSONDecodeError:
                data = {}
            if tool == "commit_changes":
                result["commit"] = data.get("commit")
                result["files"] = data.get("files", [])
                result["skipped"] = data.get("skipped", [])
            elif tool == "push_branch":
                result["remote"] = data.get("remote")
            elif tool == "create_pull_request":
                result["pull_request"] = data
        result["status"] = {"branch": "committed", "push": "pushed", "pull_request": "pr_created"}[approval["publish_up_to"]]
        return {"publication": result, "status": "published" if approval["publish_up_to"] == "pull_request" else "completed"}

    async def finalize(state: AgentState) -> dict[str, Any]:
        status = state.get("status") or "failed"
        if status == "running" and state.get("tests_passed"):
            status = "completed"
        if status not in FINAL_STATUSES:
            status = "failed"
        failure = state.get("failure")
        if status in FAILURE_CODES_BY_STATUS and not failure:
            failure = make_failure(FAILURE_CODES_BY_STATUS[status], state.get("phase") or "finalize", state.get("error") or f"the run ended with status '{status}'")
        files = sorted({c["path"] for c in state.get("applied_changes", [])})
        tests = "\n".join(f"- attempt {r['attempt']}: {r['status']} ({r['summary']})" for r in state.get("test_runs", [])) or "- none"
        publication = state.get("publication") or {}
        pr = publication.get("pull_request") or {}
        pub_text = (
            f"{publication.get('status', 'not published')}"
            + (f", branch {publication['branch']}" if publication.get("branch") else "")
            + (f", commit {publication['commit']}" if publication.get("commit") else "")
            + (f", pull request {pr.get('url')}" if pr.get("url") else "")
        )
        fallback = (
            f"Run finished with status '{status}'. Files changed: {', '.join(files) or 'none'}. "
            f"Tests:\n{tests}\nPublication: {pub_text}" + (f"\nError: {state['error']}" if state.get("error") else "")
            + (f"\nWhat to do: {failure['hint']}" if failure else "")
        )
        summary = fallback
        if deps is not None and status in {"completed", "published", "tests_failed", "publish_failed"}:
            title = (state.get("issue") or "").splitlines()[0] if state.get("issue") else f"#{state.get('issue_number')}"
            user = prompts.SUMMARY_USER.format(
                issue_title=title, status=status, plan_summary=(state.get("plan") or {}).get("summary", "-"),
                files_changed=", ".join(files) or "none", tests=tests, publication=pub_text, error=state.get("error") or "none",
            )
            try:
                summary = (await asyncio.to_thread(deps.provider.generate, prompts.SUMMARY_SYSTEM, user, purpose="summary")).strip() or fallback
            except (LLMError, BudgetExceeded):
                summary = fallback
        update: dict[str, Any] = {"status": status, "summary": summary, "phase": "finalize"}
        if failure:
            update["failure"] = failure
        return update

    # ------------------------------------------------------------------ edges
    def after(next_node: str):
        return lambda s: "finalize" if s.get("status") in FAILED_STATUSES else next_node

    def after_approval(state: AgentState) -> str:
        if state.get("status") in ("failed", "rejected"):
            return "finalize"
        return "apply" if state["approvals"][-1]["approved"] else "propose"

    def after_plan_approval(state: AgentState) -> str:
        if state.get("status") in FINAL_STATUSES:
            return "finalize"
        approvals = state.get("plan_approvals") or []
        return "plan" if approvals and not approvals[-1]["approved"] else "investigate"

    def after_tests(state: AgentState) -> str:
        if state.get("status") in FINAL_STATUSES:
            return "finalize"
        if state.get("tests_passed"):
            return "draft_publication"
        return "await_recovery" if (state.get("recovery") or {}).get("reason") else "propose"

    def after_recovery(state: AgentState) -> str:
        if state.get("status") in FINAL_STATUSES:
            return "finalize"
        return "run_tests" if state["recovery_decisions"][-1]["reason"] == "tests_could_not_run" else "propose"

    def after_publication_decision(state: AgentState) -> str:
        if state.get("status") in FINAL_STATUSES:
            return "finalize"
        return "publish" if state["publication_approvals"][-1]["approved"] else "draft_publication"

    graph = StateGraph(AgentState)
    for name, fn in [
        ("fetch_issue", fetch_issue),
        ("plan", plan),
        ("investigate", investigate),
        ("propose", propose),
        ("apply", apply),
        ("run_tests", run_tests),
    ]:
        graph.add_node(name, step(name, fn))
    # The tested change stays applied if publishing fails, so these report publish_failed.
    graph.add_node("draft_publication", step("draft_publication", draft_publication, failure_status="publish_failed"))
    graph.add_node("publish", step("publish", publish, failure_status="publish_failed"))
    graph.add_node("finalize", step("finalize", finalize))
    # Checkpoints are not wrapped: interrupt() must propagate, and their code re-runs on resume.
    graph.add_node("await_plan_approval", await_plan_approval)
    graph.add_node("await_approval", await_approval)
    graph.add_node("await_recovery", await_recovery)
    graph.add_node("await_publication", await_publication)

    graph.add_edge(START, "fetch_issue")
    graph.add_conditional_edges("fetch_issue", after("plan"), ["plan", "finalize"])
    graph.add_conditional_edges("plan", after("await_plan_approval"), ["await_plan_approval", "finalize"])
    graph.add_conditional_edges("await_plan_approval", after_plan_approval, ["plan", "investigate", "finalize"])
    graph.add_conditional_edges("investigate", after("propose"), ["propose", "finalize"])
    graph.add_conditional_edges("propose", after("await_approval"), ["await_approval", "finalize"])
    graph.add_conditional_edges("await_approval", after_approval, ["apply", "propose", "finalize"])
    graph.add_conditional_edges("apply", after("run_tests"), ["run_tests", "finalize"])
    graph.add_conditional_edges("run_tests", after_tests, ["propose", "draft_publication", "await_recovery", "finalize"])
    graph.add_conditional_edges("await_recovery", after_recovery, ["propose", "run_tests", "finalize"])
    graph.add_conditional_edges("draft_publication", after("await_publication"), ["await_publication", "finalize"])
    graph.add_conditional_edges("await_publication", after_publication_decision, ["publish", "draft_publication", "finalize"])
    graph.add_edge("publish", "finalize")
    graph.add_edge("finalize", END)
    return graph
