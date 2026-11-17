"""The category-to-test mapping stays true: listed tests exist, categories are covered, category files are fully listed."""

from __future__ import annotations

import importlib
import inspect
import subprocess
import sys

import pytest

from app.config import PROJECT_ROOT
from tests.spec_matrix import CATEGORY_FILES, MIN_TESTS_PER_CATEGORY, SPEC_CATEGORIES, SPEC_MATRIX, marker_index


def functions_in(file: str) -> set[str]:
    module = importlib.import_module(f"tests.{file.removesuffix('.py')}")
    return {name for name, obj in vars(module).items() if name.startswith("test_") and inspect.isfunction(obj) and obj.__module__ == module.__name__}


def test_every_spec_category_is_mapped() -> None:
    assert list(SPEC_MATRIX) == list(SPEC_CATEGORIES)
    for marker, tests in SPEC_MATRIX.items():
        assert len(set(tests)) == len(tests), f"{marker} lists a test twice"
        assert len(tests) >= MIN_TESTS_PER_CATEGORY, f"{SPEC_CATEGORIES[marker]} has only {len(tests)} tests"


def test_every_listed_test_exists() -> None:
    missing = []
    for test in marker_index():
        file, function = test.split("::")
        if function not in functions_in(file):
            missing.append(test)
    assert missing == [], "renamed or deleted tests are still listed in tests/spec_matrix.py"


def test_every_category_file_test_is_listed() -> None:
    listed = set(marker_index())
    unlisted = [f"{file}::{name}" for file in CATEGORY_FILES for name in sorted(functions_in(file)) if f"{file}::{name}" not in listed]
    assert unlisted == []


@pytest.mark.parametrize("marker", ["path_traversal", "pull_requests"])
def test_a_category_can_be_run_on_its_own(marker: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", "-m", marker, "-p", "no:cacheprovider"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    collected = {line.split("::")[0].removeprefix("tests/") + "::" + line.split("::")[1].split("[")[0] for line in proc.stdout.splitlines() if "::" in line}
    assert collected == set(SPEC_MATRIX[marker])
