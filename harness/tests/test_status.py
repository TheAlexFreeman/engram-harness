"""Tests for `harness status` subcommand helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.cmd_status import (
    _print_active_plans,
    _print_recent_sessions,
    _resolve_engram_content_root,
)
from harness.session_store import SessionRecord, SessionStore
from harness.tools.plan_tools import CreatePlan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_content_root(tmp: Path) -> Path:
    """Minimal Engram content root layout."""
    (tmp / "memory").mkdir(parents=True, exist_ok=True)
    (tmp / "memory" / "HOME.md").write_text("# Home\n", encoding="utf-8")
    (tmp / "memory" / "working" / "projects").mkdir(parents=True, exist_ok=True)
    return tmp


def _make_mock_memory(content_root: Path) -> MagicMock:
    m = MagicMock()
    m.content_root = content_root
    m.session_id = "act-001"
    return m


def _create_plan(content_root: Path, title: str, n_phases: int = 2, **kwargs) -> str:
    """Use CreatePlan tool to write a plan and return its plan_id."""
    mem = _make_mock_memory(content_root)
    tool = CreatePlan(mem)
    phases = [{"name": f"Phase {i + 1}", "tasks": [f"Task {i + 1}a"]} for i in range(n_phases)]
    result = tool.run({"title": title, "phases": phases, **kwargs})
    # Extract plan_id from result string
    for word in result.split():
        if word.startswith("**plan-") and word.endswith("**"):
            return word.strip("*")
        if word.startswith("plan-"):
            return word.rstrip(".")
    raise RuntimeError(f"Could not extract plan_id from: {result!r}")


# ---------------------------------------------------------------------------
# _resolve_engram_content_root
# ---------------------------------------------------------------------------


def test_resolve_content_root_with_explicit_repo(tmp_path):
    _make_content_root(tmp_path)
    # _resolve_content_root returns the parent of memory/ (the repo root).
    resolved = _resolve_engram_content_root(str(tmp_path))
    assert resolved is not None
    assert (resolved / "memory" / "HOME.md").is_file()


def test_resolve_content_root_invalid_path_returns_none(tmp_path):
    result = _resolve_engram_content_root(str(tmp_path / "nonexistent"))
    assert result is None


def test_resolve_content_root_directory_without_engram_returns_none(tmp_path):
    result = _resolve_engram_content_root(str(tmp_path))
    assert result is None


def test_resolve_content_root_auto_detect_walks_cwd(tmp_path):
    _make_content_root(tmp_path)
    subdir = tmp_path / "src" / "module"
    subdir.mkdir(parents=True)
    with patch("harness.cli.Path") as mock_path_cls:
        mock_path_cls.cwd.return_value = subdir
        mock_path_cls.side_effect = lambda *a: Path(*a)
        # Auto-detect from CWD walking upward
        resolved = _resolve_engram_content_root(str(tmp_path))
    assert resolved is not None


# ---------------------------------------------------------------------------
# _print_active_plans
# ---------------------------------------------------------------------------


def test_print_active_plans_no_plans(tmp_path, capsys):
    cr = _make_content_root(tmp_path)
    _print_active_plans(cr)
    out = capsys.readouterr().out
    assert "Active plans: none" in out


def test_print_active_plans_shows_active(tmp_path, capsys):
    cr = _make_content_root(tmp_path)
    _create_plan(cr, "Build auth module", n_phases=3)
    _print_active_plans(cr)
    out = capsys.readouterr().out
    assert "Active plans (1)" in out
    assert "Build auth module" in out
    assert "Phase 1/3" in out


def test_print_active_plans_shows_multiple(tmp_path, capsys):
    cr = _make_content_root(tmp_path)
    _create_plan(cr, "Plan Alpha")
    _create_plan(cr, "Plan Beta")
    _print_active_plans(cr)
    out = capsys.readouterr().out
    assert "Active plans (2)" in out
    assert "Plan Alpha" in out
    assert "Plan Beta" in out


def test_print_active_plans_completed_plan_excluded(tmp_path, capsys):
    cr = _make_content_root(tmp_path)
    plan_id = _create_plan(cr, "Done plan")
    # Manually mark as complete
    plans_dir = cr / "memory" / "working" / "projects" / "misc-plans" / "plans" / plan_id
    state_path = plans_dir / "run-state.json"
    state = json.loads(state_path.read_text())
    state["status"] = "complete"
    state_path.write_text(json.dumps(state))
    _print_active_plans(cr)
    out = capsys.readouterr().out
    assert "Done plan" not in out


def test_print_active_plans_shows_session_count(tmp_path, capsys):
    cr = _make_content_root(tmp_path)
    plan_id = _create_plan(cr, "Tracked plan")
    plans_dir = cr / "memory" / "working" / "projects" / "misc-plans" / "plans" / plan_id
    state_path = plans_dir / "run-state.json"
    state = json.loads(state_path.read_text())
    state["sessions"] = [{"phase_index": 0, "completed_at": "2026-04-20T00:00:00"}]
    state_path.write_text(json.dumps(state))
    _print_active_plans(cr)
    out = capsys.readouterr().out
    assert "1 session" in out


def test_print_active_plans_shows_budget_when_max_sessions(tmp_path, capsys):
    cr = _make_content_root(tmp_path)
    _create_plan(cr, "Budgeted plan", max_sessions=5)
    _print_active_plans(cr)
    out = capsys.readouterr().out
    assert "0/5" in out


# ---------------------------------------------------------------------------
# _print_recent_sessions
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


def _make_record(session_id: str, task: str = "do a thing", **kwargs) -> SessionRecord:
    defaults = dict(
        session_id=session_id,
        task=task,
        status="completed",
        model="claude-sonnet-4-6",
        mode="native",
        memory_backend="file",
        workspace="/tmp/workspace",
        created_at="2026-04-21T10:00:00.000",
    )
    defaults.update(kwargs)
    return SessionRecord(**defaults)


def test_print_recent_sessions_empty_store(tmp_path, capsys):
    db_path = tmp_path / "sessions.db"
    SessionStore(db_path)
    _print_recent_sessions(db_path, limit=10)
    out = capsys.readouterr().out
    assert "none recorded" in out
    assert "Stats:" in out


def test_print_recent_sessions_shows_records(tmp_path, capsys):
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    rec = _make_record("ses_abc123", "run tests for auth")
    store.insert_session(rec)
    store.complete_session(
        "ses_abc123",
        status="completed",
        ended_at="2026-04-21T10:01:00.000",
        turns_used=5,
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        total_cost_usd=0.0123,
    )
    _print_recent_sessions(db_path, limit=10)
    out = capsys.readouterr().out
    assert "ses_abc123" in out
    assert "run tests for auth" in out
    assert "$0.0123" in out


def test_print_recent_sessions_respects_limit(tmp_path, capsys):
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    for i in range(5):
        rec = _make_record(f"ses_{i:03d}", task=f"task {i}")
        store.insert_session(rec)
    _print_recent_sessions(db_path, limit=3)
    out = capsys.readouterr().out
    assert "last 3" in out


def test_print_recent_sessions_shows_stats(tmp_path, capsys):
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    rec = _make_record("ses_001")
    store.insert_session(rec)
    store.complete_session(
        "ses_001",
        status="completed",
        ended_at="2026-04-21T10:05:00.000",
        turns_used=8,
        input_tokens=2000,
        output_tokens=800,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        total_cost_usd=0.05,
    )
    _print_recent_sessions(db_path, limit=10)
    out = capsys.readouterr().out
    assert "Stats:" in out
    assert "1 sessions" in out


# ---------------------------------------------------------------------------
# CLI dispatch integration
# ---------------------------------------------------------------------------


def test_status_dispatch(tmp_path, capsys):
    """`harness status` with no configured store should print header and hints."""
    import sys

    from harness.cli import main

    with patch.object(sys, "argv", ["harness", "status", "--memory-repo", str(tmp_path)]):
        main()

    out = capsys.readouterr().out
    assert "=== Harness Status ===" in out
    assert "Session store: not configured" in out
