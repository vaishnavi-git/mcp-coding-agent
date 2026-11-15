"""Phase 4 building blocks: retry policy, reliable provider, prices and budget, failures, test analysis."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.config import PROJECT_ROOT
from app.llm.base import LLMAPIError, LLMAuthenticationError, LLMRateLimitError, LLMTimeoutError
from app.llm.reliable import ReliableProvider
from app.reliability import test_results
from app.reliability.budget import BudgetExceeded, BudgetLimits, PricingError, RunBudget, load_pricing
from app.reliability.failures import CodedError, failure_from_exception, make_failure
from app.reliability.retry import RetryPolicy
from app.storage.audit import AuditStore
from tests.fakes import ScriptedProvider

PRICES = PROJECT_ROOT / "config" / "model_prices.json"


# ---------------------------------------------------------------------- retry policy
def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(max_retries=6, base_delay=1, max_delay=10, jitter=0)
    assert [policy.next_delay(n) for n in range(1, 7)] == [1, 2, 4, 8, 10, 10]
    assert policy.next_delay(7) is None  # retry limit


def test_jitter_stays_within_bounds() -> None:
    low = RetryPolicy(base_delay=4, jitter=0.5, random=lambda: 0.999999)
    high = RetryPolicy(base_delay=4, jitter=0.5, random=lambda: 0.0)
    assert 2.0 <= low.next_delay(1) < 2.01 and high.next_delay(1) == 4.0


def test_retry_after_is_a_lower_bound_and_too_long_means_give_up() -> None:
    policy = RetryPolicy(max_retries=3, base_delay=1, max_delay=30, jitter=0)
    assert policy.next_delay(1, retry_after=7) == 7
    assert policy.next_delay(3, retry_after=1) == 4  # backoff already longer
    assert policy.next_delay(1, retry_after=3600) is None  # not worth an unbounded wait


@pytest.mark.parametrize("kwargs", [{"max_retries": -1}, {"multiplier": 0.5}, {"jitter": 2}, {"base_delay": -1}])
def test_invalid_policies_are_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


# ---------------------------------------------------------------------- reliable provider
def reliable(provider: ScriptedProvider, budget=None, max_retries: int = 3):
    sleeps: list[float] = []
    retries: list[dict] = []
    wrapped = ReliableProvider(
        provider, policy=RetryPolicy(max_retries=max_retries, base_delay=1, max_delay=30, jitter=0), budget=budget, on_retry=retries.append, sleep=sleeps.append
    )
    return wrapped, sleeps, retries


def test_transient_errors_are_retried_with_backoff_then_succeed() -> None:
    provider = ScriptedProvider(summary="ok")
    provider.errors["summary"] = [LLMRateLimitError("Anthropic rate limit reached (429).", retry_after=5), LLMAPIError("Anthropic API overloaded (529).", status_code=529, retryable=True)]
    wrapped, sleeps, retries = reliable(provider)
    assert wrapped.generate("s", "u", purpose="summary") == "ok"
    assert sleeps == [5, 2]  # Retry-After honored on the first retry, then exponential backoff
    assert [r["retry"] for r in retries] == [1, 2] and "LLMRateLimitError" in retries[0]["error"]
    assert provider.calls == ["summary"] * 3


def test_retries_are_limited_and_the_error_says_so() -> None:
    provider = ScriptedProvider()
    provider.errors["plan"] = LLMTimeoutError("The Anthropic API request timed out.")
    wrapped, sleeps, _ = reliable(provider, max_retries=2)
    with pytest.raises(LLMTimeoutError, match="Gave up after 3 attempts"):
        wrapped.generate_json("s", "u", {}, purpose="plan")
    assert sleeps == [1, 2] and provider.calls == ["plan"] * 3


@pytest.mark.parametrize(
    "error",
    [
        LLMAuthenticationError("Anthropic rejected the API key (401). Check ANTHROPIC_API_KEY.", status_code=401),
        LLMAPIError("Anthropic rejected the request (400): bad", status_code=400),
        LLMAPIError("Model 'x' was not found (404). Check ANTHROPIC_MODEL.", status_code=404),
    ],
)
def test_permanent_errors_are_not_retried(error) -> None:
    provider = ScriptedProvider()
    provider.errors["plan"] = error
    wrapped, sleeps, _ = reliable(provider)
    with pytest.raises(type(error)):
        wrapped.generate_json("s", "u", {}, purpose="plan")
    assert sleeps == [] and provider.calls == ["plan"]


def test_retry_after_longer_than_the_maximum_is_not_waited_for() -> None:
    provider = ScriptedProvider()
    provider.errors["plan"] = LLMRateLimitError("Anthropic rate limit reached (429); retry after 600s.", retry_after=600)
    wrapped, sleeps, _ = reliable(provider)
    with pytest.raises(LLMRateLimitError, match="RETRY_MAX_DELAY_SECONDS"):
        wrapped.generate_json("s", "u", {}, purpose="plan")
    assert sleeps == []


# ---------------------------------------------------------------------- prices
def test_prices_come_from_the_config_file() -> None:
    pricing = load_pricing("claude-sonnet-5", PRICES)
    assert pricing is not None and (pricing.input, pricing.output, pricing.cache_read, pricing.cache_write) == (2.0, 10.0, 0.2, 2.5)
    # 1M input + 100k output + 2M cache reads + 400k cache writes
    assert pricing.cost(1_000_000, 100_000, 2_000_000, 400_000) == pytest.approx(2.0 + 1.0 + 0.4 + 1.0)
    assert load_pricing("some-unlisted-model", PRICES) is None


def test_environment_prices_override_the_file() -> None:
    pricing = load_pricing("claude-sonnet-5", PRICES, {"input": 1.0, "output": 4.0, "cache_read": None, "cache_write": None})
    assert pricing is not None and pricing.source == "environment"
    assert (pricing.cache_read, pricing.cache_write) == pytest.approx((0.1, 1.25))  # default multipliers


def test_malformed_price_files_are_reported(tmp_path) -> None:
    bad = tmp_path / "prices.json"
    bad.write_text(json.dumps({"models": {"m": {"input": -1, "output": 2}}}))
    with pytest.raises(PricingError, match="non-negative"):
        load_pricing("m", bad)
    bad.write_text("{not json")
    with pytest.raises(PricingError, match="cannot read"):
        load_pricing("m", bad)


# ---------------------------------------------------------------------- run budget
def make_budget(tmp_path, **limits) -> tuple[RunBudget, AuditStore, str]:
    audit = AuditStore(tmp_path / "agent.db")
    run_id = audit.create_run(1, "demo/repo", "claude-sonnet-5")
    return RunBudget(audit, run_id, BudgetLimits(**limits), load_pricing("claude-sonnet-5", PRICES)), audit, run_id


def log_llm(budget: RunBudget, audit: AuditStore, run_id: str, **meta) -> None:
    audit.log_llm_call(run_id, {**meta, "cost_usd": budget.cost_of(meta)})


def log_tool(audit: AuditStore, run_id: str) -> None:
    audit.log_tool_call(run_id, server="s", tool="t", arguments={}, result_text="x", is_error=False, duration_ms=1, node="n", initiator="llm", risk="read")


def test_cost_limit_stops_the_next_model_call(tmp_path) -> None:
    budget, audit, run_id = make_budget(tmp_path, max_cost_usd=0.05)
    log_llm(budget, audit, run_id, purpose="plan", input_tokens=10_000, output_tokens=1_000)  # $0.02 + $0.01
    budget.check_llm("investigate")
    log_llm(budget, audit, run_id, purpose="investigate", input_tokens=10_000, output_tokens=2_000, cache_read_tokens=50_000)  # $0.05
    with pytest.raises(BudgetExceeded, match=r"spent \$0\.0800 of its \$0\.0500 limit \(MAX_TASK_COST_USD\).*\(propose\)") as info:
        budget.check_llm("propose")
    assert info.value.limit == "cost"
    # failed attempts are not billed and do not count as calls
    log_llm(budget, audit, run_id, purpose="x", error="LLMRateLimitError: 429")
    usage = budget.usage()
    assert (usage.llm_calls, usage.failed_llm_calls) == (2, 1) and usage.cost_usd == pytest.approx(0.08)


def test_call_limits_and_snapshot(tmp_path) -> None:
    budget, audit, run_id = make_budget(tmp_path, max_llm_calls=2, max_tool_calls=3)
    log_llm(budget, audit, run_id, input_tokens=1, output_tokens=1)
    log_llm(budget, audit, run_id, input_tokens=1, output_tokens=1)
    with pytest.raises(BudgetExceeded, match="MAX_LLM_CALLS_PER_RUN=2"):
        budget.check_llm("propose")
    for _ in range(2):
        log_tool(audit, run_id)
    assert budget.tool_calls_left() == 1
    budget.check_tools("investigate")
    log_tool(audit, run_id)
    with pytest.raises(BudgetExceeded, match="MAX_TOOL_CALLS_PER_RUN=3.*'propose'"):
        budget.check_tools("propose")
    snap = budget.snapshot()
    assert snap["tool_calls"] == 3 and snap["remaining"]["tool_calls"] == 0 and snap["remaining"]["llm_calls"] == 0
    assert snap["limits"]["max_cost_usd"] is None and snap["pricing"] == {"model": "claude-sonnet-5", "source": "model_prices.json"}


def test_usage_is_read_from_the_database_so_it_survives_restarts(tmp_path) -> None:
    budget, audit, run_id = make_budget(tmp_path, max_tool_calls=2)
    log_tool(audit, run_id)
    log_tool(audit, run_id)
    audit.close()
    reopened = AuditStore(tmp_path / "agent.db")
    fresh = RunBudget(reopened, run_id, BudgetLimits(max_tool_calls=2), None)
    with pytest.raises(BudgetExceeded):
        fresh.check_tools("investigate")


def test_old_databases_are_migrated(tmp_path) -> None:
    db = sqlite3.connect(tmp_path / "agent.db")
    db.executescript(
        """CREATE TABLE runs (id TEXT PRIMARY KEY, issue_number INTEGER NOT NULL, repository TEXT, model TEXT, status TEXT NOT NULL,
                              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT);
           CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT NOT NULL, purpose TEXT, model TEXT,
                              latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
                              cache_write_tokens INTEGER, stop_reason TEXT, request_id TEXT, error TEXT);
           INSERT INTO runs VALUES ('old-run', 7, 'demo/repo', 'm', 'completed', 't', 't', NULL);"""
    )
    db.commit()
    db.close()
    audit = AuditStore(tmp_path / "agent.db")
    assert audit.get_run("old-run")["failure"] is None
    audit.log_llm_call("old-run", {"purpose": "plan", "input_tokens": 5, "output_tokens": 5, "cost_usd": 0.5})
    assert audit.usage("old-run")["cost_usd"] == 0.5


# ---------------------------------------------------------------------- failures
@pytest.mark.parametrize(
    "error, code, retryable",
    [
        (BudgetExceeded("cost", "spent too much"), "budget_exceeded", False),
        (LLMAuthenticationError("bad key", status_code=401), "llm_auth", False),
        (LLMRateLimitError("429"), "llm_rate_limited", True),
        (LLMTimeoutError("timed out"), "llm_timeout", True),
        (LLMAPIError("Anthropic API overloaded (529).", status_code=529, retryable=True), "llm_unavailable", True),
        (LLMAPIError("Model 'x' was not found (404).", status_code=404), "llm_model_not_found", False),
        (LLMAPIError("The model did not return valid JSON for plan."), "llm_bad_output", True),
        (LLMAPIError("The model declined this request (stop_reason=refusal)."), "llm_refusal", False),
        (CodedError("could not read issue", code="issue_unavailable", retryable=True), "issue_unavailable", True),
        (RuntimeError("MCP server 'docs' failed to start: boom"), "mcp_server_unavailable", True),
        (PricingError("bad prices"), "pricing_unknown", False),
        (ValueError("something odd"), "internal_error", True),
    ],
)
def test_failures_are_classified_with_a_hint(error, code, retryable) -> None:
    failure = failure_from_exception(error, "plan")
    assert (failure["code"], failure["step"], failure["retryable"]) == (code, "plan", retryable)
    assert failure["hint"] and failure["message"] and failure["at"]


def test_make_failure_defaults() -> None:
    failure = make_failure("tests_could_not_run", "run_tests", "Docker is not running")
    assert failure["retryable"] is False and "Docker" in failure["hint"]


# ---------------------------------------------------------------------- test output analysis
FAILED_OUTPUT = """status: FAILED
exit_code: 1
...F.F
=========================== short test summary info ============================
FAILED tests/test_sessions.py::test_refresh_rotates_refresh_token - AssertionError: assert 'alice' is None
FAILED tests/test_sessions.py::test_logout - KeyError: 'x'
2 failed, 4 passed in 0.05s
"""

COLLECTION_OUTPUT = """status: FAILED
exit_code: 2
==================================== ERRORS ====================================
___________________ ERROR collecting tests/test_sessions.py ____________________
E   SyntaxError: invalid syntax
=========================== short test summary info ============================
ERROR tests/test_sessions.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
"""


def test_failing_tests_and_kind_are_extracted() -> None:
    result = test_results.analyze("FAILED", FAILED_OUTPUT, 1)
    assert result == {
        "failure_kind": "test_failures",
        "failing_tests": ["tests/test_sessions.py::test_refresh_rotates_refresh_token", "tests/test_sessions.py::test_logout"],
    }
    assert test_results.analyze("FAILED", COLLECTION_OUTPUT, 2)["failure_kind"] == "collection_error"
    assert test_results.analyze("TIMED OUT", "killed", None)["failure_kind"] == "timeout"
    assert test_results.analyze("FAILED", "no tests ran in 0.01s", 5)["failure_kind"] == "no_tests"
    assert test_results.analyze("PASSED", "6 passed", 0) == {"failure_kind": "passed", "failing_tests": []}


def test_progress_between_attempts_is_described() -> None:
    first = {"status": "FAILED", "failure_kind": "test_failures", "failing_tests": ["a", "b"]}
    assert "did not help" in test_results.progress_note(first, dict(first))
    note = test_results.progress_note(first, {"status": "FAILED", "failure_kind": "test_failures", "failing_tests": ["b", "c"]})
    assert "now passing: a" in note and "still failing: b" in note and "newly failing (probably broken by your last change): c" in note
    note = test_results.progress_note(first, {"status": "FAILED", "failure_kind": "collection_error", "failing_tests": []})
    assert "'test_failures'" in note and "'collection_error'" in note
    assert test_results.progress_note(None, first) == ""
