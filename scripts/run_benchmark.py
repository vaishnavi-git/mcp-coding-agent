"""Run the benchmark: sample tasks, an automatic reviewer and hidden acceptance tests.

For comparing models and per-step settings. It answers the checkpoints itself (recorded as
decided_by: evaluator), so it only runs in fixture mode on the bundled samples under
workspaces/, and never through the API or the UI. Each run calls the model and costs money:
--max-total-cost stops it before the next run once that much has been spent, on top of
MAX_TASK_COST_USD for each run. See docs/EVALUATION.md.

Usage:
  python scripts/run_benchmark.py --tasks python-42,python-43 --runs 5
  python scripts/run_benchmark.py --model <model> --runs 5 --max-total-cost 1.50
  python scripts/run_benchmark.py --step-model investigate=<model> --tasks python-42

The sandbox images for the tasks' languages must be built first (docs/SETUP.md). The runs and
their acceptance results are written to data/benchmarks/<time>.json; report them with
  python scripts/evaluate.py --batch data/benchmarks/<time>.json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import MODEL_STEPS, Settings  # noqa: E402
from app.evaluation.benchmark import BenchmarkRefused, load_tasks, run_benchmark  # noqa: E402


def step_values(pairs: list[str] | None, what: str) -> dict[str, str]:
    values = {}
    for pair in pairs or []:
        step, _, value = pair.partition("=")
        if step not in MODEL_STEPS or not value:
            raise SystemExit(f"--step-{what} needs STEP=VALUE with STEP one of {', '.join(MODEL_STEPS)} (got {pair!r})")
        values[step] = value
    return values


def show(result: dict) -> None:
    acceptance = result["acceptance"]
    cost = "n/a" if result["cost_usd"] is None else f"${result['cost_usd']:.4f}"
    print(f"{result['task']:15} {result['run_id']}  {result['status']:15} {cost:>9}  acceptance: {acceptance['status']} ({acceptance['summary'] or '-'})", flush=True)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks", help="comma-separated task ids from examples/eval/tasks.json (default: all)")
    parser.add_argument("--runs", type=int, default=1, help="runs of each task (default 1)")
    parser.add_argument("--model", help="main model for this benchmark (default: ANTHROPIC_MODEL)")
    parser.add_argument("--effort", help="effort for every step (default: ANTHROPIC_EFFORT)")
    parser.add_argument("--step-model", action="append", metavar="STEP=MODEL", help="a model for one step (repeatable)")
    parser.add_argument("--step-effort", action="append", metavar="STEP=LEVEL", help="an effort level for one step (repeatable)")
    parser.add_argument("--max-total-cost", type=float, default=2.0, help="stop before the next run once this many USD are spent (default 2.00)")
    parser.add_argument("--env-file", type=Path, help="settings file (default: .env in the project)")
    args = parser.parse_args(argv)

    settings = Settings.from_env(args.env_file)
    settings = dataclasses.replace(
        settings,
        anthropic_model=args.model or settings.anthropic_model,
        anthropic_effort=args.effort or settings.anthropic_effort,
        anthropic_model_by_step={**settings.anthropic_model_by_step, **step_values(args.step_model, "model")},
        anthropic_effort_by_step={**settings.anthropic_effort_by_step, **step_values(args.step_effort, "effort")},
    )
    tasks = load_tasks(only=[t.strip() for t in args.tasks.split(",")] if args.tasks else None)
    print(f"{len(tasks)} task(s) x {args.runs} run(s), model {settings.anthropic_model}, stop after ${args.max_total_cost:.2f}", flush=True)
    try:
        result = await run_benchmark(settings, tasks, args.runs, max_total_cost=args.max_total_cost, report=show)
    except BenchmarkRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    out = settings.data_dir / "benchmarks" / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    if result["stopped"]:
        print(result["stopped"])
    print(f"Spent ${result['total_cost_usd']:.4f}. Report: python scripts/evaluate.py --batch {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
