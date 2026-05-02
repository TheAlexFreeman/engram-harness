"""Tests for harness/session_store.py — SessionStore and SessionRecord."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.session_store import SessionRecord, SessionStore, _parse_trace_for_backfill


def _make_record(session_id: str = "ses_001", **kwargs) -> SessionRecord:
    defaults = dict(
        session_id=session_id,
        task="Test task",
        status="running",
        model="claude-sonnet-4-6",
        mode="native",
        memory_backend="file",
        workspace="/tmp/workspace",
        created_at="2026-04-21T00:00:00.000",
    )
    defaults.update(kwargs)
    return SessionRecord(**defaults)


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


# ---------------------------------------------------------------------------
# Insert and get
# ---------------------------------------------------------------------------


def test_insert_and_get(store):
    rec = _make_record()
    store.insert_session(rec)
    fetched = store.get_session("ses_001")
    assert fetched is not None
    assert fetched.session_id == "ses_001"
    assert fetched.task == "Test task"
    assert fetched.status == "running"


def test_get_nonexistent(store):
    assert store.get_session("nonexistent") is None


def test_insert_idempotent(store):
    """INSERT OR IGNORE — inserting twice does not raise and does not duplicate."""
    rec = _make_record()
    store.insert_session(rec)
    store.insert_session(rec)  # should be ignored
    results = store.list_sessions()
    assert len(results) == 1


# ---------------------------------------------------------------------------
# complete_session
# ---------------------------------------------------------------------------


def test_complete_session(store):
    rec = _make_record()
    store.insert_session(rec)
    store.complete_session(
        "ses_001",
        status="completed",
        ended_at="2026-04-21T00:05:00.000",
        turns_used=8,
        input_tokens=1000,
        output_tokens=500,
        total_cost_usd=0.05,
        tool_counts={"read_file": 5, "edit_file": 2},
        final_text="Done!",
    )
    fetched = store.get_session("ses_001")
    assert fetched.status == "completed"
    assert fetched.turns_used == 8
    assert fetched.total_cost_usd == pytest.approx(0.05)
    assert fetched.tool_counts == {"read_file": 5, "edit_file": 2}
    assert fetched.final_text == "Done!"


def test_complete_session_persists_engram_and_plan_link(store):
    """engram_session_dir + active_plan_* are stored when supplied."""
    rec = _make_record()
    store.insert_session(rec)
    store.complete_session(
        "ses_001",
        status="completed",
        ended_at="2026-04-21T00:05:00.000",
        engram_session_dir="memory/activity/2026/04/21/act-007",
        active_plan_project="auth-redesign",
        active_plan_id="token-refresh",
    )
    fetched = store.get_session("ses_001")
    assert fetched.engram_session_dir == "memory/activity/2026/04/21/act-007"
    assert fetched.active_plan_project == "auth-redesign"
    assert fetched.active_plan_id == "token-refresh"


def test_complete_session_persists_bridge_status(store):
    rec = _make_record()
    store.insert_session(rec)
    store.complete_session(
        "ses_001",
        status="completed",
        ended_at="2026-04-21T00:05:00.000",
        bridge_status="error",
        bridge_error="RuntimeError: boom",
    )
    fetched = store.get_session("ses_001")
    assert fetched.bridge_status == "error"
    assert fetched.bridge_error == "RuntimeError: boom"


def test_init_schema_adds_columns_to_existing_db(tmp_path):
    """ALTER TABLE migration brings older DBs forward.

    Fabricate a pre-active_plan database by hand-writing the older
    sessions schema (no active_plan_project / active_plan_id columns),
    then open via SessionStore and verify the columns are added and
    writable through the high-level API.
    """
    import sqlite3

    db_path = tmp_path / "old.db"
    # Older schema — same table layout as before this PR, intentionally
    # missing active_plan_project / active_plan_id. Mirrors what an
    # existing user's DB would look like on first upgrade.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id     TEXT PRIMARY KEY,
            task           TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'running',
            model          TEXT,
            mode           TEXT,
            memory_backend TEXT,
            workspace      TEXT,
            created_at     TEXT NOT NULL,
            ended_at       TEXT,
            turns_used           INTEGER,
            input_tokens         INTEGER,
            output_tokens        INTEGER,
            cache_read_tokens    INTEGER,
            cache_write_tokens   INTEGER,
            reasoning_tokens     INTEGER,
            total_cost_usd       REAL,
            tool_counts    TEXT,
            error_count    INTEGER DEFAULT 0,
            final_text         TEXT,
            max_turns_reached  INTEGER DEFAULT 0,
            trace_path          TEXT,
            engram_session_dir  TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    # Open via SessionStore — _ensure_additive_columns should add the new ones.
    store = SessionStore(db_path)
    cols = {row["name"] for row in store._conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "bridge_status" in cols
    assert "bridge_error" in cols
    assert "active_plan_project" in cols
    assert "active_plan_id" in cols
    # Writes through the high-level API land.
    store.insert_session(_make_record("ses_migrated"))
    store.complete_session(
        "ses_migrated",
        status="completed",
        ended_at="2026-04-21T00:00:00.000",
        active_plan_project="proj",
        active_plan_id="plan",
    )
    fetched = store.get_session("ses_migrated")
    assert fetched.active_plan_project == "proj"
    assert fetched.active_plan_id == "plan"


# ---------------------------------------------------------------------------
# most_recent_for_workspace
# ---------------------------------------------------------------------------


def test_most_recent_for_workspace_returns_none_when_empty(store):
    assert store.most_recent_for_workspace("/some/ws") is None


def test_most_recent_for_workspace_returns_latest_for_workspace(store):
    """Newer created_at wins; sessions for other workspaces are ignored."""
    store.insert_session(
        _make_record("s_old", workspace="/a", created_at="2026-04-21T00:00:00.000")
    )
    store.insert_session(
        _make_record("s_new", workspace="/a", created_at="2026-04-21T03:00:00.000")
    )
    store.insert_session(
        _make_record("s_other", workspace="/b", created_at="2026-04-21T05:00:00.000")
    )
    rec = store.most_recent_for_workspace("/a")
    assert rec is not None
    assert rec.session_id == "s_new"


def test_most_recent_for_workspace_returns_none_for_unknown_workspace(store):
    store.insert_session(_make_record("s1", workspace="/a"))
    assert store.most_recent_for_workspace("/never-seen") is None


# ---------------------------------------------------------------------------
# list_sessions filters
# ---------------------------------------------------------------------------


def test_list_sessions_all(store):
    store.insert_session(_make_record("s1", workspace="/a"))
    store.insert_session(_make_record("s2", workspace="/b"))
    store.insert_session(_make_record("s3", workspace="/a"))
    results = store.list_sessions()
    assert len(results) == 3


def test_list_sessions_workspace_filter(store):
    store.insert_session(_make_record("s1", workspace="/a"))
    store.insert_session(_make_record("s2", workspace="/b"))
    store.insert_session(_make_record("s3", workspace="/a"))
    results = store.list_sessions(workspace="/a")
    assert len(results) == 2
    assert all(r.workspace == "/a" for r in results)


def test_list_sessions_status_filter(store):
    store.insert_session(_make_record("s1", status="running"))
    store.insert_session(_make_record("s2", status="completed"))
    store.insert_session(_make_record("s3", status="running"))
    results = store.list_sessions(status="running")
    assert len(results) == 2
    assert all(r.status == "running" for r in results)


def test_list_sessions_limit_offset(store):
    for i in range(5):
        store.insert_session(_make_record(f"s{i}", created_at=f"2026-04-21T00:0{i}:00.000"))
    page1 = store.list_sessions(limit=2, offset=0)
    page2 = store.list_sessions(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    ids_p1 = {r.session_id for r in page1}
    ids_p2 = {r.session_id for r in page2}
    assert ids_p1.isdisjoint(ids_p2)


# ---------------------------------------------------------------------------
# FTS search
# ---------------------------------------------------------------------------


def test_fts_search(store):
    store.insert_session(_make_record("s1", task="Refactor the auth middleware"))
    store.insert_session(_make_record("s2", task="Fix the payment bug"))
    store.insert_session(_make_record("s3", task="Refactor the database layer"))
    results = store.list_sessions(search="refactor")
    assert len(results) == 2
    assert all("refactor" in r.task.lower() for r in results)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_empty(store):
    result = store.stats()
    assert result["total_sessions"] == 0
    assert result["total_cost_usd"] == pytest.approx(0.0)


def test_stats_with_data(store):
    store.insert_session(_make_record("s1"))
    store.insert_session(_make_record("s2"))
    store.complete_session("s1", status="completed", ended_at="", total_cost_usd=0.10)
    store.complete_session("s2", status="completed", ended_at="", total_cost_usd=0.20)
    result = store.stats()
    assert result["total_sessions"] == 2
    assert result["total_cost_usd"] == pytest.approx(0.30)


def test_stats_workspace_filter(store):
    store.insert_session(_make_record("s1", workspace="/a"))
    store.insert_session(_make_record("s2", workspace="/b"))
    store.complete_session("s1", status="completed", ended_at="", total_cost_usd=0.10)
    store.complete_session("s2", status="completed", ended_at="", total_cost_usd=0.20)
    result = store.stats(workspace="/a")
    assert result["total_sessions"] == 1
    assert result["total_cost_usd"] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Missing DB path → auto-create
# ---------------------------------------------------------------------------


def test_missing_db_created(tmp_path):
    db = tmp_path / "subdir" / "sessions.db"
    assert not db.exists()
    store = SessionStore(db)
    assert db.exists()
    store.close()


# ---------------------------------------------------------------------------
# backfill_from_traces
# ---------------------------------------------------------------------------


def _write_trace(path: Path, events: list[dict]) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_backfill_from_trace(tmp_path, store):
    trace_dir = tmp_path / "traces"
    trace_path = trace_dir / "20260421-000000-native.jsonl"
    _write_trace(
        trace_path,
        [
            {"ts": "2026-04-21T00:00:00.000", "kind": "session_start", "task": "Do the thing"},
            {"ts": "2026-04-21T00:00:01.000", "kind": "tool_call", "name": "read_file"},
            {"ts": "2026-04-21T00:00:02.000", "kind": "tool_call", "name": "edit_file"},
            {
                "ts": "2026-04-21T00:00:03.000",
                "kind": "session_usage",
                "input_tokens": 1000,
                "output_tokens": 200,
                "total_cost_usd": 0.01,
            },
            {"ts": "2026-04-21T00:00:04.000", "kind": "session_end", "turns": 3},
        ],
    )
    count = store.backfill_from_traces(trace_dir)
    assert count == 1
    results = store.list_sessions()
    assert len(results) == 1
    rec = results[0]
    assert rec.task == "Do the thing"
    assert rec.input_tokens == 1000
    assert rec.total_cost_usd == pytest.approx(0.01)
    assert rec.tool_counts == {"read_file": 1, "edit_file": 1}


def test_backfill_preserves_role_from_session_start(tmp_path, store):
    trace_dir = tmp_path / "traces"
    trace_path = trace_dir / "20260421-000000-native.jsonl"
    _write_trace(
        trace_path,
        [
            {
                "ts": "2026-04-21T00:00:00.000",
                "kind": "session_start",
                "task": "Roleful task",
                "role": "plan",
            },
            {"ts": "2026-04-21T00:00:01.000", "kind": "session_end", "turns": 1},
        ],
    )
    assert store.backfill_from_traces(trace_dir) == 1
    rec = store.list_sessions()[0]
    assert rec.role == "plan"


def test_backfill_idempotent(tmp_path, store):
    trace_dir = tmp_path / "traces"
    trace_path = trace_dir / "20260421-000000-native.jsonl"
    _write_trace(
        trace_path,
        [
            {"ts": "2026-04-21T00:00:00.000", "kind": "session_start", "task": "Idempotent"},
            {"ts": "2026-04-21T00:00:01.000", "kind": "session_end", "turns": 1},
        ],
    )
    count1 = store.backfill_from_traces(trace_dir)
    count2 = store.backfill_from_traces(trace_dir)
    assert count1 == 1
    assert count2 == 0
    assert len(store.list_sessions()) == 1


def test_backfill_empty_dir(tmp_path, store):
    empty = tmp_path / "empty"
    empty.mkdir()
    count = store.backfill_from_traces(empty)
    assert count == 0


def test_parse_trace_no_session_start(tmp_path):
    path = tmp_path / "bad.jsonl"
    _write_trace(path, [{"kind": "tool_call", "name": "read_file"}])
    result = _parse_trace_for_backfill(path)
    assert result is None


# ---------------------------------------------------------------------------
# Concurrent write safety
# ---------------------------------------------------------------------------


def test_concurrent_writes(tmp_path):
    """10 threads writing sessions concurrently must all succeed without error."""
    import threading

    store = SessionStore(tmp_path / "concurrent.db")
    errors: list[Exception] = []

    def write_session(idx: int) -> None:
        try:
            store.insert_session(_make_record(f"ses_{idx:03d}"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_session, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent writes raised: {errors}"
    results = store.list_sessions()
    assert len(results) == 10
    store.close()


# ---------------------------------------------------------------------------
# B4 pause / resume
# ---------------------------------------------------------------------------


def test_mark_paused_sets_status_and_checkpoint(store):
    rec = _make_record()
    store.insert_session(rec)
    store.mark_paused(
        rec.session_id,
        checkpoint_path="/abs/path/checkpoint.json",
        paused_at="2026-04-27T20:00:00",
    )
    fetched = store.get_session(rec.session_id)
    assert fetched is not None
    assert fetched.status == "paused"
    assert fetched.pause_checkpoint == "/abs/path/checkpoint.json"
    assert fetched.paused_at == "2026-04-27T20:00:00"


def test_mark_resumed_clears_pause_fields_and_status(store):
    rec = _make_record()
    store.insert_session(rec)
    store.mark_paused(
        rec.session_id,
        checkpoint_path="/cp.json",
        paused_at="2026-04-27T20:00:00",
    )
    store.mark_resumed(rec.session_id)
    fetched = store.get_session(rec.session_id)
    assert fetched is not None
    assert fetched.status == "running"
    assert fetched.pause_checkpoint is None
    assert fetched.paused_at is None


def test_list_sessions_can_filter_paused(store):
    rec_a = _make_record("ses_a")
    rec_b = _make_record("ses_b")
    store.insert_session(rec_a)
    store.insert_session(rec_b)
    store.mark_paused(
        "ses_a",
        checkpoint_path="/cp.json",
        paused_at="2026-04-27T20:00:00",
    )
    paused = store.list_sessions(status="paused")
    assert [r.session_id for r in paused] == ["ses_a"]


def test_paused_record_survives_existing_db_migration(tmp_path):
    """Re-opening an existing DB still gets the new columns via the additive
    migration path."""
    db_path = tmp_path / "old.db"
    first = SessionStore(db_path)
    first.insert_session(_make_record())
    first.close()

    second = SessionStore(db_path)
    second.mark_paused(
        "ses_001",
        checkpoint_path="/cp.json",
        paused_at="2026-04-27T20:00:00",
    )
    fetched = second.get_session("ses_001")
    second.close()
    assert fetched is not None
    assert fetched.pause_checkpoint == "/cp.json"
