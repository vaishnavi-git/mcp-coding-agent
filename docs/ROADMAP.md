# Roadmap

This is what I'm working on next. Most of it came out of the first experiment
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

## JavaScript and Java (done)

Everything that was pytest-specific now comes from a language profile (`app/languages.py`), and
the sandbox, the command rules and the test-output reading know three runners:

| | Python | JavaScript | Java |
|---|---|---|---|
| Detected by | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements.txt` | `package.json` | `pom.xml` |
| Sandbox image | `mcp-agent-sandbox` (`sandbox/`) | `mcp-agent-sandbox-js` (`sandbox/javascript/`) | `mcp-agent-sandbox-java` (`sandbox/java/`) |
| Test command | `pytest -q -p no:cacheprovider` | `node --test --test-reporter=tap` | `mvn -o -B test` |
| Allowed options | the existing pytest flags | `--test` first, a reporter, a simple `--test-name-pattern` | offline (`-o`) always, `-q`, `-B`, `-Dtest=Class#method`; the only goal is `test` |
| Failures read from | `FAILED`/`ERROR` lines | TAP `not ok` lines | surefire `<<< FAILURE!` lines, `COMPILATION ERROR` |

There are JavaScript and Java ports of the sample, with the same bug and their own issues file, and
read-only documentation sources for Node.js, MDN JavaScript, the Java SE API and JUnit 5.

A few things turned out differently from the plan:

- The JavaScript test command asks for the TAP reporter explicitly. Which reporter Node uses by
  default depends on its version and on whether output goes to a terminal.
- Maven runs without `-q`, because in quiet mode a passing run prints nothing, not even the test
  totals.
- `node --test` and Maven both exit with 0 when they find no tests. A run where nothing was tested
  now counts as failed for every runner, the way pytest already treated it.
- Maven's console library tried to unpack a native helper into `/tmp`, which the sandbox mounts
  `noexec`, and printed an error on every run. The Java image now unpacks it at build time.

## Look at the code before planning (done)

The plan used to name the files it would change before the model had seen any code. On issue #43
all five plans named files that don't exist, which is where all five unplanned edits in the
experiment came from. The plan step now works in three parts:

1. the model picks up to five searches;
2. the workflow runs them;
3. the model writes the plan with the results in front of it, and is told to use paths it saw.

The investigate step gets the same results instead of searching again, so this costs one small
extra model call per run, not extra tool calls. Whether it actually removes the unplanned edits is
something the next experiment has to show.

## Check the pull request summary (done)

Half of the drafted summaries got a detail wrong. After drafting, a second model call now compares
the text with the facts (the issue, the final diff and the recorded test results), lists what's
wrong and returns a corrected version. The reviewer sees the corrected text and the list of
corrections, and the original stays in the run record. It's on by default (`CHECK_PR_SUMMARY`) and
costs one small call per draft; whether it catches the kind of mistake the experiment found is for
the next experiment to show.

## A model and effort level per step (done)

- `ANTHROPIC_MODEL_<STEP>` and `ANTHROPIC_EFFORT_<STEP>` for `PLAN`, `INVESTIGATE`, `PROPOSE`,
  `PUBLISH` and `SUMMARY`, falling back to the general settings.
- Each call is priced at the model it actually used. Haiku 5.5 is in the price file with both of
  its rate cards (prompts up to 100K tokens, and over).
- A run records its setup, and the evaluation groups by it.
- `scripts/evaluate.py` reports cost by step, the cache hit rate, and how often the summary check
  corrected something.

Nothing has been switched to a cheaper model yet. Investigation is half the cost, so it's the
obvious candidate for Haiku 5.5, but that should come out of a comparison, not a guess.

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

## Tests for the rest

- The benchmark harness: it refuses outside the samples, records evaluator decisions, and its
  acceptance tests pass on a right fix and fail on a wrong one.
