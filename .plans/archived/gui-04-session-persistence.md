---
title: "Build Plan: Session Persistence & History"
created: 2026-04-20
source: agent-generated
trust: medium
priority: 4
effort: medium
depends_on: ["gui-03-api-server-core.md"]
context: "The API server (plan 03) stores sessions in memory — lost on restart. This plan adds a lightweight SQLite index over the existing JSONL trace files so the frontend can browse, search, and replay historical sessions without re-parsing traces on every request."
---

# Build Plan: Session Persistence & History

## Goal

Add a SQLite database that indexes session metadata from JSONL trace files,
enabling the frontend to list historical sessions, filter by task/date/cost,
and load session details — even across server restarts. The JSONL files remain
the source of truth; SQLite is a derived read-optimized index.

---

## Why SQLite, not Postgres

This is a single-user local tool. The harness runs on the developer's machine.
SQLite is zero-config, ships with Python, and stores everything in one file
alongside the traces. Adding Postgres would violate the project's "keep things
simple" principle and the ROADMAP's graceful degradation design (§10). If
SQLite is missing or corrupt, the server falls back to scanning JSONL files
directly (slower but functional).

---

## Schema

```sql
-- harness/schema.sql

CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    task           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'running',  -- running|completed|error|stopped
    model          TEXT,
    mode           TEXT,
    memory_backend TEXT,
    workspace      TEXT,

    -- Timing
    created_at     TEXT NOT NULL,   -- ISO 8601
    ended_at       TEXT,

    -- Aggregates (populated at session end)
    turns_used       INTEGER,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens   INTEGER,
    total_cost_usd   REAL,

    -- Tool usage summary (JSON blob: {"read_file": 5, "edit_file": 2, ...})
    tool_counts    TEXT,
    error_count    INTEGER DEFAULT 0,

    -- Result
    final_text     TEXT,
    max_turns_reached BOOLEAN DEFAULT FALSE,

    -- File references
    trace_path     TEXT,  -- absolute path to JSONL trace file
    engram_session_dir TEXT  -- path to engram activity dir, if applicable
);

CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace);

-- Full-text search on task descriptions
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id,
    task,
    final_text,
    content='sessions',
    content_rowid='rowid'
);

-- Trigger to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(session_id, task, final_text)
    VALUES (new.session_id, new.task, new.final_text);
END;

CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
    DELETE FROM sessions_fts WHERE session_id = old.session_id;
    INSERT INTO sessions_fts(session_id, task, final_text)
    VALUES (new.session_id, new.task, new.final_text);
END;
```

---

## `SessionStore` class

```python
# harness/session_store.py

class SessionStore:
    """SQLite-backed session index.

    All writes are synchronous (called from background threads after
    session completion). Reads are synchronous too — SQLite handles
    concurrent readers fine, and queries are fast enough for a local tool.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")  # concurrent read/write
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        ...

    def insert_session(self, session: SessionRecord) -> None:
        """Insert a new session row (status='running')."""
        ...

    def complete_session(self, session_id: str, result: RunResult, ...) -> None:
        """Update a session with final results."""
        ...

    def get_session(self, session_id: str) -> SessionRecord | None:
        """Fetch a single session by ID."""
        ...

    def list_sessions(
        self,
        *,
        workspace: str | None = None,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SessionRecord]:
        """List sessions with optional filters. Uses FTS for search."""
        ...

    def stats(self, *, workspace: str | None = None) -> dict:
        """Aggregate stats: total sessions, total cost, avg turns, etc."""
        ...

    def backfill_from_traces(self, trace_dir: Path) -> int:
        """Scan a directory for JSONL trace files and index any sessions
        not already in the database. Returns count of new sessions indexed.
        Used on server startup to pick up sessions from CLI runs."""
        ...
```

### `SessionRecord` dataclass

```python
@dataclass
class SessionRecord:
    session_id: str
    task: str
    status: str
    model: str | None
    mode: str | None
    memory_backend: str | None
    workspace: str | None
    created_at: str
    ended_at: str | None = None
    turns_used: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    tool_counts: dict[str, int] | None = None
    error_count: int = 0
    final_text: str | None = None
    max_turns_reached: bool = False
    trace_path: str | None = None
    engram_session_dir: str | None = None
```

---

## Integration with the API server

### On session create

```python
# In server.py create_session()
store.insert_session(SessionRecord(
    session_id=session_id,
    task=req.task,
    status="running",
    model=config.model,
    mode=config.mode,
    memory_backend=config.memory_backend,
    workspace=str(config.workspace),
    created_at=datetime.now().isoformat(timespec="milliseconds"),
    trace_path=str(components.trace_path),
))
```

### On session complete

```python
# In _run_session(), after run() returns
store.complete_session(
    session_id=session.id,
    result=result,
    tool_counts=session.tool_call_counts,
    error_count=session.error_count,
    engram_session_dir=...,
)
```

### Updated endpoints

**`GET /sessions`** now queries SQLite instead of the in-memory dict:

```python
@app.get("/sessions")
async def list_sessions(
    workspace: str | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    return store.list_sessions(
        workspace=workspace,
        status=status,
        search=search,
        limit=limit,
        offset=offset,
    )
```

**`GET /sessions/{id}`** checks in-memory first (for live sessions with
real-time state), falls back to SQLite (for historical sessions).

**`GET /sessions/stats`** (new):

```json
{
  "total_sessions": 142,
  "total_cost_usd": 12.47,
  "avg_turns": 6.3,
  "sessions_today": 5,
  "top_tools": {"read_file": 312, "edit_file": 89, "bash": 67}
}
```

---

## Backfill on startup

When the server starts, it calls `store.backfill_from_traces(trace_dir)` to
index any sessions created by the CLI (which doesn't write to SQLite). The
backfill parser reads the JSONL file, extracts `session_start`, `session_usage`,
and `session_end` events, and inserts a row. This means the GUI shows all
historical sessions, not just API-created ones.

```python
def _parse_trace_for_backfill(trace_path: Path) -> SessionRecord | None:
    """Extract session metadata from a JSONL trace file."""
    session_start = None
    session_usage = None
    session_end = None
    tool_counts: dict[str, int] = defaultdict(int)
    error_count = 0

    for line in trace_path.open():
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

    if not session_start:
        return None  # Not a valid session trace

    return SessionRecord(
        session_id=...,   # derive from trace filename or session_start data
        task=session_start.get("task", ""),
        status="completed" if session_end else "unknown",
        ...
    )
```

---

## Database location

Default: `{workspace}/.harness/sessions.db`

Configurable via `--db` flag on `harness serve` or `HARNESS_DB_PATH` env var.

If `--memory=engram`, also index Engram activity records: scan
`engram/core/memory/activity/` for session records and cross-reference with
trace files. This gives the GUI a unified view of all session history.

---

## File layout

```
harness/session_store.py          # SessionStore, SessionRecord
harness/schema.sql                # Schema definition (read by SessionStore)
harness/tests/test_session_store.py
```

---

## Tests

1. **test_insert_and_get** — Insert a session, retrieve by ID, verify fields.
2. **test_complete_session** — Insert running, complete with result, verify
   status/usage/tool_counts updated.
3. **test_list_sessions_filters** — Insert 5 sessions with different workspaces
   and statuses, verify each filter works.
4. **test_fts_search** — Insert sessions with different tasks, search by keyword,
   verify ranking.
5. **test_backfill_from_trace** — Write a minimal JSONL trace file, call
   `backfill_from_traces`, verify session appears in the database.
6. **test_backfill_idempotent** — Backfill twice, verify no duplicate rows.
7. **test_stats** — Insert sessions with known costs, verify aggregates.
8. **test_missing_db** — SessionStore creates the database file if it doesn't
   exist.

All tests use `tmp_path` for the database file.

---

## Implementation order

1. Create `harness/session_store.py` with schema and `SessionStore`.
2. Implement `insert_session`, `complete_session`, `get_session`, `list_sessions`.
3. Implement `backfill_from_traces` with the JSONL parser.
4. Wire into `server.py`: store creation on startup, insert/complete calls.
5. Update `GET /sessions` and `GET /sessions/{id}` endpoints.
6. Add `GET /sessions/stats`.
7. Add startup backfill call.
8. Write tests.

---

## Scope cuts

- No migration system in v1. Schema changes are handled by "drop and reindex"
  — the SQLite DB is a derived index, not a source of truth. Backfill
  rebuilds it from JSONL traces.
- No per-turn storage in SQLite. The JSONL trace file has full per-turn detail.
  SQLite stores only session-level aggregates. A future "trace viewer" feature
  would read the JSONL directly (or add a `turns` table).
- No Engram activity record cross-referencing in v1. Just trace file backfill.
- No real-time dashboard queries (e.g., cost over time charts). The `stats`
  endpoint returns simple aggregates. Complex analytics are a frontend concern
  using the raw data from `list_sessions`.
