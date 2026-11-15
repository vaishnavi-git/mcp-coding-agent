"""Minimal Streamlit UI over the FastAPI service.

Run the API first (uvicorn app.api.main:app), then:
    streamlit run app/ui/streamlit_app.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

import streamlit as st

API_URL = os.environ.get("AGENT_API_URL", "http://127.0.0.1:8000").rstrip("/")


def api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_URL}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        st.error(f"API error {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        st.error(f"Cannot reach the API at {API_URL}: {exc.reason}")
    return None


st.set_page_config(page_title="MCP Coding Agent", layout="wide")
st.title("MCP Coding Agent")

with st.sidebar:
    st.subheader("Start a run")
    issue_number = st.number_input("GitHub issue number", min_value=1, value=42, step=1)
    instructions = st.text_area("Extra instructions (optional)", height=80)
    if st.button("Start", type="primary"):
        started = api("POST", "/runs", {"issue_number": int(issue_number), "instructions": instructions})
        if started:
            st.session_state["run_id"] = started["run_id"]
    st.divider()
    runs = api("GET", "/runs") or []
    labels = {r["id"]: f"#{r['issue_number']}: {r['status']} ({r['id']})" for r in runs}
    if labels:
        current = st.session_state.get("run_id")
        ids = list(labels)
        selected = st.radio("Runs", ids, index=ids.index(current) if current in ids else 0, format_func=labels.get)
        st.session_state["run_id"] = selected
    if st.button("Refresh"):
        st.rerun()

run_id = st.session_state.get("run_id")
if not run_id:
    st.info("Start a run from the sidebar.")
    st.stop()

view = api("GET", f"/runs/{run_id}")
if not view:
    st.stop()
run, state, pending = view["run"], view["state"], view["pending_approval"]
usage, failure, retry = view.get("usage") or {}, view.get("failure"), view.get("retry") or {}


def of_limit(used: float | int, limit: float | int | None, money: bool = False) -> str:
    shown = f"${used:.2f}" if money else str(used)
    if limit is None:
        return shown
    return f"{shown} of {f'${limit:.2f}' if money else limit}"


def limit_label(name: str, limit: float | int | None, money: bool = False) -> str:
    if limit is None:
        return name
    return f"{name} (limit {f'${limit:.2f}' if money else limit})"


st.subheader(f"Run {run['id']}: issue #{run['issue_number']}")
limits = usage.get("limits") or {}
cols = st.columns(5)
cols[0].metric("Status", run["status"])
cols[1].metric("Phase", state.get("phase") or "-")
cols[2].metric("Fix attempts", state.get("fix_attempts", 0))
cols[3].metric(limit_label("Cost", limits.get("max_cost_usd"), money=True), f"${usage.get('cost_usd', 0):.2f}" if usage.get("pricing") else "unknown")
cols[4].metric(limit_label("Tool calls", limits.get("max_tool_calls")), usage.get("tool_calls", 0))
st.caption(
    f"Model calls: {of_limit(usage.get('llm_calls', 0), limits.get('max_llm_calls'))}"
    + (f" plus {usage['failed_llm_calls']} failed attempt(s), not billed" if usage.get("failed_llm_calls") else "")
    + f" · tokens in/out: {usage.get('input_tokens', 0):,} / {usage.get('output_tokens', 0):,}"
    + f" · cache read/write: {usage.get('cache_read_tokens', 0):,} / {usage.get('cache_write_tokens', 0):,}"
)
if failure:
    st.error(f"**{failure['code']}** during `{failure['step']}`: {failure['message']}")
    st.info(f"What to do: {failure['hint']}")
    if retry.get("available"):
        label = f"Retry from '{retry['from_step']}'" if retry.get("from_step") else "Retry the run"
        if st.button(f"{label} ({retry['max_retries'] - retry['retries_used']} retries left)", type="primary"):
            api("POST", f"/runs/{run_id}/retry")
            st.rerun()
    elif retry.get("reason"):
        st.caption(f"Retry not available: {retry['reason']}.")
elif run.get("error") or state.get("error"):
    st.error(run.get("error") or state.get("error"))
if run["status"] in ("queued", "running", "resuming"):
    st.info("The agent is working. Press Refresh to update.")

if state.get("issue"):
    with st.expander("Issue", expanded=False):
        st.markdown(state["issue"])

if state.get("plan"):
    st.markdown("### Plan")
    plan = state["plan"]
    st.write(plan.get("summary", ""))
    for i, step in enumerate(plan.get("steps", []), start=1):
        st.markdown(f"{i}. {step}")
    if plan.get("acceptance_criteria"):
        st.caption("Acceptance criteria: " + "; ".join(plan["acceptance_criteria"]))
    if plan.get("files_to_change"):
        st.caption("Files expected to change: " + ", ".join(f"`{f}`" for f in plan["files_to_change"]))

if state.get("searches") or state.get("files_examined"):
    with st.expander("Investigation", expanded=False):
        for s in state.get("searches", []):
            st.markdown(f"**search** `{s['query']}` ({s['by']})")
            st.code(s["result"][:1500])
        if state.get("files_examined"):
            st.markdown("**Files examined:** " + ", ".join(f"`{f}`" for f in state["files_examined"]))
        if state.get("findings"):
            st.markdown("**Findings**")
            st.write(state["findings"])

if state.get("docs_consulted"):
    reads = sum(1 for d in state["docs_consulted"] if d.get("tool") == "read")
    with st.expander(f"Documentation consulted ({reads} page{'s' if reads != 1 else ''} read)", expanded=False):
        st.caption("Recorded from the documentation tool calls. Pages that were read are cited in the pull request.")
        for d in state["docs_consulted"]:
            if d.get("tool") == "read":
                title = "".join(ch for ch in (d.get("title") or "") if ch not in "[]()<>`*_") or "(untitled)"
                st.markdown(f"- **read** {title}: {d['url']} · source `{d.get('source') or '-'}` · during {d.get('node')}")
            else:
                status = "ok" if d.get("ok") else "failed"
                st.markdown(f"- **search** `{d.get('query')}` in `{d.get('source')}` ({status}) · during {d.get('node')}")

if pending and pending.get("kind") == "plan_approval":
    st.markdown(f"### Approval needed: plan ({pending['reason']})")
    st.caption("Nothing has been investigated or changed yet. Approve the plan, or reject it with feedback to get a revised one.")
    feedback = st.text_area("Feedback (needed for a revised plan)")
    approve_col, reject_col = st.columns(2)
    if approve_col.button("Approve plan", type="primary"):
        api("POST", f"/runs/{run_id}/approval", {"approved": True})
        st.rerun()
    if reject_col.button("Reject plan"):
        api("POST", f"/runs/{run_id}/approval", {"approved": False, "feedback": feedback})
        st.rerun()
elif pending and pending.get("kind") == "recovery":
    title = "tests could not run" if pending["reason"] == "tests_could_not_run" else "tests still fail"
    st.markdown(f"### Decision needed: {title}")
    st.warning(pending["message"])
    last = pending.get("last_test_run") or {}
    if last.get("failing_tests"):
        st.markdown("**Failing tests:** " + ", ".join(f"`{name}`" for name in last["failing_tests"]))
    st.caption(
        f"Fix attempts used: {pending['fix_attempts']} of {pending['max_fix_attempts']} · "
        f"changed files: {', '.join(pending['applied_files']) or 'none'}"
    )
    for option in pending["options"]:
        st.markdown(f"- **{option['decision']}**: {option['description']}")
    guidance = st.text_area("Guidance for the next attempt (optional)") if pending["reason"] != "tests_could_not_run" else ""
    retry_col, stop_col = st.columns(2)
    if retry_col.button("Re-run the tests" if pending["reason"] == "tests_could_not_run" else "Try another fix", type="primary"):
        api("POST", f"/runs/{run_id}/approval", {"approved": True, "feedback": guidance})
        st.rerun()
    if stop_col.button("Stop the run"):
        api("POST", f"/runs/{run_id}/approval", {"approved": False})
        st.rerun()
elif pending and pending.get("kind") == "publish_approval":
    st.markdown("### Approval needed: publish to GitHub")
    st.caption(f"Push target: {pending['push_target']} · base branch: {pending['base']} · files: {', '.join(pending['files'])}")
    for action in pending["actions"]:
        st.markdown(f"- `{action['tool']}`: {action['description']}")
    level_labels = {
        "branch": "Create the branch and commit (local only)",
        "push": "…and push the branch",
        "pull_request": "…and open the pull request",
    }
    level = st.radio("Approve up to", pending["publish_options"], index=len(pending["publish_options"]) - 1, format_func=level_labels.get)
    branch_name = st.text_input("Branch name", pending["branch_name"])
    commit_message = st.text_area("Commit message", pending["commit_message"], height=110)
    pr_title = st.text_input("Pull request title", pending["pr_title"])
    pr_body = st.text_area("Pull request body (Markdown)", pending["pr_body"], height=320)
    feedback = st.text_area("Feedback (to have the agent redraft instead)")
    approve_col, reject_col = st.columns(2)
    if approve_col.button("Approve and publish", type="primary"):
        edits = {"branch_name": branch_name, "commit_message": commit_message, "pr_title": pr_title, "pr_body": pr_body}
        api("POST", f"/runs/{run_id}/approval", {"approved": True, "publish_up_to": level, "edits": edits})
        st.rerun()
    if reject_col.button("Reject"):
        api("POST", f"/runs/{run_id}/approval", {"approved": False, "feedback": feedback})
        st.rerun()
elif pending and pending.get("kind") == "patch_approval":
    st.markdown(f"### Approval needed ({pending['reason']})")
    if pending.get("rationale"):
        st.write(pending["rationale"])
    if pending.get("outside_plan"):
        st.warning("Not in the plan's list of files to change: " + ", ".join(f"`{p}`" for p in pending["outside_plan"]))
    for proposal in pending["proposals"]:
        st.markdown(f"**{proposal['path']}** (+{proposal['added']} / -{proposal['removed']})" + (" (new file)" if proposal["is_new_file"] else ""))
        st.code(proposal["diff"], language="diff")
    feedback = st.text_area("Feedback (needed if you reject and want a revision)")
    approve_col, reject_col = st.columns(2)
    if approve_col.button("Approve and apply", type="primary"):
        api("POST", f"/runs/{run_id}/approval", {"approved": True, "feedback": feedback})
        st.rerun()
    if reject_col.button("Reject"):
        api("POST", f"/runs/{run_id}/approval", {"approved": False, "feedback": feedback})
        st.rerun()

if state.get("applied_changes"):
    st.markdown("### Applied changes")
    for change in state["applied_changes"]:
        st.markdown(f"**{change['path']}** (attempt {change['attempt']})")
        st.code(change["diff"], language="diff")
if state.get("repo_diff"):
    with st.expander("Full repository diff (git diff)", expanded=True):
        st.code(state["repo_diff"], language="diff")

if state.get("test_runs"):
    st.markdown("### Test runs (Docker sandbox)")
    for run_record in state["test_runs"]:
        st.markdown(f"Attempt {run_record['attempt']}: **{run_record['status']}** ({run_record['summary']})")
        if run_record.get("failing_tests"):
            st.caption(f"{run_record.get('failure_kind')}: " + ", ".join(run_record["failing_tests"][:10]))
        with st.expander("Output"):
            st.code(run_record["output"])

publication = state.get("publication")
if publication:
    st.markdown(f"### Publication: {publication['status']}")
    pr = publication.get("pull_request") or {}
    if pr.get("url"):
        st.markdown(f"Pull request #{pr['number']}: {pr['url']}" + (" (draft)" if pr.get("draft") else ""))
    if publication.get("branch"):
        st.markdown(f"Branch `{publication['branch']}`" + (f", commit `{publication['commit']}`" if publication.get("commit") else ""))
    for step in publication.get("steps", []):
        st.markdown(f"- `{step['tool']}`: {'ok' if step['ok'] else 'FAILED'}")
    for skipped in publication.get("skipped", []):
        st.warning(f"Not committed: {skipped['path']} ({skipped['reason']})")

if state.get("summary"):
    st.markdown("### Summary")
    st.write(state["summary"])

if state.get("timeline"):
    with st.expander("Timeline (time, model calls, tool calls and cost per step)"):
        st.dataframe(
            [
                {
                    "step": e["step"],
                    "outcome": e["outcome"],
                    "started (UTC)": e["started_at"][11:19],
                    "seconds": round(e["duration_ms"] / 1000, 2),
                    "model calls": e.get("llm_calls"),
                    "tool calls": e.get("tool_calls"),
                    "cost (USD)": round(e.get("cost_usd") or 0, 4),
                }
                for e in state["timeline"]
            ],
            width="stretch",
        )

with st.expander("Audit log"):
    trail = api("GET", f"/runs/{run_id}/audit") or {}
    st.markdown("**Tool calls**")
    st.dataframe(
        [
            {k: c.get(k) for k in ("ts", "node", "initiator", "server", "tool", "risk", "attempt", "is_error", "duration_ms", "result_head")}
            for c in trail.get("tool_calls", [])
        ],
        width="stretch",
    )
    st.markdown("**LLM calls**")
    st.dataframe(trail.get("llm_calls", []), width="stretch")
    st.markdown("**Events**")
    st.dataframe(trail.get("events", []), width="stretch")
