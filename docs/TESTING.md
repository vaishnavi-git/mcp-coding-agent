# Testing

```bash
pytest                          # everything
pytest -m path_traversal        # one test category (markers below)
pytest -m docker                # only the tests that need a real Docker daemon
pytest --cov=app                # with line and branch coverage
```

The suite needs no network and no API key. The workflow tests drive the real LangGraph workflow,
real MCP servers, real git and real approvals. Only the model's turns are scripted
([tests/fakes.py](../tests/fakes.py)), and pytest runs on a copy of the repository instead of in
Docker. The three real-Docker tests (`pytest -m docker`), one per sandbox image, run only when
Docker and that image are available, and are skipped everywhere else. The JavaScript workflow test
uses the local `node` in place of Docker and is skipped if there isn't one.

## Last full run

595 tests. With Docker running and the three sandbox images built, all 595 pass; this was checked
on macOS on Apple silicon with Docker Desktop (engine 29.9.3). Without Docker, the three
real-Docker tests are skipped. A run takes about two minutes on a laptop.

| Category | Marker | Tests passing |
|---|---|---|
| Tool discovery | `discovery` | 18 |
| Valid tool calls | `valid_calls` | 9 |
| Invalid arguments | `invalid_args` | 36 |
| Path traversal attempts | `path_traversal` | 45 |
| Unauthorized file access | `unauthorized_access` | 55 |
| Dangerous command rejection | `dangerous_commands` | 124 |
| Test timeout handling | `timeouts` | 7 |
| Failed test recovery | `recovery` | 10 |
| Patch application | `patching` | 36 |
| GitHub API failures | `github_failures` | 43 |
| Pull-request creation | `pull_requests` | 15 |

Parametrized cases count separately, and a test can be in more than one category. The rest of the
tests cover reliability, the documentation reader, the API, evaluation and the docs themselves.

Coverage from `pytest --cov=app` (line and branch combined):

| Scope | Coverage |
|---|---|
| The whole `app` package | 87% |
| Without the Streamlit UI (no automated tests; checked by hand) | 92% |
| Security-critical modules (path guard, filesystem, sandbox, patching, tool loop, MCP hub) | 93% |
| Evaluation package | 86% |

Code that the stdio tests run in subprocesses (each server's `main`) isn't measured.

## Test categories

The categories above are the risks I most wanted covered. Each one is tested through every way in,
not just the module that implements the rule: a path-traversal string, for example, is tried
against reading, searching, proposing, applying and the test command.

[tests/spec_matrix.py](../tests/spec_matrix.py) lists the tests in each category, and
`conftest.py` turns that list into the markers, so `pytest -m <marker>` runs one category and
every run ends with a table of results per category. `tests/test_spec_matrix.py` fails if a listed
test is renamed or deleted, if a category has fewer than five tests, or if a test in one of the
category files isn't listed anywhere.

## What's covered

- **Tools and MCP.** The whole tool catalog is pinned: server, risk class, required arguments,
  annotations and schemas, both in-process and over the real stdio servers. Every tool has a
  valid call that succeeds, and rejects missing, mistyped and unknown arguments with a clear
  error.
- **Files and paths.** A generated set of traversal paths, symlinks pointing out of the
  repository or at protected files, letter-case variants of protected names, and writes outside
  `WRITABLE_PATHS`. Protected files stay out of search, the git diff, commits and the sandbox
  copy; a probe test running inside the sandbox confirms it can't see them.
- **Commands and the sandbox.** About 40 attempts to get around the pytest allowlist, with a real
  check that pytest reads options from `@files`, and about 35 more for `node --test` (code-loading
  options, options after file names) and Maven (other goals, other POM or settings files, other
  properties, running online). Docker isolation flags, a run killed at the time limit, and a
  sandbox server that never answers.
- **Languages.** Detection from marker files, the defaults each language picks, and reading test
  output, checked against real `node --test` and Maven output recorded from the sandbox images
  ([tests/data/runner_output](../tests/data/runner_output)). A JavaScript issue run through the
  whole workflow, from failing tests to the publication checkpoint, and both new images run for
  real in Docker.
- **Patches.** CRLF files, context whitespace and missing final newlines are kept. Stale patches
  write nothing, even when only one patch in a set is stale. Malformed, multi-file, delete,
  rename and binary patches are refused.
- **GitHub.** Branch name rules, commit file filtering, pushes to a real local remote, a diverged
  branch that is never force-pushed, and the token kept out of argv and `.git/config`. API
  failures (401, 403, 404, 410, 422, rate limits, 5xx, network errors, non-JSON responses,
  redirects, odd payloads), and an outage mid-run followed by a retry. An API-mode publish end to
  end: a real push to a local stand-in for github.com, then the exact pull request request.
- **Documentation reader.** URL policy (scheme, port, credentials, IP hosts, lookalike hosts,
  `..` and encoded escapes, query strings, login paths, secrets in URLs), private and metadata
  addresses, redirect re-checks, no cookies or credentials ever sent, size, type, timeout and
  budget limits, robots.txt, page conversion, and Sphinx, MkDocs and llms.txt search. The sites
  are simulated by a mock transport ([tests/docs_site.py](../tests/docs_site.py)).
- **Reliability.** Backoff, jitter, `Retry-After` and retry limits; model retries through the real
  Anthropic SDK (529 twice, then success, each attempt logged and only the success billed);
  prices and cost; each run limit; tool timeouts; which tool errors are retried; a failed run
  retried from the failed step without repeating earlier ones; interrupted runs; and the recovery
  and plan checkpoints.
- **Workflow.** The full run with approvals, rejection with feedback, failed tests followed by a
  fix, a patch that breaks test collection and is then repaired, a rejected fix that is never
  written, and the publication checkpoint (full, partial, edited, redrafted, declined, failing
  midway).
- **Evaluation.** Every metric checked against six recorded runs with known outcomes (published,
  recovered after a failed test run, stopped, rejected, out of budget, still waiting) and
  hand-computed values, including n/a cases.
- **The docs.** Every setting is in `.env.example`, every route is in `docs/API.md`,
  `openapi.json` is current, the README tool table and the workflow diagram match the code, links
  resolve, every test cited in `docs/SECURITY.md` exists, there's no model name or key in the
  Python code, and the Docker files pass static checks.

## Problems the tests found

- **Case variants of protected names.** On a case-insensitive file system (the macOS default),
  `.ENV` and `.GIT/config` were readable. Protected names are now compared in lower case.
- **Symlinks to protected files.** A link inside the repository pointing at `.env` exposed its
  content through whole-repository search. A new untracked link exposed its target through
  `get_git_diff`, even when the target was outside the repository.
- **Secrets in the sandbox.** The sandbox copy included `.env` and key files, so a test could
  print them into the output.
- **pytest argument files.** `pytest @file` read options from the file, getting around the option
  allowlist.
- **Line endings and whitespace.** Patching a CRLF file turned the edited lines into LF.
  Context lines were replaced by the model's copy of them, which dropped trailing whitespace.
- **Half-applied patch sets.** A stale second patch left the first one applied. All approved
  patches are now checked before any is written.
- **Unknown tool arguments.** These were silently ignored, so `end_lines` was treated as no
  argument at all. Refused calls were also missing from the audit log.
- **GitHub responses.** A non-JSON success response, a redirect from a renamed repository, or a
  payload without a number surfaced as Python exceptions.
- **Smaller ones:**
  - a startup error from an in-process server arrived wrapped in an `ExceptionGroup`;
  - reading past the end of a file returned an empty range instead of an error;
  - the rate-limit message said "resets at later" when GitHub gave no reset time.
