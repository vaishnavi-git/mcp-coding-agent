"""HTTP API for starting runs, reviewing proposals and reading the audit trail.

Run with:  uvicorn app.api.main:app --reload
Docs:      http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agents.runner import AgentRunner, RunStateError
from app.config import Settings

log = logging.getLogger("app.api")

app = FastAPI(
    title="MCP Coding Agent",
    version="1.0.0",
    description="Resolve GitHub issues through MCP servers with human approval before any file change.",
)

_runner: AgentRunner | None = None


def get_runner() -> AgentRunner:
    global _runner
    if _runner is None:
        _runner = AgentRunner(Settings.from_env())
        # Runs left executing by a previous process cannot finish; mark them so they can be retried.
        interrupted = _runner.mark_interrupted_runs()
        if interrupted:
            log.warning("marked %d run(s) as interrupted: %s", len(interrupted), ", ".join(interrupted))
    return _runner


class StartRunRequest(BaseModel):
    issue_number: int = Field(gt=0, description="GitHub issue number to resolve")
    instructions: str = Field("", max_length=2000, description="Optional extra guidance for the agent")


class PublicationEdits(BaseModel):
    branch_name: str | None = Field(None, max_length=100)
    commit_message: str | None = Field(None, max_length=5000)
    pr_title: str | None = Field(None, max_length=256)
    pr_body: str | None = Field(None, max_length=60000)


class ApprovalDecision(BaseModel):
    """One answer for every checkpoint kind (see pending_approval.kind in GET /runs/{id}):

    - plan_approval / patch_approval / publish_approval: approve, or reject (with feedback to revise).
    - recovery: approved=true retries (one more fix attempt, or re-running the tests), false stops the run.
    """

    approved: bool
    feedback: str = Field("", max_length=4000, description="Revision request after a rejection, or guidance for the next fix attempt at a recovery checkpoint")
    publish_up_to: Literal["branch", "push", "pull_request"] | None = Field(
        None, description="Publication checkpoints only: how far to go (default pull_request when approved)"
    )
    edits: PublicationEdits | None = Field(None, description="Publication checkpoints only: values to use instead of the draft")


async def _safe(coro_fn, run_id: str, *args: Any, **kwargs: Any) -> None:
    runner = get_runner()
    try:
        await coro_fn(run_id, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - background task: record, don't crash the server
        log.exception("background execution of run %s failed", run_id)
        runner.audit.update_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/tools", summary="Discover the tools exposed by the connected MCP servers")
async def tools(runner: AgentRunner = Depends(get_runner)) -> dict[str, Any]:
    try:
        return await runner.discover_tools()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/runs", status_code=202, summary="Start resolving an issue")
async def start_run(body: StartRunRequest, background: BackgroundTasks, runner: AgentRunner = Depends(get_runner)) -> dict[str, Any]:
    run_id = runner.create_run(body.issue_number, body.instructions)
    background.add_task(_safe, runner.execute, run_id, body.issue_number, body.instructions)
    return {"run_id": run_id, "status": "queued"}


@app.get("/runs", summary="List recent runs")
def list_runs(runner: AgentRunner = Depends(get_runner)) -> list[dict[str, Any]]:
    return runner.audit.list_runs()


@app.get("/runs/{run_id}", summary="Run state: plan, proposals, diff, tests, failure, usage against limits, retry")
async def get_run(run_id: str, runner: AgentRunner = Depends(get_runner)) -> dict[str, Any]:
    try:
        return await runner.view(run_id)
    except RunStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/runs/{run_id}/approval", status_code=202, summary="Answer the pending checkpoint (plan, patch, recovery or publication)")
async def decide(run_id: str, body: ApprovalDecision, background: BackgroundTasks, runner: AgentRunner = Depends(get_runner)) -> dict[str, Any]:
    try:
        runner.claim_resume(run_id)
    except RunStateError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    edits = body.edits.model_dump(exclude_none=True) if body.edits else None
    background.add_task(_safe, runner.resume, run_id, body.approved, body.feedback, publish_up_to=body.publish_up_to, edits=edits)
    return {"run_id": run_id, "status": "resuming", "approved": body.approved, "publish_up_to": body.publish_up_to}


@app.post("/runs/{run_id}/retry", status_code=202, summary="Continue a failed or interrupted run from the step that failed")
async def retry(run_id: str, background: BackgroundTasks, runner: AgentRunner = Depends(get_runner)) -> dict[str, Any]:
    try:
        plan = await runner.prepare_retry(run_id)
    except RunStateError as exc:
        code = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    background.add_task(_safe, runner.retry, run_id, prepared=True)
    return {**plan, "status": "resuming"}


@app.get("/runs/{run_id}/audit", summary="Tool calls, LLM call metadata and workflow events")
def audit(run_id: str, runner: AgentRunner = Depends(get_runner)) -> dict[str, Any]:
    try:
        return runner.audit_trail(run_id)
    except RunStateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
