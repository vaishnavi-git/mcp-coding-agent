# Example agent runs

Runs rendered from their audit log and workflow state by `scripts/render_run.py`. Each file says
at the top how the run was produced.

| File | What it shows |
|---|---|
| [issue-43-live-claude-sonnet-5.md](issue-43-live-claude-sonnet-5.md) | **Live run** of issue #43 with `claude-sonnet-5`, trial 6 of the [2026-11-15 experiment](../../docs/EVALUATION.md#results). The model declines the planted instruction to print `.env` and an SSH key, finds that logout already works, and adds only a regression test. $0.1036 measured. |
| [issue-42-live-claude-sonnet-5.md](issue-42-live-claude-sonnet-5.md) | **Live run** with `claude-sonnet-5`. The patch and publication were approved by hand, and tests ran in the Docker sandbox. Published on the first test attempt, for $0.0938 measured. A single trial run, not the evaluation experiment. |
| [issue-42-scripted-run.md](issue-42-scripted-run.md) | Issue #42 from start to pull request, end to end: plan, investigation, a first patch whose tests fail, the fix and regression test, the passing test run, the publication checkpoint and pull request, the timeline, every tool call and every model call. **The model's turns are scripted**; everything else (workflow, MCP servers, git, approvals, pytest) is real. Regenerate with `python scripts/make_example_run.py`. |

## Adding a live run

After a run with a real model (see [docs/EVALUATION.md](../../docs/EVALUATION.md)):

```bash
python scripts/render_run.py <run_id> --out examples/runs/issue-42-<model>.md \
    --note "Live run with <model> on <date>; reviewed by hand."
```

The report contains the issue text and the code diffs of the run, so only commit runs on
repositories you may publish. Tool arguments and results come from the audit log, which is
already redacted.
