# Benchmark tasks

[tasks.json](tasks.json) lists the tasks `scripts/run_benchmark.py` runs: issues #42 and #43 of the
sample repository in Python, JavaScript and Java. [acceptance/](acceptance) holds one hidden
acceptance test per task. After a run, the benchmark copies it next to the agent's final code in a
throwaway copy and runs it in that language's sandbox image.

The agent never sees these files: it can only read the repository it works on (under
`workspaces/`), and they live outside it. They test what each issue asks for through the
library's public API, not how the fix is written, and a test checks that each one passes on a
right fix and fails on the obvious wrong ones. How the benchmark works and where it refuses to
run: [docs/EVALUATION.md](../../docs/EVALUATION.md#benchmark-mode).
