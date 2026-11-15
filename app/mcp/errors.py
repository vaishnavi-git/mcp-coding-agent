"""How MCP servers mark temporary errors (Phase 4).

A tool error whose text contains ``[temporary]`` (optionally
``[temporary, retry after 30s]``) failed for a reason that may go away by
itself: a timeout, a network error, a rate limit, a 5xx response. The hub
retries such errors with backoff, but only for tools that are read-only or
idempotent according to their MCP annotations. The marker is plain text, so
the model and the audit log see it too.
"""

from __future__ import annotations

import re

from mcp.server.mcpserver.exceptions import ToolError

TEMPORARY = re.compile(r"\[temporary(?:, retry after (\d+(?:\.\d+)?)s)?\]")


def temporary_error(message: str, retry_after: float | None = None) -> ToolError:
    mark = f"[temporary, retry after {retry_after:g}s]" if retry_after is not None else "[temporary]"
    return ToolError(f"{mark} {message}")


def parse_temporary(text: str) -> tuple[bool, float | None]:
    """(is temporary, retry-after seconds) from the first line of a tool error."""
    match = TEMPORARY.search(text.split("\n", 1)[0])
    if not match:
        return False, None
    return True, float(match.group(1)) if match.group(1) else None
