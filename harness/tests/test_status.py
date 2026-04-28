"""Tests for `harness status` subcommand helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from harness.cli_helpers import resolve_content_root
from harness.cmd_status import (
    _print_active_plans,
    _print_recent_sessions,
    _resolve_workspace_dir,
)
from harness.session_store import SessionRecord, SessionStore
from harness.workspace import Workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_content_root(tmp: Path) -> Path:
    """Minimal Engram content root layout."""
    (tmp / "memory").mkdir(parents=True, exist_ok=True)
    (tmp / "memory" / "HOME.md").write_text("# Home\n", encoding="utf-8")
    return tmp


def _make_workspace_dir(tmp: Path) -> Path:
    """Return an empty workspace directory under *tmp* with the layout in place."""
    ws = Workspace(tmp, session_id="act-001")
    ws.ensure_layout()
    return ws.dir


def _create_plan(
    workspace_parent: Path,
    purpose: str,
    *,
    plan_id: str | None = None,
    project: str = "misc-plans",
    n_phases: int = 2,
    budget: dict | None = None,
) -> tuple[str, str, Path]:
    """Scaffold a workspace plan directly via ``Workspace.plan_create``.

    Workspace lives at ``workspace_parent/workspace`` (the conventional
    layout). Returns ``(project, plan_id, workspace_dir)`` so tests can
    pass the workspace directly to ``_print_active_plans``.
    """
    ws = Workspace(workspace_parent, session_id="act-001")
    ws.ensure_layout()
    # project_create seeds GOAL.md + SUMMARY.md — plan_create requires
    # the project to exist first.
    if not ws.project(project).exists():
        ws.project_create(project, goal=f"test bucket for {purpose}")
    pid = plan_id or purpose.lower().replace(" ", "-")
    phases = [{"title": f"Phase {i + 1}"} for i in range(n_phases)]
    ws.plan_create(project, pid, purpose, phases=phases, budget=budget)
    return project, pid, ws.dir


# ---------------------------------------------------------------------------
# resolve_content_root
# ---------------------------------------------------------------------------


def test_resolve_content_root_with_explicit_repo(tmp_path):
    _make_content_root(tmp_path)
    # _resolve_content_root returns the parent of memory/ (the repo root).
    resolved = resolve_content_root(str(tmp_path))
    assert resolved is not None
    assert (resolved / "memory" / "HOME.md").is_file()


def test_resolve_content_root_invalid_path_returns_none(tmp_path):
    result = resolve_content_root(str(tmp_path / "nonexistent"))
    assert result is None


def test_resolve_content_root_directory_without_engram_returns_none(tmp_path):
    result = resolve_content_root(str(tmp_path))
    assert result is None


def test_resolve_content_root_auto_detect_walks_cwd(tmp_path):
    _make_content_root(tmp_path)
    subdir = tmp_path / "src" / "module"
    subdir.mkdir(parents=True)
    with patch("harness.cli.Path") as mock_path_cls:
        mock_path_cls.cwd.return_value = subdir
        mock_path_cls.side_effect = lambda *a: Path(*a)
        # Auto-detect from CWD walking upward
        resolved = resolve_content_root(str(tmp_path))
    assert resolved is not None


# ---------------------------------------------------------------------------
# _print_active_plans
# ---------------------------------------------------------------------------


def test_print_active_plans_no_plans(tmp_path, capsys):
    ws_dir = _make_workspace_dir(tmp_path)
    _print_active_plans(ws_dir)
    out = capsys.readouterr().out
    assert "Active plans: none" in out


def test_print_active_plans_missing_workspace_dir(tmp_path, capsys):
    """A bare path that doesn't exist yet renders as 'not initialized', not a crash."""
    _print_active_plans(tmp_path / "no-workspace")
    out = capsys.readouterr().out
    assert "not initialized" in out


def test_print_active_plans_shows_active(tmp_path, capsys):
    _, _, ws_dir = _create_plan(tmp_path, "Build auth module", n_phases=3)
    _print_active_plans(ws_dir)
    out = capsys.readouterr().out
    assert "Active plans (1)" in out
    assert "Build auth module" in out
    assert "Phase 1/3" in out


def test_print_active_plans_shows_multiple(tmp_path, capsys):
    _, _, ws_dir = _create_plan(tmp_path, "Plan Alpha", plan_id="plan-alpha")
    _create_plan(tmp_path, "Plan Beta", plan_id="plan-beta")
    _print_active_plans(ws_dir)
    out = capsys.readouterr().out
    assert "Active plans (2)" in out
    assert "Plan Alpha" in out
    assert "Plan Beta" in out


def test_print_active_plans_completed_plan_excluded(tmp_path, capsys):
    project, plan_id, ws_dir = _create_plan(tmp_path, "Done plan")
    # Mark the workspace run-state as completed (new schema uses the
    # "completed" string, not "complete").
    state_path = ws_dir / "projects" / project / "plans" / f"{plan_id}.run-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "completed"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    _print_active_plans(ws_dir)
    out = capsys.readouterr().out
    assert "Done plan" not in out


def test_print_active_plans_shows_session_count(tmp_path, capsys):
    project, plan_id, ws_dir = _create_plan(tmp_path, "Tracked plan")
    # Simulate one distinct session having advanced the plan. The new
    # schema tracks this via sessions_used + sessions_touched, not a
    # list of per-session dicts.
    state_path = ws_dir / "projects" / project / "plans" / f"{plan_id}.run-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["sessions_used"] = 1
    state["sessions_touched"] = ["act-200"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    _print_active_plans(ws_dir)
    out = capsys.readouterr().out
    assert "1 session" in out


def test_print_active_plans_shows_budget_when_max_sessions(tmp_path, capsys):
    _, _, ws_dir = _create_plan(tmp_path, "Budgeted plan", budget={"max_sessions": 5})
    _print_active_plans(ws_dir)
    out = capsys.readouterr().out
    assert "0/5" in out


# ---------------------------------------------------------------------------
# _resolve_workspace_dir
# ---------------------------------------------------------------------------


def test_resolve_workspace_dir_explicit(tmp_path):
    explicit = tmp_path / "ws-elsewhere"
    resolved = _resolve_workspace_dir(str(explicit))
    assert resolved == explicit.resolve()


def test_resolve_workspace_dir_default_anchors_on_project_root():
    """No argument → fall back to <project_root>/workspace."""
    resolved = _resolve_workspace_dir(None)
    # The default lives next to the harness package, regardless of CWD.
    assert resolved.name == "workspace"
    assert (resolved.parent / "harness" / "cmd_status.py").is_file()


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
