"""Filesystem MCP server through a real MCP client session (in-process transport)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mcp import Client

from app.mcp.filesystem_server import build_server

pytestmark = pytest.mark.anyio

FIX = """--- a/authkit/sessions.py
+++ b/authkit/sessions.py
@@ -60,4 +60,5 @@
         new_session = self._new_session(session.user_id)
         self._by_access.pop(session.access_token, None)
         self._by_access[new_session.access_token] = new_session
+        self._by_refresh[new_session.refresh_token] = new_session
         return new_session
"""


def text(result) -> str:
    return "\n".join(block.text for block in result.content)


async def test_discovery_and_annotations(sample_repo: Path) -> None:
    async with Client(build_server(sample_repo)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"search_repository", "read_repository_file", "propose_patch", "apply_patch", "get_git_diff"}
        assert tools["apply_patch"].annotations.read_only_hint is False
        assert tools["apply_patch"].annotations.destructive_hint is False
        for name in ("search_repository", "read_repository_file", "propose_patch", "get_git_diff"):
            assert tools[name].annotations.read_only_hint is True
        assert tools["search_repository"].input_schema["required"] == ["query"]
        assert "directory" in tools["search_repository"].input_schema["properties"]


async def test_search_and_read(sample_repo: Path) -> None:
    async with Client(build_server(sample_repo)) as client:
        found = text(await client.call_tool("search_repository", {"query": "def refresh"}))
        assert "authkit/sessions.py:" in found
        scoped = text(await client.call_tool("search_repository", {"query": "def refresh", "directory": "tests"}))
        assert "No matches" in scoped
        content = text(await client.call_tool("read_repository_file", {"path": "authkit/sessions.py", "start_line": 55, "end_line": 58}))
        assert content.startswith("# authkit/sessions.py (lines 55-58 of")
        assert "def refresh" in content


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", ".env", ".git/config", "keys/id_rsa"])
async def test_unauthorized_reads_are_errors(sample_repo: Path, tmp_path: Path, path: str) -> None:
    (sample_repo / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-should-never-leak\n")
    (tmp_path / "outside.txt").write_text("outside")
    async with Client(build_server(sample_repo)) as client:
        result = await client.call_tool("read_repository_file", {"path": path})
        assert result.is_error
        assert "sk-ant" not in text(result)


async def test_symlink_escape_via_tool(sample_repo: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    os.symlink(secret, sample_repo / "authkit" / "notes.txt")
    async with Client(build_server(sample_repo)) as client:
        result = await client.call_tool("read_repository_file", {"path": "authkit/notes.txt"})
        assert result.is_error and "TOP SECRET" not in text(result)
        search = text(await client.call_tool("search_repository", {"query": "TOP SECRET"}))
        assert "No matches" in search


async def test_invalid_arguments(sample_repo: Path) -> None:
    async with Client(build_server(sample_repo)) as client:
        assert (await client.call_tool("read_repository_file", {})).is_error
        assert (await client.call_tool("search_repository", {"query": ""})).is_error
        assert (await client.call_tool("propose_patch", {"path": "authkit/sessions.py"})).is_error


async def test_propose_does_not_write_and_apply_does(sample_repo: Path) -> None:
    target = sample_repo / "authkit" / "sessions.py"
    before = target.read_text()
    async with Client(build_server(sample_repo)) as client:
        proposed = await client.call_tool("propose_patch", {"path": "authkit/sessions.py", "patch": FIX})
        assert not proposed.is_error, text(proposed)
        data = json.loads(text(proposed))
        assert data["valid"] and data["added"] == 1 and data["removed"] == 0
        assert target.read_text() == before  # nothing written

        applied = await client.call_tool("apply_patch", {"path": "authkit/sessions.py", "patch": FIX})
        assert not applied.is_error, text(applied)
        assert "self._by_refresh[new_session.refresh_token] = new_session" in target.read_text()

        diff = text(await client.call_tool("get_git_diff", {}))
        assert "+        self._by_refresh[new_session.refresh_token] = new_session" in diff

        again = await client.call_tool("apply_patch", {"path": "authkit/sessions.py", "patch": FIX.replace("+        self._by_refresh", "+        self._by_refresh_typo")})
        assert again.is_error  # applying on top of changed content is rejected unless the context still matches


async def test_patch_errors(sample_repo: Path) -> None:
    async with Client(build_server(sample_repo, writable_paths=("tests",))) as client:
        not_writable = await client.call_tool("propose_patch", {"path": "authkit/sessions.py", "patch": FIX})
        assert not_writable.is_error and "writable" in text(not_writable)
        mismatch = await client.call_tool("propose_patch", {"path": "tests/test_sessions.py", "patch": FIX})
        assert mismatch.is_error and "headers refer to" in text(mismatch)
        traversal = await client.call_tool("apply_patch", {"path": "../evil.py", "patch": "--- /dev/null\n+++ b/../evil.py\n@@ -0,0 +1 @@\n+x\n"})
        assert traversal.is_error and "outside" in text(traversal)
    assert not (sample_repo.parent / "evil.py").exists()


async def test_create_new_file_and_diff_includes_it(sample_repo: Path) -> None:
    patch = "--- /dev/null\n+++ b/tests/test_extra.py\n@@ -0,0 +1,2 @@\n+def test_extra():\n+    assert True\n"
    async with Client(build_server(sample_repo)) as client:
        applied = await client.call_tool("apply_patch", {"path": "tests/test_extra.py", "patch": patch})
        assert not applied.is_error, text(applied)
        diff = text(await client.call_tool("get_git_diff", {}))
        assert "+++ b/tests/test_extra.py" in diff
    async with Client(build_server(sample_repo, allow_new_files=False)) as client:
        result = await client.call_tool("propose_patch", {"path": "tests/test_other.py", "patch": patch.replace("test_extra", "test_other")})
        assert result.is_error and "disabled" in text(result)
