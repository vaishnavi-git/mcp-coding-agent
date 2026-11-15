"""Unified-diff parsing and application."""

from __future__ import annotations

import pytest

from app.mcp.patching import PatchError, apply_to_text, diff_stats, parse_patch, unified_diff

ORIGINAL = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"


def test_apply_simple_patch() -> None:
    patch = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""
    assert apply_to_text(ORIGINAL, parse_patch(patch), "calc.py") == ORIGINAL.replace("a - b", "a + b")


def test_wrong_line_numbers_are_tolerated_but_context_must_match() -> None:
    patch = """--- a/calc.py
+++ b/calc.py
@@ -40,3 +40,3 @@
 def mul(a, b):
-    return a * b
+    return b * a
"""
    result = apply_to_text(ORIGINAL, parse_patch(patch), "calc.py")
    assert result.endswith("    return b * a\n")


def test_context_mismatch_is_rejected() -> None:
    patch = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def subtract(a, b):
-    return a - b
+    return a + b
"""
    with pytest.raises(PatchError, match="does not apply"):
        apply_to_text(ORIGINAL, parse_patch(patch), "calc.py")


def test_new_file_patch() -> None:
    patch = """--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1,2 @@
+VALUE = 1
+OTHER = 2
"""
    parsed = parse_patch(patch)
    assert parsed.is_new_file
    assert apply_to_text(None, parsed, "pkg/new.py") == "VALUE = 1\nOTHER = 2\n"


def test_multiple_hunks_and_header_less_patch() -> None:
    patch = """@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
@@ -5,2 +5,3 @@
 def mul(a, b):
     return a * b
+# end
"""
    result = apply_to_text(ORIGINAL, parse_patch(patch), "calc.py")
    assert "a + b" in result and result.endswith("# end\n")


@pytest.mark.parametrize(
    "patch, message",
    [
        ("", "empty"),
        ("just some text", "no hunks"),
        ("--- a/x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", "deleting"),
        ("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-a\n+b\n", "more than one file"),
        ("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n a\n", "no changes"),
        ("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n*b\n", "invalid line"),
    ],
)
def test_malformed_patches_are_rejected(patch: str, message: str) -> None:
    with pytest.raises(PatchError, match=message):
        parse_patch(patch)


def test_no_trailing_newline_is_preserved() -> None:
    original = "a\nb"
    patch = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n\\ No newline at end of file\n"
    assert apply_to_text(original, parse_patch(patch), "f") == "a\nc"


def test_unified_diff_and_stats() -> None:
    diff = unified_diff(ORIGINAL, ORIGINAL.replace("a - b", "a + b"), "calc.py")
    assert diff.startswith("--- a/calc.py\n+++ b/calc.py\n")
    assert diff_stats(diff) == (1, 1)
