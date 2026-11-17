"""LangGraph state for one coding-agent run (persisted by the SQLite checkpointer).

Everything here is plain JSON-serializable data. LLM conversations live only
inside a node's tool loop; between nodes the workflow passes structured
results (plan, findings, proposals), which keeps checkpoints small and makes
every step inspectable from the API.

It also holds the reliability records: a structured ``failure``, a ``timeline``
of steps (duration and the model calls, tool calls and cost each one used),
human decisions at the plan and recovery checkpoints, and per test run the
failure kind and failing tests.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class Proposal(TypedDict):
    id: str
    path: str
    patch: str
    diff: str
    added: int
    removed: int
    is_new_file: bool
    patch_sha256: str


class SandboxRunRecord(TypedDict):
    attempt: int
    command: str
    status: str  # PASSED | FAILED | TIMED OUT | ERROR
    exit_code: int | None
    summary: str
    output: str
    # from app.reliability.test_results
    failure_kind: str  # passed | test_failures | collection_error | timeout | no_tests | not_run | unknown
    failing_tests: list[str]


class TimelineEntry(TypedDict, total=False):
    step: str
    outcome: str  # ok | failed
    started_at: str
    duration_ms: int
    llm_calls: int  # used by this step
    tool_calls: int
    cost_usd: float


class AgentState(TypedDict, total=False):
    run_id: str
    issue_number: int
    instructions: str  # optional extra guidance from the user

    status: str  # running | completed | published | tests_failed | rejected | failed | publish_failed
    phase: str  # name of the last completed node
    error: str  # "<step>: <message>"
    failure: dict[str, Any]  # app.reliability.failures.Failure: code, step, message, retryable, hint, at
    timeline: Annotated[list[TimelineEntry], operator.add]

    issue: str
    plan: dict[str, Any]
    # Optional plan checkpoint (REQUIRE_PLAN_APPROVAL)
    plan_approvals: Annotated[list[dict[str, Any]], operator.add]
    plan_feedback: str
    plan_revisions: int
    searches: Annotated[list[dict[str, Any]], operator.add]
    files_examined: Annotated[list[str], operator.add]
    findings: str
    # Documentation searched or read by the model (from recorded tool calls)
    docs_consulted: Annotated[list[dict[str, Any]], operator.add]

    proposals: list[Proposal]  # the batch currently awaiting approval / being applied
    proposal_rationale: str
    approvals: Annotated[list[dict[str, Any]], operator.add]
    feedback: str  # latest human feedback after a rejection
    revisions: int

    applied_changes: Annotated[list[dict[str, Any]], operator.add]
    repo_diff: str
    test_runs: Annotated[list[SandboxRunRecord], operator.add]
    fix_attempts: int
    tests_passed: bool
    # Recovery checkpoint (fix attempts used up, or tests could not run)
    recovery: dict[str, Any]  # pending: {"reason", "message"}; empty when none
    recovery_decisions: Annotated[list[dict[str, Any]], operator.add]
    recovery_guidance: str  # the reviewer's guidance for the next fix attempt
    extra_fix_attempts: int  # attempts granted by the reviewer beyond MAX_FIX_ATTEMPTS

    # Publication (branch -> commit -> push -> pull request)
    publication_draft: dict[str, Any]  # branch_name, commit_message, pr_title, pr_body, ...
    publication_feedback: str
    publication_revisions: int
    publication_approvals: Annotated[list[dict[str, Any]], operator.add]
    publication: dict[str, Any]  # status, branch, commit, pr, steps

    summary: str
