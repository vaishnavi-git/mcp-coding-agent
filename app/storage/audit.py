"""SQLite persistence: run index, tool-call log, LLM-call log and workflow events.

Phase 4 adds per-call cost, retry attempt numbers, a structured failure record
per run, and a usage query that the run budget reads (so limits hold across
restarts). Databases created by earlier phases are migrated in place.

What is stored on purpose:
- tool calls: server, tool, node, redacted arguments, and *metadata* about the
  result (size, line count, hash, first line). Not file contents.
- LLM calls: model, purpose, latency, token usage, stop reason, request id.
  Never prompts, source code or API keys.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.mcp.security import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    issue_number INTEGER NOT NULL,
    repository TEXT,
    model TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    failure TEXT,                   -- JSON failure record (Phase 4)
    instructions TEXT               -- extra guidance given at start (needed to retry from scratch)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    node TEXT,
    initiator TEXT NOT NULL,        -- "llm" or "workflow"
    server TEXT NOT NULL,
    tool TEXT NOT NULL,
    risk TEXT,
    arguments TEXT,                 -- redacted JSON, long values truncated
    is_error INTEGER NOT NULL,
    duration_ms INTEGER,
    result_chars INTEGER,
    result_lines INTEGER,
    result_sha256 TEXT,
    result_head TEXT,               -- first line only, redacted
    attempt INTEGER                 -- 1 for the first try, 2+ for automatic retries (Phase 4)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    ts TEXT NOT NULL,
    purpose TEXT,
    model TEXT,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    stop_reason TEXT,
    request_id TEXT,
    error TEXT,
    cost_usd REAL                   -- tokens x configured price; NULL if unknown or failed (Phase 4)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,             -- approval_requested, approval_decided, status, ...
    detail TEXT
);
CREATE INDEX IF NOT EXISTS tool_calls_run ON tool_calls(run_id, id);
CREATE INDEX IF NOT EXISTS llm_calls_run ON llm_calls(run_id, id);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
"""

# Columns added after Phase 1; added to existing databases on open.
MIGRATIONS = {
    "runs": {"failure": "TEXT", "instructions": "TEXT"},
    "tool_calls": {"attempt": "INTEGER"},
    "llm_calls": {"cost_usd": "REAL"},
}

_ARG_VALUE_LIMIT = 500


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _clip_args(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clip_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_args(v) for v in value]
    if isinstance(value, str) and len(value) > _ARG_VALUE_LIMIT:
        return value[:_ARG_VALUE_LIMIT] + f"... [{len(value)} chars, sha256 {hashlib.sha256(value.encode()).hexdigest()[:12]}]"
    return value


class AuditStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for column, kind in columns.items():
                    if column not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _exec(self, sql: str, params: tuple) -> None:
        with self._lock:
            self.db.execute(sql, params)
            self.db.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    # ------------------------------------------------------------- runs
    def create_run(self, issue_number: int, repository: str, model: str, instructions: str = "") -> str:
        run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        now = _now()
        self._exec(
            "INSERT INTO runs (id, issue_number, repository, model, status, created_at, updated_at, instructions) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, issue_number, repository, model, "queued", now, now, redact(instructions) if instructions else ""),
        )
        return run_id

    def update_run(self, run_id: str, status: str, error: str | None = None, failure: dict[str, Any] | None = None) -> None:
        self._exec(
            "UPDATE runs SET status = ?, error = ?, failure = ?, updated_at = ? WHERE id = ?",
            (status, redact(error) if error else None, json.dumps(redact(failure)) if failure else None, _now(), run_id),
        )

    @staticmethod
    def _run_row(row: dict) -> dict:
        row["failure"] = json.loads(row["failure"]) if row.get("failure") else None
        return row

    def get_run(self, run_id: str) -> dict | None:
        rows = self._query("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run_row(rows[0]) if rows else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        return [self._run_row(r) for r in self._query("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def all_runs(self) -> list[dict]:
        """Every run, oldest first (evaluation)."""
        return [self._run_row(r) for r in self._query("SELECT * FROM runs ORDER BY created_at", ())]

    def runs_with_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" for _ in statuses)
        return [self._run_row(r) for r in self._query(f"SELECT * FROM runs WHERE status IN ({marks}) ORDER BY created_at", tuple(statuses))]

    # ------------------------------------------------------------- logs
    def log_tool_call(
        self,
        run_id: str,
        *,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        result_text: str,
        is_error: bool,
        duration_ms: int,
        node: str | None,
        initiator: str,
        risk: str | None,
        attempt: int = 1,
    ) -> None:
        head = result_text.strip().splitlines()[0][:200] if result_text.strip() else ""
        self._exec(
            """INSERT INTO tool_calls (run_id, ts, node, initiator, server, tool, risk, arguments, is_error, duration_ms,
                                       result_chars, result_lines, result_sha256, result_head, attempt)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, _now(), node, initiator, server, tool, risk,
                json.dumps(redact(_clip_args(arguments)), default=str),
                int(is_error), duration_ms, len(result_text), result_text.count("\n") + 1 if result_text else 0,
                hashlib.sha256(result_text.encode()).hexdigest()[:16], redact(head), attempt,
            ),
        )

    def log_llm_call(self, run_id: str | None, meta: dict[str, Any]) -> None:
        self._exec(
            """INSERT INTO llm_calls (run_id, ts, purpose, model, latency_ms, input_tokens, output_tokens,
                                      cache_read_tokens, cache_write_tokens, stop_reason, request_id, error, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, _now(), meta.get("purpose"), meta.get("model"), meta.get("latency_ms"),
                meta.get("input_tokens"), meta.get("output_tokens"), meta.get("cache_read_tokens"),
                meta.get("cache_write_tokens"), meta.get("stop_reason"), meta.get("request_id"),
                redact(meta.get("error")) if meta.get("error") else None, meta.get("cost_usd"),
            ),
        )

    def log_tool_rejection(self, run_id: str, *, tool: str, node: str | None, reason: str, arguments: Any, initiator: str = "llm") -> None:
        """A requested tool call that was refused before reaching any server (``tool_rejected`` event)."""
        detail = {"tool": tool, "node": node, "initiator": initiator, "reason": reason[:500], "arguments": _clip_args(arguments)}
        self.log_event(run_id, "tool_rejected", detail)

    def log_event(self, run_id: str, kind: str, detail: dict[str, Any] | None = None) -> None:
        self._exec("INSERT INTO events (run_id, ts, kind, detail) VALUES (?,?,?,?)", (run_id, _now(), kind, json.dumps(redact(detail or {}), default=str)))

    def usage(self, run_id: str) -> dict[str, Any]:
        """Totals the run budget is checked against (see app.reliability.budget.Usage)."""
        llm = self._query(
            """SELECT COALESCE(SUM(CASE WHEN error IS NULL THEN 1 ELSE 0 END), 0) AS llm_calls,
                      COALESCE(SUM(CASE WHEN error IS NULL THEN 0 ELSE 1 END), 0) AS failed_llm_calls,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                      COALESCE(SUM(cost_usd), 0.0) AS cost_usd
               FROM llm_calls WHERE run_id = ?""",
            (run_id,),
        )[0]
        tools = self._query("SELECT COUNT(*) AS tool_calls FROM tool_calls WHERE run_id = ?", (run_id,))[0]
        return {**llm, **tools}

    def count_events(self, run_id: str, kind: str) -> int:
        return self._query("SELECT COUNT(*) AS n FROM events WHERE run_id = ? AND kind = ?", (run_id, kind))[0]["n"]

    def tool_calls(self, run_id: str) -> list[dict]:
        return self._query("SELECT * FROM tool_calls WHERE run_id = ? ORDER BY id", (run_id,))

    def llm_calls(self, run_id: str) -> list[dict]:
        return self._query("SELECT * FROM llm_calls WHERE run_id = ? ORDER BY id", (run_id,))

    def events(self, run_id: str) -> list[dict]:
        return self._query("SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,))
