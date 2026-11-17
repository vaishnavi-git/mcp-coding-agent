"""Read a test run so a failed attempt can be fixed with targeted information.

The sandbox returns the test runner's own output, and each runner reports failures its own way:

- pytest prints a "short test summary" with one ``FAILED <test id> - <reason>`` or
  ``ERROR <test id>`` line per problem (``-rfE`` is pytest's default);
- ``node --test`` with the TAP reporter prints ``not ok`` lines followed by a YAML block, and
  totals as ``# tests``, ``# pass`` and ``# fail`` comments (the spec reporter uses ``ℹ`` and ``✖``);
- Maven's surefire plugin prints ``[ERROR] <class>.<method> -- Time elapsed: ... <<< FAILURE!``
  for each failing test, and ``COMPILATION ERROR`` when the code doesn't compile.
"""

from __future__ import annotations

import re
from typing import Any

SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$", re.MULTILINE)
COLLECTION_ERROR = re.compile(r"Interrupted: \d+ errors? during collection|^ERROR collecting ", re.MULTILINE)
_PYTEST_SUMMARY = re.compile(r"^=+ (.+? in [\d.]+s.*?) =+\s*$", re.MULTILINE)

_TAP_SUBTEST = re.compile(r"^(\s*)# Subtest: (.+)$")
_TAP_RESULT = re.compile(r"^(\s*)(not ok|ok) \d+ - (.+?)(?: # (?:SKIP|TODO).*)?$")
_NODE_COUNT = re.compile(r"^(?:#|ℹ) (tests|pass|fail|cancelled|skipped|todo) (\d+)\s*$", re.MULTILINE)
_SPEC_FAILING = re.compile(r"^✖ (.+?) \([\d.]+m?s\)\s*$", re.MULTILINE)

_SUREFIRE_FAILURE = re.compile(r"^\[ERROR\] ([\w.$]+) -- Time elapsed: .*<<< (?:FAILURE|ERROR)!", re.MULTILINE)
_SUREFIRE2_FAILURE = re.compile(r"^(\w+)\(([\w.$]+)\)\s+Time elapsed: .*<<< (?:FAILURE|ERROR)!", re.MULTILINE)
_SUREFIRE_TOTALS = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)(?!.*-- in )")
_MAVEN_COMPILE_ERROR = re.compile(r"COMPILATION ERROR|Compilation failure")

MAX_TESTS = 50

KIND_TEXT = {
    "passed": "all tests passed",
    "test_failures": "one or more tests failed (assertion failures or exceptions in the tests)",
    "collection_error": "the tests could not be compiled, imported or collected (a syntax error, import error or broken fixture), so they did not run",
    "timeout": "the test run hit the sandbox time limit and was killed; look for an infinite loop, a deadlock or very slow code",
    "no_tests": "no tests were found, so none ran",
    "not_run": "the tests could not be started",
    "unknown": "the test command failed; see the output",
}


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _parse_pytest(output: str) -> dict[str, Any]:
    found = _PYTEST_SUMMARY.findall(output)
    if found:
        summary: str | None = found[-1].strip()
    else:
        tail = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
        summary = tail[-1] if tail and re.search(r"\b(passed|failed|error|errors|no tests ran)\b", tail[-1]) else None
    return {
        "failing": _unique([test_id for _, test_id, _ in SUMMARY_LINE.findall(output)]),
        "summary": summary,
        "collection_error": bool(COLLECTION_ERROR.search(output)),
        "no_tests": "no tests ran" in output,
    }


def _parse_node(output: str) -> dict[str, Any]:
    failing: list[str] = []
    load_error = False
    lines = output.splitlines()
    stack: list[str] = []
    for i, line in enumerate(lines):
        if m := _TAP_SUBTEST.match(line):
            depth = len(m.group(1)) // 4
            stack = [*stack[:depth], m.group(2).strip()]
            continue
        m = _TAP_RESULT.match(line)
        if not m or m.group(2) != "not ok":
            continue
        depth = len(m.group(1)) // 4
        block: list[str] = []
        for following in lines[i + 1 : i + 60]:
            if following.strip() == "...":
                break
            block.append(following.strip())
        if "failureType: 'subtestsFailed'" in block:
            continue  # a suite whose tests failed; the tests themselves are listed
        if any(entry.startswith("exitCode:") for entry in block):
            load_error = True  # the whole file failed, before or outside any test
        failing.append(" > ".join([*stack[:depth], m.group(3).strip()]))
    if not failing:  # spec reporter
        failing = _SPEC_FAILING.findall(output.split("✖ failing tests:", 1)[-1]) if "✖ failing tests:" in output else []
    counts = {name: int(value) for name, value in _NODE_COUNT.findall(output)}
    summary = None
    if "tests" in counts:
        if counts["tests"] == 0:
            summary = "no tests ran"
        else:
            parts = [f"{counts.get('pass', 0)} passed", f"{counts.get('fail', 0)} failed"]
            parts += [f"{counts[k]} {k}" for k in ("cancelled", "skipped", "todo") if counts.get(k)]
            summary = f"{counts['tests']} tests: " + ", ".join(parts)
    return {
        "failing": _unique(failing),
        "summary": summary,
        "collection_error": load_error,
        "no_tests": counts.get("tests") == 0,
    }


def _parse_maven(output: str) -> dict[str, Any]:
    failing = _SUREFIRE_FAILURE.findall(output) + [f"{cls}.{method}" for method, cls in _SUREFIRE2_FAILURE.findall(output)]
    compile_error = bool(_MAVEN_COMPILE_ERROR.search(output))
    totals = _SUREFIRE_TOTALS.findall(output)
    no_tests = "No tests to run." in output or bool(totals and totals[-1][0] == "0")
    if compile_error:
        summary: str | None = "compilation failed"
    elif no_tests:
        summary = "no tests ran"
    elif totals:
        run, failures, errors, skipped = totals[-1]
        summary = f"Tests run: {run}, Failures: {failures}, Errors: {errors}, Skipped: {skipped}"
    else:
        summary = None
    return {"failing": _unique(failing), "summary": summary, "collection_error": compile_error, "no_tests": no_tests}


PARSERS = {"pytest": _parse_pytest, "node": _parse_node, "maven": _parse_maven}


def parse_output(output: str, runner: str = "pytest") -> dict[str, Any]:
    """Failing tests, a one-line summary, and whether the tests failed to load or none ran."""
    return PARSERS.get(runner, _parse_pytest)(output or "")


def analyze(status: str, output: str, exit_code: int | None, runner: str = "pytest") -> dict[str, Any]:
    """Failure kind and failing test ids for one sandbox run."""
    parsed = parse_output(output, runner)
    failing = parsed["failing"]
    if status == "PASSED":
        kind = "passed"
    elif status == "TIMED OUT":
        kind = "timeout"
    elif status == "ERROR":
        kind = "not_run"
    elif parsed["collection_error"]:
        kind = "collection_error"
    elif parsed["no_tests"] or (runner == "pytest" and exit_code == 5):
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
