"""Phase 5: patch application through the filesystem server and the workflow.

test_patching.py covers the parser and applier as units. These tests check
what reaches the disk: only the changed lines change (line endings and the
original text of context lines are kept), a patch that does not apply writes
nothing, and a set of approved patches is either applied whole or not at all
when one of them has gone stale.
"""

from __future__ import annotations

import json

import pytest

from app.mcp.client import MCPHub
from app.mcp.git_utils import run_git
from tests.fakes import FakeRunner, inprocess_servers
from tests.test_agent_workflow import FULL_FIX, REGRESSION_TEST
from tests.test_reliability_workflow import make_runner, scripted

pytestmark = pytest.mark.anyio


async def apply(settings, path: str, patch: str) -> tuple[dict | None, str]:
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        outcome = await hub.call("apply_patch", {"path": path, "patch": patch})
    return (None if outcome.is_error else json.loads(outcome.text)), outcome.text


async def test_crlf_files_keep_crlf(settings) -> None:
    target = settings.repo_path / "authkit" / "windows.py"
    target.write_bytes(b"def f():\r\n    return 1\r\n\r\n\r\ndef g():\r\n    return 2\r\n")
    patch = "--- a/authkit/windows.py\n+++ b/authkit/windows.py\n@@ -4,3 +4,4 @@\n \n def g():\n-    return 2\n+    # changed\n+    return 3\n"
    result, text = await apply(settings, "authkit/windows.py", patch)
    assert result, text
    assert target.read_bytes() == b"def f():\r\n    return 1\r\n\r\n\r\ndef g():\r\n    # changed\r\n    return 3\r\n"
    assert (result["added"], result["removed"]) == (2, 1)


async def test_context_lines_keep_their_original_text(settings) -> None:
    target = settings.repo_path / "authkit" / "spacing.py"
    target.write_text("x = 1   \ny = 2\nz = 3\t\n")  # trailing whitespace the model's patch leaves out
    patch = "--- a/authkit/spacing.py\n+++ b/authkit/spacing.py\n@@ -1,3 +1,3 @@\n x = 1\n-y = 2\n+y = 20\n z = 3\n"
    result, text = await apply(settings, "authkit/spacing.py", patch)
    assert result, text
    assert target.read_text() == "x = 1   \ny = 20\nz = 3\t\n"
    assert (result["added"], result["removed"]) == (1, 1)  # the diff shows only the real change


async def test_a_file_without_a_final_newline_keeps_it_that_way(settings) -> None:
    target = settings.repo_path / "authkit" / "last.py"
    target.write_bytes(b"a = 1\r\nb = 2")
    patch = "--- a/authkit/last.py\n+++ b/authkit/last.py\n@@ -1,2 +1,3 @@\n a = 1\n b = 2\n+c = 3\n"
    result, text = await apply(settings, "authkit/last.py", patch)
    assert result, text
    assert target.read_bytes() == b"a = 1\r\nb = 2\r\nc = 3"


async def test_a_patch_that_no_longer_applies_writes_nothing(settings) -> None:
    target = settings.repo_path / "authkit" / "sessions.py"
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        proposed = await hub.call("propose_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX})
        assert not proposed.is_error
        edited = target.read_text().replace("self._by_access.pop(session.access_token, None)", "self._by_access.pop(session.access_token)")
        target.write_text(edited)  # someone edits the file after the proposal
        outcome = await hub.call("apply_patch", {"path": "authkit/sessions.py", "patch": FULL_FIX})
    assert outcome.is_error and "hunk 1 does not apply to authkit/sessions.py" in outcome.text
    assert target.read_text() == edited


@pytest.mark.parametrize(
    "path, patch, expected",
    [
        ("authkit/sessions.py", FULL_FIX.replace("authkit/sessions.py", "authkit/other.py"), "patch headers refer to 'authkit/other.py'"),
        ("authkit/sessions.py", FULL_FIX + REGRESSION_TEST, "more than one file"),
        ("authkit/sessions.py", "diff --git a/authkit/sessions.py b/authkit/sessions.py\ndeleted file mode 100644\n--- a/authkit/sessions.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", "not supported"),
        ("authkit/sessions.py", "diff --git a/authkit/sessions.py b/authkit/moved.py\nrename from authkit/sessions.py\nrename to authkit/moved.py\n", "not supported"),
        ("authkit/sessions.py", "Binary files a/authkit/sessions.py and b/authkit/sessions.py differ\n", "not supported"),
        ("authkit/sessions.py", "--- /dev/null\n+++ b/authkit/sessions.py\n@@ -0,0 +1 @@\n+x = 1\n", "already exists"),
        ("authkit/missing.py", "--- a/authkit/missing.py\n+++ b/authkit/missing.py\n@@ -1 +1 @@\n-a\n+b\n", "does not exist; to create it use '--- /dev/null'"),
        ("authkit/sessions.py", "--- a/authkit/sessions.py\n+++ b/authkit/sessions.py\n@@ -1 +1 @@\n-this line is not in the file\n+x = 1\n", "does not apply"),
        ("authkit/sessions.py", "--- a/authkit/sessions.py\n+++ b/authkit/sessions.py\n@@ -64 +64 @@\n-        return new_session\n+        return new_session\n", "the patch makes no changes"),
        ("authkit/blob.bin", "--- a/authkit/blob.bin\n+++ b/authkit/blob.bin\n@@ -1 +1 @@\n-a\n+b\n", "is a binary file"),
        ("README.md", "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-# authkit (sample target repository)\n+# Hacked\n", "outside the writable paths"),  # would apply
        ("docs/new.md", "--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1 @@\n+hi\n", "outside the writable paths"),
        ("authkit/new.py", "--- /dev/null\n+++ b/authkit/new.py\n@@ -0,0 +1,2 @@\n+a = 1\n b = 2\n", "may only contain '+' lines"),
        (".", "--- /dev/null\n+++ b/.\n@@ -0,0 +1 @@\n+x\n", "cannot write to the repository root"),
    ],
)
async def test_invalid_patches_are_rejected_and_write_nothing(settings, path: str, patch: str, expected: str) -> None:
    repo = settings.repo_path
    (repo / "authkit" / "blob.bin").write_bytes(b"a\x00b\n")
    run_git(repo, "add", "authkit/blob.bin")
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    async with MCPHub(inprocess_servers(settings, FakeRunner())) as hub:
        for tool in ("propose_patch", "apply_patch"):
            outcome = await hub.call(tool, {"path": path, "patch": patch})
            assert outcome.is_error and expected in outcome.text, (tool, outcome.text)
    after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert after == before


async def test_a_stale_patch_stops_the_whole_set_before_anything_is_written(settings) -> None:
    runner, audit = make_runner(settings, scripted(), FakeRunner())
    view = await runner.start(42)
    run_id = view["run"]["id"]
    assert [p["path"] for p in view["pending_approval"]["proposals"]] == ["authkit/sessions.py", "tests/test_sessions.py"]

    # While the reviewer looks at the patches, someone edits the second file.
    tests_file = settings.repo_path / "tests" / "test_sessions.py"
    tests_file.write_text(tests_file.read_text().replace("with pytest.raises(InvalidToken):", "with pytest.raises(InvalidToken):  # edited"))

    view = await runner.resume(run_id, approved=True)
    assert view["run"]["status"] == "failed"
    failure = view["failure"]
    assert failure["code"] == "apply_failed" and failure["step"] == "apply" and not failure["retryable"]
    assert "approved patch for tests/test_sessions.py no longer applies" in failure["message"] and "nothing was written" in failure["message"]
    assert "nothing was written" in failure["hint"]
    # The first patch was valid but was not written either: only the manual edit is in the working tree.
    assert run_git(settings.repo_path, "status", "--porcelain").strip() == "M tests/test_sessions.py"
    assert not any(c["tool"] == "apply_patch" for c in audit.tool_calls(run_id))
