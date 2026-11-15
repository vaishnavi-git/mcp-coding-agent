# MCP Coding Agent: Implementation Plan

An agent that takes a GitHub issue and implements the change in a controlled
repository. Claude (Anthropic Messages API) does the reasoning. Every action on
the outside world goes through MCP servers (GitHub, filesystem, sandbox). A
LangGraph workflow decides the order of steps and where a human must approve.

## Decisions

| Area | Choice |
|---|---|
| Language | Python 3.11+ |
| LLM | Anthropic Messages API through the official `anthropic` SDK, behind an `LLMProvider` interface |
| Model | `ANTHROPIC_MODEL` env var (default `claude-sonnet-5`); key from `ANTHROPIC_API_KEY`. Neither is hardcoded in application logic |
| Agent orchestration | LangGraph `StateGraph`, human approval via `interrupt()`, state persisted by the SQLite checkpointer |
| Tool protocol | MCP Python SDK 2.x: four stdio servers (GitHub, filesystem, sandbox, docs), one client hub with runtime tool discovery |
| Test sandbox | Docker only (`--network none`, read-only rootfs, dropped capabilities, CPU/memory/PID limits, timeout) |
| Persistence | SQLite: LangGraph checkpoints, run index, audit log |
| API | FastAPI |
| UI | Streamlit (thin client over the API) |
| Tests | pytest |

## Repository scope (Phase 1)

- **Target repository:** a local git checkout set by `REPO_PATH`. The project ships a sample
  repository (`examples/sample-repo`, a small token/session library with a real bug) and a
  setup script that copies it to `workspaces/sample-repo` and runs `git init`.
- **Issue source:** the GitHub REST API (`GITHUB_REPOSITORY`, optional `GITHUB_TOKEN`), or a
  JSON fixture (`GITHUB_MODE=fixture`) so the sample flow runs without GitHub.
- **Readable:** every file inside `REPO_PATH` except forbidden paths.
- **Writable:** paths under `WRITABLE_PATHS` (comma-separated directory prefixes; empty means
  the whole repository), excluding forbidden paths.
- **New files:** allowed through `apply_patch` with a `/dev/null` source, only in writable
  paths. Controlled by `ALLOW_NEW_FILES`.
- **Forbidden (never read or written):** anything outside `REPO_PATH`, `.git/`, `.env` and
  `.env.*`, private keys (`id_rsa*`, `id_ed25519*`, `*.pem`, `*.key`, `*.p12`), `.ssh/`, `.aws/`,
  `.netrc`, `.npmrc`, `.pypirc`, `credentials.json`, and symlinks that resolve outside the repo.
- **File deletion:** not supported by any tool.

## Folder structure

```
mcp-coding-agent/
├── app/
│   ├── config.py                  # Settings from environment / .env
│   ├── llm/
│   │   ├── base.py                # LLMProvider interface, neutral types, error classes
│   │   ├── anthropic_provider.py  # Anthropic Messages API implementation
│   │   └── reliable.py            # retry with backoff + budget check around any provider (Phase 4)
│   ├── mcp/
│   │   ├── security.py            # path guard, command allowlist, secret redaction
│   │   ├── patching.py            # unified-diff parser/applier (fuzzy offsets, exact context)
│   │   ├── git_utils.py           # hardened git subprocess wrapper
│   │   ├── github_server.py       # MCP server: get_github_issue
│   │   ├── filesystem_server.py   # MCP server: search, read, propose/apply patch, git diff
│   │   ├── sandbox_server.py      # MCP server: run_tests (Docker)
│   │   ├── docs_server.py         # MCP server: documentation search and reading (Phase 3)
│   │   ├── web_guard.py           # URL policy and guarded HTTP fetcher (Phase 3)
│   │   ├── html_text.py           # HTML -> readable text, main-content extraction (Phase 3)
│   │   ├── doc_search.py          # Sphinx / MkDocs / llms.txt search indexes (Phase 3)
│   │   └── client.py              # MCP client hub: connect, discover, classify, call, audit
│   ├── reliability/               # Phase 4
│   │   ├── retry.py               # RetryPolicy: retry limits, exponential backoff with jitter
│   │   ├── budget.py              # model prices, run budget (cost, LLM calls, tool calls)
│   │   ├── failures.py            # structured failure records: code, step, message, hint
│   │   └── test_results.py        # pytest output analysis (failing tests, failure kind, progress)
│   ├── agents/
│   │   ├── state.py               # AgentState schema
│   │   ├── prompts.py             # system/user prompt templates
│   │   ├── tool_loop.py           # bounded LLM <-> MCP tool loop used inside graph nodes
│   │   ├── coding_agent.py        # LangGraph workflow (nodes, edges, interrupts)
│   │   └── runner.py              # start / resume / inspect runs (shared by API and tests)
│   ├── evaluation/                # Phase 6
│   │   ├── data.py                # load recorded runs (audit log + checkpoints)
│   │   ├── metrics.py             # per-run and aggregate metrics
│   │   ├── report.py              # Markdown / JSON evaluation report
│   │   ├── run_report.py          # one run as a readable walkthrough
│   │   └── trials.py              # prepare the sample repository for an evaluation trial
│   ├── storage/
│   │   └── audit.py               # SQLite run index + tool-call and LLM-call logs
│   ├── api/
│   │   └── main.py                # FastAPI app
│   └── ui/
│       └── streamlit_app.py       # Streamlit UI
├── config/docs_sources.json       # documentation sources the agent may read (Phase 3)
├── config/model_prices.json       # USD per million tokens, per model (Phase 4 cost limit)
├── examples/
│   ├── sample-repo/               # target repo for demos and tests
│   ├── issues/                    # issue fixtures (#42, #43) and their description
│   └── runs/                      # rendered example runs (Phase 6)
├── sandbox/Dockerfile             # test-runner image (python + pytest, non-root)
├── scripts/
│   ├── setup_sample_repo.py
│   ├── evaluate.py                # metrics from recorded runs (Phase 6)
│   ├── render_run.py              # one run as Markdown (Phase 6)
│   ├── prepare_trial.py           # reset the sample repository for one evaluation trial
│   ├── make_example_run.py        # regenerate examples/runs/issue-42-scripted-run.md
│   └── export_openapi.py          # docs/openapi.json
├── tests/                         # spec_matrix.py maps spec test categories to tests (Phase 5)
├── docs/                          # architecture, setup, API, security, testing, evaluation, this plan
├── Dockerfile                     # app image (API + UI), non-root (Phase 6)
├── docker-compose.yml             # api, ui, sandbox image (Phase 6)
├── .env.example
└── pyproject.toml
```

## Phase 1 tools

| Server | Tool | Inputs | Output | Permission | Errors |
|---|---|---|---|---|---|
| github | `get_github_issue` | `issue_number: int` | Markdown: title, state, labels, body, comments | read, automatic | not found, auth/permission, rate limit, network/timeout: each returned as an MCP error result with a clear message |
| filesystem | `search_repository` | `query: str`, `directory: str = "."` | `path:line: text` matches (capped) | read, automatic | invalid/forbidden directory, empty query |
| filesystem | `read_repository_file` | `path: str`, `start_line`, `end_line` | numbered lines with header | read, automatic | missing, forbidden, outside repo, binary, too large |
| filesystem | `propose_patch` | `path: str`, `patch: str` (unified diff) | validation result, normalized diff, +/- stats; **writes nothing** | read (proposal), automatic | malformed diff, context mismatch, multi-file, delete, forbidden/non-writable path |
| filesystem | `apply_patch` | `path: str`, `patch: str` | applied diff | **write: requires human approval** | same as propose, plus stale file |
| filesystem | `get_git_diff` | none | `git diff HEAD` plus new untracked files | read, automatic | not a git repository |
| sandbox | `run_tests` | `test_command: str` | status, exit code, pytest summary, truncated output | runs automatically, but only inside the Docker sandbox and only for allowlisted commands | not allowlisted, shell syntax, Docker unavailable, timeout |

Risk levels come from MCP tool annotations (`readOnlyHint`, `destructiveHint`, `openWorldHint`).
The client reads them at discovery time, and the host can override them.

## Approval rules

| Action | Approval |
|---|---|
| Read / search files, read issue, view diff | No |
| Search or read documentation (allowlisted sources only, Phase 3) | No |
| Propose a patch | No (nothing is written) |
| Apply a patch (write files) | **Yes**: the workflow pauses at a LangGraph `interrupt` |
| Run tests | Automatic, in the Docker sandbox, allowlisted commands only |
| Create branch (and commit to it) | **Yes**: publication checkpoint (Phase 2) |
| Push the branch | **Yes**: publication checkpoint, listed separately |
| Create a pull request | **Yes**: publication checkpoint, listed separately |
| Force push, push to the default branch | Never (no tool can do it) |
| Arbitrary shell commands | Not available |

Workflow structure enforces approval as well as the policy. The model can only reach
read-only tools and `propose_patch`. `apply_patch` is called by a graph node after a human
approves, and it receives exactly the patch text the human saw.

## Phase 1 workflow (LangGraph)

```
START
  -> fetch_issue        get_github_issue (required first: no plan without an issue)
  -> plan               LLM -> structured plan + search queries (shown to the user)
  -> investigate        run every planned search_repository query, then an LLM loop over
                        read-only tools (search/read) to gather context
  -> propose            LLM loop with read-only tools + propose_patch; must end with >= 1 valid patch
  -> await_approval     interrupt(): plan, patches and diffs go to the human
       rejected + feedback -> propose (up to MAX_REVISIONS) | else -> finalize
  -> apply              apply_patch for each approved patch, then get_git_diff (diff shown)
  -> run_tests          run_tests(TEST_COMMAND) in Docker
       failed and attempts left -> propose (with the failure output) -> await_approval ...
  -> finalize           LLM summary of the outcome
END
```

Guards enforced in code, not only in prompts:

- No `plan` until the issue has been read.
- No `propose` until at least one `search_repository` call has succeeded.
- No file write without a recorded approval for that exact patch.
- Bounded loops: tool calls per node, revisions, and fix attempts.

## Security

- Path validation: resolve symlinks, then require containment in `REPO_PATH`. Forbidden
  globs are checked on every path component.
- Command allowlist: `shlex` parsing. Shell metacharacters are rejected. The argv prefix must
  match the allowlist (`pytest`, `python -m pytest`), and extra arguments must be safe pytest
  flags or in-repo paths. `rm`, `sudo`, `curl`, `git push --force` etc. are rejected with an
  explicit message.
- Docker isolation: the container runs on a disposable copy of the repository. It has
  `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, and memory, CPU and
  PID limits, and runs as a non-root user. On timeout it is killed.
- Secrets: MCP servers start with a minimal environment. Only the GitHub server receives
  `GITHUB_TOKEN`. The Anthropic key stays in the host process. Audit entries and tool output
  pass through secret redaction.
- Logging: every tool call (LLM-initiated or workflow-initiated) is recorded with redacted
  arguments and result metadata (size, hash, status line), not file contents. LLM calls
  record model, latency, token usage, stop reason and request id. Prompts, source code and
  API keys are never logged.
- Untrusted content: issue text, files and test output are data. The system prompt says so,
  and the model has no write tools to act on injected instructions.

## Testing plan

Phase 1 (this phase):

- Security: path traversal (`../`, absolute paths, symlink escape), forbidden files (`.env`,
  SSH keys, `.git`), writable-path enforcement, redaction.
- Patching: valid patch, offset hunks, new file, malformed/multi-file/delete patches,
  context mismatch.
- MCP: server connection over stdio, in-process discovery, annotations to risk level, valid
  calls, invalid arguments, unknown tools.
- GitHub server: fixture mode; API mode against a mock transport: success, 404, 401, 403
  rate limit, network failure.
- Sandbox server: allowlist accept/reject (`rm -rf`, `sudo`, `git push --force`, `;`, `curl`),
  Docker flags, timeout handling (fake `docker` binary), real Docker run when available.
- LLM provider: request shape, tool-use parsing, error mapping (401/429/timeout/5xx) through
  the real SDK with a mock HTTP transport, no secrets or source in logs.
- Workflow: issue to plan to search to proposal, then pause for approval with no write
  before approval. Approve, apply, diff, failing tests, fix, approve, passing tests. Also the
  rejection-with-feedback path. Uses a scripted LLM and a fake sandbox runner.
- API: start run, poll, approve, completion.

## Phase 2: GitHub workflow

### Tools (GitHub MCP server)

The model never receives these tools. Only the workflow calls them, after approval, with
exactly the values the human approved.

| Tool | Inputs | Output | Risk (from annotations) | Rules enforced by the server |
|---|---|---|---|---|
| `create_branch` | `branch_name` | confirmation | write (local) | Must start with `GITHUB_BRANCH_PREFIX` (default `agent/`), be a valid git ref, not be the default branch, and not already exist. Uncommitted changes carry over to it |
| `commit_changes` | `message` | short SHA, files committed, files skipped | write (local) | Refused on the default branch or a detached HEAD. Stages only changed files that pass the path guard (inside `WRITABLE_PATHS`, never forbidden files). Hooks are disabled. "Nothing to commit" is an error |
| `push_branch` | none | remote and branch | external | Pushes the current branch only, never the default branch, and **never with force** (plain refspec, no `+`, no `--force`). API mode pushes explicitly to `https://github.com/<GITHUB_REPOSITORY>.git` with the token in git's environment config (not argv, not `.git/config`). Fixture mode pushes to `origin`. A rejected (non-fast-forward) push returns a clear error that suggests a new branch name |
| `create_pull_request` | `title`, `body` | number, URL, head, base, draft | external | Head is the current branch (never the default). Base is `GITHUB_DEFAULT_BRANCH`. Draft by default (`GITHUB_PR_DRAFT`). API errors 401/403/404/422/rate limit/5xx/network become actionable messages. Fixture mode checks that the branch exists on `origin` and writes `data/pull_requests/<n>.json` |

### Workflow after the tests pass

```
run_tests (PASSED)
  -> draft_publication   LLM drafts: branch slug, commit message, PR title, summary, risks.
                         The workflow writes the PR's "Changes" and "Tests" sections itself
                         from recorded results, so test results cannot be invented.
  -> await_publication   interrupt(): branch name, commit message, files, push target,
                         PR title and body. Each action is listed separately and the reviewer
                         chooses how far to go:
                           branch        create the branch and commit (local only)
                           push          + push the branch
                           pull_request  + open the pull request
                         The reviewer may edit branch name, commit message, title or body.
       rejected + feedback -> draft_publication (up to MAX_REVISIONS)
       rejected            -> finalize (status "completed": changes stay local and uncommitted)
  -> publish             create_branch -> commit_changes -> push_branch -> create_pull_request,
                         only the approved steps, with the approved values (checked by SHA-256)
  -> finalize
```

New run statuses: `published` (pull request created) and `publish_failed` (an approved step
failed; the error says which, and what was already done). If the reviewer stopped before the
PR, the run ends `completed` and the `publication` record shows how far it went.

### Least-privilege GitHub token

Use a fine-grained personal access token limited to the one target repository:

| Permission | Level | Needed for |
|---|---|---|
| Metadata | Read | Required by GitHub for every token |
| Issues | Read | `get_github_issue` |
| Contents | Read and write | `push_branch` |
| Pull requests | Read and write | `create_pull_request` |

Grant nothing else: no administration, workflows, secrets or other repositories. Phase 1 alone
needs only Metadata and Issues read. Recommended on the repository side: protect the default
branch and add a ruleset that limits this token's pushes to `agent/*` branches.

### Phase 2 tests

- Server, fixture mode against a local bare `origin`: valid and invalid branch names (no prefix,
  default branch, existing branch, `..`, leading `-`, spaces). Commits refused on the default
  branch. Forbidden and non-writable files are never committed. "Nothing to commit". The push
  creates the remote branch. Pushing the default branch is refused. A diverged remote is
  rejected without forcing. A PR requires a pushed branch.
- API mode (mock transport): PR request shape (head, base, draft). Failures: 401, 403 (missing
  "Pull requests: write"), 404, 422 (PR already exists / no commits), rate limit, 5xx, network
  error. The push keeps the token out of argv and `.git/config`.
- Workflow: tests pass, then the publication checkpoint, then branch, commit, push and PR. The
  PR body contains the real test results and `Fixes #<n>`. Also: approve only up to `branch` or
  `push`, edit the title before approving, reject with feedback (redraft), reject without
  feedback, and a failure during publish (`publish_failed`, with partial results recorded).
  The model is never offered a GitHub write tool.
- API: approve a publication with `publish_up_to` and edits.

## Phase 3: documentation browser

Spec: "Add a browser or documentation MCP server that allows the agent to read technical
documentation. The browser tool must be restricted to documentation and research tasks. It must
not log in to websites or perform unrelated account actions."

### Design choice: a documentation reader, not a headless browser

The server fetches pages over HTTPS with GET and converts them to text. It does not run
JavaScript, fill forms, submit anything, or keep cookies. A real browser engine would be able
to log in and act on accounts; this one cannot, by construction. The pages agents need
(official docs, RFCs, PEPs) are server-rendered or published as Markdown, so nothing is lost.

There is also no general web search. Search runs only inside the configured documentation
sources, using each site's own published search index:

| Index type | Published by | Example |
|---|---|---|
| `sphinx` | Sphinx sites (`searchindex.js`) | docs.python.org, docs.pytest.org, requests |
| `mkdocs` | MkDocs sites (`search/search_index.json`) | python-httpx.org |
| `llms_txt` | sites with an `llms.txt` link list | modelcontextprotocol.io, platform.claude.com, docs.github.com |
| none | read-only sources (no search) | rfc-editor.org, peps.python.org |

Sources live in `config/docs_sources.json` (`DOCS_SOURCES_FILE`). Each has a name, a
description, a `base_url` (the URL prefix the agent may read), and an optional index. Adding a
source is a reviewed config change; the agent cannot add one.

### Tools (docs MCP server)

All three are read-only (`readOnlyHint`, `openWorldHint`), so the model may call them during
investigate and propose. Nothing they do needs approval, because nothing they do changes
anything.

| Tool | Inputs | Output | Errors |
|---|---|---|---|
| `list_documentation_sources` | none | name, description, base URL, searchable or not | none |
| `search_documentation` | `query`, `source` (required, so one query never downloads every index), `max_results` (1-10) | ranked results: title, URL (with section anchor), kind (api / section / page), snippet | unknown source, empty or too long query, index unavailable, fetch budget used up |
| `read_documentation` | `url`, `offset` (default 0), `max_chars` (500-20000, default 12000) | title, final URL, character range, page text (Markdown-like; starts at the `#anchor` section if given), up to 10 links to other allowed pages | URL not allowed (and why), robots.txt disallows, HTTP error, unsupported content type, too large, timeout, fetch budget used up |

### Rules enforced by the server

1. **Allowlisted URLs only.** `https` only, default port only, no user/password in the URL, no
   IP-address hosts, no query strings, at most 2048 characters. Host and path must fall under
   a configured `base_url` after normalization (`..` segments, encoded slashes and backslashes
   are rejected).
2. **No logins or account actions.** GET only. No cookies are stored or sent, no
   `Authorization` header, and `trust_env` is off (so `.netrc` credentials and proxy variables
   are ignored). The server process receives no secrets. Paths that are login or account flows
   (`login`, `logout`, `signin`, `signup`, `register`, `oauth`, `authorize`, `sso`, `checkout`,
   `cart`, ...) are refused even under an allowed prefix.
3. **No exfiltration through URLs.** A URL that contains anything matching the secret patterns
   (API keys, tokens, private keys) is refused, and query strings are not allowed at all.
4. **No private network access.** The host must resolve only to public addresses (blocks
   `127.0.0.1`, `10.0.0.0/8`, `169.254.169.254`, `::1`, ...). Known limit: the check and the
   connection resolve separately, so a DNS-rebinding race remains possible; the URL allowlist
   is the primary control.
5. **Redirects are re-checked.** At most 3, followed manually. Every hop must pass rules 1-4.
6. **Bounded responses.** Content types: HTML, XHTML, plain text, Markdown, reStructuredText.
   Pages are capped at 2 MB (`DOCS_MAX_PAGE_BYTES`) and indexes at 16 MB, streamed and aborted
   when exceeded. Timeout 15 s per request. At most 40 network fetches per server session
   (`DOCS_MAX_FETCHES`), on top of the workflow's tool-call limit per step.
7. **Polite.** robots.txt is honored for the server's User-Agent (`DOCS_RESPECT_ROBOTS`).
   Indexes are cached on disk for 24 hours and pages in memory, so repeated lookups do not
   refetch.
8. **Untrusted content.** Page text is labeled as external reference material. The system
   prompt already tells the model that such content is data, not instructions, and the model
   still has no write tools. Output passes through the hub's secret redaction, and every call
   is in the audit log (URL in the arguments, result size and hash).

### Workflow changes

- `investigate` and `propose` offer the three docs tools when the docs server is connected
  (`DOCS_ENABLED=true`, the default). With docs disabled the workflow runs exactly as before.
  The repository is always searched first: the planned searches run before the model gets any
  tool.
- Every successful `read_documentation` call is recorded in the run state as
  `docs_consulted` (URL, title, node). The API and UI show it, and the PR body gets a
  **References** section built from these records, not from the model's claims.
- Prompts: consult documentation only when library or standard behavior is uncertain, prefer
  official sources, cite what was used.

### Phase 3 tests

- URL policy: allowed prefix; `http`, other ports, userinfo, IP hosts, lookalike hosts
  (`docs.python.org.evil.com`), prefix escapes (`/3/../`, `%2e%2e`, `%2f`, backslash), query
  strings, login and OAuth paths, secrets in the URL.
- Fetcher (mock transport): private and metadata IPs blocked; redirect to a disallowed host
  blocked; redirect limit; no `Cookie` or `Authorization` header ever sent, even after
  `Set-Cookie`; only GET; content-type rejection; size cap; timeout and network errors;
  robots.txt disallow; fetch budget.
- HTML conversion: main-content extraction (`main`, `article`, `role=main`), scripts, styles,
  navigation and forms removed, headings, lists, code blocks and tables kept, `#anchor`
  sections, paging with `offset`.
- Search: synthetic Sphinx, MkDocs and llms.txt indexes (API object names, section titles,
  stemmed terms, snippets), source filter, unknown source, results limited to readable URLs,
  index cached on disk.
- MCP: discovery (three read-only tools), invalid arguments, a real stdio start of the server.
- Workflow: the model reads documentation during investigation, the read is recorded and
  appears under References in the PR body; docs disabled; a docs failure does not stop the run.

## Phase 4: reliability

Spec items: structured agent state, retry limits, exponential backoff, tool timeouts, maximum
number of tool calls, maximum task cost, recovery after failed tests, clear failure messages,
human approval checkpoints.

### Retries with exponential backoff

One `RetryPolicy` (`app/reliability/retry.py`) is used for model calls and tool calls. The delay
before retry *n* is `min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * 2^(n-1))`, scaled by
a random factor between 0.5 and 1 (jitter). A `Retry-After` value from the server is honored;
if it is longer than the maximum delay the call is not retried (waiting would not be bounded).

| Calls | Retried when | Limit | Never retried |
|---|---|---|---|
| Anthropic API | 429, 5xx, 529 overloaded, timeouts, connection errors | `ANTHROPIC_MAX_RETRIES` (3) | 400, 401, 403, 404, refusals |
| MCP tools | the server marks the error `[temporary]` (GitHub/docs timeouts, network errors, 429, 5xx), or the call timed out | `TOOL_MAX_RETRIES` (2) | any tool that is not read-only or idempotent according to its annotations (`apply_patch`, branch, commit, push, pull request) |

The SDK's own retries are switched off, so every attempt is visible: each one is a row in the
LLM or tool-call log, and each retry is an `llm_retry` / `tool_retry` event with its delay.

### Tool timeouts

Every MCP call has a per-call deadline (MCP `read_timeout_seconds`): `TOOL_TIMEOUT_SECONDS` (60)
for the GitHub and filesystem servers, the docs request timeout x 6 + 30 s for the docs server,
and `SANDBOX_TIMEOUT_SECONDS` + 120 s for the sandbox (Docker kills the test container first;
the hub deadline only catches a stuck server, and it is not retried). A timeout returns a tool
error that names the tool and the limit; the server process keeps running and later calls work.

### Run budget: tool calls, model calls and cost

`RunBudget` (`app/reliability/budget.py`) reads usage from the audit database, so the budget
survives restarts and covers every invocation of a run (start, each approval, each retry).

| Limit | Default | Checked |
|---|---|---|
| `MAX_TOOL_CALLS_PER_STEP` | 25 | in the tool loop; every call the model requests counts, including rejected ones |
| `MAX_TOOL_CALLS_PER_RUN` | 150 | before every tool call the model requests, and when a step that calls tools starts |
| `MAX_LLM_CALLS_PER_RUN` | 100 | before every model call (failed attempts do not count) |
| `MAX_TASK_COST_USD` | 5.00 (0 = off) | before every model call |

Cost = tokens x the model's price (`config/model_prices.json`, or the `LLM_PRICE_*_PER_MTOK`
environment variables), with cache reads and writes priced separately. Each LLM-call row stores
its cost. If a cost limit is set and the configured model has no price, the run does not start
and says how to fix it. A call that is already running is not interrupted, so a run can exceed
the cost limit by at most one model call.

Approved actions are never cut off halfway: applying approved patches, running the tests and
publishing approved steps always finish, so files are never left half-changed by a budget stop.
The next step that needs the model or new tool calls stops the run with `budget_exceeded`.

### Structured failures

Every failed run has a `failure` record: `code`, `step`, `message`, `retryable` (whether retrying
without changing anything may work) and `hint` (what to do). Examples:

| Code | Example hint |
|---|---|
| `llm_auth` | Check ANTHROPIC_API_KEY |
| `llm_rate_limited` | Anthropic kept rate limiting after N retries; wait, then retry the run |
| `budget_exceeded` | Raise MAX_TASK_COST_USD if this issue needs more, then retry the run |
| `pricing_unknown` | Add the model to config/model_prices.json or set LLM_PRICE_* |
| `mcp_server_unavailable` | Server X failed to start: ... |
| `issue_unavailable`, `no_valid_patch`, `apply_failed`, `tests_could_not_run`, `tests_failed`, `publish_failed`, `interrupted`, `internal_error` | ... |

The existing `error` string is kept (`"<step>: <message>"`).

### Retrying a failed run

`POST /runs/{id}/retry` resumes a failed or interrupted run from the last LangGraph checkpoint
before the step that failed. Work that already succeeded (issue, plan, approvals, applied
patches) is not repeated. Limits and rules:

- At most `MAX_RUN_RETRIES` (3) retries per run.
- Only steps that are safe to run again can be retried: reading the issue, planning,
  investigating, proposing, running tests, drafting the publication, and the approval
  checkpoints. A failure inside `apply` or `publish` (which change files or GitHub) is not
  retried automatically; the failure message says what was already done.
- When the API starts, runs left `running` or `resuming` by a previous process are marked
  `interrupted` (failure code `interrupted`), and can then be retried.
- A run that failed while a checkpoint was waiting for a human goes back to waiting.

### Recovery after failed tests

- Test output is analyzed (`app/reliability/test_results.py`): failure kind (assertion failures,
  collection or import errors, timeout, no tests collected) and the list of failing tests.
- From the second run on, the model is told which tests are still failing, which ones its last
  change broke, and which ones it fixed. If nothing changed, it is told so explicitly.
- When fix attempts are used up, or the tests could not run at all (for example Docker is not
  running), the run pauses at a **recovery checkpoint** instead of ending.

### Human approval checkpoints

| Checkpoint | When | Decisions |
|---|---|---|
| `plan_approval` (optional, `REQUIRE_PLAN_APPROVAL=true`) | after the plan, before any investigation | approve, reject with feedback (re-plan), reject |
| `patch_approval` (Phase 1) | before any file is written | approve, reject with feedback, reject |
| `recovery` (new) | fix attempts used up, or tests could not run | retry (one more fix attempt, with optional guidance; or re-run the tests), stop |
| `publish_approval` (Phase 2) | before branch, commit, push, pull request | approve up to a level, edit, reject with feedback, reject |

All of them use the same endpoint, `POST /runs/{id}/approval` (`approved=true` means retry at the
recovery checkpoint, `feedback` is the guidance).

### Structured agent state

`AgentState` keeps its typed records and gains: `failure`; `timeline` (one entry per step: start
time, duration, outcome, and the model calls, tool calls and cost that step used);
`plan_approvals`; `recovery_decisions`; `extra_fix_attempts`; and per test run, `failure_kind`
and `failing_tests`. The API view adds live `usage` (calls, tokens, cost against each limit) and
`retry` (whether a retry is available and how many are left).

### Phase 4 tests

- Retry policy: delays grow exponentially and are capped; jitter bounds; `Retry-After` honored;
  too-long `Retry-After` not waited for; retry limit.
- Reliable provider: retries 429/5xx/timeouts then succeeds; gives up with the attempt count;
  never retries 401/400; checks the budget before every attempt.
- Budget: prices from file and environment; cost with cache tokens; each limit enforced; usage
  survives a new `RunBudget`; old databases are migrated.
- Hub: per-call timeout with a clear message and a usable session afterwards; temporary errors
  retried for read-only tools only; write tools never retried; GitHub and docs servers mark
  temporary errors.
- Workflow: cost limit stops the run before the next model call; run tool-call limit; per-step
  limit counts rejected calls; a rate-limited plan step is retried; a failed run is retried from
  the failed step without repeating earlier steps; retry limit; no retry inside `apply`;
  interrupted runs; test-failure analysis reaches the model; recovery checkpoint (retry with
  guidance, re-run tests after Docker comes back, stop); plan approval checkpoint; timeline and
  failure records in the view; the API retry endpoint.

## Phase 5: tests

Phases 1–4 already test every area on the spec's list, but each phase tested its own pieces.
Phase 5 checks each spec category as a whole: every entry point an attack or failure can come
through, not just the module that implements a rule. It also makes the mapping from category to
tests explicit and checked.

### Traceability

`tests/spec_matrix.py` maps each spec category to the tests that cover it. `conftest.py` turns
the mapping into pytest markers, so one category can be run on its own (`pytest -m
path_traversal`). Every run ends with a table of passed, failed and skipped tests per category.
`tests/test_spec_matrix.py` fails if a listed test no longer exists or a category has too few
tests, so renaming or deleting a test cannot silently drop coverage.

| Marker | Spec category |
|---|---|
| `discovery` | Tool discovery |
| `valid_calls` | Valid tool calls |
| `invalid_args` | Invalid arguments |
| `path_traversal` | Path traversal attempts |
| `unauthorized_access` | Unauthorized file access |
| `dangerous_commands` | Dangerous command rejection |
| `timeouts` | Test timeout handling |
| `recovery` | Failed test recovery |
| `patching` | Patch application |
| `github_failures` | GitHub API failures |
| `pull_requests` | Pull-request creation |

### New tests by category

- **Tool discovery:** a contract for the whole catalog. Each of the 14 tools has the expected
  server, risk class and required arguments, a description, explicit annotations and a valid JSON
  Schema. Adding or changing a tool fails this test until the table is updated, which forces a
  review of its risk. Duplicate tool names across servers stop startup.
- **Valid tool calls:** each discovered tool has a valid example call that succeeds against the
  real servers, and each call is logged with its server, risk, node and duration. A new tool
  without an example fails the test.
- **Invalid arguments:** every tool rejects a missing required argument, a wrong type and an
  unknown argument. Semantic limits (issue number 0, empty query, over-long titles, negative
  offsets) give a clear error. Nothing is written when the arguments are invalid. Rejected calls
  appear in the audit log.
- **Path traversal:** a generated set of traversal strings (`..` chains, absolute paths, `~`,
  NUL bytes, symlinked files and directories pointing outside) is tried against every
  path-taking entry point: read, search directory, patch path, patch headers, new-file creation
  in a new directory, and test paths in the test command. Each attempt is rejected, and a
  sentinel file outside the repository is unchanged.
- **Unauthorized file access:**
  - case variants of protected names (`.ENV`, `.GIT/config`, `ID_RSA`);
  - symlinks inside the repository that point at a protected file;
  - search never returns protected content;
  - protected files cannot be created or changed by a patch;
  - the git diff and commits never include them;
  - the sandbox copy of the repository leaves them out.
- **Dangerous commands:** attempts to get around the allowlist, such as pytest `@argument-files`,
  `-c`/`-o`/`--rootdir`/`--basetemp`, environment prefixes, other interpreters, absolute
  binary paths, tabs and newlines, and Unicode look-alikes. None of them reach the runner.
- **Test timeouts:**
  - a run killed at the sandbox limit is recorded as `timeout`, and the model is told it was a
    time limit, not a failing assertion;
  - a sandbox server that hangs hits the hub deadline;
  - that run goes to the recovery checkpoint instead of being repeated automatically;
  - the configured limits reach the Docker command.
- **Failed test recovery:** a patch that breaks collection (a syntax error), followed by a fix
  that passes; the second attempt applies on top of the first; a fix that the reviewer rejects
  is not applied.
- **Patch application:**
  - CRLF files keep CRLF;
  - context lines keep their original text, including trailing whitespace;
  - a patch that no longer applies writes nothing;
  - if any approved patch in a set is stale, no file in the set is written;
  - header/path mismatches, multi-file, delete, rename and binary patches are rejected.
- **GitHub API failures:**
  - non-JSON responses, a moved repository (3xx), an unexpected payload, 410, and secondary rate
    limits;
  - the token never appears in any error;
  - at workflow level, a GitHub outage while reading the issue fails the run as
    `issue_unavailable`, and retrying the run after GitHub recovers continues from that step.
- **Pull-request creation:**
  - an end-to-end publish in API mode: push to a local bare remote standing in for github.com, and
    the exact PR request against a mock API, covering title, body, head, base and draft;
  - the body contains the issue link, the recorded test result and the changed files;
  - no PR without approval or after a lower publish level;
  - a duplicate PR is refused;
  - a PR failure after the push reports what was already done.

### Problems found and fixed

Most of these were found while planning, by probing the code. The two symlink leaks were found
by the new tests.

- **Case-insensitive file systems.**
  - Problem: on macOS, `.ENV` opened `.env` and `.GIT/config` opened `.git/config`.
  - Fix: protected names are compared in lower case (`security.is_protected`).
- **pytest argument files.**
  - Problem: pytest 9 reads `@file` arguments as extra options, which got around the flag
    allowlist.
  - Fix: arguments starting with `@` are rejected.
- **CRLF files.**
  - Problem: patching a CRLF file turned the edited lines into LF, which left mixed line endings.
  - Fix: every line keeps its own ending, added lines use the file's usual one, and context lines
    keep the file's text, including trailing whitespace.
- **Partial applies.**
  - Problem: approved patches were applied one at a time, so a stale second patch left the first
    one applied.
  - Fix: `apply` checks every patch with `propose_patch` before writing any.
- **Secrets in the sandbox.**
  - Problem: the sandbox copy included `.env` and key files, so a test could print them.
  - Fix: protected files are left out of the copy.
- **Unknown arguments.**
  - Problem: they were silently ignored (`end_lines` instead of `end_line`).
  - Fix: the tool loop rejects them with the list of accepted arguments.
- **Rejected tool calls.**
  - Problem: calls refused for an unknown tool, invalid arguments or a used-up limit were missing
    from the audit log.
  - Fix: they are logged as `tool_rejected` events.
- **GitHub responses.**
  - Problem: a non-JSON success body, a 3xx redirect (renamed repository) or a payload without a
    number surfaced as a Python exception.
  - Fix: each is now a clear tool error.
- **Symlinks to protected files (found by the tests).** A link such as `authkit/env_link -> ../.env`
  exposed the secret through whole-repository search, and a new untracked link exposed its target
  through `get_git_diff`, even when the target was outside the repository. Search now skips links
  whose target is protected, and the diff never shows a new link's content.
- **Smaller fixes:**
  - the hub now raises an in-process server's startup error directly, not wrapped in an
    `ExceptionGroup`;
  - reading past the end of a file is an error;
  - the rate-limit message no longer says "resets at later" when GitHub gives no reset time.

### Coverage

`pytest --cov=app` (pytest-cov, in the `dev` extra) reports line and branch coverage per module.
The README quotes only numbers measured from an actual run. Code the stdio tests run in
subprocesses (each server's `main`) is not measured.

## Phase 6: documentation and evaluation

### Deliverables

| Spec deliverable | Where |
|---|---|
| README | `README.md`, reorganized into the spec's sections (problem statement, architecture, MCP server responsibilities, tool definitions, security model, example workflow, testing, evaluation results, cost and latency, future improvements). Details move to `docs/`. |
| Architecture diagram | `docs/ARCHITECTURE.md`: Mermaid diagrams of the components, the workflow graph and one run as a sequence; the ASCII overview stays in the README |
| Setup instructions | `docs/SETUP.md`: local install, Docker Compose, a real GitHub repository, every setting |
| `.env.example` | Every setting the code reads; a test enforces this |
| `Dockerfile` | App image (API and UI): non-root, git and the Docker CLI, no secrets baked in. The sandbox keeps its own `sandbox/Dockerfile` |
| `docker-compose.yml` | `api`, `ui`, and a build-only `sandbox` service for the test image; ports bound to 127.0.0.1 |
| API documentation | `docs/API.md` (endpoints, examples, statuses, checkpoint kinds) and `docs/openapi.json`, exported by `scripts/export_openapi.py`; a test checks both are current |
| Security documentation | `docs/SECURITY.md`: threat model, then each security requirement from the spec mapped to its control and the tests that prove it; residual risks |
| Evaluation report | `docs/EVALUATION.md`: metric definitions, the experiment protocol, and results only as produced by `scripts/evaluate.py` from recorded runs (none yet) |
| Example GitHub issue | `examples/issues/sample-repo.json` (#42 bug, #43 with a prompt-injection attempt), described in `examples/issues/README.md` |
| Example agent run | `examples/runs/`, rendered by `scripts/render_run.py` from a recorded run, with its provenance stated |
| Known limitations | README, plus residual risks in `docs/SECURITY.md` |

### Evaluation

`app/evaluation/` loads runs from the audit database and the LangGraph checkpoints and computes
the metrics. `scripts/evaluate.py` prints them as Markdown or JSON, per run and grouped by model,
filtered by time window, run id or issue. Runs still waiting at a checkpoint are listed but left
out of the rates. The script never fills in a value it cannot compute; it reports "n/a".

| Metric | Definition |
|---|---|
| Issue resolved | The run ended `published` or `completed` and its last test run passed. Approximate: real resolution is the reviewer's judgement, which `published` reflects |
| Test-pass rate | First-attempt pass rate (the first test run passed) and pass rate over all test runs that actually ran |
| Tool calls | Calls per run, including retry attempts, split into model-initiated and workflow; plus calls refused before reaching a server |
| Completion time | Wall clock from start to finish, and agent working time (the sum of step durations in the timeline). The difference is mostly time waiting for reviewers |
| Recovery rate | Of the runs with at least one failed or timed-out test run, the share whose last test run passed |
| Unnecessary file edits | *Unplanned*: an applied file not listed in the approved plan's `files_to_change`. *Reverted*: an applied file with no change left in the final diff. Both as a count and as a share of edited files |
| Cost per completed task | Total cost of every run in the set, failed runs included, divided by the number of resolved runs; also the mean cost per run |
| Latency | p50/p95 of model-call latency by purpose, tool-call duration by tool, and step duration |

To make "files changed but not in the approved plan" measurable, the plan gains a
`files_to_change` field: the model lists the files it expects to change or create, and the
reviewer sees the list. Older runs without it are left out of the unplanned-edit metric.

### Code changes

- `app/evaluation/` (data loading, metrics, run report) with `scripts/evaluate.py`,
  `scripts/render_run.py` and `scripts/export_openapi.py`.
- `files_to_change` in the plan schema, prompt and UI.
- `SANDBOX_WORK_DIR`: where the sandbox puts its repository copies. In Docker Compose the API
  starts test containers through the host's Docker daemon, so the copy must be at the same path
  on the host and in the API container.
- `.env.example` gains `GITHUB_API_URL`, `GITHUB_WEB_URL`, `SANDBOX_WORK_DIR` and the UI's
  `AGENT_API_URL`.
- **No model name in Python code** (the spec's rule). `app/config.py` used to fall back to
  `claude-sonnet-5`. The model now comes only from `ANTHROPIC_MODEL` (set in `.env.example`);
  without it, a run fails with `model_not_configured` before anything runs, and a test scans
  `app/` and `scripts/` for model names and keys.
- **Reproducible trials.** Added after the first experiment: `app/evaluation/trials.py` and
  `scripts/prepare_trial.py` reset the sample repository, archive old fixture pull requests, and
  give issue-43 trials a start with issue 42 fixed. The first two trials ran without these steps
  and were set aside (see docs/EVALUATION.md).
- **Proposed files outside the plan are flagged.** The patch checkpoint lists them
  (`outside_plan`), and the UI shows a warning, so the reviewer sees unplanned edits before
  approving instead of only in the evaluation.

### Phase 6 tests

- **Evaluation:** scripted runs with known outcomes (published on the first try, recovered after
  a failed test run, stopped with failing tests, rejected, failed on the budget, still waiting).
  Every metric is checked against hand-computed values, including n/a cases.
- **Run report:** rendered from a recorded run and checked for content and missing secrets.
- **Documentation stays true:**
  - every setting is in `.env.example`, and every route is in `docs/API.md`;
  - `docs/openapi.json` matches the app;
  - the README tool table matches the discovered tools;
  - relative links in the Markdown resolve;
  - every spec security requirement in `docs/SECURITY.md` names tests that exist.
- **Docker files (static checks only; Docker is not installed here):** the compose file parses,
  binds ports to 127.0.0.1, holds no secrets, and points at existing build contexts. The images
  run as non-root, and `.env`, `data/` and `workspaces/` are kept out of the build context.
