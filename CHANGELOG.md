# Changelog

## Unreleased

- JavaScript and Java repositories. The language is detected from `package.json` or `pom.xml`
  (or set with `PROJECT_LANGUAGE`) and picks the test command, its allowlist and the sandbox
  image. `node --test` and Maven each get their own option rules and output parsing, and there
  are JavaScript and Java versions of the sample repository, sandbox images for both, and
  read-only docs for Node.js, MDN, the Java SE API and JUnit 5.
- A test run in which no tests ran now counts as failed for every runner.
- The plan is written after the first code searches instead of before: the model picks the
  searches, the workflow runs them, and the plan (including the files it expects to change) is
  written with the results in view. Investigation reuses those results.
- The drafted pull request text is fact-checked against the issue, the diff and the test runs
  before the publication checkpoint. Corrections are shown to the reviewer, with the original
  kept. `CHECK_PR_SUMMARY=false` turns it off.
- Each step can have its own model and effort level (`ANTHROPIC_MODEL_<STEP>`,
  `ANTHROPIC_EFFORT_<STEP>`). Every call is priced at the model it used, Haiku 5.5 is in the price
  file with its two rate cards, and each run records its setup.
- The evaluation report groups runs by setup and adds cost by step, the cache hit rate, and how
  often the summary check corrected the pull request text.
- Benchmark mode (`scripts/run_benchmark.py`): the sample issues in all three languages, an
  automatic reviewer recorded as `evaluator`, and hidden acceptance tests run after each run. It
  refuses to run outside fixture mode and the bundled samples. `scripts/evaluate.py --batch`
  reports one benchmark, including the acceptance pass rate. `--batch` can be repeated, and with
  more than one setup the cost-by-step table has a column for each.
- The summary check now sees the approved plan and is told what isn't a problem (facts that
  follow from the code, rewordings). In the benchmark it had struck a true statement about the
  plan because it couldn't see it. Claims about the issue must match the issue's own words, after
  a replay showed the first version of this change letting that mistake through. The final
  version got all 9 replays right (three recorded drafts, three times each).
- `scripts/replay_summary_check.py` runs the current summary check again on drafts from recorded
  runs, without running the agent.
- Docker Compose works, checked for the first time on macOS. Fixed: the app image failed to
  build with a group ID that already exists in the base image, such as macOS's usual group 20.
  New: `WORKSPACES_DIR` lets the sample repositories live outside cloud-synced folders.
- `.env.example` now suggests `claude-sonnet-5-5`, after the benchmark showed it as accurate as
  Sonnet 5 on the sample tasks, a third cheaper and twice as fast.
- Results: the first live JavaScript and Java runs, and Haiku 5.5 for the investigate step
  ([docs/EVALUATION.md](docs/EVALUATION.md#haiku-55-for-investigation-2026-11-17)).
- Benchmark results for Claude Sonnet 5 against Sonnet 5.5 on the Python sample
  ([docs/evaluation/results-2026-11-17-benchmark.md](docs/evaluation/results-2026-11-17-benchmark.md)).
- The long implementation plan is now split into [design notes](docs/DESIGN.md), this changelog
  and a [roadmap](docs/ROADMAP.md). The README has been rewritten.
- Comments and docstrings no longer refer to the stages the project was built in.
- Fixed: installing from a clean clone failed because `dependencies` had ended up under
  `[project.urls]` in `pyproject.toml`.

## 1.0.0 (2026-11-15)

First public release, built between 13 and 15 November 2026.

### Agent

- Reads a GitHub issue, plans, searches and reads the repository, proposes patches, waits for
  approval, applies them, runs the tests in Docker, and retries with the failing tests in view.
- LangGraph workflow with SQLite checkpoints, so runs survive restarts and wait for approval as
  long as needed.
- The model only gets read-only tools and `propose_patch`. Approved patches are applied by the
  workflow and checked against what was approved by SHA-256.
- The plan lists the files it expects to change, and the patch checkpoint flags proposed files
  outside that list.

### GitHub

- Publication checkpoint: branch and commit, push, and pull request are approved separately, and
  the drafted text can be edited first.
- The pull request's *Changes*, *Tests* and *References* sections come from recorded results, not
  from the model.
- Branches use an `agent/` prefix. No force push, no pushes to the default branch, draft pull
  requests by default. Fixture mode runs the whole flow against a local bare repository.

### Documentation reader

- A read-only MCP server for an allowlist of documentation sites. HTTPS GET only, no cookies, no
  logins, search through each site's own index.

### Reliability

- Retries with exponential backoff for model and tool calls; write tools are never retried.
- Per-call tool timeouts, and limits on tool calls, model calls and cost per run.
- Structured failure records with hints. Failed runs can be retried from the failed step.
- A recovery checkpoint when fix attempts run out or the tests can't run.

### Security fixes found by testing

- Protected names are compared case-insensitively (`.ENV` used to open `.env` on macOS).
- pytest `@argument-files` are refused.
- Protected files are left out of the sandbox copy of the repository.
- Symlinks to protected files no longer leak through search or the diff.
- CRLF files keep their line endings when patched.
- A stale patch in an approved set means nothing in the set is written.
- Unknown tool arguments are refused instead of ignored, and refused calls are logged.

### Docs, evaluation and CI

- Architecture, setup, API (with `openapi.json`), security, testing and evaluation docs, with
  tests that keep them in step with the code.
- `scripts/evaluate.py` computes the metrics from recorded runs. Results of a 10-run experiment
  with `claude-sonnet-5` are in [docs/EVALUATION.md](docs/EVALUATION.md).
- Dockerfile and Docker Compose setup.
- GitHub Actions runs the linter and all 512 tests, including the Docker sandbox test.
