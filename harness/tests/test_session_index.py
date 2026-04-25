"""Tests for harness.session_index — shared SessionStore wiring helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from harness.session_index import (
    engram_session_metadata,
    new_cli_session_id,
    open_session_store_from_env,
    record_completed_session,
)
from harness.session_store import SessionRecord, SessionStore
from harness.usage import Usage


def _record(session_id: str, *, workspace: str = "/tmp/ws") -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        task="t",
        status="running",
        model="claude-sonnet-4-6",
        mode="native",
        memory_backend="file",
        workspace=workspace,
        created_at="2026-04-25T00:00:00.000",
    )


# ---------------------------------------------------------------------------
# new_cli_session_id
# ---------------------------------------------------------------------------


def test_new_cli_session_id_is_prefixed_and_unique() -> None:
    a = new_cli_session_id()
    b = new_cli_session_id()
    assert a.startswith("cli_")
    assert b.startswith("cli_")
    assert a != b


# ---------------------------------------------------------------------------
# open_session_store_from_env
# ---------------------------------------------------------------------------


def test_open_session_store_from_env_returns_none_without_env(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_DB_PATH", raising=False)
    assert open_session_store_from_env() is None


def test_open_session_store_from_env_creates_db_at_path(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "subdir" / "sessions.db"
    monkeypatch.setenv("HARNESS_DB_PATH", str(db_path))
    store = open_session_store_from_env()
    try:
        assert store is not None
        assert db_path.is_file()
    finally:
        if store is not None:
            store.close()


def test_open_session_store_from_env_returns_none_on_bad_file(tmp_path, monkeypatch) -> None:
    bad = tmp_path / "junk.db"
    bad.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("HARNESS_DB_PATH", str(bad))
    assert open_session_store_from_env() is None


# ---------------------------------------------------------------------------
# engram_session_metadata
# ---------------------------------------------------------------------------


def test_engram_session_metadata_none_for_no_engram() -> None:
    assert engram_session_metadata(None) == (None, None, None)


def test_engram_session_metadata_returns_dir_with_no_workspace_plans(tmp_path) -> None:
    """Engram present but workspace empty → engram_dir set, plan fields None."""
    from harness.workspace import Workspace

    ws = Workspace(tmp_path, session_id="act-001")
    ws.ensure_layout()
    engram = SimpleNamespace(
        session_dir_rel="memory/activity/2026/04/25/act-007",
        workspace_dir=ws.dir,
    )
    engram_dir, project, plan_id = engram_session_metadata(engram)
    assert engram_dir == "memory/activity/2026/04/25/act-007"
    assert project is None
    assert plan_id is None


def test_engram_session_metadata_links_active_plan(tmp_path) -> None:
    from harness.workspace import Workspace

    ws = Workspace(tmp_path, session_id="act-001")
    ws.ensure_layout()
    ws.project_create("auth-redesign", goal="goal")
    ws.plan_create(
        "auth-redesign",
        "token-refresh",
        "Implement offline-capable token refresh",
        phases=[{"title": "Schema"}],
    )
    engram = SimpleNamespace(
        session_dir_rel="memory/activity/2026/04/25/act-007",
        workspace_dir=ws.dir,
    )
    engram_dir, project, plan_id = engram_session_metadata(engram)
    assert engram_dir == "memory/activity/2026/04/25/act-007"
    assert project == "auth-redesign"
    assert plan_id == "token-refresh"


# ---------------------------------------------------------------------------
# record_completed_session
# ---------------------------------------------------------------------------


def test_record_completed_session_no_op_when_store_none() -> None:
    """No store → no exception; nothing happens."""
    record_completed_session(
        None,
        session_id="ses",
        status="completed",
        ended_at="2026-04-25T00:01:00",
        turns_used=1,
        usage=Usage.zero(),
        tool_call_log=[],
        final_text=None,
        max_turns_reached=False,
    )


def test_record_completed_session_writes_aggregates_and_metadata(tmp_path) -> None:
    """End-to-end: insert running row, complete it, verify final state.

    Covers tool_counts derivation, error_count derivation, engram_session_dir
    population, active_plan link, and usage rollup all in one shot.
    """
    from harness.workspace import Workspace

    db_path = tmp_path / "x.db"
    store = SessionStore(db_path)
    try:
        store.insert_session(_record("ses_complete"))
        # Workspace with one active plan so the link gets populated.
        ws = Workspace(tmp_path / "ws_root", session_id="act-001")
        ws.ensure_layout()
        ws.project_create("p", goal="g")
        ws.plan_create("p", "plan-a", "Ship it", phases=[{"title": "P1"}])
        engram = SimpleNamespace(
            session_dir_rel="memory/activity/2026/04/25/act-007",
            workspace_dir=ws.dir,
        )
        usage = Usage(input_tokens=1000, output_tokens=500, total_cost_usd=0.0123)
        record_completed_session(
            store,
            session_id="ses_complete",
            status="completed",
            ended_at="2026-04-25T00:05:00",
            turns_used=4,
            usage=usage,
            tool_call_log=[
                {"name": "read_file", "is_error": False},
                {"name": "read_file", "is_error": False},
                {"name": "edit_file", "is_error": True},
            ],
            final_text="all done",
            max_turns_reached=False,
            engram_memory=engram,
        )
        rec = store.get_session("ses_complete")
        assert rec is not None
        assert rec.status == "completed"
        assert rec.turns_used == 4
        assert rec.tool_counts == {"read_file": 2, "edit_file": 1}
        assert rec.error_count == 1
        assert rec.input_tokens == 1000
        assert rec.output_tokens == 500
        assert rec.total_cost_usd == 0.0123
        assert rec.final_text == "all done"
        assert rec.engram_session_dir == "memory/activity/2026/04/25/act-007"
        assert rec.active_plan_project == "p"
        assert rec.active_plan_id == "plan-a"
    finally:
        store.close()


def test_record_completed_session_swallows_exceptions(tmp_path) -> None:
    """A broken Usage that raises during attribute access shouldn't propagate."""
    db_path = tmp_path / "x.db"
    store = SessionStore(db_path)
    try:
        store.insert_session(_record("ses_explode"))

        class _ExplodingUsage:
            def __getattr__(self, name):
                raise RuntimeError(f"boom: {name}")

        record_completed_session(
            store,
            session_id="ses_explode",
            status="completed",
            ended_at="2026-04-25T00:00:00",
            turns_used=1,
            usage=_ExplodingUsage(),  # type: ignore[arg-type]
            tool_call_log=[],
            final_text=None,
            max_turns_reached=False,
        )
        # The original "running" row stays put; no crash means we win.
        rec = store.get_session("ses_explode")
        assert rec is not None
        assert rec.status == "running"
    finally:
        store.close()
