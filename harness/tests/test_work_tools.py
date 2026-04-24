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
    WorkProjectPlan,
    WorkProjectResolve,
    WorkProjectStatus,
    WorkPromote,
    WorkRead,
    WorkScratch,
    WorkSearch,
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


def test_project_status_reads_existing_summary_without_regenerating(ws: Workspace) -> None:
    WorkProjectCreate(ws).run({"name": "alpha", "goal": "explore alpha", "questions": ["Q1"]})
    summary = ws.project("alpha").summary_path
    summary.write_text("# stale\n\nDo not overwrite this read.\n", encoding="utf-8")

    out_project = WorkProjectStatus(ws).run({"name": "alpha"})
    out_status = WorkStatus(ws).run({"project": "alpha"})

    assert "Do not overwrite this read" in out_project
    assert "Do not overwrite this read" in out_status
    assert summary.read_text(encoding="utf-8").startswith("# stale")


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
        "work_search",
        "work_project_list",
        "work_project_status",
    }
    expected_mutating = {
        "work_thread",
        "work_jot",
        "work_note",
        "work_scratch",
        "work_promote",
        "work_project_create",
        "work_project_goal",
        "work_project_ask",
        "work_project_resolve",
        "work_project_archive",
        "work_project_plan",
    }
    all_tools = (
        WorkStatus,
        WorkThread,
        WorkJot,
        WorkNote,
        WorkRead,
        WorkSearch,
        WorkScratch,
        WorkPromote,
        WorkProjectCreate,
        WorkProjectGoal,
        WorkProjectAsk,
        WorkProjectResolve,
        WorkProjectList,
        WorkProjectStatus,
        WorkProjectArchive,
        WorkProjectPlan,
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
    assert "work_promote" not in names
    assert "work_project_create" not in names
    assert "work_project_archive" not in names
    assert "work_project_plan" not in names
    # Read-only ones stay.
    assert "work_status" in names
    assert "work_read" in names
    assert "work_search" in names
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
        "work_search",
        "work_scratch",
        "work_promote",
        "work_project_create",
        "work_project_goal",
        "work_project_ask",
        "work_project_resolve",
        "work_project_list",
        "work_project_status",
        "work_project_archive",
        "work_project_plan",
    ):
        assert expected in names, f"missing in full profile: {expected}"
    # Full profile creates the workspace up-front.
    assert (memory.content_root / "workspace").is_dir()


def test_build_memory_disables_test_postconditions_under_no_shell(tmp_path: Path) -> None:
    from harness.config import SessionConfig, ToolProfile, _build_memory
    from harness.tests.test_engram_memory import _make_engram_repo

    repo = _make_engram_repo(tmp_path)
    config = SessionConfig(
        workspace=tmp_path / "scratch-ws",
        memory_backend="engram",
        memory_repo=repo,
        tool_profile=ToolProfile.NO_SHELL,
    )
    _memory, _engram, extras = _build_memory(config)
    plan_tool = next(t for t in extras if t.name == "work_project_plan")
    assert plan_tool._allow_test_postconditions is False


# ---------------------------------------------------------------------------
# work_search
# ---------------------------------------------------------------------------


def test_search_requires_non_empty_query(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        WorkSearch(ws).run({"query": ""})


def test_search_returns_manifest_with_matches(ws: Workspace) -> None:
    ws.project_create("alpha", goal="support token refresh flows")
    ws.write_note(
        "auth-notes",
        content="token refresh relies on the existing session table",
        project="alpha",
    )
    ws.project_create("beta", goal="migration planning for billing")
    ws.write_note("schema", content="migrate payments table", project="beta")

    out = WorkSearch(ws).run({"query": "token refresh", "k": 5})
    assert "workspace search" in out
    assert "projects/alpha/notes/auth-notes.md" in out
    # beta project doesn't match this query.
    assert "projects/beta/" not in out


def test_search_scoped_to_single_project(ws: Workspace) -> None:
    ws.project_create("alpha", goal="alpha goal")
    ws.write_note("a-note", content="useful content about X", project="alpha")
    ws.project_create("beta", goal="unrelated")
    ws.write_note("b-note", content="useful content about X", project="beta")

    out_alpha = WorkSearch(ws).run({"query": "useful content", "project": "alpha"})
    assert "projects/alpha/notes/a-note.md" in out_alpha
    assert "projects/beta/" not in out_alpha

    out_beta = WorkSearch(ws).run({"query": "useful content", "project": "beta"})
    assert "projects/beta/notes/b-note.md" in out_beta
    assert "projects/alpha/" not in out_beta


def test_search_no_matches_friendly_message(ws: Workspace) -> None:
    ws.project_create("alpha", goal="some goal")
    out = WorkSearch(ws).run({"query": "xyzzy-no-match"})
    assert "no matches for" in out


def test_search_clamps_k(ws: Workspace) -> None:
    """k out of range should silently clamp, not raise."""
    ws.project_create("alpha", goal="populate")
    for i in range(3):
        ws.write_note(f"note-{i}", content="fill content hit keyword", project="alpha")
    # k too high: should clamp to max, not raise
    WorkSearch(ws).run({"query": "keyword", "k": 9999})
    # k too low: clamps to min
    WorkSearch(ws).run({"query": "keyword", "k": -1})


def test_search_skips_summary_md(ws: Workspace) -> None:
    """SUMMARY.md is auto-generated and should not appear in results."""
    ws.project_create("alpha", goal="unique-phrase-only-in-goal and summary")
    ws.regenerate_summary(ws.project("alpha"))
    out = WorkSearch(ws).run({"query": "unique-phrase-only-in-goal"})
    assert "projects/alpha/GOAL.md" in out
    assert "SUMMARY.md" not in out


def test_search_matches_two_char_acronyms(ws: Workspace) -> None:
    """Acronyms like UI, DB, CI must be searchable (Codex P2 regression).

    The prior ``len(t) > 2`` filter silently dropped 2-char tokens from
    the query, so a search for ``UI`` returned nothing even when
    matching files existed. Make sure the common acronym case works.
    """
    ws.project_create(
        "alpha",
        goal="explore the UI and the DB interactions end-to-end",
    )
    ws.write_note(
        "ui-notes",
        content="UI components: header, sidebar, content panel",
        project="alpha",
    )
    ws.write_note(
        "db-notes",
        content="DB schema design for the session tables",
        project="alpha",
    )
    # 2-char single-token query.
    out_ui = WorkSearch(ws).run({"query": "UI"})
    assert "ui-notes.md" in out_ui
    out_db = WorkSearch(ws).run({"query": "DB"})
    assert "db-notes.md" in out_db


def test_promote_preserves_leading_thematic_break(ws: Workspace, engram: EngramMemory) -> None:
    """A note starting with a Markdown thematic break must not be stripped.

    ``---`` can open frontmatter or mark a thematic break. Under the
    naive strip rule ("first two ``---`` lines delimit frontmatter"),
    any note that uses a thematic break at the top would have real
    content silently deleted when promoted. The stricter rule only
    strips when the YAML between delimiters parses to a non-empty
    dict.
    """
    ws.write_note(
        "with-break",
        content=(
            "---\n\n# Real Title\n\nThis is important content.\n\n---\n\nMore content follows.\n"
        ),
    )
    WorkPromote(ws, engram).run({"path": "notes/with-break.md", "dest": "knowledge/preserved.md"})
    dest_abs = engram.content_root / "memory" / "knowledge" / "preserved.md"
    body = dest_abs.read_text(encoding="utf-8")
    # Title and content must survive the promote.
    assert "# Real Title" in body
    assert "This is important content." in body
    assert "More content follows." in body


def test_promote_ignores_non_dict_yaml_block(ws: Workspace, engram: EngramMemory) -> None:
    """A `---...---` block whose content isn't a YAML mapping is preserved."""
    ws.write_note(
        "plain-text-block",
        content=(
            "---\njust a plain paragraph\nnot a key-value map\n---\n\nBody after the break.\n"
        ),
    )
    WorkPromote(ws, engram).run({"path": "notes/plain-text-block.md", "dest": "knowledge/plain.md"})
    body = (engram.content_root / "memory" / "knowledge" / "plain.md").read_text(encoding="utf-8")
    # The fresh frontmatter block is there, but the thematic break and
    # its content inside the body are preserved.
    assert "just a plain paragraph" in body
    assert "Body after the break." in body


# ---------------------------------------------------------------------------
# work_promote
# ---------------------------------------------------------------------------


def test_promote_copies_workspace_note_to_memory(ws: Workspace, engram: EngramMemory) -> None:
    ws.write_note(
        "auth-redesign",
        content="# Auth redesign\n\nToken refresh flow needs a dedicated table.",
    )
    tool = WorkPromote(ws, engram)
    out = tool.run(
        {
            "path": "notes/auth-redesign.md",
            "dest": "knowledge/architecture/auth-redesign.md",
        }
    )
    assert "Promoted" in out
    dest_abs = engram.content_root / "memory" / "knowledge" / "architecture" / "auth-redesign.md"
    assert dest_abs.is_file()
    body = dest_abs.read_text(encoding="utf-8")
    # Frontmatter applied.
    assert body.startswith("---\n")
    assert "source: agent-generated" in body
    assert "trust: medium" in body
    assert "origin_workspace: workspace/notes/auth-redesign.md" in body
    # Body content preserved.
    assert "Token refresh flow needs a dedicated table." in body
    # Workspace file remains (one-way copy).
    assert (ws.notes_dir / "auth-redesign.md").is_file()


def test_promote_strips_source_frontmatter(ws: Workspace, engram: EngramMemory) -> None:
    """If the workspace file has frontmatter, it must be stripped before wrap."""
    note = ws.write_note(
        "prefixed",
        content="---\nsomekey: somevalue\n---\n\n# Real body\n\nContent here.",
    )
    assert note.is_file()
    tool = WorkPromote(ws, engram)
    tool.run({"path": "notes/prefixed.md", "dest": "knowledge/sample.md"})
    dest_abs = engram.content_root / "memory" / "knowledge" / "sample.md"
    body = dest_abs.read_text(encoding="utf-8")
    # Source frontmatter is gone (only the new fresh one remains).
    assert "somekey: somevalue" not in body
    assert "# Real body" in body


def test_promote_rejects_missing_workspace_file(ws: Workspace, engram: EngramMemory) -> None:
    out = WorkPromote(ws, engram).run({"path": "notes/ghost.md", "dest": "knowledge/anywhere.md"})
    assert "no such workspace file" in out


def test_promote_rejects_non_md_dest(ws: Workspace, engram: EngramMemory) -> None:
    ws.write_note("x", content="body")
    with pytest.raises(ValueError):
        WorkPromote(ws, engram).run({"path": "notes/x.md", "dest": "knowledge/x.txt"})


def test_promote_rejects_dest_outside_memory(ws: Workspace, engram: EngramMemory) -> None:
    ws.write_note("x", content="body")
    with pytest.raises(ValueError):
        WorkPromote(ws, engram).run({"path": "notes/x.md", "dest": "../escape.md"})


def test_promote_refuses_to_overwrite(ws: Workspace, engram: EngramMemory) -> None:
    """Memory files are governed; promote should never silently clobber."""
    # Create a memory file directly.
    dest_rel = "knowledge/existing.md"
    dest_abs = engram.content_root / "memory" / dest_rel
    dest_abs.parent.mkdir(parents=True, exist_ok=True)
    dest_abs.write_text("---\ntrust: high\n---\n\nSomething precious.\n", encoding="utf-8")
    ws.write_note("fresh", content="new content")
    with pytest.raises(ValueError):
        WorkPromote(ws, engram).run({"path": "notes/fresh.md", "dest": dest_rel})


def test_promote_rejects_invalid_trust(ws: Workspace, engram: EngramMemory) -> None:
    ws.write_note("x", content="body")
    with pytest.raises(ValueError):
        WorkPromote(ws, engram).run(
            {"path": "notes/x.md", "dest": "knowledge/x.md", "trust": "banana"}
        )


def test_promote_commits_to_engram_repo(ws: Workspace, engram: EngramMemory) -> None:
    """The promoted file should land as its own git commit with the [chat] prefix."""
    import subprocess

    ws.write_note("note", content="new durable knowledge")
    WorkPromote(ws, engram).run({"path": "notes/note.md", "dest": "knowledge/new.md"})
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(engram.repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[chat] promote" in log.stdout
    assert "knowledge/new.md" in log.stdout


# ---------------------------------------------------------------------------
# work_project_plan — op-dispatched create / brief / advance / list
# ---------------------------------------------------------------------------


def _mk_plan_tool(ws: Workspace, engram: EngramMemory) -> WorkProjectPlan:
    # verify_cwd=None lets grep/test checks resolve against the process cwd
    # (fine for tests that don't exercise automated verification).
    return WorkProjectPlan(ws, engram=engram, verify_cwd=None)


def test_plan_tool_op_required(ws: Workspace, engram: EngramMemory) -> None:
    with pytest.raises(ValueError):
        _mk_plan_tool(ws, engram).run({})
    with pytest.raises(ValueError):
        _mk_plan_tool(ws, engram).run({"op": "not-an-op"})


def test_plan_tool_create_emits_trace_and_refreshes_summary(
    ws: Workspace, engram: EngramMemory
) -> None:
    ws.project_create("auth", goal="redesign")
    out = _mk_plan_tool(ws, engram).run(
        {
            "op": "create",
            "project": "auth",
            "plan_id": "token",
            "purpose": "Implement offline-capable token refresh",
            "phases": [{"title": "Schema"}, {"title": "Endpoint"}],
            "budget": {"max_sessions": 3},
        }
    )
    assert "Created plan" in out
    events = engram.trace_events
    assert any(ev.event == "plan_create" and "auth/token" in ev.reason for ev in events)
    # SUMMARY.md should now mention the active plan.
    summary = ws.project("auth").summary_path.read_text(encoding="utf-8")
    assert "token" in summary
    assert "active" in summary


def test_plan_tool_brief_shows_current_phase(ws: Workspace, engram: EngramMemory) -> None:
    ws.project_create("auth", goal="g")
    _mk_plan_tool(ws, engram).run(
        {
            "op": "create",
            "project": "auth",
            "plan_id": "p",
            "purpose": "first plan",
            "phases": [
                {"title": "Phase one", "postconditions": ["grep:foo::bar.py"]},
                {"title": "Phase two"},
            ],
        }
    )
    out = _mk_plan_tool(ws, engram).run({"op": "brief", "project": "auth", "plan_id": "p"})
    assert "Phase one" in out
    assert "[grep]" in out
    assert "Status: **active**" in out


def test_plan_tool_brief_missing_plan_friendly(ws: Workspace, engram: EngramMemory) -> None:
    ws.project_create("auth", goal="g")
    out = _mk_plan_tool(ws, engram).run({"op": "brief", "project": "auth", "plan_id": "ghost"})
    assert "plan not found" in out


def test_plan_tool_advance_complete_emits_trace(ws: Workspace, engram: EngramMemory) -> None:
    ws.project_create("auth", goal="g")
    _mk_plan_tool(ws, engram).run(
        {
            "op": "create",
            "project": "auth",
            "plan_id": "p",
            "purpose": "first plan",
            "phases": [{"title": "P1"}, {"title": "P2"}],
        }
    )
    out = _mk_plan_tool(ws, engram).run(
        {
            "op": "advance",
            "project": "auth",
            "plan_id": "p",
            "action": "complete",
            "checkpoint": "step 1 done",
        }
    )
    assert "Completed phase 1" in out
    events = [ev for ev in engram.trace_events if ev.event == "plan_advance"]
    assert events
    assert "action=complete" in events[-1].detail


def test_plan_tool_advance_fail_records_failure(ws: Workspace, engram: EngramMemory) -> None:
    ws.project_create("auth", goal="g")
    _mk_plan_tool(ws, engram).run(
        {
            "op": "create",
            "project": "auth",
            "plan_id": "p",
            "purpose": "x",
            "phases": [{"title": "P1"}],
        }
    )
    out = _mk_plan_tool(ws, engram).run(
        {
            "op": "advance",
            "project": "auth",
            "plan_id": "p",
            "action": "fail",
            "reason": "broken build",
        }
    )
    assert "Recorded failure" in out
    assert "broken build" in out


def test_plan_tool_advance_awaiting_approval_message(ws: Workspace, engram: EngramMemory) -> None:
    ws.project_create("auth", goal="g")
    _mk_plan_tool(ws, engram).run(
        {
            "op": "create",
            "project": "auth",
            "plan_id": "p",
            "purpose": "x",
            "phases": [{"title": "gated", "requires_approval": True}],
        }
    )
    out = _mk_plan_tool(ws, engram).run(
        {"op": "advance", "project": "auth", "plan_id": "p", "action": "complete"}
    )
    assert "requires user approval" in out
    _plan, state = ws.plan_load("auth", "p")
    approval_id = state["pending_approval"]["id"]

    # A model-supplied approved=True flag does not complete the gate by itself.
    out2 = _mk_plan_tool(ws, engram).run(
        {
            "op": "advance",
            "project": "auth",
            "plan_id": "p",
            "action": "complete",
            "approved": True,
        }
    )
    assert "requires user approval" in out2

    ws.plan_grant_approval("auth", "p", approval_id, approved_by="test-user")
    out3 = _mk_plan_tool(ws, engram).run(
        {"op": "advance", "project": "auth", "plan_id": "p", "action": "complete"}
    )
    assert "complete" in out3.lower()


def test_plan_tool_advance_verify_failure_blocks(
    ws: Workspace, engram: EngramMemory, tmp_path: Path
) -> None:
    ws.project_create("auth", goal="g")
    _mk_plan_tool(ws, engram).run(
        {
            "op": "create",
            "project": "auth",
            "plan_id": "p",
            "purpose": "x",
            "phases": [
                {
                    "title": "verifier",
                    "postconditions": ["grep:xyz::does-not-exist.md"],
                }
            ],
        }
    )
    # Use a tool whose verify_cwd is tmp_path (empty directory).
    tool = WorkProjectPlan(ws, engram=engram, verify_cwd=tmp_path)
    out = tool.run(
        {
            "op": "advance",
            "project": "auth",
            "plan_id": "p",
            "action": "complete",
            "verify": True,
        }
    )
    assert "not advanced" in out
    assert "grep" in out


def test_plan_tool_no_shell_profile_blocks_test_postcondition(
    ws: Workspace,
    engram: EngramMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws.project_create("auth", goal="g")
    _mk_plan_tool(ws, engram).run(
        {
            "op": "create",
            "project": "auth",
            "plan_id": "p",
            "purpose": "x",
            "phases": [{"title": "verifier", "postconditions": ["test:echo should-not-run"]}],
        }
    )

    called = False

    def _fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr("harness.workspace.subprocess.run", _fake_run)
    tool = WorkProjectPlan(
        ws,
        engram=engram,
        verify_cwd=tmp_path,
        allow_test_postconditions=False,
    )
    out = tool.run(
        {
            "op": "advance",
            "project": "auth",
            "plan_id": "p",
            "action": "complete",
            "verify": True,
        }
    )
    assert "not advanced" in out
    assert "disabled by the current tool profile" in out
    assert called is False


def test_plan_tool_list_shows_progress(ws: Workspace, engram: EngramMemory) -> None:
    ws.project_create("auth", goal="g")
    _mk_plan_tool(ws, engram).run(
        {
            "op": "create",
            "project": "auth",
            "plan_id": "alpha",
            "purpose": "first",
            "phases": [{"title": "X"}, {"title": "Y"}],
        }
    )
    _mk_plan_tool(ws, engram).run(
        {
            "op": "create",
            "project": "auth",
            "plan_id": "beta",
            "purpose": "second",
            "phases": [{"title": "P"}],
        }
    )
    out = _mk_plan_tool(ws, engram).run({"op": "list", "project": "auth"})
    assert "alpha" in out
    assert "beta" in out
    assert "active" in out


def test_plan_tool_list_empty(ws: Workspace, engram: EngramMemory) -> None:
    ws.project_create("auth", goal="g")
    out = _mk_plan_tool(ws, engram).run({"op": "list", "project": "auth"})
    assert "no plans" in out
