"""Structured failure records (Phase 4).

Every failed run carries one ``Failure``: a stable ``code`` for programs and
evaluation scripts, the ``step`` where it happened, the ``message`` (what went
wrong), ``retryable`` (whether retrying the run without changing anything may
succeed) and a ``hint`` (what the user can do about it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TypedDict

from app.llm.base import LLMAPIError, LLMAuthenticationError, LLMError, LLMRateLimitError, LLMTimeoutError
from app.mcp.client import ToolNotFound

from .budget import BudgetExceeded, PricingError


class Failure(TypedDict):
    code: str
    step: str
    message: str
    retryable: bool
    hint: str
    at: str


class CodedError(Exception):
    """An expected failure with a failure code (see HINTS)."""

    def __init__(self, message: str, *, code: str = "workflow_error", retryable: bool | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


HINTS: dict[str, str] = {
    "budget_exceeded": "Raise the limit named in the message if this issue needs more (set it in the environment and restart the API), then retry the run. Work done so far is kept.",
    "model_not_configured": "Set ANTHROPIC_MODEL in .env or the environment (.env.example has the recommended value), restart the API, then retry the run.",
    "pricing_unknown": "Add the model to config/model_prices.json, or set LLM_PRICE_INPUT_PER_MTOK and LLM_PRICE_OUTPUT_PER_MTOK, or set MAX_TASK_COST_USD=0 to run without a cost limit. Then retry the run.",
    "llm_auth": "Check ANTHROPIC_API_KEY and that the key may use ANTHROPIC_MODEL, restart the API, then retry the run.",
    "llm_rate_limited": "Anthropic kept rate limiting after the automatic retries. Wait a minute, then retry the run; it continues from this step.",
    "llm_timeout": "The Anthropic API did not answer within ANTHROPIC_TIMEOUT_SECONDS, even after retries. Retry the run; it continues from this step.",
    "llm_unavailable": "The Anthropic API was unavailable or overloaded, even after retries. Retry the run later; it continues from this step.",
    "llm_model_not_found": "Check ANTHROPIC_MODEL, restart the API, then retry the run.",
    "llm_bad_request": "Anthropic rejected the request as invalid. Check ANTHROPIC_MODEL, ANTHROPIC_EFFORT and ANTHROPIC_THINKING.",
    "llm_bad_output": "The model's answer was cut off or not in the expected format. Retry the run; if it was cut off, raise ANTHROPIC_MAX_TOKENS.",
    "llm_refusal": "The model declined to continue. Check the issue text, or start a new run with different instructions.",
    "llm_error": "Retry the run. If it fails again, look up the request id in the LLM call log.",
    "mcp_server_unavailable": "An MCP server could not start (see the message). Fix its configuration, then retry the run.",
    "tool_missing": "A required MCP server is not connected. Check the server configuration, then retry the run.",
    "issue_unavailable": "Check the issue number, GITHUB_REPOSITORY and GITHUB_TOKEN. If GitHub was only temporarily unavailable, retry the run.",
    "search_failed": "The repository could not be searched. Check REPO_PATH, then retry the run.",
    "plan_invalid": "Retry the run, or start a new run with instructions that name the code to look at.",
    "no_valid_patch": "Retry the run (the model may do better on a second try), or start a new run with instructions that narrow the change.",
    "model_refused": "The model declined to continue. Check the issue text, or start a new run with different instructions.",
    "approval_mismatch": "A patch differed from the one that was approved, so it was not written. Start a new run.",
    "apply_failed": "Every patch is checked before any is written, so if a file changed after the proposal nothing was written (the message says so). Otherwise some approved patches may already be applied: check `git status` and `git diff` before starting a new run.",
    "tests_could_not_run": "Tests run only in the Docker sandbox. Make sure Docker is running and the sandbox image is built (docker build -t mcp-agent-sandbox:latest sandbox/).",
    "tests_failed": "The approved changes are applied but the tests still fail. Review the diff and the test output, then fix by hand or discard with `git restore` and `git clean`.",
    "publish_failed": "The tested changes are in the working tree. The message says which publication steps already ran.",
    "interrupted": "The process stopped while the run was executing (for example the API restarted). Retry the run to continue from the last completed step.",
    "internal_error": "Unexpected error. Retry the run; if it fails again, check the API log.",
    "workflow_error": "See the message. Retry the run, or start a new one.",
}

RETRYABLE_BY_DEFAULT = {
    "model_not_configured",
    "llm_rate_limited",
    "llm_timeout",
    "llm_unavailable",
    "llm_bad_output",
    "llm_error",
    "mcp_server_unavailable",
    "plan_invalid",
    "no_valid_patch",
    "interrupted",
    "internal_error",
}


def make_failure(code: str, step: str, message: str, *, retryable: bool | None = None, hint: str | None = None) -> Failure:
    return Failure(
        code=code,
        step=step,
        message=message,
        retryable=code in RETRYABLE_BY_DEFAULT if retryable is None else retryable,
        hint=hint or HINTS.get(code, HINTS["workflow_error"]),
        at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _llm_code(exc: LLMError) -> str:
    text = str(exc)
    if isinstance(exc, LLMAuthenticationError):
        return "llm_auth"
    if isinstance(exc, LLMRateLimitError):
        return "llm_rate_limited"
    if isinstance(exc, LLMTimeoutError):
        return "llm_timeout"
    if isinstance(exc, LLMAPIError):
        if exc.status_code == 404:
            return "llm_model_not_found"
        if "refusal" in text:
            return "llm_refusal"
        if exc.retryable:
            return "llm_unavailable"
        if "valid JSON" in text or "max_tokens" in text.lower():
            return "llm_bad_output"
        if exc.status_code == 400:
            return "llm_bad_request"
    return "llm_error"


def failure_from_exception(exc: BaseException, step: str) -> Failure:
    if isinstance(exc, BudgetExceeded):
        return make_failure("budget_exceeded", step, str(exc))
    if isinstance(exc, PricingError):
        return make_failure("pricing_unknown", step, str(exc))
    if isinstance(exc, CodedError):
        return make_failure(exc.code, step, str(exc), retryable=exc.retryable)
    if isinstance(exc, LLMError):
        return make_failure(_llm_code(exc), step, str(exc))
    if isinstance(exc, ToolNotFound):
        return make_failure("tool_missing", step, str(exc).strip("'\""))
    if isinstance(exc, RuntimeError) and "MCP server" in str(exc):
        return make_failure("mcp_server_unavailable", step, str(exc))
    return make_failure("internal_error", step, f"{type(exc).__name__}: {exc}")
