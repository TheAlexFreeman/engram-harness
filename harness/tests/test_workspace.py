"""Tests for the Workspace backend (harness/workspace.py).

Covers directory layout, CURRENT.md parser/writer round-trip, thread
lifecycle (open/update/close), closed-thread archive rotation, freeform
notes, working notes, scratch, project CRUD, and SUMMARY.md
auto-generation. The tool-layer tests live in ``test_work_tools.py``.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from harness.workspace import (
    CurrentDoc,
    ResolvedQuestion,
    Thread,
    Workspace,
    parse_current,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A Workspace rooted at an empty tmp_path with a stable session id."""
    w = Workspace(tmp_path, session_id="act-001")
    w.ensure_layout()
    return w


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def test_ensure_layout_creates_expected_tree(tmp_path: Path) -> None:
    w = Workspace(tmp_path, session_id="act-001")
    w.ensure_layout()
    assert (tmp_path / "workspace").is_dir()
    for sub in ("notes", "projects", "projects/_archive", "scratch", "archive"):
        assert (tmp_path / "workspace" / sub).is_dir()
    assert (tmp_path / "workspace" / "CURRENT.md").is_file()
    assert (tmp_path / "workspace" / ".gitignore").read_text(encoding="utf-8") == "scratch/\n"
    assert "Archived threads" in (tmp_path / "workspace" / "archive" / "threads.md").read_text(
        encoding="utf-8"
    )


def test_ensure_layout_is_idempotent(tmp_path: Path) -> None:
    """Calling ensure_layout twice must not clobber existing CURRENT.md content."""
    w = Workspace(tmp_path, session_id="act-001")
    w.ensure_layout()
    w.open_thread("auth-redesign", status="active", next_action="draft")
    before = w.current_path.read_text(encoding="utf-8")
    w.ensure_layout()
    after = w.current_path.read_text(encoding="utf-8")
    assert before == after


# ---------------------------------------------------------------------------
# CURRENT.md parser round-trip
# ---------------------------------------------------------------------------


def test_parse_current_round_trip() -> None:
    raw = (
        "## Threads\n\n"
        "### auth-redesign [active] (project: auth-redesign)\n"
        "Draft token refresh flow.\n\n"
        "### logging-audit [blocked]\n"
        "Waiting on prod log access.\n\n"
        "## Closed\n\n"
        "### data-migration — completed, 2.3M rows (2026-04-20)\n\n"
        "## Notes\n\n"
        "- `2026-04-23T08:30:00` user prefers kebab-case\n"
    )
    doc = parse_current(raw)
    assert [t.name for t in doc.threads] == ["auth-redesign", "logging-audit"]
    assert doc.threads[0].status == "active"
    assert doc.threads[0].project == "auth-redesign"
    assert doc.threads[0].next == "Draft token refresh flow."
    assert doc.threads[1].project is None
    assert doc.closed[0].name == "data-migration"
    assert doc.closed[0].closed == "2026-04-20"
    assert doc.notes[0].content == "user prefers kebab-case"

    # Render and re-parse: structure must be preserved.
    doc2 = parse_current(doc.render())
    assert [t.name for t in doc2.threads] == [t.name for t in doc.threads]
    assert [c.name for c in doc2.closed] == [c.name for c in doc.closed]
    assert [n.content for n in doc2.notes] == [n.content for n in doc.notes]


def test_parse_current_empty_doc() -> None:
    doc = parse_current("")
    assert doc.threads == []
    assert doc.closed == []
    assert doc.notes == []


def test_current_doc_find_and_close() -> None:
    doc = CurrentDoc(
        threads=[Thread(name="t1", status="active"), Thread(name="t2", status="blocked")],
    )
    assert doc.find_thread("t2").status == "blocked"
    assert doc.find_thread("unknown") is None
    closed = doc.close_thread("t1", summary="done", today="2026-04-23")
    assert closed is not None
    assert len(doc.threads) == 1
    assert doc.closed[0].name == "t1"
    assert doc.close_thread("nope", "x", today="2026-04-23") is None


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def test_open_thread_rejects_duplicates(ws: Workspace) -> None:
    ws.open_thread("t1")
    with pytest.raises(ValueError):
        ws.open_thread("t1")


def test_open_thread_rejects_unknown_project(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.open_thread("t1", project="does-not-exist")


def test_open_thread_validates_kebab_case(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.open_thread("UPPERCASE")
    with pytest.raises(ValueError):
        ws.open_thread("with spaces")


def test_update_thread_sets_status_and_next(ws: Workspace) -> None:
    ws.open_thread("t1", next_action="step A")
    ws.update_thread("t1", status="blocked", next_action="step B")
    doc = ws.read_current()
    t = doc.find_thread("t1")
    assert t.status == "blocked"
    assert t.next == "step B"


def test_update_thread_unknown_raises(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.update_thread("missing", status="active")


def test_close_thread_moves_to_closed_section(ws: Workspace) -> None:
    ws.open_thread("t1", status="active")
    ws.close_thread("t1", summary="wrapped up")
    doc = ws.read_current()
    assert doc.find_thread("t1") is None
    assert doc.closed[0].name == "t1"
    assert "wrapped up" in doc.closed[0].summary


# ---------------------------------------------------------------------------
# Closed-thread archive rotation
# ---------------------------------------------------------------------------


def test_closed_threads_archive_after_retention(tmp_path: Path) -> None:
    """Closed threads older than 7 days should rotate into archive/threads.md."""
    today = date(2026, 4, 30)
    w = Workspace(tmp_path, session_id="act-001", today_provider=lambda: today)
    w.ensure_layout()
    # Seed CURRENT.md with a mix of old and recent closed threads.
    old_date = (today - timedelta(days=10)).isoformat()
    recent_date = (today - timedelta(days=2)).isoformat()
    w.current_path.write_text(
        "## Threads\n\n"
        "## Closed\n\n"
        f"### expired-thread — old summary ({old_date})\n\n"
        f"### recent-thread — recent summary ({recent_date})\n\n"
        "## Notes\n",
        encoding="utf-8",
    )
    # A thread op triggers the rotation.
    w.jot("trigger rotation")
    doc = w.read_current()
    assert [c.name for c in doc.closed] == ["recent-thread"]
    archived = (w.archive_dir / "threads.md").read_text(encoding="utf-8")
    assert "expired-thread" in archived
    assert "old summary" in archived


# ---------------------------------------------------------------------------
# Jots and working notes
# ---------------------------------------------------------------------------


def test_jot_appends_timestamped_note(ws: Workspace) -> None:
    n = ws.jot("user prefers kebab-case")
    assert n.content == "user prefers kebab-case"
    doc = ws.read_current()
    assert doc.notes[-1].content == "user prefers kebab-case"


def test_jot_rejects_empty_content(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.jot("")
    with pytest.raises(ValueError):
        ws.jot("   ")


def test_write_note_create_and_append(ws: Workspace) -> None:
    path = ws.write_note("auth-redesign", content="## Intro\n\nFirst draft.")
    assert path.read_text(encoding="utf-8").startswith("## Intro")
    ws.write_note("auth-redesign", append="## Next\n\nSecond draft.")
    text = path.read_text(encoding="utf-8")
    assert "First draft." in text and "Second draft." in text


def test_write_note_append_requires_existing_file(ws: Workspace) -> None:
    with pytest.raises(FileNotFoundError):
        ws.write_note("no-such-note", append="fragment")


def test_write_note_rejects_both_content_and_append(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.write_note("t", content="x", append="y")
    with pytest.raises(ValueError):
        ws.write_note("t")


def test_write_note_to_project_requires_existing_project(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.write_note("alpha", content="x", project="does-not-exist")


def test_write_note_to_project_lands_under_project_notes(ws: Workspace) -> None:
    ws.project_create("p1", goal="explore p1")
    path = ws.write_note("alpha", content="scoped", project="p1")
    assert path.relative_to(ws.dir).as_posix() == "projects/p1/notes/alpha.md"


# ---------------------------------------------------------------------------
# Scratch
# ---------------------------------------------------------------------------


def test_scratch_appends_to_session_file(ws: Workspace) -> None:
    path = ws.scratch_append("first hypothesis")
    ws.scratch_append("second hypothesis")
    assert path.name == "act-001.md"
    body = path.read_text(encoding="utf-8")
    assert "first hypothesis" in body
    assert "second hypothesis" in body


def test_scratch_rejects_empty_content(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.scratch_append("")


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def test_read_file_rejects_traversal(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.read_file("../../outside.md")
    with pytest.raises(ValueError):
        ws.read_file("notes/../../outside.md")
    with pytest.raises(ValueError):
        ws.read_file("/etc/passwd")


def test_read_file_returns_contents(ws: Workspace) -> None:
    ws.write_note("alpha", content="hello")
    assert "hello" in ws.read_file("notes/alpha.md")


def test_read_file_missing(ws: Workspace) -> None:
    with pytest.raises(FileNotFoundError):
        ws.read_file("notes/nonexistent.md")


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_project_create_scaffolds_files_and_opens_thread(ws: Workspace) -> None:
    p = ws.project_create(
        "auth-redesign",
        goal="Support offline token refresh",
        questions=["Reuse session table?", "Max offline window?"],
    )
    assert p.goal_path.is_file()
    assert p.summary_path.is_file()
    assert p.questions_path.is_file()
    # Linked thread in CURRENT.md.
    doc = ws.read_current()
    t = doc.find_thread("auth-redesign")
    assert t is not None
    assert t.project == "auth-redesign"


def test_project_create_rejects_duplicates(ws: Workspace) -> None:
    ws.project_create("alpha", goal="goal-1")
    with pytest.raises(ValueError):
        ws.project_create("alpha", goal="goal-2")


def test_project_create_requires_goal(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.project_create("alpha", goal="")


def test_project_update_goal_refreshes_modified_and_summary(ws: Workspace) -> None:
    ws.project_create("alpha", goal="first goal")
    ws.project_update_goal("alpha", "second goal")
    p = ws.project("alpha")
    body = p.summary_path.read_text(encoding="utf-8")
    assert "second goal" in body
    # First goal no longer present in the current SUMMARY (git history retains it).
    assert "first goal" not in body


def test_project_ask_and_resolve(ws: Workspace) -> None:
    ws.project_create("alpha", goal="explore")
    ws.project_ask("alpha", "Does Y work with Z?")
    ws.project_ask("alpha", "What's the deadline?")
    entry = ws.project_resolve("alpha", 1, "Yes, with a caveat about timeouts.")
    assert isinstance(entry, ResolvedQuestion)
    p = ws.project("alpha")
    summary = p.summary_path.read_text(encoding="utf-8")
    assert "~Does Y work with Z?~" in summary
    assert "Yes, with a caveat about timeouts." in summary
    # Remaining open question renumbered to #1.
    assert "1. What's the deadline?" in summary


def test_project_resolve_out_of_range(ws: Workspace) -> None:
    ws.project_create("alpha", goal="explore", questions=["Q1"])
    with pytest.raises(ValueError):
        ws.project_resolve("alpha", 99, "answer")
    with pytest.raises(ValueError):
        ws.project_resolve("alpha", 0, "answer")


def test_project_list_excludes_archive_by_default(ws: Workspace) -> None:
    ws.project_create("alpha", goal="g1")
    ws.project_create("beta", goal="g2")
    ws.project_archive("alpha", summary="done")
    active = [p.name for p in ws.list_projects()]
    assert active == ["beta"]
    all_with_arc = [p.name for p in ws.list_projects(include_archived=True)]
    assert set(all_with_arc) == {"alpha", "beta"}


def test_project_archive_moves_dir_and_closes_threads(ws: Workspace) -> None:
    ws.project_create("alpha", goal="explore")
    # create_project already opens a linked thread named "alpha".
    ws.project_archive("alpha", summary="Shipped in v1.2")
    # Project dir moved.
    assert not (ws.projects_dir / "alpha").exists()
    assert (ws.projects_dir / "_archive" / "alpha").is_dir()
    # Linked thread auto-closed.
    doc = ws.read_current()
    assert doc.find_thread("alpha") is None
    assert any(c.name == "alpha" for c in doc.closed)


def test_project_archive_requires_summary(ws: Workspace) -> None:
    ws.project_create("alpha", goal="explore")
    with pytest.raises(ValueError):
        ws.project_archive("alpha", summary="")


def test_regenerate_summary_lists_project_files(ws: Workspace) -> None:
    ws.project_create("alpha", goal="explore")
    ws.write_note("token-analysis", content="notes", project="alpha")
    p = ws.project("alpha")
    ws.regenerate_summary(p)
    body = p.summary_path.read_text(encoding="utf-8")
    assert "notes/token-analysis.md" in body
    # Auto-generated files (GOAL.md, SUMMARY.md, questions.md) are excluded.
    assert "GOAL.md" not in body
    assert "SUMMARY.md" not in body
