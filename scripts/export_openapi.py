"""Write the API's OpenAPI schema to docs/openapi.json (a test checks it is current).

Usage: python scripts/export_openapi.py [--out docs/openapi.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.main import app  # noqa: E402
from app.config import PROJECT_ROOT  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "docs" / "openapi.json"


def schema_text() -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    args.out.write_text(schema_text())
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
