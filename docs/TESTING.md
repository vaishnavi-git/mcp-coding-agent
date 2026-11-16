# Testing

```bash
pytest                          # everything
pytest -m path_traversal        # one spec test category (markers below)
pytest -m docker                # only the test that needs a real Docker daemon
pytest --cov=app                # with line and branch coverage
```

The suite needs no network and no API key. The workflow tests drive the real LangGraph workflow,
real MCP servers, real git and real approvals. The model's turns are scripted
([tests/fakes.py](../tests/fakes.py)), and pytest runs on a copy of the repository in place of
Docker. `tests/test_sandbox_server.py::test_real_docker_run` runs only when Docker and the
`mcp-agent-sandbox:latest` image are available; everywhere else it is skipped.

## Last full run

512 tests. With Docker running and the sandbox image built, all 512 pass; this was checked on
macOS on Apple silicon with Docker Desktop (engine 29.9.3). Without Docker, the real-Docker test
is skipped. A run takes about 100 seconds on a laptop, or about two minutes with coverage on.

| Spec category | Marker | Tests passing |
|---|---|---|
| Tool discovery | `discovery` | 18 |
| Valid tool calls | `valid_calls` | 9 |
| Invalid arguments | `invalid_args` | 36 |
| Path traversal attempts | `path_traversal` | 45 |
| Unauthorized file access | `unauthorized_access` | 55 |
| Dangerous command rejection | `dangerous_commands` | 77 |
| Test timeout handling | `timeouts` | 7 |
| Failed test recovery | `recovery` | 10 |
| Patch application | `patching` | 36 |
| GitHub API failures | `github_failures` | 43 |
| Pull-request creation | `pull_requests` | 15 |

Parametrized cases count separately, and a test can be in more than one category. The other
tests cover reliability, the documentation reader, the API, evaluation and the docs themselves.

Coverage, measured with `pytest --cov=app` on the same run (line and branch combined):

| Scope | Coverage |
|---|---|
| The whole `app` package | 86% |
| Without the Streamlit UI (no automated tests; checked by hand) | 92% |
| Security-critical modules (path guard, filesystem, sandbox, patching, tool loop, MCP hub) | 92% |
| Evaluation package | 86% |

Code that the stdio tests run in subprocesses (each server's `main`) is not measured.

## Spec test categories

[tests/spec_matrix.py](../tests/spec_matrix.py) maps each test category in the project spec to
the tests that cover it. `conftest.py` turns the mapping into the pytest markers in the table
above, so `pytest -m <marker>` runs one category, and every run ends with a table of results per
category. `tests/test_spec_matrix.py` fails if a listed test is renamed or deleted, if a category
has fewer than five tests, or if a Phase 5 test is in no category.

## What each phase added

- **Phase 1:** tool discovery; MCP connections (in-process and real stdio); valid and invalid tool
  arguments; path traversal, symlink escapes and forbidden files; dangerous command rejection;
  Docker isolation flags and timeouts; GitHub issue API failures; Anthropic error mapping through
  the real SDK; and the full workflow, including failed-test recovery.
- **Phase 2:** branch name rules; commit file filtering; pushes to a real local remote; a
  diverged branch that is never force-pushed; token kept out of argv and `.git/config`; PR
  request shape; PR API failures (401/403/404/422/rate limit/5xx/network); and the publication
  checkpoint end to end (full, partial, edited, redrafted, declined, failing midway).
- **Phase 3:** URL policy (scheme, port, credentials, IP hosts, lookalike hosts, `..` and encoded
  escapes, query strings, login paths, secrets in URLs); private and metadata addresses; redirect
  re-checks; no cookies or credentials ever sent; size, type, timeout and budget limits;
  robots.txt; page conversion and section anchors; Sphinx, MkDocs and llms.txt search; index
  caching; the docs tools over MCP (in-process and real stdio); and the workflow recording and
  citing what the model read. The documentation sites are simulated by a mock transport
  ([tests/docs_site.py](../tests/docs_site.py)).
- **Phase 4:** backoff delays, jitter, `Retry-After` and retry limits; model retries through the
  real Anthropic SDK (529 twice, then success, each attempt logged, only the success billed);
  prices and cost; each run limit; old databases migrated; tool timeouts; which tool errors are
  retried (read-only and idempotent tools only); cost and call limits stopping a run; a failed run
  retried from the failed step without repeating earlier steps; retry limits; no automatic retry
  inside `apply`; interrupted runs; test-failure analysis reaching the model; the recovery and
  plan checkpoints; the timeline; and the retry endpoint.
- **Phase 5:** each spec category tested through every entry point, not only the module that
  implements the rule.
  - **Tool contract:** the whole catalog is pinned (server, risk class, required arguments,
    explicit annotations, valid schemas), both in-process and over the production stdio servers.
    Every tool has a passing valid call.
  - **Invalid arguments:** every tool rejects missing, mistyped and unknown arguments. Size and
    range limits give clear errors.
  - **Path traversal:** a generated set of attack paths is sent through read, search, propose,
    apply and the test command. Symlinks pointing out of the repository and at protected files
    are covered too.
  - **Protected files:** letter-case variants are blocked, and protected files stay out of
    search, the git diff, commits and the sandbox. A probe test running inside the sandbox
    confirms it cannot see them.
  - **Commands:** about 40 attempts to get around the allowlist are rejected, with a real check
    that pytest applies options from `@files`.
  - **Timeouts:** a run killed at the time limit, and a sandbox server that never answers.
  - **Recovery:** a patch that breaks test collection and is then repaired; a rejected fix that
    is never written.
  - **Patches:** CRLF files, context whitespace and missing final newlines are kept. Stale
    patches write nothing, even when only one patch in a set is stale.
  - **GitHub:** non-JSON responses, redirects, odd payloads, 410 and secondary rate limits; an
    outage mid-run followed by a retry.
  - **Pull requests:** an API-mode publish end to end: a real push to a local stand-in for
    github.com, then the exact PR request.

- **Phase 6:** the evaluation metrics, checked against six recorded runs with known outcomes
  (published, recovered after a failed test run, stopped, rejected, out of budget, still
  waiting) and against hand-computed values. Also n/a when a price is unknown, the run report,
  the evaluation and report scripts, and checks that keep the documentation true:
  - every setting is in `.env.example`;
  - every route is in `docs/API.md`, and `openapi.json` is current;
  - the README tool table and the workflow diagram match the code;
  - links resolve, and every test cited in `docs/SECURITY.md` exists;
  - no model name or key is in the Python code;
  - static checks of the Docker files.

The workflow tests use a scripted LLM and run pytest on a copy of the sample repo in place of
Docker. `tests/test_sandbox_server.py::test_real_docker_run` runs only when Docker and the
`mcp-agent-sandbox:latest` image are available.

## Problems Phase 5 found and fixed

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
- **Unknown tool arguments.** These were silently ignored, so `end_lines` was read as no
  argument. Refused calls were also missing from the audit log.
- **GitHub responses.** A non-JSON success response, a redirect from a renamed repository, or a
  payload without a number surfaced as Python exceptions.
- **Smaller fixes:**
  - a startup error from an in-process server arrived wrapped in an `ExceptionGroup`;
  - reading past the end of a file returned an empty range instead of an error;
  - the rate-limit message said "resets at later" when GitHub gave no reset time.

