"""Start, resume, retry and inspect agent runs.

Each invocation (start, resume after an approval, or retry after a failure)
opens the MCP servers, compiles the graph with the SQLite checkpointer, runs
until the next interrupt or the end, then closes everything. Runs therefore
survive process restarts: the API can be restarted while a run waits for
approval.

Phase 4:
- every invocation gets a ``RunBudget`` (tool calls, model calls, cost) whose
  usage comes from the audit database, and a provider wrapped in
  ``ReliableProvider`` (retries with backoff, budget check before each call);
- failures outside the graph (an MCP server that does not start, a missing
  API key, an unexpected exception) still produce a structured failure record;
- ``retry`` continues a failed or interrupted run from the last checkpoint
  before the step that failed, without repeating completed work;
- ``mark_interrupted_runs`` flags runs that a previous process left running.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.config import Settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.reliable import ReliableProvider
from app.mcp.client import MCPHub, stdio_server_configs, tool_retry_policy
from app.reliability.budget import BudgetLimits, Pricing, PricingError, RunBudget, load_pricing
from app.reliability.failures import CodedError, failure_from_exception, make_failure
from app.reliability.retry import RetryPolicy
from app.storage.audit import AuditStore

from .coding_agent import RESUMABLE_STEPS, AgentDeps, build_graph

log = logging.getLogger("app.runner")

ProviderFactory = Callable[[str], LLMProvider]
HubFactory = Callable[[str], AbstractAsyncContextManager[MCPHub]]

RETRYABLE_STATUSES = {"failed", "publish_failed", "interrupted"}
FAILURE_STATUSES = {"failed", "publish_failed", "tests_failed", "interrupted"}
STALE_STATUSES = ("queued", "running", "resuming")


class RunStateError(Exception):
    """The run does not exist or is not in a state that allows the request."""


def _root_cause(exc: BaseException) -> BaseException:
    """Errors raised inside the MCP client's task groups arrive wrapped; report the real one."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


class AgentRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        audit: AuditStore | None = None,
        provider_factory: ProviderFactory | None = None,
        hub_factory: HubFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.audit = audit or AuditStore(settings.database_path)
        self.provider_factory = provider_factory or self._default_provider
        self.hub_factory = hub_factory or self._default_hub
        self.sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}

    def _default_provider(self, run_id: str) -> LLMProvider:
        return AnthropicProvider.from_settings(self.settings)

    def _default_hub(self, run_id: str) -> MCPHub:
        return MCPHub(stdio_server_configs(self.settings), audit=self.audit, run_id=run_id, retry=tool_retry_policy(self.settings))

    def _config(self, run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 100}

    # ------------------------------------------------------------------ budget
    def limits(self) -> BudgetLimits:
        s = self.settings
        return BudgetLimits(max_cost_usd=s.max_task_cost_usd or None, max_llm_calls=s.max_llm_calls_per_run or None, max_tool_calls=s.max_tool_calls_per_run or None)

    def pricing(self) -> Pricing | None:
        s = self.settings
        return load_pricing(s.anthropic_model, s.model_prices_file, s.price_overrides)

    def budget(self, run_id: str) -> RunBudget:
        """Raises PricingError (malformed price file) or CodedError (no model configured, or a cost limit without a price)."""
        if not self.settings.anthropic_model:
            # The model name is never hardcoded; it comes only from ANTHROPIC_MODEL.
            raise CodedError("ANTHROPIC_MODEL is not set, so there is no model to run", code="model_not_configured")
        pricing = self.pricing()
        if self.settings.max_task_cost_usd and pricing is None:
            raise CodedError(
                f"MAX_TASK_COST_USD is {self.settings.max_task_cost_usd:g} but there is no price for model {self.settings.anthropic_model!r}, "
                "so the cost limit cannot be enforced",
                code="pricing_unknown",
            )
        return RunBudget(self.audit, run_id, self.limits(), pricing)

    def _usage(self, run_id: str) -> dict[str, Any]:
        try:
            pricing = self.pricing()
        except PricingError:
            pricing = None
        return RunBudget(self.audit, run_id, self.limits(), pricing).snapshot()

    def _provider(self, run_id: str, budget: RunBudget) -> LLMProvider:
        inner = self.provider_factory(run_id)
        inner.metrics_sink = lambda meta: self.audit.log_llm_call(run_id, {**meta, "cost_usd": budget.cost_of(meta)})
        s = self.settings
        policy = RetryPolicy(max_retries=s.anthropic_max_retries, base_delay=s.retry_base_delay_seconds, max_delay=s.retry_max_delay_seconds)
        return ReliableProvider(inner, policy=policy, budget=budget, on_retry=lambda info: self.audit.log_event(run_id, "llm_retry", info), sleep=self.sleep)

    # ------------------------------------------------------------------ commands
    def create_run(self, issue_number: int, instructions: str = "") -> str:
        if issue_number < 1:
            raise ValueError("issue_number must be a positive integer")
        return self.audit.create_run(issue_number, self.settings.github_repository, self.settings.anthropic_model, instructions)

    async def execute(self, run_id: str, issue_number: int, instructions: str = "") -> dict[str, Any]:
        initial = {
            "run_id": run_id,
            "issue_number": issue_number,
            "instructions": instructions,
            "status": "running",
            "revisions": 0,
            "fix_attempts": 0,
            "publication_revisions": 0,
            "plan_revisions": 0,
            "extra_fix_attempts": 0,
        }
        return await self._invoke(run_id, initial)

    async def start(self, issue_number: int, instructions: str = "") -> dict[str, Any]:
        run_id = self.create_run(issue_number, instructions)
        return await self.execute(run_id, issue_number, instructions)

    def claim_resume(self, run_id: str) -> None:
        """Reserve a waiting run for one approval decision (prevents double submission)."""
        run = self.audit.get_run(run_id)
        if run is None:
            raise RunStateError(f"run {run_id} not found")
        if run["status"] != "awaiting_approval":
            raise RunStateError(f"run {run_id} is '{run['status']}', not awaiting approval")
        self.audit.update_run(run_id, "resuming")

    async def resume(
        self,
        run_id: str,
        approved: bool,
        feedback: str = "",
        decided_by: str = "human",
        *,
        publish_up_to: str | None = None,
        edits: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Answer the pending checkpoint. ``publish_up_to`` and ``edits`` apply to publication approvals."""
        run = self.audit.get_run(run_id)
        if run is None:
            raise RunStateError(f"run {run_id} not found")
        if run["status"] == "awaiting_approval":
            self.claim_resume(run_id)
        elif run["status"] != "resuming":
            raise RunStateError(f"run {run_id} is '{run['status']}', not awaiting approval")
        decision: dict[str, Any] = {"approved": approved, "feedback": feedback, "decided_by": decided_by}
        if publish_up_to is not None:
            decision["publish_up_to"] = publish_up_to
        if edits:
            decision["edits"] = {k: v for k, v in edits.items() if v is not None}
        return await self._invoke(run_id, Command(resume=decision))

    # ------------------------------------------------------------------ retry (Phase 4)
    async def _resume_point(self, run_id: str) -> tuple[str, str | None, dict[str, Any] | None]:
        """Where a retry would continue: (kind, step, config).

        kind: "fresh" (no checkpoint yet), "await" (a checkpoint is waiting for a
        human), "resume" (stopped between steps), "replay" (a step failed: go back
        to the checkpoint before it), "finished" (nothing left to run), or
        "unknown" (no failed step recorded).
        """
        if not self.settings.checkpoint_path.exists():
            return "fresh", None, None
        async with AsyncSqliteSaver.from_conn_string(str(self.settings.checkpoint_path)) as saver:
            graph = build_graph(None).compile(checkpointer=saver)
            snapshot = await graph.aget_state(self._config(run_id))
            if not snapshot.values and not snapshot.next:
                return "fresh", None, None
            if any(task.interrupts for task in snapshot.tasks):
                return "await", snapshot.next[0] if snapshot.next else None, None
            if snapshot.next:
                return "resume", snapshot.next[0], self._config(run_id)
            failed_step = (snapshot.values.get("failure") or {}).get("step")
            if not failed_step:
                return ("finished" if snapshot.values.get("status") else "unknown"), None, None
            async for earlier in graph.aget_state_history(self._config(run_id)):
                if earlier.next == (failed_step,):
                    return "replay", failed_step, {**earlier.config, "recursion_limit": 100}
            return "unknown", failed_step, None

    async def prepare_retry(self, run_id: str) -> dict[str, Any]:
        """Check that a retry is allowed and reserve the run for it."""
        run = self.audit.get_run(run_id)
        if run is None:
            raise RunStateError(f"run {run_id} not found")
        if run["status"] not in RETRYABLE_STATUSES:
            raise RunStateError(f"run {run_id} is '{run['status']}'; only failed or interrupted runs can be retried")
        used = self.audit.count_events(run_id, "run_retry")
        limit = self.settings.max_run_retries
        if used >= limit:
            raise RunStateError(f"run {run_id} has already been retried {used} time(s), its limit (MAX_RUN_RETRIES={limit}); start a new run")
        kind, step, _ = await self._resume_point(run_id)
        if kind == "unknown":
            raise RunStateError(f"run {run_id} has no failed step to continue from; start a new run")
        if kind in ("resume", "replay") and step not in RESUMABLE_STEPS:
            raise RunStateError(
                f"run {run_id} stopped in '{step}', which changes files or GitHub, so it is not retried automatically. "
                "Check the repository (git status) and start a new run."
            )
        self.audit.update_run(run_id, "resuming")
        self.audit.log_event(run_id, "run_retry", {"from_step": step, "kind": kind, "retry": used + 1, "previous_status": run["status"]})
        return {"run_id": run_id, "from_step": step, "kind": kind, "retry": used + 1, "retries_left": limit - used - 1}

    async def retry(self, run_id: str, *, prepared: bool = False) -> dict[str, Any]:
        """Continue a failed or interrupted run from the last checkpoint before the failed step."""
        if not prepared:
            await self.prepare_retry(run_id)
        run = self.audit.get_run(run_id)
        assert run is not None
        kind, _, config = await self._resume_point(run_id)
        if kind == "fresh":
            return await self.execute(run_id, run["issue_number"], run.get("instructions") or "")
        if kind in ("await", "finished"):
            return await self._settle(run_id)  # back to the pending checkpoint, or to the recorded outcome
        return await self._invoke(run_id, None, config=config)

    def mark_interrupted_runs(self) -> list[str]:
        """Runs left queued/running/resuming by a previous process can never finish on their own."""
        marked = []
        for run in self.audit.runs_with_status(*STALE_STATUSES):
            failure = make_failure("interrupted", "-", f"the run was '{run['status']}' when the previous API process stopped")
            self.audit.update_run(run["id"], "interrupted", error=failure["message"], failure=failure)
            self.audit.log_event(run["id"], "run_interrupted", {"previous_status": run["status"]})
            marked.append(run["id"])
        return marked

    # ------------------------------------------------------------------ execution
    async def _invoke(self, run_id: str, payload: Any, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunStateError(f"run {run_id} is already executing")
        async with lock:
            self.audit.update_run(run_id, "running")
            self.audit.log_event(run_id, "invocation_started", {"resume": isinstance(payload, Command), "retry": config is not None})
            where = "startup"
            try:
                budget = self.budget(run_id)
                provider = self._provider(run_id, budget)
                async with self.hub_factory(run_id) as hub, AsyncSqliteSaver.from_conn_string(str(self.settings.checkpoint_path)) as saver:
                    graph = build_graph(AgentDeps(hub, provider, self.settings, self.audit, run_id, budget)).compile(checkpointer=saver)
                    where = "workflow"
                    await graph.ainvoke(payload, config or self._config(run_id))
            except Exception as exc:  # noqa: BLE001 - every failure must end in a recorded status
                exc = _root_cause(exc)
                if where == "workflow":
                    _, step, _ = await self._resume_point(run_id)
                    where = step or "workflow"
                log.warning("run %s failed in %s: %s: %s", run_id, where, type(exc).__name__, exc)
                failure = failure_from_exception(exc, where)
                self.audit.update_run(run_id, "failed", error=f"{where}: {failure['message']}", failure=failure)
                self.audit.log_event(run_id, "run_failed", {"failure": failure})
                return await self.view(run_id)
        return await self._settle(run_id)

    async def _settle(self, run_id: str) -> dict[str, Any]:
        """Record the run's status from its checkpointed state after an invocation."""
        view = await self.view(run_id)
        pending = view["pending_approval"]
        if pending:
            status = "awaiting_approval"
            self.audit.log_event(
                run_id,
                "approval_requested",
                {
                    "kind": pending.get("kind"),
                    "paths": [p["path"] for p in pending.get("proposals", [])] or pending.get("files", []),
                    "branch": pending.get("branch_name"),
                    "reason": pending.get("reason"),
                },
            )
        else:
            status = view["state"].get("status") or "failed"
        failure = view["state"].get("failure") if status in FAILURE_STATUSES else None
        self.audit.update_run(run_id, status, error=view["state"].get("error") if not pending else None, failure=failure)
        self.audit.log_event(run_id, "status", {"status": status, **({"failure": failure["code"]} if failure else {})})
        return await self.view(run_id)

    # ------------------------------------------------------------------ queries
    async def view(self, run_id: str) -> dict[str, Any]:
        run = self.audit.get_run(run_id)
        if run is None:
            raise RunStateError(f"run {run_id} not found")
        values: dict[str, Any] = {}
        pending = None
        if self.settings.checkpoint_path.exists():
            async with AsyncSqliteSaver.from_conn_string(str(self.settings.checkpoint_path)) as saver:
                graph = build_graph(None).compile(checkpointer=saver)
                snapshot = await graph.aget_state(self._config(run_id))
                values = dict(snapshot.values or {})
                for task in snapshot.tasks:
                    for item in task.interrupts:
                        pending = item.value
        files = list(dict.fromkeys(values.get("files_examined", [])))
        state = {
            "status": values.get("status"),
            "phase": values.get("phase"),
            "error": values.get("error"),
            "failure": values.get("failure"),
            "issue": values.get("issue"),
            "plan": values.get("plan"),
            "plan_approvals": values.get("plan_approvals", []),
            "plan_revisions": values.get("plan_revisions", 0),
            "searches": values.get("searches", []),
            "files_examined": files,
            "findings": values.get("findings"),
            "docs_consulted": values.get("docs_consulted", []),
            "proposals": values.get("proposals", []),
            "proposal_rationale": values.get("proposal_rationale"),
            "approvals": values.get("approvals", []),
            "applied_changes": values.get("applied_changes", []),
            "repo_diff": values.get("repo_diff"),
            "test_runs": values.get("test_runs", []),
            "fix_attempts": values.get("fix_attempts", 0),
            "extra_fix_attempts": values.get("extra_fix_attempts", 0),
            "revisions": values.get("revisions", 0),
            "tests_passed": values.get("tests_passed", False),
            "recovery_decisions": values.get("recovery_decisions", []),
            "publication_draft": values.get("publication_draft"),
            "publication_approvals": values.get("publication_approvals", []),
            "publication_revisions": values.get("publication_revisions", 0),
            "publication": values.get("publication"),
            "timeline": values.get("timeline", []),
            "summary": values.get("summary"),
        }
        failure = run.get("failure") or (state["failure"] if run["status"] in FAILURE_STATUSES else None)
        return {"run": run, "state": state, "pending_approval": pending, "failure": failure, "usage": self._usage(run_id), "retry": await self._retry_info(run)}

    async def _retry_info(self, run: dict[str, Any]) -> dict[str, Any]:
        used = self.audit.count_events(run["id"], "run_retry")
        info: dict[str, Any] = {"available": False, "from_step": None, "reason": None, "retries_used": used, "max_retries": self.settings.max_run_retries}
        if run["status"] not in RETRYABLE_STATUSES:
            return info
        if used >= self.settings.max_run_retries:
            info["reason"] = f"retry limit reached (MAX_RUN_RETRIES={self.settings.max_run_retries})"
            return info
        kind, step, _ = await self._resume_point(run["id"])
        info["from_step"] = step
        if kind == "unknown":
            info["reason"] = "no failed step to continue from"
        elif kind in ("resume", "replay") and step not in RESUMABLE_STEPS:
            info["reason"] = f"the run stopped in '{step}', which changes files or GitHub"
        else:
            info["available"] = True
        return info

    def audit_trail(self, run_id: str) -> dict[str, Any]:
        if self.audit.get_run(run_id) is None:
            raise RunStateError(f"run {run_id} not found")
        return {"tool_calls": self.audit.tool_calls(run_id), "llm_calls": self.audit.llm_calls(run_id), "events": self.audit.events(run_id)}

    async def discover_tools(self) -> dict[str, Any]:
        async with self.hub_factory("discovery") as hub:
            return {
                "servers": {name: {"instructions": text} for name, text in hub.instructions.items()},
                "tools": [spec.describe() for spec in sorted(hub.tools.values(), key=lambda s: (s.server, s.name))],
            }
