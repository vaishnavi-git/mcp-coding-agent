"""Prepare the sample repository for one evaluation trial, then start the run yourself.

Resets the sample repository, moves earlier fixture pull requests to an archive, and for
issue 43 commits issue 42's fix first so the tests start green (see
app/evaluation/trials.py and docs/EVALUATION.md).

Usage: python scripts/prepare_trial.py 42
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.evaluation.trials import prepare_trial  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("issue", type=int, help="the issue the next run is for (42 or 43 in the sample)")
    parser.add_argument("--env-file", type=Path, help="settings file (default: .env in the project)")
    args = parser.parse_args()
    settings = Settings.from_env(args.env_file)
    done = prepare_trial(args.issue, settings.repo_path, settings.data_dir)
    print(f"Repository ready for issue #{args.issue}.")
    for message in done["starting_commits"]:
        print(f"  starting point: {message}")
    if done["archived_pull_requests"]:
        print("  earlier fixture pull requests moved to the archive")


if __name__ == "__main__":
    main()
