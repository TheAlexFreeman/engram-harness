"""Tests for the ``work:`` tool wrappers (harness/tools/work_tools.py).

These exercise the agent-facing surface: input validation, output
formatting, and — where applicable — that state changes emit
``memory_trace`` events on the attached ``EngramMemory`` instance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.engram_memory import EngramMemory
from harness.tests.test_engram_memory import _make_engram_repo
from harness.tools.work_tools import (
    WorkJot,
    WorkNote,
    WorkProjectArchive,
    WorkProjectAsk,
    WorkProjectCreate,
    WorkProjectGoal,
    WorkProjectList,
    WorkProjectResolve,
    WorkProjectStatus,
    WorkRead,
    WorkScratch,
    WorkStatus,
    WorkThread,
)
from harness.workspace import Workspace

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engram(tmp_path: Path) -> EngramMemory:
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, embed=False)
    mem.start_session("work tool tests")
    return mem


@pytest.fixture
def ws(engram: EngramMemory) -> Workspace:
    w = Workspace(engram.content_root, session_id=engram.session_id)
    w.ensure_layout()
    return w


# ---------------------------------------------------------------------------
# work_status
# ---------------------------------------------------------------------------


def test_status_shows_empty_current(ws: Workspace) -> None:
    out = WorkStatus(ws).run({})
    assert "# workspace/CURRENT.md" in out
    assert "## Threads" in out
    assert "## Notes" in out


def test_status_with_project_includes_summary(ws: Workspace) -> None:
    ws.project_create("alpha", goal="explore alpha")
    out = WorkStatus(ws).run({"project": "alpha"})
    assert "# workspace/CURRENT.md" in out
    assert "# projects/alpha/SUMMARY.md" in out
    assert "explore alpha" in out


def test_status_with_unknown_project_graceful(ws: Workspace) -> None:
    out = WorkStatus(ws).run({"project": "ghost"})
    assert "does not exist" in out


# ---------------------------------------------------------------------------
# work_thread
# ---------------------------------------------------------------------------


def test_thread_open_emits_trace(ws: Workspace, engram: EngramMemory) -> None:
    tool = WorkThread(ws, engram=engram)
    out = tool.run(
        {
            "name": "auth-redesign",
            "open": True,
            "status": "active",
            "next": "draft token refresh flow",
        }
    )
    assert "Opened thread" in out
    events = engram.trace_events
    assert any(ev.event == "thread_update" and "opened:auth-redesign" in ev.reason for ev in events)


def test_thread_close_emits_trace(ws: Workspace, engram: EngramMemory) -> None:
    tool = WorkThread(ws, engram=engram)
    tool.run({"name": "t1", "open": True})
    tool.run({"name": "t1", "close": True, "summary": "done"})
    reasons = [ev.reason for ev in engram.trace_events if ev.event == "thread_update"]
    assert any(r.startswith("opened:") for r in reasons)
    assert any(r.startswith("closed:") for r in reasons)


def test_thread_rejects_open_and_close(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        WorkThread(ws).run({"name": "x", "open": True, "close": True})


def test_thread_requires_name(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        WorkThread(ws).run({"name": ""})


def test_thread_update_persists_status(ws: Workspace) -> None:
    tool = WorkThread(ws)
    tool.run({"name": "t1", "open": True})
    tool.run({"name": "t1", "status": "blocked", "next": "awaiting review"})
    doc = ws.read_current()
    t = doc.find_thread("t1")
    assert t.status == "blocked"
    assert t.next == "awaiting review"


# ---------------------------------------------------------------------------
# work_jot
# ---------------------------------------------------------------------------


def test_jot_rejects_overlong(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        WorkJot(ws).run({"content": "x" * 2000})


def test_jot_writes_to_notes_section(ws: Workspace) -> None:
    WorkJot(ws).run({"content": "short observation"})
    doc = ws.read_current()
    assert doc.notes[-1].content == "short observation"


# ---------------------------------------------------------------------------
# work_note
# ---------------------------------------------------------------------------


def test_note_requires_exactly_one_of_content_or_append(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        WorkNote(ws).run({"title": "x"})
    with pytest.raises(ValueError):
        WorkNote(ws).run({"title": "x", "content": "a", "append": "b"})


def test_note_creates_then_appends(ws: Workspace) -> None:
    tool = WorkNote(ws)
    tool.run({"title": "analysis", "content": "## Intro\nFirst draft."})
    tool.run({"title": "analysis", "append": "## More\nSecond draft."})
    body = (ws.notes_dir / "analysis.md").read_text(encoding="utf-8")
    assert "First draft." in body
    assert "Second draft." in body


def test_note_rejects_append_to_missing(ws: Workspace) -> None:
    with pytest.raises(FileNotFoundError):
        WorkNote(ws).run({"title": "no-such-note", "append": "fragment"})


# ---------------------------------------------------------------------------
# work_read
# ---------------------------------------------------------------------------


def test_read_returns_workspace_file(ws: Workspace) -> None:
    ws.write_note("alpha", content="payload-content")
    out = WorkRead(ws).run({"path": "notes/alpha.md"})
    assert "payload-content" in out
    assert "# workspace/notes/alpha.md" in out


def test_read_missing_returns_friendly_message(ws: Workspace) -> None:
    out = WorkRead(ws).run({"path": "notes/ghost.md"})
    assert "no such workspace file" in out


def test_read_rejects_traversal(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        WorkRead(ws).run({"path": "../../escape.md"})


# ---------------------------------------------------------------------------
# work_scratch
# ---------------------------------------------------------------------------


def test_scratch_writes_to_session_file(ws: Workspace) -> None:
    WorkScratch(ws).run({"content": "hypothesis A"})
    WorkScratch(ws).run({"content": "hypothesis B"})
    path = ws.scratch_path()
    body = path.read_text(encoding="utf-8")
    assert "hypothesis A" in body
    assert "hypothesis B" in body


def test_scratch_rejects_empty_and_overlong(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        WorkScratch(ws).run({"content": ""})
    with pytest.raises(ValueError):
        WorkScratch(ws).run({"content": "x" * 5000})


# ---------------------------------------------------------------------------
# Project tools
# ---------------------------------------------------------------------------


def test_project_create_scaffolds_and_traces(ws: Workspace, engram: EngramMemory) -> None:
    tool = WorkProjectCreate(ws, engram=engram)
    out = tool.run(
        {
            "name": "auth-redesign",
            "goal": "Support offline token refresh",
            "questions": ["Reuse session table?"],
        }
    )
    assert "Created project" in out
    assert (ws.projects_dir / "auth-redesign" / "GOAL.md").is_file()
    assert any(ev.event == "project_create" for ev in engram.trace_events)


def test_project_create_rejects_bad_questions(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        WorkProjectCreate(ws).run({"name": "x", "goal": "g", "questions": [1, 2, 3]})


def test_project_goal_read_and_update(ws: Workspace, engram: EngramMemory) -> None:
    WorkProjectCreate(ws).run({"name": "alpha", "goal": "initial"})
    tool = WorkProjectGoal(ws, engram=engram)
    # Read.
    out_read = tool.run({"name": "alpha"})
    assert "initial" in out_read
    # Update.
    out_upd = tool.run({"name": "alpha", "goal": "refined goal text"})
    assert "Updated goal" in out_upd
    assert any(ev.event == "project_goal_update" for ev in engram.trace_events)


def test_project_ask_adds_question(ws: Workspace) -> None:
    WorkProjectCreate(ws).run({"name": "alpha", "goal": "explore"})
    out = WorkProjectAsk(ws).run({"name": "alpha", "question": "What's the plan?"})
    assert "Added question #1" in out


def test_project_resolve_emits_trace(ws: Workspace, engram: EngramMemory) -> None:
    WorkProjectCreate(ws).run({"name": "alpha", "goal": "explore", "questions": ["Q1", "Q2"]})
    out = WorkProjectResolve(ws, engram=engram).run(
        {"name": "alpha", "index": 1, "answer": "Yes, with caveats."}
    )
    assert "Resolved question 1" in out
    assert any(ev.event == "question_resolved" for ev in engram.trace_events)


def test_project_list_empty_and_populated(ws: Workspace) -> None:
    # Empty.
    out_empty = WorkProjectList(ws).run({})
    assert "no projects yet" in out_empty
    # Populated.
    WorkProjectCreate(ws).run({"name": "alpha", "goal": "goal-a"})
    WorkProjectCreate(ws).run({"name": "beta", "goal": "goal-b"})
    out = WorkProjectList(ws).run({})
    assert "alpha" in out
    assert "beta" in out
    assert "goal-a" in out


def test_project_status_returns_summary(ws: Workspace) -> None:
    WorkProjectCreate(ws).run({"name": "alpha", "goal": "explore alpha", "questions": ["Q1"]})
    out = WorkProjectStatus(ws).run({"name": "alpha"})
    assert "# projects/alpha/SUMMARY.md" in out
    assert "explore alpha" in out
    assert "Q1" in out


def test_project_status_missing_graceful(ws: Workspace) -> None:
    out = WorkProjectStatus(ws).run({"name": "ghost"})
    assert "does not exist" in out


def test_project_archive_emits_trace_and_moves_dir(ws: Workspace, engram: EngramMemory) -> None:
    WorkProjectCreate(ws).run({"name": "alpha", "goal": "explore"})
    out = WorkProjectArchive(ws, engram=engram).run({"name": "alpha", "summary": "Shipped"})
    assert "Archived project" in out
    assert (ws.projects_dir / "_archive" / "alpha").is_dir()
    assert any(ev.event == "project_archive" for ev in engram.trace_events)


def test_project_archive_requires_summary(ws: Workspace) -> None:
    WorkProjectCreate(ws).run({"name": "alpha", "goal": "explore"})
    with pytest.raises(ValueError):
        WorkProjectArchive(ws).run({"name": "alpha", "summary": ""})


# ---------------------------------------------------------------------------
# Tool-profile classification and read-only behaviour
# ---------------------------------------------------------------------------


def test_work_tools_declare_mutates_flag() -> None:
    """Every work tool must self-classify so config can filter read_only sessions.

    The --tool-profile=read_only contract advertises "no writes". Config
    relies on each tool's ``mutates`` attribute to decide whether to
    register it in that profile. Missing the attribute would silently
    re-register a mutating tool as read-only.
    """
    expected_read_only = {
        "work_status",
        "work_read",
        "work_project_list",
        "work_project_status",
    }
    expected_mutating = {
        "work_thread",
        "work_jot",
        "work_note",
        "work_scratch",
        "work_project_create",
        "work_project_goal",
        "work_project_ask",
        "work_project_resolve",
        "work_project_archive",
    }
    all_tools = (
        WorkStatus,
        WorkThread,
        WorkJot,
        WorkNote,
        WorkRead,
        WorkScratch,
        WorkProjectCreate,
        WorkProjectGoal,
        WorkProjectAsk,
        WorkProjectResolve,
        WorkProjectList,
        WorkProjectStatus,
        WorkProjectArchive,
    )
    read_only_names = {cls.name for cls in all_tools if not cls.mutates}
    mutating_names = {cls.name for cls in all_tools if cls.mutates}
    assert read_only_names == expected_read_only
    assert mutating_names == expected_mutating


def test_status_tolerates_missing_workspace(tmp_path: Path) -> None:
    """With read_only profile we skip ensure_layout; work_status must still work.

    The tool reports the workspace as uninitialized rather than creating
    it — otherwise the read-only contract would be broken by the very
    first read.
    """
    from harness.workspace import Workspace as _Workspace

    ws = _Workspace(tmp_path, session_id="act-001")
    # Intentionally no ensure_layout() — mimic read_only mode.
    assert not ws.current_path.is_file()
    out = WorkStatus(ws).run({})
    assert "workspace not initialized" in out
    # And the tool did not secretly create it.
    assert not ws.current_path.is_file()


def test_build_memory_filters_work_tools_under_read_only(tmp_path: Path) -> None:
    """_build_memory must drop mutating work tools when tool_profile=read_only."""
    from harness.config import SessionConfig, ToolProfile, _build_memory
    from harness.tests.test_engram_memory import _make_engram_repo

    repo = _make_engram_repo(tmp_path)
    config = SessionConfig(
        workspace=tmp_path / "scratch-ws",
        memory_backend="engram",
        memory_repo=repo,
        tool_profile=ToolProfile.READ_ONLY,
    )
    memory, _engram, extras = _build_memory(config)
    names = {t.name for t in extras}
    # Mutating work tools must not be registered.
    assert "work_thread" not in names
    assert "work_jot" not in names
    assert "work_note" not in names
    assert "work_scratch" not in names
    assert "work_project_create" not in names
    assert "work_project_archive" not in names
    # Read-only ones stay.
    assert "work_status" in names
    assert "work_read" in names
    assert "work_project_list" in names
    assert "work_project_status" in names
    # And the workspace layout wasn't eagerly created.
    workspace_root = memory.content_root / "workspace"
    assert not workspace_root.is_dir()


def test_build_memory_registers_all_work_tools_under_full(tmp_path: Path) -> None:
    """Regression guard: non-read_only profiles keep every work tool."""
    from harness.config import SessionConfig, ToolProfile, _build_memory
    from harness.tests.test_engram_memory import _make_engram_repo

    repo = _make_engram_repo(tmp_path)
    config = SessionConfig(
        workspace=tmp_path / "scratch-ws",
        memory_backend="engram",
        memory_repo=repo,
        tool_profile=ToolProfile.FULL,
    )
    memory, _engram, extras = _build_memory(config)
    names = {t.name for t in extras}
    for expected in (
        "work_status",
        "work_thread",
        "work_jot",
        "work_note",
        "work_read",
        "work_scratch",
        "work_project_create",
        "work_project_goal",
        "work_project_ask",
        "work_project_resolve",
        "work_project_list",
        "work_project_status",
        "work_project_archive",
    ):
        assert expected in names, f"missing in full profile: {expected}"
    # Full profile creates the workspace up-front.
    assert (memory.content_root / "workspace").is_dir()
