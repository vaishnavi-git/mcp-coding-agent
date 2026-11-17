"""Compute evaluation metrics from recorded runs (definitions: docs/EVALUATION.md).

Reads the audit database and LangGraph checkpoints in DATA_DIR (from .env or
the environment, like the API). Nothing is run or estimated: runs that did not
happen produce no numbers.

Usage:
  python scripts/evaluate.py                               # all runs, Markdown to stdout
  python scripts/evaluate.py --since 2026-11-20T09:00 --issue 42 --issue 43
  python scripts/evaluate.py --run 20261120-091502-ab12cd --json
  python scripts/evaluate.py --since 2026-11-20 --out docs/evaluation/results-2026-11-20.md
  python scripts/evaluate.py --batch data/benchmarks/20261120T091502.json   # one benchmark (repeat to compare)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import PROJECT_ROOT, Settings  # noqa: E402
from app.evaluation import load_runs, run_metrics, summarize  # noqa: E402
from app.evaluation.report import render_json, render_markdown  # noqa: E402


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def source_text(database: Path) -> str:
    """Where the runs came from, without absolute paths (reports get published)."""
    try:
        return database.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return f"DATA_DIR/{database.name}"


def selection_text(args: argparse.Namespace) -> str:
    parts = []
    if args.since:
        parts.append(f"started at or after {args.since}")
    if args.until:
        parts.append(f"started before {args.until}")
    if args.issue:
        parts.append("issues " + ", ".join(f"#{n}" for n in args.issue))
    if args.batch:
        parts.append(("benchmarks " if len(args.batch) > 1 else "benchmark ") + ", ".join(b.stem for b in args.batch))
    if args.run:
        parts.append("runs " + ", ".join(args.run))
    return "; ".join(parts) or "all recorded runs"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", type=timestamp, help="only runs started at or after this time (ISO 8601; UTC if no offset)")
    parser.add_argument("--until", type=timestamp, help="only runs started before this time")
    parser.add_argument("--issue", type=int, action="append", help="only this issue number (repeatable)")
    parser.add_argument("--run", action="append", help="only this run id (repeatable)")
    parser.add_argument("--batch", type=Path, action="append", help="only the runs of this benchmark (the file scripts/run_benchmark.py wrote; repeatable)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    parser.add_argument("--out", type=Path, help="write to this file instead of stdout")
    parser.add_argument("--env-file", type=Path, help="settings file (default: .env in the project)")
    args = parser.parse_args(argv)

    settings = Settings.from_env(args.env_file)
    if not settings.database_path.exists():
        print(f"No audit database at {settings.database_path}: no runs have been recorded.", file=sys.stderr)
        return 1
    run_ids = list(args.run or [])
    for batch in args.batch or []:
        run_ids += [r["run_id"] for r in json.loads(batch.read_text())["runs"]]
    records = await load_runs(settings, run_ids=run_ids or None, since=args.since, until=args.until, issues=args.issue)
    rows = [run_metrics(r) for r in records]
    summary = summarize(rows)
    source, selection = source_text(settings.database_path), selection_text(args)
    text = render_json(summary, rows, source=source, selection=selection) if args.json else render_markdown(summary, rows, source=source, selection=selection)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
        print(f"Wrote {args.out} ({len(rows)} runs).", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
