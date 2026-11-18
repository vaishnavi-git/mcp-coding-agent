"""Filesystem MCP server: search, read, propose and apply patches, show the git diff.

All access is confined to one repository by PathGuard. ``propose_patch``
validates a patch and returns the resulting diff without touching the disk.
``apply_patch`` is the only tool that writes, and the agent workflow calls it
only after a human approves the exact patch. There is no delete tool.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .git_utils import GitError, is_git_repo, run_git
from .patching import PatchError, apply_to_text, diff_stats, parse_patch, unified_diff
from .security import PathGuard, PathViolation, has_other_names, looks_binary

MAX_READ_CHARS = 200_000
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_FILE_BYTES = 1_000_000
MAX_DIFF_CHARS = 80_000

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

INSTRUCTIONS = """\
Read-only search and file access for the approved repository, plus patch
proposal. Paths are relative to the repository root; protected files (.git,
.env, private keys, credentials) and anything outside the repository are
inaccessible. Patches are single-file unified diffs (---/+++ headers and @@
hunks with 3 lines of context). Use propose_patch to validate a patch; nothing
is written until a human approves it."""


def build_server(repo_path: str | Path, writable_paths: tuple[str, ...] = (), allow_new_files: bool = True) -> MCPServer:
    guard = PathGuard(repo_path, writable_paths, allow_new_files)
    server = MCPServer("filesystem", instructions=INSTRUCTIONS, version="1.0.0")

    def resolve(path: str, **kwargs) -> Path:
        try:
            return guard.resolve(path, **kwargs)
        except PathViolation as exc:
            raise ToolError(str(exc)) from exc

    def read_text(file: Path) -> str:
        data = file.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ToolError(f"{guard.relative(file)} is larger than {MAX_FILE_BYTES} bytes")
        if looks_binary(data):
            raise ToolError(f"{guard.relative(file)} is a binary file")
        return data.decode("utf-8", errors="replace")

    def prepare(path: str, patch: str) -> tuple[Path, str, str, str, bool]:
        """Validate a patch for ``path``; return (file, rel, original, updated, is_new)."""
        try:
            parsed = parse_patch(patch)
        except PatchError as exc:
            raise ToolError(f"invalid patch: {exc}") from exc
        file = resolve(path, for_write=True)
        rel = guard.relative(file)
        for header in (parsed.old_path, parsed.new_path):
            if header is not None and (header[2:] if header.startswith("./") else header) != rel:
                raise ToolError(f"patch headers refer to {header!r} but path is {rel!r}")
        exists = file.exists()
        if exists and not file.is_file():
            raise ToolError(f"{rel} is not a regular file")
        if not exists:
            if not guard.allow_new_files:
                raise ToolError("creating new files is disabled for this repository")
            if not parsed.is_new_file:
                raise ToolError(f"{rel} does not exist; to create it use '--- /dev/null' and '+++ b/{rel}'")
        original = read_text(file) if exists else None
        try:
            updated = apply_to_text(original, parsed, rel)
        except PatchError as exc:
            raise ToolError(str(exc)) from exc
        if original == updated:
            raise ToolError("the patch makes no changes")
        if len(updated.encode()) > MAX_FILE_BYTES:
            raise ToolError("the patched file would be too large")
        return file, rel, original or "", updated, not exists

    @server.tool(annotations=READ)
    def search_repository(query: str, directory: str = ".", max_results: int = 100) -> str:
        """Search file contents in the repository (case-insensitive, plain text).

        Returns one `path:line: text` entry per matching line.

        Args:
            query: Text to search for (1-200 characters).
            directory: Directory to search, relative to the repository root.
            max_results: Maximum number of matching lines to return (1-500).
        """
        if not query or not query.strip() or len(query) > 200:
            raise ToolError("query must be 1-200 characters")
        max_results = max(1, min(max_results, 500))
        start = resolve(directory, must_exist=True)
        needle = re.compile(re.escape(query), re.IGNORECASE)
        hits: list[str] = []
        scanned = 0
        for file in guard.walk_files(start):
            try:
                if file.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                data = file.read_bytes()
            except OSError:
                continue
            if looks_binary(data):
                continue
            scanned += 1
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
                if needle.search(line):
                    hits.append(f"{guard.relative(file)}:{number}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        return "\n".join(hits) + f"\n... stopped at {max_results} matches"
        if not hits:
            return f"No matches for {query!r} in {scanned} files under {guard.relative(start)}."
        return "\n".join(hits)

    @server.tool(annotations=READ)
    def read_repository_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Read a text file from the repository, with line numbers.

        Args:
            path: File path relative to the repository root.
            start_line: First line to return (1-based).
            end_line: Last line to return (inclusive); defaults to the end of the file.
        """
        file = resolve(path, must_exist=True)
        if not file.is_file():
            raise ToolError(f"{guard.relative(file)} is not a file")
        lines = read_text(file).splitlines()
        start = max(1, start_line)
        if start > len(lines) and lines:
            raise ToolError(f"start_line {start_line} is past the end of {guard.relative(file)} ({len(lines)} lines)")
        end = len(lines) if end_line is None else max(start - 1, min(len(lines), end_line))
        out = [f"# {guard.relative(file)} (lines {start}-{end} of {len(lines)})"]
        size = 0
        for number in range(start, end + 1):
            row = f"{number:>5}  {lines[number - 1]}"
            size += len(row)
            if size > MAX_READ_CHARS:
                out.append(f"... truncated; continue with start_line={number}")
                break
            out.append(row)
        return "\n".join(out)

    @server.tool(annotations=READ)
    def propose_patch(path: str, patch: str) -> dict:
        """Validate a single-file unified diff against the current file WITHOUT writing it.

        Returns the normalized diff that would be applied and its size. Use this to
        propose a change; a human must approve it before it is applied.

        Args:
            path: File the patch applies to, relative to the repository root.
            patch: Unified diff for that one file (---/+++ headers, @@ hunks with context).
                   For a new file use '--- /dev/null'.
        """
        _, rel, original, updated, is_new = prepare(path, patch)
        diff = unified_diff(original, updated, rel)
        added, removed = diff_stats(diff)
        return {
            "valid": True,
            "path": rel,
            "is_new_file": is_new,
            "added": added,
            "removed": removed,
            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "diff": diff,
        }

    @server.tool(annotations=WRITE)
    def apply_patch(path: str, patch: str) -> dict:
        """Apply a single-file unified diff to the repository and return the resulting diff.

        Requires human approval. Fails without writing anything if the patch no longer applies.

        Args:
            path: File the patch applies to, relative to the repository root.
            patch: The approved unified diff.
        """
        file, rel, original, updated, is_new = prepare(path, patch)
        file.parent.mkdir(parents=True, exist_ok=True)
        resolve(rel, for_write=True)  # re-check after mkdir (parent could be a new symlink)
        file.write_text(updated, encoding="utf-8")
        diff = unified_diff(original, updated, rel)
        added, removed = diff_stats(diff)
        return {"applied": True, "path": rel, "is_new_file": is_new, "added": added, "removed": removed, "diff": diff}

    @server.tool(annotations=READ)
    def get_git_diff() -> str:
        """Show all uncommitted changes in the repository (modified tracked files and new files) as a unified diff."""
        root = guard.root
        if not is_git_repo(root):
            raise ToolError("the repository is not a git repository, so there is no baseline to diff against")
        try:
            tracked = run_git(root, "diff", "--no-ext-diff", "HEAD", "--")
            untracked = run_git(root, "ls-files", "--others", "--exclude-standard").splitlines()
        except GitError as exc:
            raise ToolError(str(exc)) from exc
        parts = [_hide_protected_changes(tracked, guard)] if tracked.strip() else []
        for rel in untracked:
            if guard.is_forbidden(rel):
                parts.append(f"(new protected file not shown: {rel})\n")
                continue
            if (root / rel).is_symlink():
                # A link's content is its target's: it may be a protected file or lie outside the repository.
                parts.append(f"(new symbolic link not shown: {rel})\n")
                continue
            try:
                file = guard.resolve(rel, must_exist=True)
                parts.append(unified_diff("", read_text(file), rel))
            except PathViolation:
                parts.append(f"(new protected file not shown: {rel})\n")
            except (ToolError, OSError):
                parts.append(f"(new binary or unreadable file: {rel})\n")
        text = "".join(parts).strip()
        if not text:
            return "(no uncommitted changes)"
        if len(text) > MAX_DIFF_CHARS:
            text = text[:MAX_DIFF_CHARS] + "\n... diff truncated"
        return text

    return server


_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def _hide_protected_changes(diff: str, guard: PathGuard) -> str:
    """Drop the part of a ``git diff`` for any protected file, or any file with extra hard links.

    A tracked file can be protected (a committed .env) or be replaced after checkout by a hard
    link to a file elsewhere; either way its new content must not reach the model.
    """
    out: list[str] = []
    hiding = False
    for line in diff.splitlines(keepends=True):
        match = _FILE_HEADER.match(line.rstrip("\n"))
        if match:
            rel = match.group(2)
            hiding = guard.is_forbidden(rel) or has_other_names(guard.root / rel)
            if hiding:
                out.append(f"(changes to protected file not shown: {rel})\n")
                continue
        if not hiding:
            out.append(line)
    return "".join(out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Filesystem MCP server (stdio)")
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--writable", default="", help="Comma-separated writable directory prefixes (empty = whole repo)")
    parser.add_argument("--no-new-files", action="store_true")
    args = parser.parse_args(argv)
    writable = tuple(p for p in args.writable.split(",") if p.strip())
    build_server(args.repo_path, writable, not args.no_new_files).run("stdio")


if __name__ == "__main__":
    main()
