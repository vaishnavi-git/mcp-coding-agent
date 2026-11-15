"""Parse and apply single-file unified diffs.

Model-written diffs often have wrong line numbers or hunk counts, so this
applier treats ``@@`` line numbers as hints only. It does require the
context and removed lines to match the file (exactly, or ignoring trailing
whitespace), so a patch can never land somewhere it was not meant to.

Only the changed lines change: context lines keep the file's own text
(including trailing whitespace the model may have dropped), every line keeps
its own line ending, and added lines use the file's usual ending (CRLF files
stay CRLF).
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(ValueError):
    pass


@dataclass
class Hunk:
    old_start: int
    new_start: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (" " | "-" | "+", text)

    @property
    def old_block(self) -> list[str]:
        return [text for tag, text in self.lines if tag in " -"]

    @property
    def new_block(self) -> list[str]:
        return [text for tag, text in self.lines if tag in " +"]


@dataclass
class FilePatch:
    old_path: str | None  # None means /dev/null (new file)
    new_path: str | None
    hunks: list[Hunk]
    has_headers: bool

    @property
    def is_new_file(self) -> bool:
        return self.has_headers and self.old_path is None


def _header_path(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_patch(text: str) -> FilePatch:
    if not text or not text.strip():
        raise PatchError("patch is empty")
    lines = text.replace("\r\n", "\n").split("\n")
    old_path = new_path = None
    has_headers = False
    hunks: list[Hunk] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git"):
            if has_headers or hunks:
                raise PatchError("patch touches more than one file; send one patch per file")
            i += 1
            continue
        if line.startswith(("deleted file mode", "rename from", "rename to", "Binary files")):
            raise PatchError("deleting, renaming or binary changes are not supported")
        if line.startswith(("index ", "new file mode", "similarity index", "old mode", "new mode")):
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if has_headers or hunks:
                raise PatchError("patch touches more than one file; send one patch per file")
            old_path, new_path = _header_path(line[4:]), _header_path(lines[i + 1][4:])
            if new_path is None:
                raise PatchError("deleting files is not supported")
            has_headers = True
            i += 2
            continue
        match = _HUNK_RE.match(line)
        if match:
            hunk = Hunk(old_start=int(match.group(1)), new_start=int(match.group(3)))
            i += 1
            # Read until the next hunk/file header; hunk counts are not trusted.
            while i < len(lines) and not _HUNK_RE.match(lines[i]) and not lines[i].startswith("diff --git") and not (
                lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ ")
            ):
                body = lines[i]
                if body.startswith("\\"):  # "\ No newline at end of file"
                    i += 1
                    continue
                if body == "":
                    hunk.lines.append((" ", ""))  # context line whose leading space was stripped
                elif body[0] in " -+":
                    hunk.lines.append((body[0], body[1:]))
                else:
                    raise PatchError(f"invalid line in hunk {len(hunks) + 1}: {body[:80]!r} (lines must start with ' ', '-' or '+')")
                i += 1
            while hunk.lines and hunk.lines[-1] == (" ", ""):
                hunk.lines.pop()  # trailing blank lines are separators, not context
            if not any(tag in "-+" for tag, _ in hunk.lines):
                raise PatchError(f"hunk {len(hunks) + 1} has no changes")
            hunks.append(hunk)
            continue
        if not hunks and not has_headers:
            i += 1  # tolerate a short preamble before the diff
            continue
        if line.strip() == "":
            i += 1
            continue
        raise PatchError(f"unexpected line outside a hunk: {line[:80]!r}")
    if not hunks:
        raise PatchError("no hunks found; the patch must be a unified diff with @@ hunk headers")
    return FilePatch(old_path, new_path, hunks, has_headers)


def _find_block(lines: list[str], block: list[str], hint: int, start: int, loose: bool) -> int | None:
    n = len(block)
    if n == 0:
        return min(max(hint, start), len(lines))
    norm = (lambda s: s.rstrip()) if loose else (lambda s: s)
    target = [norm(s) for s in block]
    best: int | None = None
    for pos in range(start, len(lines) - n + 1):
        if [norm(s) for s in lines[pos : pos + n]] == target and (best is None or abs(pos - hint) < abs(best - hint)):
            best = pos
    return best


def apply_to_text(original: str | None, patch: FilePatch, path: str) -> str:
    """Return the patched text, or raise PatchError explaining which hunk failed."""
    if patch.is_new_file or original is None:
        if original is not None:
            raise PatchError(f"{path} already exists; diff against the current content instead of /dev/null")
        if any(tag != "+" for hunk in patch.hunks for tag, _ in hunk.lines):
            raise PatchError("a new-file patch may only contain '+' lines")
        body = [text for hunk in patch.hunks for _, text in hunk.lines]
        return "\n".join(body) + "\n"

    had_trailing_newline = original.endswith("\n")
    raw = original.split("\n")
    if had_trailing_newline:
        raw = raw[:-1]
    # Match on text without "\r"; remember each line's own ending so it can be restored.
    lines = [s[:-1] if s.endswith("\r") else s for s in raw]
    eols = ["\r\n" if s.endswith("\r") else "\n" for s in raw]
    if raw and not had_trailing_newline:
        eols[-1] = "\r" if raw[-1].endswith("\r") else ""
    terminated = [e for e in eols if e in ("\r\n", "\n")]
    usual_eol = "\r\n" if terminated.count("\r\n") * 2 > len(terminated) else "\n"  # for added lines
    offset = 0
    cursor = 0
    for number, hunk in enumerate(patch.hunks, start=1):
        old = hunk.old_block
        hint = max(0, hunk.old_start - 1 + offset)
        pos = _find_block(lines, old, hint, cursor, loose=False)
        if pos is None:
            pos = _find_block(lines, old, hint, cursor, loose=True)
        if pos is None:
            first = next((s for s in old if s.strip()), old[0] if old else "")
            raise PatchError(
                f"hunk {number} does not apply to {path}: its context/removed lines (starting {first.strip()[:60]!r}) "
                f"were not found near line {hunk.old_start}. Re-read the file and regenerate the patch."
            )
        new_lines: list[str] = []
        new_eols: list[str] = []
        k = pos
        for tag, text in hunk.lines:
            if tag == " ":  # keep the file's own context line, not the patch's copy of it
                new_lines.append(lines[k])
                new_eols.append(eols[k])
                k += 1
            elif tag == "-":
                k += 1
            else:
                new_lines.append(text)
                new_eols.append(usual_eol)
        lines[pos : pos + len(old)] = new_lines
        eols[pos : pos + len(old)] = new_eols
        offset += len(new_lines) - len(old)
        cursor = pos + len(new_lines)
    out = []
    for i, (line, eol) in enumerate(zip(lines, eols, strict=True)):
        last = i == len(lines) - 1
        if last and not had_trailing_newline:
            eol = eol if eol in ("", "\r") else ""  # the file still ends without a newline
        elif eol in ("", "\r"):  # the old unterminated last line is no longer last
            eol = usual_eol if eol == "" else "\r\n"
        out.append(line + eol)
    return "".join(out)


def unified_diff(old: str, new: str, path: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}" if old else "/dev/null",
        tofile=f"b/{path}",
    )
    out = []
    for line in diff:
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def diff_stats(diff: str) -> tuple[int, int]:
    added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return added, removed
