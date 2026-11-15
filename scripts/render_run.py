"""Render one recorded run as a Markdown walkthrough (issue, plan, diffs, tests, PR, audit log).

The output contains the issue text and code diffs from the run; treat it like
the repository. Secrets in tool arguments and results were already redacted in
the audit log.

Usage:
  python scripts/render_run.py <run_id>
  python scripts/render_run.py <run_id> --out examples/runs/my-run.md --note "Live run, reviewed by hand."
  python scripts/render_run.py --latest
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.evaluation import load_runs  # noqa: E402
from app.evaluation.run_report import render_run  # noqa: E402
from app.storage.audit import AuditStore  # noqa: E402


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", nargs="?", help="run id (see GET /runs or the UI)")
    parser.add_argument("--latest", action="store_true", help="render the most recent run")
    parser.add_argument("--note", help="a provenance note shown at the top (how the run was produced)")
    parser.add_argument("--out", type=Path, help="write to this file instead of stdout")
    parser.add_argument("--env-file", type=Path, help="settings file (default: .env in the project)")
    args = parser.parse_args(argv)

    settings = Settings.from_env(args.env_file)
    if not settings.database_path.exists():
        print(f"No audit database at {settings.database_path}.", file=sys.stderr)
        return 1
    run_id = args.run_id
    if args.latest:
        recent = AuditStore(settings.database_path).list_runs(limit=1)
        run_id = recent[0]["id"] if recent else None
    if not run_id:
        parser.error("give a run id or --latest")
    records = await load_runs(settings, run_ids=[run_id])
    if not records:
        print(f"Run {run_id} not found.", file=sys.stderr)
        return 1
    text = render_run(records[0], note=args.note)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
        print(f"Wrote {args.out}.", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
