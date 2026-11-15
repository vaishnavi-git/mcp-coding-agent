"""Read a pytest run so a failed attempt can be fixed with targeted information.

The sandbox returns pytest's own output. pytest prints a "short test summary"
with one ``FAILED <test id> - <reason>`` / ``ERROR <test id>`` line per problem
(``-rfE`` is pytest's default), which is what this module relies on.
"""

from __future__ import annotations

import re
from typing import Any

SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$", re.MULTILINE)
COLLECTION_ERROR = re.compile(r"Interrupted: \d+ errors? during collection|^ERROR collecting ", re.MULTILINE)
MAX_TESTS = 50

KIND_TEXT = {
    "passed": "all tests passed",
    "test_failures": "one or more tests failed (assertion failures or exceptions in the tests)",
    "collection_error": "pytest could not import or collect the tests (syntax error, import error or broken fixture), so no test ran",
    "timeout": "the test run hit the sandbox time limit and was killed; look for an infinite loop, a deadlock or very slow code",
    "no_tests": "pytest collected no tests",
    "not_run": "the tests could not be started",
    "unknown": "the test command failed; see the output",
}


def analyze(status: str, output: str, exit_code: int | None) -> dict[str, Any]:
    """Failure kind and failing test ids for one sandbox run."""
    failing: list[str] = []
    for _, test_id, _ in SUMMARY_LINE.findall(output or ""):
        if test_id not in failing:
            failing.append(test_id)
    if status == "PASSED":
        kind = "passed"
    elif status == "TIMED OUT":
        kind = "timeout"
    elif status == "ERROR":
        kind = "not_run"
    elif COLLECTION_ERROR.search(output or ""):
        kind = "collection_error"
    elif exit_code == 5 or "no tests ran" in (output or ""):
        kind = "no_tests"
    elif failing:
        kind = "test_failures"
    else:
        kind = "unknown"
    return {"failure_kind": kind, "failing_tests": failing[:MAX_TESTS]}


def compare(previous: list[str], current: list[str]) -> dict[str, list[str]]:
    before, now = set(previous), set(current)
    return {
        "still_failing": [t for t in current if t in before],
        "newly_failing": [t for t in current if t not in before],
        "fixed": [t for t in previous if t not in now],
    }


def _names(tests: list[str], limit: int = 15) -> str:
    shown = ", ".join(tests[:limit])
    return shown + (f" and {len(tests) - limit} more" if len(tests) > limit else "")


def progress_note(previous: dict[str, Any] | None, current: dict[str, Any]) -> str:
    """What changed between two failed runs, in words for the model."""
    if not previous or previous.get("status") == "PASSED":
        return ""
    if previous.get("failure_kind") != current.get("failure_kind"):
        return (
            f"Compared with the previous run: it failed with '{previous.get('failure_kind')}', "
            f"this one with '{current.get('failure_kind')}'."
        )
    diff = compare(previous.get("failing_tests", []), current.get("failing_tests", []))
    if not diff["fixed"] and not diff["newly_failing"]:
        return "Compared with the previous run: the same tests still fail, so your last change did not help. Reconsider the root cause before patching again."
    parts = []
    if diff["fixed"]:
        parts.append(f"now passing: {_names(diff['fixed'])}")
    if diff["still_failing"]:
        parts.append(f"still failing: {_names(diff['still_failing'])}")
    if diff["newly_failing"]:
        parts.append(f"newly failing (probably broken by your last change): {_names(diff['newly_failing'])}")
    return "Compared with the previous run: " + "; ".join(parts) + "."
