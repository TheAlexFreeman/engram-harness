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


def test_notes_section_preserves_freeform_content_across_mutations(
    tmp_path: Path,
) -> None:
    """Freeform content under ## Notes must survive a thread/jot round-trip.

    The Notes section is documented as freeform — jots are one valid shape,
    but migrated content, paragraphs, and sub-headings can live here too.
    Earlier iterations of the parser discarded anything that didn't match
    the ``- `ts` body`` jot regex, so every thread_update or jot silently
    truncated the Notes section. This regression guard plants freeform
    content, performs a mutation, and verifies the content survived.
    """
    w = Workspace(tmp_path, session_id="act-001")
    w.ensure_layout()
    # Hand-write a CURRENT.md with a mix of structured and freeform Notes.
    w.current_path.write_text(
        "## Threads\n\n"
        "## Closed\n\n"
        "## Notes\n\n"
        "### Sub-heading under Notes\n\n"
        "A paragraph describing something important.\n"
        "It spans two lines.\n\n"
        "- `2026-04-23T08:00:00` an existing structured jot\n",
        encoding="utf-8",
    )
    # A thread op triggers read+rewrite of CURRENT.md. The freeform Notes
    # content must not be clobbered.
    w.open_thread("t1", status="active", next_action="first step")
    body_after_thread = w.current_path.read_text(encoding="utf-8")
    for needle in (
        "Sub-heading under Notes",
        "A paragraph describing something important.",
        "It spans two lines.",
        "an existing structured jot",
    ):
        assert needle in body_after_thread, f"missing after thread op: {needle!r}"
    # A subsequent jot interleaves cleanly without swallowing freeform content.
    w.jot("another observation")
    body_after_jot = w.current_path.read_text(encoding="utf-8")
    assert "Sub-heading under Notes" in body_after_jot
    assert "another observation" in body_after_jot
    # The structured ``.notes`` view still surfaces the timestamped entries.
    doc = w.read_current()
    assert any(n.content == "an existing structured jot" for n in doc.notes)
    assert any(n.content == "another observation" for n in doc.notes)


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


# ---------------------------------------------------------------------------
# Plans — Workspace backend
# ---------------------------------------------------------------------------


def test_plan_create_writes_yaml_and_run_state(ws: Workspace) -> None:
    ws.project_create("auth", goal="redesign auth")
    plan_path = ws.plan_create(
        "auth",
        "token-refresh",
        "Implement offline-capable token refresh",
        phases=[{"title": "Schema design"}, {"title": "Endpoint"}],
        budget={"max_sessions": 4, "deadline": "2026-05-01"},
    )
    assert plan_path.name == "token-refresh.yaml"
    state_path = plan_path.with_name("token-refresh.run-state.json")
    assert state_path.is_file()
    plan, state = ws.plan_load("auth", "token-refresh")
    assert plan["purpose"] == "Implement offline-capable token refresh"
    assert len(plan["phases"]) == 2
    assert plan["budget"]["max_sessions"] == 4
    assert state["status"] == "active"
    assert state["current_phase"] == 0


def test_plan_create_rejects_unknown_project(ws: Workspace) -> None:
    with pytest.raises(ValueError):
        ws.plan_create("ghost", "p1", "purpose", phases=[{"title": "a"}])


def test_plan_create_rejects_duplicate(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    ws.plan_create("p", "dup", "purpose", phases=[{"title": "a"}])
    with pytest.raises(ValueError):
        ws.plan_create("p", "dup", "again", phases=[{"title": "b"}])


def test_plan_create_rejects_invalid_phase(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    with pytest.raises(ValueError):
        ws.plan_create("p", "bad", "purpose", phases=[{}])  # missing title
    with pytest.raises(ValueError):
        ws.plan_create("p", "bad", "purpose", phases=[])  # empty list


def test_plan_create_rejects_bad_budget(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    with pytest.raises(ValueError):
        ws.plan_create(
            "p", "bad", "purpose", phases=[{"title": "a"}], budget={"deadline": "not-a-date"}
        )


def test_plan_advance_complete_progresses_phase(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    ws.plan_create(
        "p",
        "plan-a",
        "purpose",
        phases=[{"title": "P1"}, {"title": "P2"}],
    )
    r = ws.plan_advance("p", "plan-a", "complete", checkpoint="done step 1")
    assert r["report"]["action"] == "complete"
    assert r["state"]["current_phase"] == 1
    assert r["state"]["phases_completed"] == [0]
    assert r["state"]["last_checkpoint"] == "done step 1"


def test_plan_advance_final_phase_marks_completed(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    ws.plan_create("p", "plan-a", "purpose", phases=[{"title": "P1"}])
    r = ws.plan_advance("p", "plan-a", "complete")
    assert r["state"]["status"] == "completed"


def test_plan_advance_fail_records_failure(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    ws.plan_create("p", "plan-a", "purpose", phases=[{"title": "P1"}])
    r = ws.plan_advance("p", "plan-a", "fail", reason="broken build")
    assert r["report"]["action"] == "fail"
    failures = r["state"]["failure_history"]
    assert len(failures) == 1
    assert failures[0]["reason"] == "broken build"
    # Still on phase 0.
    assert r["state"]["current_phase"] == 0


def test_plan_advance_requires_approval_pauses(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    ws.plan_create(
        "p",
        "plan-a",
        "purpose",
        phases=[{"title": "gated", "requires_approval": True}],
    )
    r = ws.plan_advance("p", "plan-a", "complete")
    assert r["report"]["action"] == "awaiting_approval"
    assert r["state"]["status"] == "awaiting_approval"
    approval_id = r["report"]["approval_request_id"]

    # A model-supplied approved=True flag cannot complete the gate by itself.
    r2 = ws.plan_advance("p", "plan-a", "complete", approved=True)
    assert r2["report"]["action"] == "awaiting_approval"

    ws.plan_grant_approval("p", "plan-a", approval_id, approved_by="tester")
    r3 = ws.plan_advance("p", "plan-a", "complete", approved=True)
    assert r3["report"]["action"] == "complete"
    assert r3["state"]["status"] == "completed"


def test_plan_advance_verify_blocks_on_failed_check(ws: Workspace, tmp_path: Path) -> None:
    ws.project_create("p", goal="g")
    ws.plan_create(
        "p",
        "plan-a",
        "purpose",
        phases=[
            {
                "title": "verify phase",
                "postconditions": ["grep:nonexistent::missing.md"],
            }
        ],
    )
    r = ws.plan_advance("p", "plan-a", "complete", verify=True, cwd=tmp_path)
    assert r["report"]["action"] == "verify_failed"
    # State unchanged — still on phase 0.
    assert r["state"]["current_phase"] == 0


def test_plan_verify_postconditions_grep_hit(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "foo.py"
    target.write_text("def refresh_interval(): return 300\n", encoding="utf-8")
    phase = {"postconditions": ["grep:refresh_interval::foo.py"]}
    results = ws.plan_verify_postconditions(phase, cwd=tmp_path)
    assert len(results) == 1
    assert results[0]["kind"] == "grep"
    assert results[0]["passed"] is True


def test_plan_verify_postconditions_grep_miss(ws: Workspace, tmp_path: Path) -> None:
    phase = {"postconditions": ["grep:xyz::missing.md"]}
    results = ws.plan_verify_postconditions(phase, cwd=tmp_path)
    assert results[0]["passed"] is False
    assert "file not found" in results[0]["detail"]


def test_plan_verify_postconditions_test_pass(ws: Workspace, tmp_path: Path) -> None:
    import sys

    phase = {"postconditions": [f'test:{sys.executable} -c "import sys; sys.exit(0)"']}
    results = ws.plan_verify_postconditions(phase, cwd=tmp_path)
    assert results[0]["kind"] == "test"
    assert results[0]["passed"] is True


def test_plan_verify_postconditions_test_fail(ws: Workspace, tmp_path: Path) -> None:
    import sys

    phase = {"postconditions": [f'test:{sys.executable} -c "import sys; sys.exit(3)"']}
    results = ws.plan_verify_postconditions(phase, cwd=tmp_path)
    assert results[0]["passed"] is False
    assert "exit code 3" in results[0]["detail"]


def test_plan_verify_postconditions_manual_always_passes(ws: Workspace) -> None:
    phase = {"postconditions": ["migration lands cleanly"]}
    results = ws.plan_verify_postconditions(phase)
    assert results[0]["kind"] == "manual"
    assert results[0]["passed"] is True


def test_plan_list_summary(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    ws.plan_create("p", "plan-a", "first plan", phases=[{"title": "P1"}])
    ws.plan_create("p", "plan-b", "second plan", phases=[{"title": "Q1"}, {"title": "Q2"}])
    items = ws.plan_list("p")
    ids = {item["plan_id"] for item in items}
    assert ids == {"plan-a", "plan-b"}


def test_summary_auto_includes_active_plan(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    ws.plan_create("p", "plan-a", "active plan description", phases=[{"title": "P1"}])
    ws.regenerate_summary(ws.project("p"))
    body = ws.project("p").summary_path.read_text(encoding="utf-8")
    assert "Active plan" in body
    assert "plan-a" in body
    assert "active plan description" in body


def test_plan_create_starts_sessions_used_at_zero(ws: Workspace) -> None:
    """A freshly-created plan has done no work yet.

    If sessions_used were 1 at creation, a plan with max_sessions: 1
    would look fully consumed before any phase advanced. Codex P2 on
    PR #8.
    """
    ws.project_create("p", goal="g")
    ws.plan_create(
        "p",
        "plan-a",
        "purpose",
        phases=[{"title": "P1"}],
        budget={"max_sessions": 1},
    )
    _, state = ws.plan_load("p", "plan-a")
    assert state["sessions_used"] == 0
    assert state["sessions_touched"] == []


def test_plan_advance_increments_sessions_used_once_per_session(
    tmp_path: Path,
) -> None:
    """Repeated advances in the same session must not double-count.

    Budget tracking interprets max_sessions as "distinct harness
    sessions that interacted with the plan". Incrementing per
    advance call would make a 4-phase plan look like 4 sessions of
    work done in a single sitting.
    """
    ws_a = Workspace(tmp_path, session_id="act-A")
    ws_a.ensure_layout()
    ws_a.project_create("p", goal="g")
    ws_a.plan_create(
        "p",
        "plan-a",
        "purpose",
        phases=[{"title": "P1"}, {"title": "P2"}, {"title": "P3"}],
    )
    ws_a.plan_advance("p", "plan-a", "complete")
    ws_a.plan_advance("p", "plan-a", "fail", reason="oops")
    ws_a.plan_advance("p", "plan-a", "complete")
    _, state = ws_a.plan_load("p", "plan-a")
    assert state["sessions_used"] == 1
    assert state["sessions_touched"] == ["act-A"]

    # Second session takes over — sessions_used goes to 2.
    ws_b = Workspace(tmp_path, session_id="act-B")
    ws_b.plan_advance("p", "plan-a", "complete")
    _, state = ws_b.plan_load("p", "plan-a")
    assert state["sessions_used"] == 2
    assert state["sessions_touched"] == ["act-A", "act-B"]


def test_plan_advance_without_session_id_does_not_track(tmp_path: Path) -> None:
    """Workspaces without a session_id (standalone smoke tests) skip tracking.

    sessions_used stays at its last known value rather than growing
    unboundedly with every advance.
    """
    ws = Workspace(tmp_path, session_id=None)
    ws.ensure_layout()
    ws.project_create("p", goal="g")
    ws.plan_create("p", "plan-a", "purpose", phases=[{"title": "P1"}, {"title": "P2"}])
    ws.plan_advance("p", "plan-a", "complete")
    ws.plan_advance("p", "plan-a", "complete")
    _, state = ws.plan_load("p", "plan-a")
    assert state["sessions_used"] == 0
    assert state["sessions_touched"] == []


# ---------------------------------------------------------------------------
# list_active_plans — single source of truth for the workspace plan scan
# ---------------------------------------------------------------------------


def test_list_active_plans_returns_empty_when_no_plans(ws: Workspace) -> None:
    assert ws.list_active_plans() == []


def test_list_active_plans_returns_only_active_plans(ws: Workspace) -> None:
    ws.project_create("p", goal="g")
    ws.plan_create("p", "active-a", "active plan", phases=[{"title": "P1"}])
    ws.plan_create("p", "done-a", "done plan", phases=[{"title": "P1"}])
    # Mark the second plan completed.
    state_path = (
        ws.dir / "projects" / "p" / "plans" / "done-a.run-state.json"
    )
    import json as _json

    state = _json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "completed"
    state_path.write_text(_json.dumps(state), encoding="utf-8")

    found = ws.list_active_plans()
    assert [ap.plan_id for ap in found] == ["active-a"]


def test_list_active_plans_sorts_most_recently_modified_first(
    ws: Workspace, tmp_path: Path
) -> None:
    import os as _os

    ws.project_create("p", goal="g")
    ws.plan_create("p", "older", "older plan", phases=[{"title": "P1"}])
    older_state = ws.dir / "projects" / "p" / "plans" / "older.run-state.json"
    _os.utime(older_state, (1_700_000_000, 1_700_000_000))
    ws.plan_create("p", "newer", "newer plan", phases=[{"title": "P1"}])

    found = ws.list_active_plans()
    assert [ap.plan_id for ap in found] == ["newer", "older"]


def test_list_active_plans_tolerates_malformed_run_state(
    ws: Workspace,
) -> None:
    """A broken JSON file shouldn't take out the whole listing."""
    ws.project_create("p", goal="g")
    ws.plan_create("p", "good", "fine plan", phases=[{"title": "P1"}])
    bad = ws.dir / "projects" / "p" / "plans" / "broken.run-state.json"
    bad.write_text("{not: valid json", encoding="utf-8")
    # Sibling YAML so the path looks plausible.
    bad.with_suffix(".yaml").parent  # noqa: B018 — keep import-style consistency
    (ws.dir / "projects" / "p" / "plans" / "broken.yaml").write_text(
        "plan_id: broken\npurpose: x\nphases: []\n", encoding="utf-8"
    )

    found = ws.list_active_plans()
    assert [ap.plan_id for ap in found] == ["good"]


def test_list_active_plans_carries_plan_doc_and_state(ws: Workspace) -> None:
    """Callers should be able to render briefings without re-reading disk."""
    ws.project_create("p", goal="g")
    ws.plan_create(
        "p",
        "ship-it",
        "Ship the thing",
        phases=[{"title": "Phase A"}, {"title": "Phase B"}],
    )
    found = ws.list_active_plans()
    assert len(found) == 1
    ap = found[0]
    assert ap.project == "p"
    assert ap.plan_id == "ship-it"
    assert ap.plan_doc.get("purpose") == "Ship the thing"
    assert ap.run_state.get("status") == "active"
    assert ap.state_path.is_file()
