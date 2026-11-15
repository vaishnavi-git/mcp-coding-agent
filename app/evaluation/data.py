"""Load recorded runs: the run row, its checkpointed state, and its audit trail."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agents.runner import AgentRunner
from app.config import Settings


@dataclass
class RunRecord:
    run: dict[str, Any]  # audit row: id, issue_number, model, status, created_at, updated_at, failure, ...
    state: dict[str, Any]  # the workflow state as GET /runs/{id} shows it
    pending: dict[str, Any] | None = None  # the checkpoint the run is waiting at, if any
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def load_runs(
    settings: Settings,
    *,
    run_ids: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    issues: list[int] | None = None,
    runner: AgentRunner | None = None,
) -> list[RunRecord]:
    """Runs matching every given filter, oldest first. ``since``/``until`` apply to the start time."""
    runner = runner or AgentRunner(settings)
    records = []
    for row in runner.audit.all_runs():
        started = parse_time(row["created_at"])
        if run_ids and row["id"] not in run_ids:
            continue
        if issues and row["issue_number"] not in issues:
            continue
        if since and (started is None or started < since):
            continue
        if until and (started is None or started >= until):
            continue
        view = await runner.view(row["id"])
        trail = runner.audit_trail(row["id"])
        records.append(
            RunRecord(
                run=view["run"],
                state=view["state"],
                pending=view["pending_approval"],
                usage=view["usage"],
                tool_calls=trail["tool_calls"],
                llm_calls=trail["llm_calls"],
                events=trail["events"],
            )
        )
    return records
