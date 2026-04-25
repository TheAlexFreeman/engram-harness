"""Tests for harness.engram_memory.EngramMemory and detect_engram_repo."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from harness.engram_memory import EngramMemory, detect_engram_repo


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)


def _make_engram_repo(tmp: Path) -> Path:
    """Build a minimal Engram-shaped repo: <tmp>/core/memory/HOME.md and friends."""
    repo = tmp
    core = repo / "core"
    mem = core / "memory"
    (mem / "users").mkdir(parents=True)
    (mem / "knowledge").mkdir()
    (mem / "skills").mkdir()
    (mem / "activity").mkdir()
    (mem / "working").mkdir()

    (mem / "HOME.md").write_text(
        "# Home\n\nWelcome. Routing notes for testing.\n", encoding="utf-8"
    )
    (mem / "users" / "SUMMARY.md").write_text(
        "# Users\n\n- Tester (the default test user)\n", encoding="utf-8"
    )
    (mem / "activity" / "SUMMARY.md").write_text("# Activity\n\nNothing yet.\n", encoding="utf-8")
    (mem / "knowledge" / "ssr.md").write_text(
        "---\nsource: user-stated\ntrust: high\n---\n\n# SSR\n\nServer-side rendering notes.\n",
        encoding="utf-8",
    )
    (mem / "knowledge" / "celery.md").write_text(
        "---\nsource: agent-generated\ntrust: medium\n---\n\n# Celery\n\nDistributed task queue notes.\n",
        encoding="utf-8",
    )

    _git_init(repo)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    return repo


@pytest.fixture
def engram_repo(tmp_path: Path) -> Path:
    return _make_engram_repo(tmp_path)


def test_detect_engram_repo_finds_core_layout(engram_repo: Path) -> None:
    found = detect_engram_repo(engram_repo)
    assert found == engram_repo


def test_detect_engram_repo_walks_up(engram_repo: Path, tmp_path: Path) -> None:
    nested = engram_repo / "core" / "memory" / "knowledge"
    found = detect_engram_repo(nested)
    # Walking up from a deep path returns the first ancestor whose layout
    # is recognised. `<engram_repo>/core` directly contains `memory/HOME.md`,
    # so it is matched before the outer `engram_repo` (which would need the
    # `core/` prefix). Both are valid roots and both work with EngramMemory.
    assert found in {engram_repo, engram_repo / "core"}


def test_engram_memory_bootstrap(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False)
    assert mem.session_id == "act-001"
    ctx = mem.start_session("optimize the celery worker pool")
    assert "memory/HOME.md" in ctx
    assert "Users" in ctx
    assert mem.task == "optimize the celery worker pool"


def test_bootstrap_is_task_independent(engram_repo: Path) -> None:
    """Bootstrap no longer runs a task-based search — that's memory_context's job.

    The knowledge fixture contains ``celery.md``; the old bootstrap would have
    surfaced it under a ``## Task-relevant excerpts`` header. Under the new
    split, that header must be absent and the agent fetches celery context
    via ``memory_context`` when it decides the task calls for it.
    """
    mem = EngramMemory(engram_repo, embed=False)
    bootstrap = mem.start_session("optimize the celery worker pool")
    assert "Task-relevant excerpts" not in bootstrap
    # celery.md from the fixture should not be front-loaded by the bootstrap.
    assert "celery.md" not in bootstrap
    # But memory_context can still reach it on demand.
    ctx_out = mem.context(["domain:celery"], budget="S")
    assert "celery" in ctx_out.lower()


def test_bootstrap_identical_across_tasks(engram_repo: Path) -> None:
    """Two sessions opened with different tasks get the same primer body.

    The task string is included verbatim in the header; everything else
    (HOME, users, activity, working scratchpads) is task-independent and
    must render identically so the agent's bootstrap stays deterministic.
    """
    mem_a = EngramMemory(engram_repo, embed=False)
    mem_b = EngramMemory(engram_repo, embed=False)
    a = mem_a.start_session("optimize celery worker pool")
    b = mem_b.start_session("improve ssr caching")
    # Strip the header's Task line before comparing.
    body_a = "\n".join(line for line in a.splitlines() if not line.startswith("Task:"))
    body_b = "\n".join(line for line in b.splitlines() if not line.startswith("Task:"))
    assert body_a == body_b


def test_engram_memory_recall_keyword(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("celery tuning")
    hits = mem.recall("celery worker pool", k=3)
    assert hits, "expected keyword hit on celery.md"
    paths = [h.content.split("\n", 1)[0] for h in hits]
    assert any("celery.md" in p for p in paths)
    assert mem.recall_events  # logged for trace bridge


def test_engram_memory_record_and_end_session(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("test session")
    mem.record("read_file failed: missing.md", kind="error")
    mem.record("noted user preference for terse output", kind="note")
    mem.end_session("Test summary text.")

    summary_path = engram_repo / "core" / mem.session_dir_rel / "summary.md"
    assert summary_path.is_file(), f"summary not written: {summary_path}"
    text = summary_path.read_text(encoding="utf-8")
    assert "Test summary text." in text
    assert "[error]" in text
    assert "[note]" in text
    # Frontmatter present
    assert text.startswith("---\n")
    assert "source: agent-generated" in text
    assert "trust: medium" in text
    # Commit landed
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(engram_repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "[chat] harness session" in log.stdout


def test_engram_memory_bumps_session_id(engram_repo: Path) -> None:
    first = EngramMemory(engram_repo, embed=False)
    first.start_session("first")
    first.end_session("done")
    second = EngramMemory(engram_repo, embed=False)
    assert second.session_id == "act-002"


def test_end_session_defer_artifacts_skips_file_but_stores_summary(
    engram_repo: Path,
) -> None:
    """defer_artifacts=True hands ownership to the trace bridge.

    The summary string is still captured on the memory object so the
    bridge can render it; nothing lands on disk under the session dir.
    """
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("deferred wrap-up")
    mem.end_session("agent wrap-up text", defer_artifacts=True)

    summary_path = engram_repo / "core" / mem.session_dir_rel / "summary.md"
    assert not summary_path.exists(), "deferred path should not write summary.md"
    assert mem.session_summary == "agent wrap-up text"


def test_end_session_defer_artifacts_does_not_commit(engram_repo: Path) -> None:
    """defer_artifacts implies no commit even if skip_commit isn't set."""
    before = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(engram_repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    mem = EngramMemory(engram_repo, embed=False)
    mem.start_session("no-commit")
    mem.end_session("nope", defer_artifacts=True)
    after = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(engram_repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert before == after


def test_engram_memory_rejects_missing_repo(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        EngramMemory(tmp_path / "no-such-dir", embed=False)


def test_engram_memory_explicit_prefix(tmp_path: Path) -> None:
    """--memory-repo points at the parent directory; content_prefix='core' resolves."""
    repo = _make_engram_repo(tmp_path)
    mem = EngramMemory(repo, content_prefix="core", embed=False)
    assert mem.content_root == (repo / "core").resolve()


# ---------------------------------------------------------------------------
# _active_plan_briefing — workspace layout (workspace is a peer of memory,
# not a subdirectory of the engram content root)
# ---------------------------------------------------------------------------


def _seed_workspace_plan(
    workspace_parent: Path,
    *,
    project: str,
    plan_id: str,
    purpose: str,
    phases: list[dict],
    status: str = "active",
    current_phase: int = 0,
    body_override: str | None = None,
) -> Path:
    """Write a plan + run-state pair under ``workspace_parent/workspace``.

    Returns the workspace directory itself so tests can pass it straight
    into ``EngramMemory(workspace_dir=...)``.
    """
    from harness.workspace import Workspace

    ws = Workspace(workspace_parent, session_id="act-999")
    ws.ensure_layout()
    if not ws.project(project).exists():
        ws.project_create(project, goal=f"fixture for {purpose}")
    ws.plan_create(project, plan_id, purpose, phases=phases)
    state_path = (
        workspace_parent
        / "workspace"
        / "projects"
        / project
        / "plans"
        / f"{plan_id}.run-state.json"
    )
    import json as _json

    if body_override is None:
        state = _json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = status
        state["current_phase"] = current_phase
        state_path.write_text(_json.dumps(state), encoding="utf-8")
    else:
        state_path.write_text(body_override, encoding="utf-8")
    return ws.dir


def test_active_plan_briefing_returns_empty_when_no_workspace(engram_repo: Path) -> None:
    """Without ``workspace_dir`` the bootstrap silently skips the briefing."""
    mem = EngramMemory(engram_repo, embed=False)
    assert mem._active_plan_briefing() == ""


def test_active_plan_briefing_returns_empty_when_no_plans(
    engram_repo: Path, tmp_path: Path
) -> None:
    workspace_dir = tmp_path / "ws_root" / "workspace"
    workspace_dir.mkdir(parents=True)
    mem = EngramMemory(engram_repo, embed=False, workspace_dir=workspace_dir)
    assert mem._active_plan_briefing() == ""


def test_active_plan_briefing_picks_the_active_plan(
    engram_repo: Path, tmp_path: Path
) -> None:
    workspace_dir = _seed_workspace_plan(
        tmp_path / "ws_root",
        project="alpha",
        plan_id="offline-refresh",
        purpose="Implement offline-capable token refresh",
        phases=[{"title": "Schema design"}, {"title": "Endpoint"}],
    )
    mem = EngramMemory(engram_repo, embed=False, workspace_dir=workspace_dir)
    out = mem._active_plan_briefing()
    assert "offline-refresh" in out
    assert "Implement offline-capable token refresh" in out
    assert "Schema design" in out
    # Pointer to the replacement tool, not the retired resume_plan.
    assert "work_project_plan" in out
    assert "resume_plan" not in out


def test_active_plan_briefing_skips_completed_plans(
    engram_repo: Path, tmp_path: Path
) -> None:
    workspace_dir = _seed_workspace_plan(
        tmp_path / "ws_root",
        project="alpha",
        plan_id="shipped",
        purpose="already done",
        phases=[{"title": "X"}],
        status="completed",
    )
    mem = EngramMemory(engram_repo, embed=False, workspace_dir=workspace_dir)
    assert mem._active_plan_briefing() == ""


def test_active_plan_briefing_tolerates_malformed_run_state(
    engram_repo: Path, tmp_path: Path
) -> None:
    workspace_dir = _seed_workspace_plan(
        tmp_path / "ws_root",
        project="alpha",
        plan_id="broken",
        purpose="x",
        phases=[{"title": "A"}],
        body_override="{not: valid json",
    )
    mem = EngramMemory(engram_repo, embed=False, workspace_dir=workspace_dir)
    # Skips the broken file rather than raising.
    assert mem._active_plan_briefing() == ""


def test_active_plan_briefing_tolerates_stat_errors(
    engram_repo: Path, tmp_path: Path
) -> None:
    """A stale symlink or vanished run-state file must not raise OSError.

    glob() + stat() is racy: a deleted file between glob and sort
    would otherwise abort start_session(). Regression guard from the
    PR #9 Codex review. We plant a broken symlink alongside a real
    active plan and assert the briefing still surfaces the real one.
    """
    workspace_dir = _seed_workspace_plan(
        tmp_path / "ws_root",
        project="alpha",
        plan_id="real",
        purpose="valid plan",
        phases=[{"title": "Ship"}],
    )
    # Plant a broken symlink where glob() will see it but stat() blows up.
    plans_dir = workspace_dir / "projects" / "alpha" / "plans"
    bad_link = plans_dir / "ghost.run-state.json"
    try:
        bad_link.symlink_to(plans_dir / "does-not-exist-anywhere.json")
    except (OSError, NotImplementedError):
        # Windows without symlink privilege — simulate by touching and
        # immediately removing the file after glob by using a file
        # whose parent has disappeared. Fall back to deleting the
        # target we'd stat: overwrite run-state with an unreadable
        # mode. Simpler: skip the os-dependent setup and directly
        # prove the sort doesn't crash on a missing stat by removing
        # a seeded file mid-flow — covered instead by the unit layer
        # below.
        import pytest

        pytest.skip("symlink creation unavailable on this platform")
    try:
        mem = EngramMemory(engram_repo, embed=False, workspace_dir=workspace_dir)
        out = mem._active_plan_briefing()
        assert "real" in out
        assert "valid plan" in out
    finally:
        bad_link.unlink(missing_ok=True)


def test_active_plan_briefing_prefers_most_recently_modified(
    engram_repo: Path, tmp_path: Path
) -> None:
    import os as _os

    workspace_parent = tmp_path / "ws_root"
    workspace_dir = _seed_workspace_plan(
        workspace_parent,
        project="alpha",
        plan_id="older",
        purpose="stale",
        phases=[{"title": "X"}],
    )
    # Bump mtime on the older plan so it looks ancient, then create a newer
    # one that should win.
    older = workspace_dir / "projects" / "alpha" / "plans" / "older.run-state.json"
    _os.utime(older, (1_700_000_000, 1_700_000_000))
    _seed_workspace_plan(
        workspace_parent,
        project="alpha",
        plan_id="newer",
        purpose="fresh",
        phases=[{"title": "Y"}],
    )
    mem = EngramMemory(engram_repo, embed=False, workspace_dir=workspace_dir)
    out = mem._active_plan_briefing()
    assert "newer" in out
    assert "older" not in out


# ---------------------------------------------------------------------------
# _previous_session_block — bootstrap continuity hint backed by a provider
# (SessionStore in production; SimpleNamespace shim in tests).
# ---------------------------------------------------------------------------


def _fake_prev_session(
    *,
    session_id: str = "ses_prev",
    task: str = "previous task",
    final_text: str | None = "Wrote utils.py and tests.",
    status: str = "completed",
    ended_at: str | None = None,
    engram_session_dir: str | None = None,
    active_plan_project: str | None = None,
    active_plan_id: str | None = None,
):
    from types import SimpleNamespace

    return SimpleNamespace(
        session_id=session_id,
        task=task,
        final_text=final_text,
        status=status,
        ended_at=ended_at if ended_at is not None else datetime.now().isoformat(timespec="seconds"),
        engram_session_dir=engram_session_dir,
        active_plan_project=active_plan_project,
        active_plan_id=active_plan_id,
    )


def test_previous_session_block_empty_when_no_provider(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False)
    assert mem._previous_session_block() == ""


def test_previous_session_block_empty_when_provider_returns_none(
    engram_repo: Path,
) -> None:
    mem = EngramMemory(engram_repo, embed=False, previous_session_provider=lambda: None)
    assert mem._previous_session_block() == ""


def test_previous_session_block_renders_recent_session(engram_repo: Path) -> None:
    rec = _fake_prev_session(
        task="Implement offline-capable token refresh",
        final_text="Wrote tests for the refresh endpoint.",
        active_plan_project="auth-redesign",
        active_plan_id="token-refresh",
        engram_session_dir="memory/activity/2026/04/25/act-007",
    )
    mem = EngramMemory(engram_repo, embed=False, previous_session_provider=lambda: rec)
    out = mem._previous_session_block()
    assert "## Previous session" in out
    assert "Implement offline-capable token refresh" in out
    assert "Wrote tests for the refresh endpoint" in out
    assert "ses_prev" in out
    assert "auth-redesign" in out
    assert "token-refresh" in out
    assert "memory/activity/2026/04/25/act-007" in out


def test_previous_session_block_dropped_when_too_old(engram_repo: Path) -> None:
    """Sessions older than the recency window are stale — pretend they don't exist."""
    rec = _fake_prev_session(
        ended_at=(datetime.now() - timedelta(days=30)).isoformat(timespec="seconds"),
    )
    mem = EngramMemory(engram_repo, embed=False, previous_session_provider=lambda: rec)
    assert mem._previous_session_block() == ""


def test_previous_session_block_omits_self_when_id_matches(engram_repo: Path) -> None:
    """Provider handing back our own row must not produce a self-referential block."""
    mem = EngramMemory(engram_repo, embed=False)
    rec = _fake_prev_session(session_id=mem.session_id)
    mem._previous_session_provider = lambda: rec  # type: ignore[assignment]
    assert mem._previous_session_block() == ""


def test_previous_session_block_swallows_provider_errors(engram_repo: Path) -> None:
    """A SessionStore I/O hiccup must not break bootstrap."""

    def boom():
        raise RuntimeError("db offline")

    mem = EngramMemory(engram_repo, embed=False, previous_session_provider=boom)
    assert mem._previous_session_block() == ""


def test_previous_session_block_skips_resume_hint_when_no_plan_link(
    engram_repo: Path,
) -> None:
    """Without an active_plan_* link, no plan resume hint is rendered."""
    rec = _fake_prev_session(
        active_plan_project=None,
        active_plan_id=None,
    )
    mem = EngramMemory(engram_repo, embed=False, previous_session_provider=lambda: rec)
    out = mem._previous_session_block()
    assert "## Previous session" in out
    assert "work_project_plan" not in out


def test_start_session_appends_previous_session_block(engram_repo: Path) -> None:
    """The new section lands as part of the bootstrap output, not just standalone."""
    rec = _fake_prev_session(task="Refactor the auth middleware")
    mem = EngramMemory(engram_repo, embed=False, previous_session_provider=lambda: rec)
    bootstrap = mem.start_session("continue auth refactor")
    assert "## Previous session" in bootstrap
    assert "Refactor the auth middleware" in bootstrap


def test_start_session_omits_block_when_provider_silent(engram_repo: Path) -> None:
    mem = EngramMemory(engram_repo, embed=False, previous_session_provider=lambda: None)
    bootstrap = mem.start_session("fresh start")
    assert "## Previous session" not in bootstrap
