# Design notes

These notes explain why the project is put together the way it is. For what the pieces are and
how a run flows through them, see [ARCHITECTURE.md](ARCHITECTURE.md). For each security rule and
the tests that check it, see [SECURITY.md](SECURITY.md).

## The main idea: the model only reads

The model can search and read the repository, read the issue, read documentation, and propose a
patch. That's all it can do. It is never given a tool that writes a file, runs a command or
touches GitHub.

Writes happen in workflow steps that run after a person approves:

- `apply_patch` is called by the `apply` step with exactly the patch the reviewer saw. The
  approval stores a SHA-256 of each patch, and the step checks it again before writing.
- The GitHub write tools are called by the `publish` step with the branch name, commit message
  and pull request text the reviewer approved, possibly after editing them.

So a prompt injection in an issue or a file can at most make the model *propose* something bad,
and the reviewer sees that proposal as a diff before anything happens.

Some rules are enforced in the workflow code rather than left to the prompt:

- No plan until the issue has been read.
- No proposal until at least one repository search has succeeded.
- No file write without a recorded approval for that exact patch.
- Every loop is bounded: tool calls per step and per run, revisions, fix attempts and retries.

## Choices

| Area | Choice | Why |
|---|---|---|
| Model | Anthropic Messages API through the official `anthropic` SDK, behind a small `LLMProvider` interface | The interface lets the tests use a scripted provider, so the whole workflow is tested without an API key |
| Model name | Only from `ANTHROPIC_MODEL` | Nothing model-specific is hardcoded, and a test scans `app/` and `scripts/` for model names and keys |
| Orchestration | LangGraph `StateGraph`, approvals with `interrupt()`, SQLite checkpointer | A run can wait for a reviewer for hours and survive a restart, and a failed run can be retried from the step that failed |
| Tools | MCP Python SDK; four servers, each its own stdio process | Each server gets only the environment it needs, so only the GitHub server ever sees the GitHub token |
| Tests | Docker only | The agent's code changes run with no network, a read-only root and resource limits; there is no "run it on the host" fallback |
| Storage | SQLite | Checkpoints, the run index and the audit log, in one place with no extra service |
| API and UI | FastAPI, with Streamlit as a thin client over the API | Everything the UI can do is also an HTTP call, which is how the tests drive it |

## What the agent can touch

- **The repository:** a local git checkout set by `REPO_PATH`. The project ships a sample
  repository (`examples/sample-repo`, a small token and session library with a real bug), and
  `scripts/setup_sample_repo.py` copies it to `workspaces/sample-repo` and runs `git init`.
- **Issues:** from the GitHub REST API, or from a JSON file in fixture mode
  (`GITHUB_MODE=fixture`), so the sample runs without a GitHub repository.
- **Reading:** any file inside `REPO_PATH` that isn't protected.
- **Writing:** only under `WRITABLE_PATHS` (comma-separated prefixes; empty means the whole
  repository), and never protected files. New files are allowed through a patch from
  `/dev/null` if `ALLOW_NEW_FILES` is on. No tool can delete a file.
- **Protected, never read or written:** anything outside the repository, `.git/`, `.env` and
  `.env.*`, private keys (`id_rsa*`, `id_ed25519*`, `*.pem`, `*.key`, `*.p12`), `.ssh/`, `.aws/`,
  `.netrc`, `.npmrc`, `.pypirc` and `credentials.json`, in any letter case, and symlinks that
  point at any of these or outside the repository.

## Languages

Python, JavaScript and Java differ in only a few places, and those live in one profile per
language (`app/languages.py`): the files that identify a project, the sandbox image, the default
test command and its allowlist, and the sample repository. The language is detected from the
repository when the agent starts, or set with `PROJECT_LANGUAGE`.

Two things depend on the test runner rather than on the language, so they're keyed by the command
that actually runs:

- **Which options are allowed.** pytest, `node --test` and Maven each get a short list. For Node
  that means nothing that loads or evaluates other code (`--require`, `--import`, `--eval`,
  loaders), and `--test` has to come first, because anything before it makes node run a script.
  For Maven, `test` is the only goal and `-o` is required.
- **How the output is read.** pytest's short summary, TAP `not ok` lines from Node, and surefire's
  `<<< FAILURE!` lines from Maven all end up as the same failure kind and list of failing tests, so
  the retry logic doesn't care which language it's working on.

Tests never have network access, so a language's dependencies have to be in its image. The Java
image runs `mvn test` once at build time on a small project with the sample's plugin and JUnit
versions, and Maven then runs offline against that cache.

## Approvals

| Action | Needs approval? |
|---|---|
| Reading the issue, searching and reading files, viewing the diff | No |
| Searching and reading documentation (allowlisted sites only) | No |
| Proposing a patch | No, because nothing is written |
| Applying a patch | Yes. The run pauses until the reviewer approves, rejects with feedback, or rejects |
| Running the tests | No, but only the configured command, only in Docker, and only if it passes the allowlist |
| Creating a branch and committing | Yes, at the publication checkpoint |
| Pushing the branch | Yes, listed separately at the same checkpoint |
| Opening a pull request | Yes, listed separately at the same checkpoint |
| Force pushing, pushing to the default branch, running arbitrary commands | Never. No tool can do these |

There are two more checkpoints. Plan approval is optional (`REQUIRE_PLAN_APPROVAL`) and happens
after the plan's searches but before any file is read. Recovery comes up when the fix attempts are used up or the tests
couldn't run at all; the reviewer can allow another attempt, with guidance, or stop. All four
checkpoints use the same endpoint, `POST /runs/{id}/approval`.

## Publishing to GitHub

After the tests pass, the model drafts a branch name, commit message and pull request. The
workflow then adds the *Changes* and *Tests* sections itself from what was recorded, so the pull
request can't claim tests passed when they didn't.

The rest of the text is still the model's, and in the first experiment half of the summaries got a
detail wrong. So a second call checks the draft against the issue, the final diff and the test
runs, lists anything they contradict or don't support, and returns corrected text. The reviewer
sees the corrected version, what was changed, and the original. If the check finds nothing, the
draft is used exactly as written, and if the check itself fails, the draft is used and marked as
unchecked. `CHECK_PR_SUMMARY=false` turns it off.

At the publication checkpoint the reviewer chooses how far to go:

- `branch`: create the branch and commit, locally only;
- `push`: also push the branch;
- `pull_request`: also open the pull request.

They can edit any of the drafted text first. Rules enforced by the GitHub server itself, whatever
the caller asks:

- Branch names must start with `GITHUB_BRANCH_PREFIX` (`agent/` by default), and can't be the
  default branch or an existing branch.
- Commits only stage changed files that pass the path checks, with git hooks turned off.
- Pushes are never forced and never go to the default branch. A rejected push suggests a new
  branch name instead of retrying with force.
- In API mode the token reaches git through its environment config, never through the command
  line or `.git/config`.
- Pull requests are drafts by default (`GITHUB_PR_DRAFT`).

The token should be a fine-grained one scoped to the target repository. [SETUP.md](SETUP.md#using-a-real-github-repository)
lists the exact permissions.

## The documentation reader

I wanted the model to be able to check library behavior, but a headless browser can log in,
click buttons and submit forms, and the requirement was that this tool can't do any of that. So
it isn't a browser. It's a reader that does HTTPS GETs on an allowlist of documentation sites and
turns the pages into text. It runs no JavaScript, sends no cookies or credentials, and ignores
`.netrc` and proxy settings.

There's no general web search either. Search only works inside a configured site, using the
search index the site already publishes:

| Index | Published by | Examples |
|---|---|---|
| `sphinx` | Sphinx sites (`searchindex.js`) | docs.python.org, docs.pytest.org |
| `mkdocs` | MkDocs sites (`search/search_index.json`) | python-httpx.org |
| `llms_txt` | sites with an `llms.txt` link list | modelcontextprotocol.io, docs.github.com |
| none | read-only sources | rfc-editor.org, peps.python.org |

The sources are in [config/docs_sources.json](../config/docs_sources.json). Adding one is a
reviewed config change; the agent can't add one.

Other rules: no query strings, no IP-address hosts, no login or account paths, no URLs containing
anything that looks like a secret, only public addresses, redirects re-checked at every hop, size
and time limits, robots.txt honored, and a fetch budget per session. Every page the model reads is
recorded, and the pull request's *References* section is built from those records rather than
from what the model says it read.

The one gap I know of is DNS rebinding: the address check and the connection resolve the name
separately. The URL allowlist is the main control.

## Reliability

**Retries.** Model calls and tool calls share one retry policy with exponential backoff and
jitter, capped at `RETRY_MAX_DELAY_SECONDS`. A `Retry-After` from the server is honored, unless it
is longer than the cap, in which case the call fails rather than waiting an unbounded time. The
SDK's own retries are off, so each attempt is a row in the log.

| Calls | Retried on | Never retried |
|---|---|---|
| Anthropic API | 429, 5xx, 529, timeouts, connection errors (`ANTHROPIC_MAX_RETRIES`) | 400, 401, 403, 404, refusals |
| MCP tools | errors the server marks as temporary, and timeouts (`TOOL_MAX_RETRIES`) | anything that isn't read-only or idempotent: applying patches, branch, commit, push, pull request |

**Timeouts.** Every tool call has a deadline. The sandbox's deadline is longer than Docker's own
limit, so normally Docker kills the test container first and the hub deadline only catches a
stuck server.

**Budget.** Usage is read back from the audit database, so it survives restarts and covers every
invocation of a run.

| Limit | Default | Checked |
|---|---|---|
| `MAX_TOOL_CALLS_PER_STEP` | 25 | in the tool loop, counting refused calls too |
| `MAX_TOOL_CALLS_PER_RUN` | 150 | before every tool call the model asks for |
| `MAX_LLM_CALLS_PER_RUN` | 100 | before every model call |
| `MAX_TASK_COST_USD` | 5.00 (0 turns it off) | before every model call |

Each step can use its own model and effort level (`ANTHROPIC_MODEL_<STEP>`,
`ANTHROPIC_EFFORT_<STEP>`), so cost is worked out per call: that call's tokens times the price of
the model it used, from [config/model_prices.json](../config/model_prices.json), with cache reads
and writes priced separately. Haiku 5.5 has two rate cards split at a 100K-token prompt, and the
price file can express that. Before a run starts with a cost limit, every model it might use must
have a price. Since the check runs before each call, a run can
go over the cost limit by one call at most. Approved actions are never cut off halfway: applying
patches, running tests and publishing always finish, and the next step that needs the model
stops the run instead.

**Failures.** Every failed run gets a record with a code, the step, a message, whether retrying
might help, and a hint, for example `llm_auth` ("Check ANTHROPIC_API_KEY") or `budget_exceeded`
("Raise MAX_TASK_COST_USD if this issue needs more, then retry the run").

**Retrying a run.** `POST /runs/{id}/retry` resumes from the last checkpoint before the failed
step, so the issue, plan, approvals and applied patches aren't repeated. Only steps that are safe
to run twice can be retried. A failure inside `apply` or `publish` isn't retried automatically,
and its message says what was already done. Runs that were mid-step when the API stopped are
marked `interrupted` on the next start and can be retried.

**Failed tests.** The test output is parsed into a failure kind (assertions, collection errors,
timeout, no tests) and a list of failing tests. On the next attempt the model is told which tests
still fail, which ones its last change broke and which it fixed, or that nothing changed.

## Testing

The tests are grouped by risk rather than by module: path traversal, unauthorized file access,
dangerous commands, timeouts, recovery, patch application, GitHub failures and so on. Each group
tries every way in, not just the function that implements the rule. `tests/spec_matrix.py` maps
each group to its tests, `conftest.py` turns that into pytest markers (`pytest -m path_traversal`),
and a test fails if a listed test disappears.

Working this way found real problems:

- On macOS, `.ENV` opened `.env`, and `.GIT/config` opened `.git/config`. Protected names are now
  compared in lower case.
- pytest reads `@file` arguments as extra options, which got around the flag allowlist. Arguments
  starting with `@` are now refused.
- Patching a CRLF file turned the edited lines into LF. Each line now keeps its own ending.
- If the second of two approved patches was stale, the first had already been written. Every
  patch is now checked before any is written.
- The sandbox copy of the repository included `.env`, so a test could have printed it. Protected
  files are now left out of the copy.
- A symlink to `.env` inside the repository leaked its contents through search and through the
  diff of untracked files. Both now skip such links.
- Unknown tool arguments (`end_lines` for `end_line`) were silently ignored, and refused tool
  calls were missing from the audit log. Both are fixed.

[TESTING.md](TESTING.md) has the rest.

## Evaluation

The metrics are defined in [EVALUATION.md](EVALUATION.md). Two of them needed changes to the
agent:

- To count edits to files the plan didn't mention, the plan has to say which files it expects to
  change, so it has a required `files_to_change` field. The patch checkpoint also flags proposed
  files outside that list, so the reviewer sees them before approving.
- To make trials comparable, `scripts/prepare_trial.py` resets the sample repository, archives
  fixture pull requests from earlier runs, and starts issue 43 trials with issue 42 already
  fixed. I added it after the first two trials of the experiment went wrong without it.

Reviewing every run by hand doesn't scale to comparing models, so there's also a benchmark mode
that answers the checkpoints with a fixed policy and judges each result with hidden acceptance
tests. An automatic reviewer that approves its own changes is exactly what the rest of the design
avoids, so it's fenced in: it refuses to run unless GitHub is in fixture mode and the repository
is one of the bundled samples, the API can't record a decision as the evaluator's, and the
product never uses it. Details are in [EVALUATION.md](EVALUATION.md#benchmark-mode).
