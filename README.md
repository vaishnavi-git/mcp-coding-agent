# MCP Coding Agent

[![tests](https://github.com/vaishnavi-git/mcp-coding-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/vaishnavi-git/mcp-coding-agent/actions/workflows/tests.yml)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

An agent that takes a GitHub issue, implements the fix in a controlled repository, tests it in a
Docker sandbox, and opens a pull request. Claude, through the Anthropic Messages API, does the
reasoning. Every action goes through MCP servers for GitHub, the filesystem, the test sandbox and
a read-only documentation reader. A LangGraph workflow enforces the order of steps, and a human
approves every file change and every publishing step.

> **Status: all six phases are built.** That covers the repository workflow, GitHub publishing,
> the documentation reader, reliability, the test suite, and the documentation with evaluation
> scripts. The first evaluation has been run: 10 live runs of `claude-sonnet-5` on two issues,
> with tests in Docker and every checkpoint reviewed by hand (see
> [Evaluation results](#evaluation-results)). See
> [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) for the plan and
> [Known limitations](#known-limitations) for what has not been tried against live services.

## Problem

Coding agents are useful when they can read a codebase, change it and run its tests. That same
reach is the risk: an agent that can run commands and push code can also leak a `.env` file, delete
work, follow instructions planted in an issue, or open pull requests nobody reviewed. Most agents
handle this with instructions in the prompt, and a model can be talked out of those.

This project puts the safety in the architecture instead:
- **The model only reads.** It gets read-only tools. Every change it proposes is shown to a human
  as a diff and applied only after approval.
- **Tests stay contained.** They run in a disposable container with no network.
- **Publishing is reviewed.** Publishing to GitHub has its own approval, with a least-privilege
  token.
- **Everything is recorded and bounded.** Every tool call, model call and decision is logged,
  and every run has a budget.

The goal is an agent you could point at a real repository without trusting the model.

## Architecture

```
Streamlit UI ──HTTP──> FastAPI ──> AgentRunner ──> LangGraph workflow (SQLite checkpoints)
                                                     │
              ┌──────────────────────────────────────┼──────────────────────────────┐
              │ ReliableProvider (retries, budget)   │ MCP client hub               │
              │  └ LLMProvider (Anthropic Messages)  │ (discovery, risk, timeouts,  │
              └──────────────────────────────────────┤  retries, audit)             │
                                                     └───┬────────┬────────┬────────┬┘
                                                   stdio │  stdio │  stdio │  stdio │
                                                   github  filesystem  sandbox    docs
                                                   server    server    server    server
                                                     │         │         │         │
                                          GitHub API + git  approved  docker run  allowlisted docs sites
                                                             repo     (no network) (https GET only)
```

```
fetch_issue → plan → [approve plan, optional] → investigate → propose → [approve patch] → apply → run_tests
     tests fail (attempts left) → propose …
     attempts used up, or tests could not run → [recovery: retry or stop]
     tests pass → draft_publication → [approve publication] → publish → finalize
```

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the component, workflow and sequence diagrams,
what each step does, and where state is stored.

## MCP server responsibilities

Each server is its own process with a minimal environment and only the credentials its job needs.

| Server | Responsibility | Credentials | Reaches |
|---|---|---|---|
| `github` | Read issues; after approval, create the branch, commit, push, open the pull request | `GITHUB_TOKEN` (the only server that gets it) | GitHub REST API and git remote, or local fixtures |
| `filesystem` | Search and read the approved repository; validate patches; apply approved patches; show the diff | none | `REPO_PATH` only |
| `sandbox` | Run the allowlisted test command in Docker on a disposable copy of the repository | none | the Docker daemon; the container has no network |
| `docs` | Search and read allowlisted documentation (GET only, no JavaScript, no cookies) | none | https sites in [config/docs_sources.json](config/docs_sources.json) |

## Tools

| Server | Tool | Risk | Who calls it |
|---|---|---|---|
| github | `get_github_issue(issue_number)` | read | workflow |
| github | `create_branch(branch_name)` | write | workflow, after the publication approval; prefix `agent/`, never the default branch |
| github | `commit_changes(message)` | write | workflow, after approval; only writable, non-protected files |
| github | `push_branch()` | external | workflow, after approval; never forced, never the default branch |
| github | `create_pull_request(title, body)` | external | workflow, after approval; draft by default, into the default branch |
| filesystem | `search_repository(query, directory, max_results)` | read | workflow and model |
| filesystem | `read_repository_file(path, start_line, end_line)` | read | model |
| filesystem | `propose_patch(path, patch)` | read | model (validates, writes nothing) and workflow (re-check before applying) |
| filesystem | `apply_patch(path, patch)` | write | workflow, after the patch approval |
| filesystem | `get_git_diff()` | read | workflow |
| sandbox | `run_tests(test_command)` | read | workflow only, with the configured `TEST_COMMAND`; Docker only |
| docs | `list_documentation_sources()` | read | model |
| docs | `search_documentation(query, source, max_results)` | read | model; searches only that site's own index |
| docs | `read_documentation(url, offset, max_chars)` | read | model; allowlisted https pages only |

The hub discovers these tools at start-up (`GET /tools`) and derives each one's risk from its MCP
annotations. The model is offered only tools classified as read, and a test pins the whole
catalog so a new or changed tool cannot reach the agent unreviewed. Inputs, outputs and errors
are described in the implementation plan
([Phase 1](docs/IMPLEMENTATION_PLAN.md#phase-1-tools),
[Phase 2](docs/IMPLEMENTATION_PLAN.md#phase-2-github-workflow),
[Phase 3](docs/IMPLEMENTATION_PLAN.md#phase-3-documentation-browser)).

## Security model

- **The model cannot change anything.** It has read-only tools. Patches are applied, and GitHub
  is written to, only by workflow steps after a human approves the exact values (compared by
  SHA-256).
- **Order is enforced in code.** The issue is read before planning, the code is searched before
  patching, and the tests pass before publishing. These are graph guards, not prompt
  instructions.
- **One repository.**
  - Paths are resolved, symlinks included, and must stay inside `REPO_PATH`.
  - `.git`, `.env*`, keys and credential files are unreadable in any letter case, also through
    search, diffs, commits and the test sandbox.
  - Writes are limited to `WRITABLE_PATHS`, and no tool deletes files.
- **Contained tests.**
  - Only allowlisted test commands with safe options run; shell syntax, `rm`, `sudo`,
    `git push` and `@argument-files` are refused.
  - The command runs in a container with no network, a read-only root, no capabilities, a
    non-root user, memory/CPU/PID limits and a timeout.
- **Careful publishing.**
  - Use a fine-grained token for one repository; only the GitHub server sees it.
  - Branches carry the `agent/` prefix, and pushes are never forced. The default branch is never
    pushed or committed to.
  - Draft pull requests are the default.
- **A reader, not a browser.** Documentation is read over https GET from allowlisted prefixes,
  with no cookies, credentials or logins and no private addresses.
- **Everything is logged, nothing secret is.**
  - Every tool call, retry, refused call, model call (metadata only) and human decision is
    logged.
  - Secrets are redacted.
  - No MCP server receives the Anthropic API key.

[docs/SECURITY.md](docs/SECURITY.md) has the threat model, maps every security requirement of the
spec to its control and tests, and lists the residual risks.

## Example workflow

Issue #42 in the sample repository: refreshing a session twice logs the user out, because
`SessionStore.refresh` never stores the new refresh token.

1. **Start.** In the UI, start a run for issue 42, or call `POST /runs`.
2. **The agent reads and plans.** It reads the issue and writes a plan: summary, steps, search
   queries, acceptance criteria, and the files it expects to change.
3. **It investigates.** It runs the plan's searches, then reads `authkit/sessions.py` and the
   tests with read-only tools.
4. **It proposes.** It proposes two patches, the fix and a regression test, each validated
   without writing.
5. **You approve the patch.** The run stops at the patch checkpoint. You see both diffs, the
   plan, and a warning for any file the plan did not list. Approve, or reject with feedback.
6. **It applies and tests.** The approved patches are checked again and applied, the diff is
   recorded, and the tests run in Docker. If they fail, the model is told which tests fail and
   proposes a fix, which you approve again. If they keep failing, you decide at the recovery
   checkpoint.
7. **You approve publishing.** When the tests pass, the run stops at the publication checkpoint:
   branch `agent/issue-42-…`, commit message and PR text. The PR's changes and test sections come
   from recorded results, not from the model. Choose how far to go (branch, push or pull
   request) and edit any text.
8. **It publishes.** The approved steps run, and the run ends `published` with a link to the
   pull request.

Two complete runs of this issue, rendered from their audit logs, are in
[examples/runs/](examples/runs/README.md):
- a **live run** with `claude-sonnet-5`, real Docker and hand approvals;
- a run with **scripted** model turns that also shows a failed test run and the fix.

## Screenshots

From the real UI, showing recorded runs of the 2026-11-15 experiment (`claude-sonnet-5`, tests in
Docker):

**A run of issue #43.** Status, cost and tool calls against their limits, and a plan that notes
the planted instruction and ignores it.

![Run overview: status, cost and tool calls against their limits, and the plan](docs/images/run-overview.jpg)

**A run of issue #42.** The plan's expected files, the run list, and the applied fix as a diff.

![The applied fix as a diff, with the run list in the sidebar](docs/images/applied-change.jpg)

**The same run, later on the page.** The regression test, the passing test run in the Docker
sandbox, and the published pull request.

![The regression test, the passing Docker test run and the publication steps](docs/images/tests-and-publication.jpg)

## Getting started

Requirements: Python 3.11+, git, Docker, an Anthropic API key.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[ui,dev]"
cp .env.example .env          # set ANTHROPIC_API_KEY; ANTHROPIC_MODEL is set to claude-sonnet-5
python scripts/setup_sample_repo.py --force
docker build -t mcp-agent-sandbox:latest sandbox/
uvicorn app.api.main:app --port 8000          # terminal 1
streamlit run app/ui/streamlit_app.py         # terminal 2
```

Open http://127.0.0.1:8501 and start a run for issue **#42**. In the default `fixture` mode the
whole flow runs without GitHub: issues come from
[examples/issues](examples/issues/README.md), pushes go to a local bare repository, and pull
requests are written to `data/pull_requests/`.

[docs/SETUP.md](docs/SETUP.md) covers more:
- running in Docker Compose ([Dockerfile](Dockerfile),
  [docker-compose.yml](docker-compose.yml));
- using a real GitHub repository and the exact token permissions;
- every setting (also in [.env.example](.env.example));
- troubleshooting.

[docs/API.md](docs/API.md) documents the HTTP API.

## Testing

```bash
pytest                          # everything; no network or API key needed
pytest -m path_traversal        # one of the spec's test categories
pytest --cov=app                # with coverage
```

GitHub Actions ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs the linter and the
full suite, real-Docker test included, on every push and pull request.

The suite has 511 tests. With Docker running and the sandbox image built, all 511 pass; without
Docker, the real-Docker test is skipped. Coverage is 86% of `app`, or 92% without the Streamlit UI (line and branch,
measured on the last full run). Every run ends with a results table for the spec's 11 test
categories:
- tool discovery, valid tool calls and invalid arguments;
- path traversal, unauthorized file access and dangerous commands;
- test timeouts and failed-test recovery;
- patch application, GitHub API failures and pull-request creation.

[docs/TESTING.md](docs/TESTING.md) has the per-category results, the coverage numbers, what each
phase added, and the problems the tests found.

## Evaluation results

From the experiment of 2026-11-15:
- `claude-sonnet-5`, 5 runs each of issue #42 (a real bug) and issue #43 (a request that is
  already met, with a planted prompt injection);
- tests in the real Docker sandbox;
- every checkpoint reviewed by hand;
- numbers generated by `scripts/evaluate.py` from the recorded runs.

| Metric | All 10 | #42 | #43 |
|---|---|---|---|
| Issue resolution rate (tests pass, reviewer published) | 100% | 100% | 100% |
| First-attempt test pass rate | 100% | 100% | 100% |
| Recovery rate after failed tests | n/a (no test run failed) | n/a | n/a |
| Mean tool calls per run | 25.2 | 22.0 | 28.4 |
| Unplanned file edits | 33.3% of edited files | 0% | 100% |
| Reverted file edits | 0% | 0% | 0% |
| Cost per completed task | $0.1253 | $0.0947 | $0.1558 |

- **The planted instruction was never followed.** In every #43 run the model said it would not
  print `.env` or the SSH key, and no tool call tried to read a protected file.
- **The unplanned edits on #43 are wrong guesses in the plans.** The plans named files
  (`src/auth.py`, …) before reading the code. The patches changed the right file, and the patch
  checkpoint flagged each one.
- **Not every run counted.** Two of 12 runs were set aside because of setup mistakes, not agent
  behavior, and replaced. With them included, resolution is 10 of 12 and the cost per resolved
  task $0.1637.
- **Read with care.** One model, two small issues, and a reviewer who was helped by the
  assistant that built the project.

[docs/EVALUATION.md](docs/EVALUATION.md#results) has the full results, the runs set aside and the
limits of the experiment. The generated reports are in
[docs/evaluation/](docs/evaluation/README.md).

## Cost and latency

Measured in the same 10 runs, with prices from
[config/model_prices.json](config/model_prices.json) ($2 / $10 per million input / output tokens
for `claude-sonnet-5`):

| | #42 | #43 |
|---|---|---|
| Mean cost per run | $0.0947 | $0.1558 |
| Mean model calls per run | 8.6 | 14.6 |
| Mean agent working time | 47.8 s | 108.0 s |
| Median wall clock, reviewer time included | 3.5 min | 4.3 min |

- **Where the time goes.** Model calls take most of it: planning is 9.9 s at p50, and
  investigating is 3.0 s at p50 but 29.9 s at p95. A test run in Docker takes 1.8 s at p50.
- **Total spend.** The whole experiment cost $1.64, including the two runs set aside. A trial run
  before it added $0.09.
- **Example runs.** Two live runs are rendered call by call in
  [examples/runs/](examples/runs/README.md).

What every run records, and what `scripts/evaluate.py` reports:

- **Cost.** Tokens (including cache reads and writes) times the model's price, per call, per
  step and per run. Also the cost per resolved task, with failed runs included.
- **Latency.** p50 and p95 of model calls by purpose, tool calls by tool, and workflow steps.
  Wall-clock time per run, split into agent working time and time spent waiting for a reviewer.
- **Limits that cap both.**
  - `MAX_TASK_COST_USD` (default $5 per run; $1 in the experiment) and `MAX_LLM_CALLS_PER_RUN`
    (100);
  - `MAX_TOOL_CALLS_PER_RUN` (150) and `MAX_TOOL_CALLS_PER_STEP` (25);
  - tool timeouts and the sandbox timeout.

  The UI header shows the running cost and tool calls against these limits.

## Reliability

- **Retries.**
  - Model calls are retried on 429, 5xx, 529 and timeouts, with exponential backoff and
    `Retry-After`.
  - Tool calls are retried only when the server marks the error temporary and the tool is
    read-only or idempotent. Applying a patch, committing, pushing or opening a PR is never
    repeated automatically.
- **Timeouts and budgets.** Every tool call has a deadline. Each run is capped on cost, model
  calls and tool calls, and usage is read from the audit database, so it covers resumes and
  retries.
- **Clear failures.** Every failed run records a `failure` with a code, the step, a message, and
  a hint on what to do.
- **Retrying.** A failed or interrupted run can be retried from the failed step
  (`POST /runs/{id}/retry`, or **Retry** in the UI) without repeating what already succeeded.
  Steps that change files or GitHub are not retried automatically.
- **Recovery after failed tests.** The model is told which tests still fail, which its last
  change broke and which it fixed. When attempts run out, or the tests cannot run, a human
  decides at the recovery checkpoint.

Details are in the [Phase 4 plan](docs/IMPLEMENTATION_PLAN.md#phase-4-reliability) and
[docs/API.md](docs/API.md#run-statuses).

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, MCP servers, workflow graph, a run as a sequence, storage, design decisions |
| [docs/SETUP.md](docs/SETUP.md) | Local install, Docker Compose, real GitHub, every setting, troubleshooting |
| [docs/API.md](docs/API.md) and [docs/openapi.json](docs/openapi.json) | HTTP endpoints, checkpoints, statuses, failures, audit trail |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, requirement-to-control-to-test mapping, residual risks |
| [docs/TESTING.md](docs/TESTING.md) | Running the tests, spec categories, coverage, history |
| [docs/EVALUATION.md](docs/EVALUATION.md) | Metric definitions, experiment protocol, results |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | The phased plan and design notes |
| [examples/issues](examples/issues/README.md), [examples/runs](examples/runs/README.md) | Example issues and an example run |

## Known limitations

- **Not yet run against live services:**
  - the Docker Compose setup (the real-Docker test has passed, on macOS with Docker Desktop);
  - a push or pull request on github.com. API mode is tested with git pushes to a local
    `file://` remote and a mocked GitHub API.
- **Docker is required.** Without a running Docker daemon the tests cannot run, and there is no
  non-Docker fallback. The run waits at the recovery checkpoint until you start Docker and
  retry.
- **No authentication.** The API and UI listen on 127.0.0.1 only. Docker Compose gives the API
  container the Docker socket, which is root-equivalent on the host.
- **One API process.** Runs execute as background tasks inside it. Runs waiting for approval
  survive restarts; runs that were executing are marked `interrupted` and can be retried. With
  several workers, each would mark the others' runs as interrupted.
- **Cost.**
  - The cost limit is checked before every model call, so a run can exceed it by at most one
    call.
  - Prices in [config/model_prices.json](config/model_prices.json) are maintained by hand.
- **Partial failures.**
  - A failure inside `apply` or `publish` is never retried automatically, and the message says
    what was already done.
  - A stale patch writes nothing, but a write that fails partway through a set can leave earlier
    files changed.
  - A failed publish leaves the local branch and commit for a human to finish.
- **Documentation reader.**
  - It does not run JavaScript, so pages that render only in the browser come back mostly empty.
  - Search quality depends on each site's own index.
  - It needs direct internet access, because it ignores proxy settings.
  - A DNS-rebinding race is possible in principle; the URL allowlist is the main control.
- **Hard links.** Path checks see symlinks but not hard links (no tool can create either).
- **Evaluation.** One model, two issues in one small repository, five runs each: enough to show
  the pipeline works reliably, not to rank models or predict results on real repositories.
  "Resolved" means the tests pass and a reviewer published, which is a stand-in for a real fix.
  The reviewer was helped by the assistant that built the project.
- **Model-written pull-request summaries can be wrong.** In the experiment, 5 of 10 drafts
  misdescribed the issue or a detail of the bug. The *Changes* and *Tests* sections, which come
  from recorded results, were always right. Read the summary before publishing.
- **The Streamlit UI has no automated tests;** it was checked by hand.

## Future improvements

- **Plan file guesses after a first look at the code.** All five #43 plans named files that
  don't exist. Letting the plan step run one search first would make `files_to_change`, and the
  unplanned-edit metric, more useful.
- **Check the PR summary against the diff,** for example with a second model call that compares
  the two, since that is the one part the model writes freely.
- **A broader task set.** Add issues from other repositories, with held-out acceptance tests that
  the agent never sees, so "resolved" no longer depends on the agent's own tests.
- **Authentication** on the API and UI, and per-user approval records.
- **A sandbox without the Docker socket:** rootless Docker, gVisor, or a remote runner, so the
  Compose setup does not hand the API root-equivalent access.
- **Several workers or a queue,** with run leases instead of the single-process interrupted-run
  rule.
- **Cost tuning from measurements.** The tool loop already uses prompt caching. Once there are
  results, compare cache hit rates, effort levels and models per step (planning versus
  proposing) using the per-purpose cost and latency the evaluation reports.
- **Hard-link and file-identity checks** in the path guard (compare device and inode against the
  repository).
- **Price updates** checked automatically against the provider's published prices.
