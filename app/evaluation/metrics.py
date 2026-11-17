"""Evaluation metrics, per run and aggregated (definitions: docs/EVALUATION.md).

Rates are computed over *finished* runs only; runs still waiting at a
checkpoint, running, or interrupted are counted but not rated. Any value that
cannot be computed from the records is None (shown as "n/a"): for example a
cost when the model has no configured price, or a recovery rate when no test
run ever failed.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .data import RunRecord, parse_time

FINISHED_STATUSES = frozenset({"published", "completed", "tests_failed", "rejected", "failed", "publish_failed"})
RESOLVED_STATUSES = frozenset({"published", "completed"})
DECISION_EVENTS = frozenset({"plan_decided", "approval_decided", "recovery_decided", "publication_decided"})
_DIFF_PATH = re.compile(r"^\+\+\+ b/(.+)$|^\(new (?:protected file|symbolic link|binary or unreadable file)(?: not shown)?: (.+)\)$", re.MULTILINE)


def percentile(values: Iterable[float], pct: float) -> float | None:
    """Nearest-rank percentile (p50 = median of an odd-sized list); None for no values."""
    data = sorted(values)
    if not data:
        return None
    rank = max(1, math.ceil(pct / 100 * len(data)))
    return data[rank - 1]


def mean(values: Iterable[float]) -> float | None:
    data = list(values)
    return sum(data) / len(data) if data else None


def ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _human_wait_seconds(events: list[dict[str, Any]]) -> float:
    """Time between each approval request and the decision that answered it."""
    total, asked = 0.0, None
    for event in events:
        if event["kind"] == "approval_requested":
            asked = parse_time(event["ts"])
        elif event["kind"] in DECISION_EVENTS and asked is not None:
            decided = parse_time(event["ts"])
            if decided is not None:
                total += max(0.0, (decided - asked).total_seconds())
            asked = None
    return total


def _detail(event: dict[str, Any]) -> dict[str, Any]:
    detail = event.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            return {}
    return detail if isinstance(detail, dict) else {}


def files_in_diff(diff: str | None) -> set[str]:
    """Paths that still have a change in a get_git_diff result."""
    return {a or b for a, b in _DIFF_PATH.findall(diff or "")}


def run_metrics(record: RunRecord) -> dict[str, Any]:
    run, state = record.run, record.state
    status = run["status"]
    finished = status in FINISHED_STATUSES
    test_runs = state.get("test_runs") or []
    ran = [t for t in test_runs if t.get("status") != "ERROR"]  # ERROR: the tests could not start
    last = ran[-1]["status"] if ran else None
    had_failure = any(t.get("status") in ("FAILED", "TIMED OUT") for t in ran)

    plan = state.get("plan") or {}
    planned = {p.removeprefix("./") for p in plan["files_to_change"]} if isinstance(plan.get("files_to_change"), list) else None
    edited = list(dict.fromkeys(c["path"] for c in state.get("applied_changes") or []))
    in_final_diff = files_in_diff(state.get("repo_diff"))
    unplanned = [p for p in edited if planned is not None and p not in planned]
    reverted = [p for p in edited if state.get("repo_diff") is not None and p not in in_final_diff]

    successful_llm = [c for c in record.llm_calls if not c.get("error")]
    cost_known = all(c.get("cost_usd") is not None for c in successful_llm)
    started, ended = parse_time(run.get("created_at")), parse_time(run.get("updated_at"))
    timeline = state.get("timeline") or []

    by_step_cost: dict[str, float | None] = {}
    by_step_calls: dict[str, int] = defaultdict(int)
    for c in successful_llm:
        purpose = c.get("purpose") or "?"
        by_step_calls[purpose] += 1
        known = by_step_cost.get(purpose, 0.0)
        by_step_cost[purpose] = None if known is None or c.get("cost_usd") is None else known + c["cost_usd"]
    check = (state.get("publication_draft") or {}).get("summary_check") or {}
    decisions = [d for key in ("plan_approvals", "approvals", "recovery_decisions", "publication_approvals") for d in state.get(key) or []]
    reviewers = sorted({str(d.get("decided_by") or "human") for d in decisions})
    acceptance = next((_detail(e) for e in reversed(record.events) if e["kind"] == "acceptance_tests"), None)

    return {
        "run_id": run["id"],
        "issue": run["issue_number"],
        "model": run.get("model") or "unknown",
        "setup": describe_setup(run.get("setup"), run.get("model")),
        "status": status,
        "finished": finished,
        "failure_code": (run.get("failure") or {}).get("code") if isinstance(run.get("failure"), dict) else None,
        "started_at": run.get("created_at"),
        "resolved": finished and status in RESOLVED_STATUSES and last == "PASSED",
        "published": status == "published",
        "test_runs": len(ran),
        "test_runs_passed": sum(1 for t in ran if t.get("status") == "PASSED"),
        "first_test_run_passed": (ran[0]["status"] == "PASSED") if ran else None,
        "had_test_failure": had_failure,
        "recovered": had_failure and last == "PASSED",
        "fix_attempts": state.get("fix_attempts", 0),
        "tool_calls": len(record.tool_calls),
        "tool_calls_by_model": sum(1 for c in record.tool_calls if c.get("initiator") == "llm"),
        "tool_calls_by_workflow": sum(1 for c in record.tool_calls if c.get("initiator") != "llm"),
        "tool_retries": sum(1 for c in record.tool_calls if (c.get("attempt") or 1) > 1),
        "tool_calls_rejected": sum(1 for e in record.events if e["kind"] == "tool_rejected"),
        "llm_calls": len(successful_llm),
        "llm_calls_failed": len(record.llm_calls) - len(successful_llm),
        "input_tokens": sum(c.get("input_tokens") or 0 for c in successful_llm),
        "output_tokens": sum(c.get("output_tokens") or 0 for c in successful_llm),
        "cache_read_tokens": sum(c.get("cache_read_tokens") or 0 for c in successful_llm),
        "cache_write_tokens": sum(c.get("cache_write_tokens") or 0 for c in successful_llm),
        "calls_by_step": dict(sorted(by_step_calls.items())),
        "cost_by_step": dict(sorted(by_step_cost.items())),
        "summary_check": check.get("status"),
        "reviewer": "/".join(reviewers) if reviewers else None,
        "acceptance": acceptance.get("status") if acceptance else None,  # PASSED, FAILED, TIMED OUT, NOT RUN; None outside the benchmark
        "cost_usd": sum(c.get("cost_usd") or 0.0 for c in successful_llm) if cost_known else None,
        "wall_seconds": (ended - started).total_seconds() if finished and started and ended else None,
        "agent_seconds": sum(e.get("duration_ms") or 0 for e in timeline) / 1000 if timeline else None,
        "human_wait_seconds": _human_wait_seconds(record.events),
        "files_edited": edited,
        "files_planned": sorted(planned) if planned is not None else None,
        "unplanned_edits": unplanned if planned is not None else None,
        "reverted_edits": reverted,
        "llm_latency_ms": [(c.get("purpose") or "?", c["latency_ms"]) for c in successful_llm if c.get("latency_ms") is not None],
        "tool_duration_ms": [(c["tool"], c["duration_ms"]) for c in record.tool_calls if c.get("duration_ms") is not None],
        "step_duration_ms": [(e["step"], e["duration_ms"]) for e in timeline if e.get("duration_ms") is not None],
    }


def describe_setup(setup: dict[str, Any] | None, model: str | None = None) -> str:
    """A short label for how a run was configured: the main model, then any per-step changes in brackets."""
    if not setup:
        return model or "unknown"
    extras = [f"effort {setup['effort']}"] if setup.get("effort") else []
    extras += [f"{step}: {name}" for step, name in (setup.get("models_by_step") or {}).items()]
    extras += [f"{step} effort {level}" for step, level in (setup.get("effort_by_step") or {}).items()]
    if setup.get("check_pr_summary") is False:
        extras.append("no summary check")
    name = setup.get("model") or model or "unknown"
    return f"{name} ({', '.join(extras)})" if extras else name


def _steps(rows: list[dict[str, Any]], total_cost: float | None) -> dict[str, dict[str, Any]]:
    steps: dict[str, dict[str, Any]] = {}
    for r in rows:
        for purpose, calls in r["calls_by_step"].items():
            entry = steps.setdefault(purpose, {"calls": 0, "cost_usd": 0.0})
            entry["calls"] += calls
            cost = r["cost_by_step"].get(purpose)
            entry["cost_usd"] = None if entry["cost_usd"] is None or cost is None else entry["cost_usd"] + cost
    for entry in steps.values():
        entry["share"] = ratio(entry["cost_usd"], total_cost) if entry["cost_usd"] is not None and total_cost else None
    return dict(sorted(steps.items(), key=lambda item: -(item[1]["cost_usd"] or 0)))


def _distribution(pairs: Iterable[tuple[str, float]]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, list[float]] = defaultdict(list)
    for key, value in pairs:
        by_key[key].append(value)
    return {key: {"n": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95), "max": max(v)} for key, v in sorted(by_key.items())}


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    done = [r for r in rows if r["finished"]]
    resolved = [r for r in done if r["resolved"]]
    tested = [r for r in done if r["test_runs"]]
    failed_once = [r for r in done if r["had_test_failure"]]
    with_plan = [r for r in done if r["unplanned_edits"] is not None]
    edited_planned = sum(len(r["files_edited"]) for r in with_plan)
    edited_all = sum(len(r["files_edited"]) for r in done)
    costs = [r["cost_usd"] for r in done]
    cost_known = bool(done) and all(c is not None for c in costs)
    total_cost = sum(costs) if cost_known else None  # type: ignore[arg-type]
    prompt_tokens = sum(r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"] for r in done)
    checked = [r for r in done if r["summary_check"] in ("ok", "corrected")]
    judged = [r for r in done if r["acceptance"] is not None]
    return {
        "runs": len(rows),
        "finished": len(done),
        "unfinished": len(rows) - len(done),
        "status_counts": dict(sorted(_count(r["status"] for r in rows).items())),
        "resolved": len(resolved),
        "resolution_rate": ratio(len(resolved), len(done)),
        "published_rate": ratio(sum(1 for r in done if r["published"]), len(done)),
        "first_attempt_pass_rate": ratio(sum(1 for r in tested if r["first_test_run_passed"]), len(tested)),
        "test_run_pass_rate": ratio(sum(r["test_runs_passed"] for r in done), sum(r["test_runs"] for r in done)),
        "recovery_rate": ratio(sum(1 for r in failed_once if r["recovered"]), len(failed_once)),
        "runs_with_test_failure": len(failed_once),
        "mean_tool_calls": mean(r["tool_calls"] for r in done),
        "mean_tool_calls_by_model": mean(r["tool_calls_by_model"] for r in done),
        "mean_tool_calls_rejected": mean(r["tool_calls_rejected"] for r in done),
        "mean_tool_retries": mean(r["tool_retries"] for r in done),
        "mean_llm_calls": mean(r["llm_calls"] for r in done),
        "mean_input_tokens": mean(r["input_tokens"] for r in done),
        "mean_output_tokens": mean(r["output_tokens"] for r in done),
        "mean_wall_seconds": mean(r["wall_seconds"] for r in done if r["wall_seconds"] is not None),
        "median_wall_seconds": percentile([r["wall_seconds"] for r in done if r["wall_seconds"] is not None], 50),
        "mean_agent_seconds": mean(r["agent_seconds"] for r in done if r["agent_seconds"] is not None),
        "mean_human_wait_seconds": mean(r["human_wait_seconds"] for r in done),
        "files_edited": edited_all,
        "unplanned_edits": sum(len(r["unplanned_edits"]) for r in with_plan) if with_plan else None,
        "unplanned_edit_rate": ratio(sum(len(r["unplanned_edits"]) for r in with_plan), edited_planned) if with_plan else None,
        "reverted_edits": sum(len(r["reverted_edits"]) for r in done),
        "reverted_edit_rate": ratio(sum(len(r["reverted_edits"]) for r in done), edited_all),
        "total_cost_usd": total_cost,
        "mean_cost_per_run_usd": total_cost / len(done) if total_cost is not None else None,
        "cost_per_resolved_task_usd": total_cost / len(resolved) if total_cost is not None and resolved else None,
        "cache_hit_rate": ratio(sum(r["cache_read_tokens"] for r in done), prompt_tokens),
        "cost_by_step": _steps(done, total_cost),
        "summary_checks": len(checked),
        "summary_checks_corrected": sum(1 for r in checked if r["summary_check"] == "corrected"),
        "acceptance_runs": len(judged),
        "acceptance_passed": sum(1 for r in judged if r["acceptance"] == "PASSED"),
        "acceptance_pass_rate": ratio(sum(1 for r in judged if r["acceptance"] == "PASSED"), len(judged)),
        "reviewers": dict(sorted(_count(r["reviewer"] or "none" for r in done).items())),
        "llm_latency_ms": _distribution(p for r in done for p in r["llm_latency_ms"]),
        "tool_duration_ms": _distribution(p for r in done for p in r["tool_duration_ms"]),
        "step_duration_ms": _distribution(p for r in done for p in r["step_duration_ms"]),
    }


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return dict(counts)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-run metrics: overall, per main model, and per setup (models and effort per step)."""
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_setup: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_model[row["model"]].append(row)
        by_setup[row["setup"]].append(row)
    return {
        "overall": _aggregate(rows),
        "by_model": {model: _aggregate(group) for model, group in sorted(by_model.items())},
        "by_setup": {setup: _aggregate(group) for setup, group in sorted(by_setup.items())},
    }
