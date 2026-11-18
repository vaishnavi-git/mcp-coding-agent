"""Copy a bundled sample repository into workspaces/ and initialise git.

Usage: python scripts/setup_sample_repo.py [--language python|javascript|java | --sample NAME] [--dest PATH] [--force]

Python goes to workspaces/sample-repo (the default REPO_PATH), JavaScript to
workspaces/sample-repo-js and Java to workspaces/sample-repo-java. --sample picks any
bundled sample by name, such as sample-shop (the harder Python one).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import PROJECT_ROOT  # noqa: E402
from app.languages import ALIASES, PROFILES, SAMPLES  # noqa: E402
from app.sample_repo import setup_sample_repo  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--language", default="python", choices=[*PROFILES, *ALIASES], help="which sample (default: python)")
    parser.add_argument("--sample", choices=list(SAMPLES), help="a bundled sample by name (overrides --language)")
    parser.add_argument("--dest", help="where to put it (default: workspaces/<sample name>)")
    parser.add_argument("--force", action="store_true", help="Delete and recreate the destination")
    args = parser.parse_args()
    profile = PROFILES[SAMPLES[args.sample].language if args.sample else ALIASES.get(args.language, args.language)]
    name = args.sample or profile.sample
    dest = Path(args.dest) if args.dest else PROJECT_ROOT / "workspaces" / name
    path = setup_sample_repo(dest, force=args.force, language=profile.name, sample=args.sample)
    print(f"Sample repository ready at {path}")
    if dest.resolve() == (PROJECT_ROOT / "workspaces" / "sample-repo").resolve():
        print("This is the default REPO_PATH, so there is nothing else to set.")
    else:
        writable = ",".join(SAMPLES[name].writable_paths)
        print(f"Set REPO_PATH={path} and WRITABLE_PATHS={writable} in .env (and GITHUB_ISSUES_FILE=examples/issues/{SAMPLES[name].issues_file}).")
        print(f"The test command and sandbox image for {profile.name} are picked automatically;")
        print(f"build the image with: docker build -t {profile.sandbox_image} {profile.sandbox_context}/")


if __name__ == "__main__":
    main()
