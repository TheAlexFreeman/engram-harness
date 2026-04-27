-- Session index schema for the harness API server.
-- SQLite is a derived read-optimized index; JSONL files are the source of truth.
-- Schema changes: drop and re-index from JSONL traces (no migration system needed).

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
    turns_used           INTEGER,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cache_read_tokens    INTEGER,
    cache_write_tokens   INTEGER,
    reasoning_tokens     INTEGER,
    total_cost_usd       REAL,

    -- Tool usage summary (JSON blob: {"read_file": 5, "edit_file": 2, ...})
    tool_counts    TEXT,
    error_count    INTEGER DEFAULT 0,

    -- Result
    final_text         TEXT,
    max_turns_reached  INTEGER DEFAULT 0,  -- SQLite boolean as 0/1

    -- File references
    trace_path          TEXT,   -- absolute path to JSONL trace file
    engram_session_dir  TEXT,   -- path to engram activity dir, if applicable
    bridge_status       TEXT,   -- skipped|ok|error
    bridge_error        TEXT,

    -- Workspace plan link, populated at session-end from the most-recently
    -- modified active plan in the agent's workspace. Together they form an
    -- index that lets the harness answer "which session(s) advanced plan X"
    -- without re-scanning workspace state. The matching index is created in
    -- SessionStore._ensure_indexes() so older DBs ALTER first, then index.
    active_plan_project TEXT,
    active_plan_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_created   ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_status    ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace);

-- Full-text search on task descriptions and final responses.
-- Stored independently (not a content table) for simplicity and reliability.
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id UNINDEXED,
    task,
    final_text
);
