"""Copy examples/sample-repo to workspaces/sample-repo and initialise git.

Usage: python scripts/setup_sample_repo.py [--dest PATH] [--force]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import PROJECT_ROOT  # noqa: E402
from app.sample_repo import setup_sample_repo  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default=str(PROJECT_ROOT / "workspaces" / "sample-repo"))
    parser.add_argument("--force", action="store_true", help="Delete and recreate the destination")
    args = parser.parse_args()
    path = setup_sample_repo(Path(args.dest), force=args.force)
    print(f"Sample repository ready at {path}")
    print("Set REPO_PATH to this directory (it is the default).")


if __name__ == "__main__":
    main()
