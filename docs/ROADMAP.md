# Roadmap

This is what I'm working on next. It came out of the first experiment
([EVALUATION.md](EVALUATION.md#results)): the agent solved every task, but the runs showed where it
wastes money and where it gets things wrong.

Where the money went in the 10 counted runs:

| Step | Share of cost |
|---|---|
| investigate | 50% |
| propose | 29% |
| plan | 9% |
| draft_publication | 8% |
| summary | 4% |

Most of it is output tokens, mostly thinking. 68% of input tokens were already served from the
prompt cache, so caching alone won't change much.

## JavaScript and Java

Everything pytest-specific moves into a language profile (`app/languages.py`):

| | Python | JavaScript | Java |
|---|---|---|---|
| Detected by | `pyproject.toml`, `setup.py`, `requirements.txt` | `package.json` | `pom.xml` |
| Sandbox image | `mcp-agent-sandbox` (`sandbox/Dockerfile`) | `mcp-agent-sandbox-js` (`sandbox/javascript/`) | `mcp-agent-sandbox-java` (`sandbox/java/`) |
| Test command | `pytest -q -p no:cacheprovider` | `node --test` | `mvn -o -q -B test` |
| Allowed options | the existing pytest flags | `--test`, `--test-reporter=tap/spec`, a simple `--test-name-pattern`; never `--require`, `--import`, `--eval`, loaders or inspectors | offline (`-o`) always; `-q`, `-B`, a simple `-Dtest=Class[#method]`; the only goal is `test` |
| Output parsed from | `FAILED`/`ERROR` lines | TAP `not ok` lines and `# pass`/`# fail` | surefire `Tests run:` totals, `[ERROR]` lines, `COMPILATION ERROR` |

- `PROJECT_LANGUAGE` (`auto` by default) picks the profile. `TEST_COMMAND`,
  `TEST_COMMAND_ALLOWLIST` and `SANDBOX_IMAGE` still win when they're set.
- The sandbox keeps `--network none`, so dependencies have to be in the image. The Java image
  downloads JUnit 5 and the Maven plugins when it's built. The JavaScript sample only uses Node's
  built-in test runner.
- `examples/sample-repo-js` and `examples/sample-repo-java` are ports of the same session library
  with the same bug, each with its own issues file. `setup_sample_repo.py --language` sets them up.
- The documentation reader gets read-only sources for Node.js, MDN JavaScript, the Java SE API
  and JUnit 5.

## Look at the code before planning

Right now the plan has to name the files it will change before the model has seen any code. On
issue #43 all five plans named files that don't exist, which is where all five unplanned edits in
the experiment came from. The plan step will work in three parts:

1. the model suggests searches;
2. the workflow runs them;
3. the model writes the plan with the results in front of it.

The "search before editing" rule still holds, since those searches are recorded before any patch
is proposed.

## Check the pull request summary

Half of the drafted summaries got a detail wrong. After drafting, a second model call will compare
the summary with the facts (the issue, the final diff and the recorded test results), list what's
wrong and return a corrected version. The reviewer sees the corrected text and the list of
corrections, and the original stays in the run record.

## A model and effort level per step

- `ANTHROPIC_MODEL_<STEP>` and `ANTHROPIC_EFFORT_<STEP>` for `PLAN`, `INVESTIGATE`, `PROPOSE`,
  `PUBLISH` and `SUMMARY`, falling back to the general settings.
- Each call is priced at the model it actually used, and Haiku 5.5 is added to the price file.
- A run records its model setup, and the evaluation can group by it.
- `scripts/evaluate.py` reports cost by step and the cache hit rate.

## A benchmark mode for comparing models

Comparing setups fairly needs more runs than I can review by hand, so there will be an
evaluation-only harness:

- It answers the checkpoints with a fixed policy: approve the plan and the patch, allow one more
  attempt at a recovery and then stop, publish up to a pull request. Those decisions are recorded
  as `decided_by: evaluator`, not as a person.
- After each run it copies hidden acceptance tests (`examples/eval/acceptance/`, never shown to
  the agent) next to the final code and runs them in the sandbox.
- Tasks come from `examples/eval/tasks.json`, and it's started with
  `scripts/run_benchmark.py --setup <name> --runs <n>`.
- It refuses to run unless `GITHUB_MODE=fixture` and the repository is one of the bundled samples
  under `workspaces/`. The API and UI never use it; the agent itself always needs a person.

The first comparison will be `claude-sonnet-5` against `claude-sonnet-5-5`, 10 runs each under
the same harness. The hand-reviewed results from the first experiment stay as they are and won't
be mixed in.

## Tests for all of this

- Language detection, and the same kinds of bypass attempts against the new command validators
  as against the pytest one.
- The output parsers, checked against real `node --test` and Maven output.
- Real Docker runs for the JavaScript and Java images, with CI building all three.
- The three-part plan step, the summary check, per-step models and pricing.
- The benchmark harness: it refuses outside the samples, records evaluator decisions, and its
  acceptance tests pass on a right fix and fail on a wrong one.
