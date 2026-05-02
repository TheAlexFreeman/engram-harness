"""F4: role as observability dimension.

F1 wired roles into the prompt; F2 enforced the write boundary; F3 propagated
roles through subagents. F4 makes ``role`` first-class for downstream
observability: SessionRecord stores it, cmd_status displays it, and the
``session_start`` trace event carries it so cmd_eval / cmd_drift can correlate
metrics by role.

Out of F4 v1 scope (deferred follow-on): per-role eval breakdowns and the
C4 "role/behavior mismatch" drift rule. Both need ≥N sessions per role to
produce meaningful baselines, so they ship in a follow-on after F4 has
been live long enough to accumulate data.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from harness.runner import _emit_session_start
from harness.session_store import SessionRecord, SessionStore

# ---------------------------------------------------------------------------
# SessionRecord — role round-trip through SQLite
# ---------------------------------------------------------------------------


def test_session_record_default_role_is_none() -> None:
    """Pre-F1 sessions had no role; SessionRecord defaults preserve this."""
    record = SessionRecord(session_id="x", task="y", status="running")
    assert record.role is None


def test_session_record_role_round_trips_through_dict() -> None:
    record = SessionRecord(session_id="x", task="y", status="running", role="plan")
    d = record.as_dict()
    assert d["role"] == "plan"

    from_dict = SessionRecord.from_row(d)
    assert from_dict.role == "plan"


def test_session_record_none_role_round_trips(tmp_path: Path) -> None:
    """Sessions without a role still round-trip — the column accepts NULL."""
    store = SessionStore(tmp_path / "sessions.db")
    record = SessionRecord(
        session_id="s-no-role",
        task="t",
        status="running",
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    store.insert_session(record)
    retrieved = store.list_sessions(limit=1)
    assert len(retrieved) == 1
    assert retrieved[0].role is None
    store.close()


@pytest.mark.parametrize("role", ["chat", "plan", "research", "build"])
def test_session_record_with_role_round_trips_through_db(
    tmp_path: Path, role: str
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    record = SessionRecord(
        session_id=f"s-{role}",
        task="t",
        status="running",
        created_at=datetime.now().isoformat(timespec="seconds"),
        role=role,
    )
    store.insert_session(record)
    retrieved = store.list_sessions(limit=1)
    assert len(retrieved) == 1
    assert retrieved[0].role == role
    store.close()


def test_role_column_added_on_schema_upgrade(tmp_path: Path) -> None:
    """An older DB without the role column gets the column added on init.

    Simulate by creating a SessionStore (which auto-upgrades), dropping
    the column manually via ALTER TABLE (SQLite doesn't support DROP
    COLUMN pre-3.35; instead reopen as v0 by rebuilding without role).
    Easier to test the upgrade by verifying the additive list is honored
    when the column is absent.
    """
    db_path = tmp_path / "sessions.db"
    # Manually create a sessions table missing the role column.
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE sessions ("
        "session_id TEXT PRIMARY KEY, task TEXT, status TEXT, "
        "model TEXT, mode TEXT, memory_backend TEXT, workspace TEXT, "
        "created_at TEXT, ended_at TEXT, turns_used INTEGER, "
        "input_tokens INTEGER, output_tokens INTEGER, "
        "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
        "reasoning_tokens INTEGER, total_cost_usd REAL, tool_counts TEXT, "
        "error_count INTEGER, final_text TEXT, max_turns_reached INTEGER, "
        "trace_path TEXT, engram_session_dir TEXT)"
    )
    conn.commit()
    conn.close()

    # SessionStore.__init__ runs _ensure_additive_columns and should add ``role``.
    store = SessionStore(db_path)
    cols = {row["name"] for row in store._conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "role" in cols, "F4 schema upgrade should add the role column"
    store.close()


# ---------------------------------------------------------------------------
# _emit_session_start — payload only includes role when set
# ---------------------------------------------------------------------------


class _CapturingTracer:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def event(self, kind: str, **data) -> None:
        self.events.append((kind, data))


def test_emit_session_start_omits_role_when_none() -> None:
    """Backward compat: omit ``role`` from the payload when unset so the
    JSONL trace looks identical to pre-F1."""
    tracer = _CapturingTracer()
    _emit_session_start(tracer, task="hello")
    assert tracer.events == [("session_start", {"task": "hello"})]


def test_emit_session_start_includes_role_when_set() -> None:
    tracer = _CapturingTracer()
    _emit_session_start(tracer, task="hello", role="plan")
    assert tracer.events == [("session_start", {"task": "hello", "role": "plan"})]


def test_emit_session_start_passes_through_extra_fields() -> None:
    """Extra kwargs are forwarded — preserves the runner.py interactive
    path's ``opener`` field."""
    tracer = _CapturingTracer()
    _emit_session_start(tracer, task="t", role="research", opener="hello")
    assert tracer.events == [
        ("session_start", {"task": "t", "role": "research", "opener": "hello"})
    ]


# ---------------------------------------------------------------------------
# loop.run — session_start event records role
# ---------------------------------------------------------------------------


def test_loop_run_session_start_includes_role() -> None:
    """End-to-end: ``loop.run(role='plan')`` emits a session_start event
    with role on the trace."""
    from harness.loop import run
    from harness.tests.test_parallel_tools import (
        NullTracer,
        ScriptedMode,
        _ScriptedResponse,
    )
    from harness.tools.subagent import NullMemory

    captured: list[tuple[str, dict]] = []

    class _Tracer(NullTracer):
        def event(self, kind, **data):
            captured.append((kind, data))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    mode = ScriptedMode([_ScriptedResponse(tool_calls=[], text="done")])
    tracer = _Tracer()

    run(
        task="investigate X",
        mode=mode,
        tools={},
        memory=NullMemory(),
        tracer=tracer,
        max_turns=2,
        role="research",
        reflect=False,
    )

    starts = [e for e in captured if e[0] == "session_start"]
    assert len(starts) == 1
    assert starts[0][1].get("role") == "research"


def test_loop_run_session_start_omits_role_when_none() -> None:
    """Backward compat: no role kwarg → no role on event."""
    from harness.loop import run
    from harness.tests.test_parallel_tools import (
        NullTracer,
        ScriptedMode,
        _ScriptedResponse,
    )
    from harness.tools.subagent import NullMemory

    captured: list[tuple[str, dict]] = []

    class _Tracer(NullTracer):
        def event(self, kind, **data):
            captured.append((kind, data))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    mode = ScriptedMode([_ScriptedResponse(tool_calls=[], text="done")])
    tracer = _Tracer()

    run(
        task="x",
        mode=mode,
        tools={},
        memory=NullMemory(),
        tracer=tracer,
        max_turns=2,
        reflect=False,
    )

    starts = [e for e in captured if e[0] == "session_start"]
    assert len(starts) == 1
    assert "role" not in starts[0][1]


# ---------------------------------------------------------------------------
# cmd_status — display role tag for sessions that have one
# ---------------------------------------------------------------------------


def test_cmd_status_displays_role_tag_when_set(tmp_path: Path, capsys) -> None:
    """``harness status`` should show the role for sessions that have one."""
    from harness.cmd_status import _print_recent_sessions

    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    store.insert_session(
        SessionRecord(
            session_id="s-with-role",
            task="run a thing",
            status="completed",
            created_at="2026-05-02T10:00:00",
            total_cost_usd=0.0123,
            role="plan",
        )
    )
    store.insert_session(
        SessionRecord(
            session_id="s-no-role",
            task="legacy session",
            status="completed",
            created_at="2026-05-02T11:00:00",
            total_cost_usd=0.0001,
        )
    )
    store.close()

    _print_recent_sessions(db_path, limit=5)
    out = capsys.readouterr().out

    # Session with role gets a [role] tag in the printout.
    assert "[plan]" in out
    # Session without role doesn't get a tag — no spurious "[None]".
    legacy_line = [line for line in out.splitlines() if "legacy session" in line]
    assert legacy_line
    assert "[None]" not in legacy_line[0]
    assert "[plan]" not in legacy_line[0]


# ---------------------------------------------------------------------------
# Subagent role observability — F3 already added subagent.role to subagent_run;
# this guards the wiring with a simple regression check.
# ---------------------------------------------------------------------------


def test_subagent_run_event_carries_role(monkeypatch) -> None:
    """``subagent_run`` carries ``role`` so cmd_eval per-role breakdowns
    can attribute subagent costs to the right role."""
    from harness.config import _wire_subagent_spawn
    from harness.tests.test_parallel_tools import ScriptedMode, _ScriptedResponse
    from harness.tools.subagent import SpawnSubagent

    parent_tools = {"spawn_subagent": SpawnSubagent()}

    class _Tracer:
        def __init__(self):
            self.events: list[tuple[str, dict]] = []

        def event(self, kind, **data):
            self.events.append((kind, data))

        def close(self):
            pass

    class _ScriptedModeWithForTools(ScriptedMode):
        def for_tools(self, tools, *, system=None):  # noqa: ARG002
            return self

    tracer = _Tracer()
    _wire_subagent_spawn(
        parent_tools,
        mode=_ScriptedModeWithForTools(
            [_ScriptedResponse(tool_calls=[], text="ok")]
        ),
        parent_tracer=tracer,
        pricing_loader=lambda: None,
        parent_role="research",
    )

    parent_tools["spawn_subagent"].run({"task": "go", "allowed_tools": []})

    summary = [e for e in tracer.events if e[0] == "subagent_run"]
    assert len(summary) == 1
    assert summary[0][1]["role"] == "research"
