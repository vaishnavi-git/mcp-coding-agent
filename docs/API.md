# HTTP API

The FastAPI server (`uvicorn app.api.main:app --port 8000`) is what the UI uses, and you can use
it directly. Interactive docs are at http://127.0.0.1:8000/docs. The machine-readable schema is
[openapi.json](openapi.json); regenerate it with `python scripts/export_openapi.py`, and a test
fails if it is out of date.

The API has no authentication and listens on 127.0.0.1. Do not expose it on a network.

## Endpoints

| Method | Path | Purpose | Success | Errors |
|---|---|---|---|---|
| `GET` | `/health` | Liveness check | 200 `{"status": "ok"}` | |
| `GET` | `/tools` | Start the MCP servers and list their tools with risk classes | 200 | 503 if a server fails to start |
| `POST` | `/runs` | Start a run for an issue | 202 `{"run_id", "status": "queued"}` | 422 invalid body |
| `GET` | `/runs` | The 50 most recent runs | 200 list of run rows | |
| `GET` | `/runs/{run_id}` | Full run state: plan, proposals, diffs, tests, publication, failure, usage, retry | 200 | 404 |
| `POST` | `/runs/{run_id}/approval` | Answer the pending checkpoint | 202 `{"run_id", "status": "resuming", ...}` | 404; 409 if the run is not waiting |
| `POST` | `/runs/{run_id}/retry` | Continue a failed or interrupted run from the failed step | 202 | 404; 409 if it cannot be retried |
| `GET` | `/runs/{run_id}/audit` | Tool calls, model-call metadata and events | 200 | 404 |

Work happens in background tasks: `POST /runs`, `/approval` and `/retry` return at once, and you
poll `GET /runs/{run_id}` until `run.status` changes.

## A run from the command line

```bash
# start
curl -s -X POST localhost:8000/runs -H 'content-type: application/json' \
     -d '{"issue_number": 42, "instructions": "Keep the change minimal."}'
# {"run_id": "20261114-101502-ab12cd", "status": "queued"}

# poll until it waits at a checkpoint
curl -s localhost:8000/runs/20261114-101502-ab12cd | jq '.run.status, .pending_approval.kind'
# "awaiting_approval"
# "patch_approval"

# read the proposed diffs, then approve them
curl -s localhost:8000/runs/20261114-101502-ab12cd | jq -r '.pending_approval.proposals[].diff'
curl -s -X POST localhost:8000/runs/20261114-101502-ab12cd/approval \
     -H 'content-type: application/json' -d '{"approved": true}'

# when the tests pass: publish up to a pull request, with an edited title
curl -s -X POST localhost:8000/runs/20261114-101502-ab12cd/approval \
     -H 'content-type: application/json' \
     -d '{"approved": true, "publish_up_to": "pull_request", "edits": {"pr_title": "Make refresh tokens single-use"}}'
```

## `POST /runs`

```json
{"issue_number": 42, "instructions": "optional guidance, up to 2000 characters"}
```

`issue_number` must be positive. The run is recorded first, so it shows up in `GET /runs` even if
it fails before its first step.

## `GET /runs/{run_id}`

```json
{
  "run":   {"id": "...", "issue_number": 42, "repository": "demo/sample-repo", "model": "claude-sonnet-5",
            "status": "awaiting_approval", "created_at": "...", "updated_at": "...", "error": null, "failure": null},
  "state": {"plan": {...}, "searches": [...], "files_examined": [...], "proposals": [...], "approvals": [...],
            "applied_changes": [...], "repo_diff": "...", "test_runs": [...], "publication": {...},
            "timeline": [...], "docs_consulted": [...], "summary": "..."},
  "pending_approval": {"kind": "patch_approval", ...},
  "failure": null,
  "usage": {"llm_calls": 7, "tool_calls": 12, "input_tokens": 41230, "output_tokens": 3120, "cost_usd": 0.1137,
            "limits": {"max_cost_usd": 5.0, "max_llm_calls": 100, "max_tool_calls": 150},
            "remaining": {...}, "pricing": {"model": "claude-sonnet-5", "source": "model_prices.json"}},
  "retry": {"available": false, "from_step": null, "reason": null, "retries_used": 0, "max_retries": 3}
}
```

The numbers above only show the shape; they are not measurements.

- `state.plan`: `summary`, `steps`, `search_queries`, `acceptance_criteria`,
  `files_to_change`.
- `state.test_runs[]`: `attempt`, `command`, `status` (`PASSED`, `FAILED`, `TIMED OUT`,
  `ERROR`), `exit_code`, `summary`, `output`, `failure_kind`, `failing_tests`.
- `state.timeline[]`: one entry per step, with `step`, `outcome`, `started_at`, `duration_ms`,
  `llm_calls`, `tool_calls` and `cost_usd`.

### Run statuses

| Status | Meaning |
|---|---|
| `queued`, `running`, `resuming` | Work in progress |
| `awaiting_approval` | Waiting at a checkpoint (`pending_approval.kind`) |
| `published` | Pull request created |
| `completed` | Tests pass. Publication was declined, or stopped at commit or push (see `state.publication.status`) |
| `tests_failed` | The tests still failed and the reviewer chose to stop |
| `rejected` | The plan or patch was rejected without feedback |
| `publish_failed` | An approved publishing step failed; the error names the step and what was already done |
| `failed` | Any other failure; `failure.code` says which |
| `interrupted` | The API stopped while the run was executing; retry it to continue |

### Failure record

Every failed run has `failure`: `code`, `step`, `message`, `retryable`, `hint` (what to do) and
`at`. Codes include:
- `budget_exceeded` and `pricing_unknown`;
- `llm_rate_limited`, `llm_unavailable`, `llm_auth` and other `llm_*` codes;
- `issue_unavailable` and `mcp_server_unavailable`;
- `no_valid_patch`, `apply_failed`, `tests_could_not_run`, `tests_failed` and `publish_failed`.

The full list is in [app/reliability/failures.py](../app/reliability/failures.py).

## Checkpoints: `POST /runs/{run_id}/approval`

One body answers every checkpoint:

```json
{"approved": true, "feedback": "", "publish_up_to": "pull_request", "edits": {"branch_name": "...", "commit_message": "...", "pr_title": "...", "pr_body": "..."}}
```

| `pending_approval.kind` | Shows | `approved: true` | `approved: false` |
|---|---|---|---|
| `plan_approval` (only with `REQUIRE_PLAN_APPROVAL=true`; the plan's searches have already run) | `plan`, `reason` | Investigate | With `feedback`: a revised plan. Without: run `rejected` |
| `patch_approval` | `proposals[]` (`path`, `diff`, `added`, `removed`, `is_new_file`, `patch_sha256`), `outside_plan` (proposed files the plan did not list), `rationale`, `plan`, `reason` | Apply exactly these patches, then run the tests | With `feedback`: a revised proposal (up to `MAX_REVISIONS`). Without: run `rejected` |
| `recovery` | `reason` (`fix_attempts_exhausted` or `tests_could_not_run`), `message`, `last_test_run`, `fix_attempts`, `applied_files`, `options` | One more fix attempt (`feedback` becomes guidance for the model), or run the tests again | Stop: `tests_failed`, or `failed` if the tests never ran |
| `publish_approval` | `branch_name`, `commit_message`, `pr_title`, `pr_body`, `base`, `draft`, `files`, `actions`, `publish_options` | Publish up to `publish_up_to` (`branch`, `push` or `pull_request`; default `pull_request`), with any `edits` | With `feedback`: a redrafted publication. Without: `completed`, nothing published |

`publish_up_to` and `edits` are only used at `publish_approval`. Edited values are validated
before anything is published (an invalid branch name fails before the branch is created), and the
decision records which fields were edited.

## `POST /runs/{run_id}/retry`

Continues a `failed` or `interrupted` run from the last checkpoint before the failed step, at
most `MAX_RUN_RETRIES` times. The response says where it continues from:

```json
{"run_id": "...", "from_step": "propose", "kind": "replay", "retry": 1, "retries_left": 2, "status": "resuming"}
```

409 when the run is in another state, when the retry limit is reached, or when it failed in
`apply` or `publish`: those steps change files or GitHub, so the run is not retried
automatically. `GET /runs/{run_id}` shows under `retry` whether a retry is available, and why
not.

## `GET /runs/{run_id}/audit`

```json
{"tool_calls": [...], "llm_calls": [...], "events": [...]}
```

- **`tool_calls[]`:** `ts`, `node`, `initiator` (`llm` or `workflow`), `server`, `tool`, `risk`,
  `arguments` (redacted, long values clipped), `is_error`, `duration_ms`, `result_chars`,
  `result_sha256`, `result_head` (first line, redacted) and `attempt`.
- **`llm_calls[]`:** `purpose`, `model`, `latency_ms`, token counts, `stop_reason`,
  `request_id`, `error` and `cost_usd`. Never prompts or responses.
- **`events[]`:** `kind` and `detail`. Kinds include `approval_requested`, `approval_decided`,
  `plan_decided`, `recovery_decided`, `publication_decided`, `tool_rejected`, `tool_retry`,
  `llm_retry`, `run_retry`, `run_failed`, `run_interrupted` and `status`.

## `GET /tools`

Starts the MCP servers and returns each tool's `server`, `name`, `risk` (`read`, `write`,
`external`, `destructive`), `description`, MCP `annotations` and `input_schema`, plus each
server's instructions. The tool list is also in the [README](../README.md#tools).
