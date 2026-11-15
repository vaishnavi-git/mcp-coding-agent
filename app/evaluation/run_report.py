"""Render one recorded run as a readable Markdown walkthrough (scripts/render_run.py).

The report contains the issue text and the code diffs of the run, so treat it
like the repository itself. Tool arguments and results come from the audit
log, which is already redacted; the report adds nothing that is not recorded.
"""

from __future__ import annotations

import json
from typing import Any

from .data import RunRecord
from .metrics import run_metrics
from .report import NA, ms, secs, usd


def _fence(text: str, lang: str = "") -> list[str]:
    fence = "````" if "```" in text else "```"
    return [f"{fence}{lang}", text.rstrip("\n"), fence]


def _quote(text: str) -> list[str]:
    return ["> " + line if line else ">" for line in text.strip().splitlines()]


def _json_or_text(value: str | None) -> Any:
    try:
        return json.loads(value or "")
    except ValueError:
        return value


def render_run(record: RunRecord, *, note: str | None = None) -> str:
    run, state = record.run, record.state
    m = run_metrics(record)
    out = [f"# Run `{run['id']}`: issue #{run['issue_number']}", ""]
    if note:
        out += _quote(note) + [""]
    out += [
        "| Status | Model | Started | Last update | Model calls | Tool calls | Cost | Agent time | Reviewer wait |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| {run['status']} | `{run.get('model')}` | {run.get('created_at')} | {run.get('updated_at')} | {m['llm_calls']} | {m['tool_calls']} "
        f"| {usd(m['cost_usd'])} | {secs(m['agent_seconds'])} | {secs(m['human_wait_seconds'])} |",
        "",
    ]
    failure = run.get("failure")
    if isinstance(failure, dict) and failure:
        out += [f"**Failure** `{failure.get('code')}` in `{failure.get('step')}`: {failure.get('message')}", "", f"*What to do:* {failure.get('hint')}", ""]
    if record.pending:
        out += [f"**Waiting at checkpoint:** `{record.pending.get('kind')}`", ""]

    if state.get("issue"):
        out += ["## 1. Issue", "", *_quote(state["issue"]), ""]

    plan = state.get("plan") or {}
    if plan:
        out += ["## 2. Plan", "", plan.get("summary", ""), ""]
        out += [f"{i}. {step}" for i, step in enumerate(plan.get("steps", []), start=1)] + [""]
        if plan.get("acceptance_criteria"):
            out += ["Acceptance criteria:", ""] + [f"- {c}" for c in plan["acceptance_criteria"]] + [""]
        if plan.get("files_to_change"):
            out += ["Files expected to change: " + ", ".join(f"`{f}`" for f in plan["files_to_change"]), ""]
        for decision in state.get("plan_approvals") or []:
            out += [f"Plan checkpoint: {'approved' if decision.get('approved') else 'rejected'}" + (f" with feedback: {decision['feedback']}" if decision.get("feedback") else ""), ""]

    if state.get("searches") or state.get("files_examined"):
        out += ["## 3. Investigation", ""]
        for search in state.get("searches") or []:
            hits = len([line for line in (search.get("result") or "").splitlines() if line.strip()]) if search.get("ok") else 0
            out.append(f"- search `{search.get('query')}` ({search.get('by')}): {hits} matching line(s)" if search.get("ok") else f"- search `{search.get('query')}` failed")
        if state.get("files_examined"):
            out.append("- files read: " + ", ".join(f"`{f}`" for f in state["files_examined"]))
        out.append("")
        if state.get("findings"):
            out += ["Findings reported by the model:", "", *_quote(state["findings"]), ""]
        if state.get("docs_consulted"):
            out += ["Documentation consulted:", ""]
            for doc in state["docs_consulted"]:
                out.append(f"- read {doc.get('url')}" if doc.get("tool") == "read" else f"- searched `{doc.get('query')}` in `{doc.get('source')}`")
            out.append("")

    approvals = state.get("approvals") or []
    if approvals:
        out += ["## 4. Patch checkpoints", ""]
        for i, decision in enumerate(approvals, start=1):
            verdict = "approved" if decision.get("approved") else "rejected"
            files = ", ".join(f"`{p}`" for p in (decision.get("patches") or {}))
            out.append(f"{i}. {verdict} by {decision.get('decided_by', 'human')} at {decision.get('decided_at')}: {files}" + (f". Feedback: {decision['feedback']}" if decision.get("feedback") else ""))
        out.append("")

    changes = state.get("applied_changes") or []
    if changes:
        out += ["## 5. Applied changes", ""]
        for change in changes:
            out += [f"**`{change['path']}`** (fix attempt {change.get('attempt', 0)}, +{change.get('added')} / -{change.get('removed')})", ""]
            out += _fence(change.get("diff") or "", "diff") + [""]

    test_runs = state.get("test_runs") or []
    if test_runs:
        out += ["## 6. Test runs", "", "| Attempt | Status | Summary | Failure kind | Failing tests |", "|---|---|---|---|---|"]
        for t in test_runs:
            failing = ", ".join(f"`{x}`" for x in t.get("failing_tests") or []) or "-"
            out.append(f"| {t.get('attempt')} | {t.get('status')} | {t.get('summary')} | {t.get('failure_kind', NA)} | {failing} |")
        out.append("")
        for decision in state.get("recovery_decisions") or []:
            out += [f"Recovery checkpoint ({decision.get('reason')}): {'retry' if decision.get('approved') else 'stop'}" + (f", guidance: {decision['feedback']}" if decision.get("feedback") else ""), ""]

    publication = state.get("publication") or {}
    if publication:
        out += ["## 7. Publication", "", f"Status: `{publication.get('status')}`" + (f", branch `{publication['branch']}`" if publication.get("branch") else ""), ""]
        for step in publication.get("steps") or []:
            out.append(f"- `{step['tool']}`: {'ok' if step.get('ok') else 'failed'}")
        pr = publication.get("pull_request") or {}
        if pr:
            out.append(f"- pull request: {pr.get('url')} ({'draft' if pr.get('draft') else 'ready'}) into `{pr.get('base')}`")
        out.append("")
        approved = [a for a in state.get("publication_approvals") or [] if a.get("approved")]
        if approved and (approved[-1].get("values") or {}).get("pr_body"):
            values = approved[-1]["values"]
            out += [f"Pull request **{values.get('pr_title')}**:", "", *_fence(values["pr_body"], "markdown"), ""]

    timeline = state.get("timeline") or []
    if timeline:
        out += ["## 8. Timeline", "", "| Step | Outcome | Duration | Model calls | Tool calls | Cost |", "|---|---|---|---|---|---|"]
        out += [f"| `{e['step']}` | {e.get('outcome')} | {ms(e.get('duration_ms'))} | {e.get('llm_calls', 0)} | {e.get('tool_calls', 0)} | {usd(e.get('cost_usd'))} |" for e in timeline]
        out.append("")

    if record.tool_calls:
        out += ["## 9. Tool calls (audit log)", "", "| # | Step | By | Tool | Risk | Result | Duration | Attempt |", "|---|---|---|---|---|---|---|---|"]
        for i, call in enumerate(record.tool_calls, start=1):
            result = "error" if call.get("is_error") else "ok"
            out.append(f"| {i} | {call.get('node')} | {call.get('initiator')} | `{call.get('tool')}` | {call.get('risk')} | {result} | {ms(call.get('duration_ms'))} | {call.get('attempt') or 1} |")
        rejected = [e for e in record.events if e["kind"] == "tool_rejected"]
        if rejected:
            out += ["", f"{len(rejected)} requested call(s) were refused before reaching a server:"]
            for event in rejected:
                detail = _json_or_text(event.get("detail")) or {}
                out.append(f"- `{detail.get('tool')}` during {detail.get('node')}: {detail.get('reason')}")
        out.append("")

    if record.llm_calls:
        out += ["## 10. Model calls", "", "| # | Purpose | Model | Latency | Input tokens | Output tokens | Cost | Error |", "|---|---|---|---|---|---|---|---|"]
        for i, call in enumerate(record.llm_calls, start=1):
            out.append(
                f"| {i} | {call.get('purpose')} | `{call.get('model')}` | {ms(call.get('latency_ms'))} | {call.get('input_tokens') or 0} | "
                f"{call.get('output_tokens') or 0} | {usd(call.get('cost_usd'))} | {call.get('error') or '-'} |"
            )
        out.append("")

    if state.get("summary"):
        out += ["## 11. Final summary", "", *_quote(state["summary"]), ""]
    return "\n".join(out)
