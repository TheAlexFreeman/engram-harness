"""SQLite-backed session index for the harness API server.

SQLite is a derived read-optimized index — not the source of truth.
The JSONL trace files are authoritative. The database can always be
rebuilt from traces via backfill_from_traces().

All writes are synchronous (called from background threads after
session completion). Concurrent reads are fine; SQLite WAL mode
allows readers while a writer is active.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass
class SessionRecord:
    session_id: str
    task: str
    status: str
    model: str | None = None
    mode: str | None = None
    memory_backend: str | None = None
    workspace: str | None = None
    created_at: str = ""
    ended_at: str | None = None
    turns_used: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_cost_usd: float | None = None
    tool_counts: dict[str, int] | None = None
    error_count: int = 0
    final_text: str | None = None
    max_turns_reached: bool = False
    trace_path: str | None = None
    engram_session_dir: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "session_id": self.session_id,
            "task": self.task,
            "status": self.status,
            "model": self.model,
            "mode": self.mode,
            "memory_backend": self.memory_backend,
            "workspace": self.workspace,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "turns_used": self.turns_used,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_cost_usd": self.total_cost_usd,
            "tool_counts": json.dumps(self.tool_counts) if self.tool_counts else None,
            "error_count": self.error_count,
            "final_text": self.final_text,
            "max_turns_reached": 1 if self.max_turns_reached else 0,
            "trace_path": self.trace_path,
            "engram_session_dir": self.engram_session_dir,
        }
        return d

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SessionRecord:
        tool_counts = None
        if row.get("tool_counts"):
            try:
                tool_counts = json.loads(row["tool_counts"])
            except (json.JSONDecodeError, TypeError):
                pass
        return cls(
            session_id=row["session_id"],
            task=row["task"],
            status=row["status"],
            model=row.get("model"),
            mode=row.get("mode"),
            memory_backend=row.get("memory_backend"),
            workspace=row.get("workspace"),
            created_at=row.get("created_at", ""),
            ended_at=row.get("ended_at"),
            turns_used=row.get("turns_used"),
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cache_read_tokens=row.get("cache_read_tokens"),
            cache_write_tokens=row.get("cache_write_tokens"),
            reasoning_tokens=row.get("reasoning_tokens"),
            total_cost_usd=row.get("total_cost_usd"),
            tool_counts=tool_counts,
            error_count=row.get("error_count", 0),
            final_text=row.get("final_text"),
            max_turns_reached=bool(row.get("max_turns_reached", 0)),
            trace_path=row.get("trace_path"),
            engram_session_dir=row.get("engram_session_dir"),
        )


class SessionStore:
    """SQLite-backed session index.

    Thread-safe for concurrent reads. Writes should only happen from
    one thread at a time per session (the session's background thread).
    Uses WAL journal mode for concurrent read/write.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._write_lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.executescript(schema)
        self._conn.commit()

    def insert_session(self, record: SessionRecord) -> None:
        d = record.as_dict()
        cols = ", ".join(d.keys())
        placeholders = ", ".join(f":{k}" for k in d)
        with self._write_lock:
            self._conn.execute(
                f"INSERT OR IGNORE INTO sessions ({cols}) VALUES ({placeholders})", d
            )
            # Keep FTS index in sync
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions_fts(session_id, task, final_text) VALUES (?, ?, ?)",
                (record.session_id, record.task, record.final_text),
            )
            self._conn.commit()

    def complete_session(
        self,
        session_id: str,
        *,
        status: str,
        ended_at: str,
        turns_used: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        total_cost_usd: float | None = None,
        tool_counts: dict[str, int] | None = None,
        error_count: int = 0,
        final_text: str | None = None,
        max_turns_reached: bool = False,
        engram_session_dir: str | None = None,
    ) -> None:
        tool_counts_json = json.dumps(tool_counts) if tool_counts else None
        params = {
            "session_id": session_id,
            "status": status,
            "ended_at": ended_at,
            "turns_used": turns_used,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_cost_usd": total_cost_usd,
            "tool_counts": tool_counts_json,
            "error_count": error_count,
            "final_text": final_text,
            "max_turns_reached": 1 if max_turns_reached else 0,
            "engram_session_dir": engram_session_dir,
        }
        with self._write_lock:
            self._conn.execute(
                """
                UPDATE sessions SET
                    status = :status,
                    ended_at = :ended_at,
                    turns_used = :turns_used,
                    input_tokens = :input_tokens,
                    output_tokens = :output_tokens,
                    cache_read_tokens = :cache_read_tokens,
                    cache_write_tokens = :cache_write_tokens,
                    reasoning_tokens = :reasoning_tokens,
                    total_cost_usd = :total_cost_usd,
                    tool_counts = :tool_counts,
                    error_count = :error_count,
                    final_text = :final_text,
                    max_turns_reached = :max_turns_reached,
                    engram_session_dir = :engram_session_dir
                WHERE session_id = :session_id
                """,
                params,
            )
            # Update FTS index for changed final_text
            if final_text is not None:
                self._conn.execute(
                    "UPDATE sessions_fts SET final_text = ? WHERE session_id = ?",
                    (final_text, session_id),
                )
            self._conn.commit()

    def get_session(self, session_id: str) -> SessionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return SessionRecord.from_row(dict(row))

    def list_sessions(
        self,
        *,
        workspace: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionRecord]:
        if search:
            # Use FTS for keyword search (all named params to avoid mixing)
            rows = self._conn.execute(
                """
                SELECT s.* FROM sessions s
                JOIN sessions_fts fts ON s.session_id = fts.session_id
                WHERE sessions_fts MATCH :search
                  AND (:workspace IS NULL OR s.workspace = :workspace)
                  AND (:status IS NULL OR s.status = :status)
                ORDER BY s.created_at DESC
                LIMIT :limit OFFSET :offset
                """,
                {
                    "search": search,
                    "workspace": workspace,
                    "status": status,
                    "limit": limit,
                    "offset": offset,
                },
            ).fetchall()
        else:
            clauses = []
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if workspace:
                clauses.append("workspace = :workspace")
                params["workspace"] = workspace
            if status:
                clauses.append("status = :status")
                params["status"] = status
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = self._conn.execute(
                f"SELECT * FROM sessions {where} ORDER BY created_at DESC "
                f"LIMIT :limit OFFSET :offset",
                params,
            ).fetchall()
        return [SessionRecord.from_row(dict(r)) for r in rows]

    def stats(self, *, workspace: str | None = None) -> dict[str, Any]:
        where = "WHERE workspace = ?" if workspace else ""
        params = (workspace,) if workspace else ()
        row = self._conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_sessions,
                COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd,
                COALESCE(AVG(turns_used), 0) AS avg_turns
            FROM sessions {where}
            """,
            params,
        ).fetchone()
        return {
            "total_sessions": row["total_sessions"],
            "total_cost_usd": row["total_cost_usd"],
            "avg_turns": row["avg_turns"],
        }

    def backfill_from_traces(self, trace_dir: Path) -> int:
        """Index any JSONL trace files not already in the database.

        Returns the count of new sessions indexed.
        """
        count = 0
        for trace_path in trace_dir.glob("**/*.jsonl"):
            try:
                record = _parse_trace_for_backfill(trace_path)
            except Exception:
                continue
            if record is None:
                continue
            existing = self.get_session(record.session_id)
            if existing is not None:
                continue
            self.insert_session(record)
            if record.status != "running":
                self.complete_session(
                    record.session_id,
                    status=record.status,
                    ended_at=record.ended_at or record.created_at,
                    turns_used=record.turns_used,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cache_read_tokens=record.cache_read_tokens,
                    cache_write_tokens=record.cache_write_tokens,
                    reasoning_tokens=record.reasoning_tokens,
                    total_cost_usd=record.total_cost_usd,
                    tool_counts=record.tool_counts,
                    error_count=record.error_count,
                    final_text=record.final_text,
                    max_turns_reached=record.max_turns_reached,
                )
            count += 1
        return count

    def close(self) -> None:
        self._conn.close()


def _parse_trace_for_backfill(trace_path: Path) -> SessionRecord | None:
    """Extract session metadata from a JSONL trace file."""
    session_start: dict | None = None
    session_usage: dict | None = None
    session_end: dict | None = None
    tool_counts: dict[str, int] = defaultdict(int)
    error_count = 0

    try:
        for line in trace_path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            kind = ev.get("kind")
            if kind == "session_start":
                session_start = ev
            elif kind == "session_usage":
                session_usage = ev
            elif kind == "session_end":
                session_end = ev
            elif kind == "tool_call":
                tool_counts[ev.get("name", "unknown")] += 1
            elif kind == "tool_result" and ev.get("is_error"):
                error_count += 1
    except (json.JSONDecodeError, OSError):
        return None

    if not session_start:
        return None

    # Derive session_id from trace filename (e.g. ACTIONS.native.jsonl → use parent dir name)
    parent_name = trace_path.parent.name
    if parent_name.startswith("act-") or parent_name.startswith("chat-"):
        session_id = parent_name
    else:
        # Fallback: use stem of trace filename
        session_id = trace_path.stem

    status = "completed"
    if session_end:
        reason = session_end.get("reason")
        if reason == "max_turns":
            status = "completed"
        elif reason == "stopped":
            status = "stopped"
    elif session_usage:
        status = "completed"
    else:
        status = "unknown"

    u = session_usage or {}

    return SessionRecord(
        session_id=session_id,
        task=session_start.get("task", ""),
        status=status,
        created_at=session_start.get("ts", ""),
        ended_at=session_end.get("ts") if session_end else None,
        turns_used=session_end.get("turns") if session_end else None,
        input_tokens=u.get("input_tokens"),
        output_tokens=u.get("output_tokens"),
        cache_read_tokens=u.get("cache_read_tokens"),
        cache_write_tokens=u.get("cache_write_tokens"),
        reasoning_tokens=u.get("reasoning_tokens"),
        total_cost_usd=u.get("total_cost_usd"),
        tool_counts=dict(tool_counts) if tool_counts else None,
        error_count=error_count,
        final_text=None,  # not stored in trace events
        max_turns_reached=(session_end.get("reason") == "max_turns") if session_end else False,
        trace_path=str(trace_path),
    )
